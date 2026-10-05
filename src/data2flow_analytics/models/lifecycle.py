"""모델 생애주기(ANA-07): 학습·버전, 평가 지표, 재학습(수동·일정·성능 저하), 드리프트(PSI), 적용(BR-ANA-12).

상태: TRAINING → CANDIDATE → ACTIVE → RETIRED, TRAINING → FAILED. 한 분석의 ACTIVE는 하나(부분 유일 인덱스).
새 모델이 기존보다 나쁘면 자동으로 ACTIVE가 되지 않고 CANDIDATE로 남는다(강제 적용은 force=true).
"""

from __future__ import annotations

import hashlib
import logging
import pickle
from datetime import datetime, timedelta

import numpy as np

from ..analysis import repository as repo
from ..analysis.service import listing, page_args
from ..clock import iso, parse_iso
from ..common.errors import BusinessError, ErrorCode, FieldError
from ..common.jsonutil import canonical_json, to_jsonable
from ..deps import Deps
from ..events import publisher as ev
from ..loader.bindings import parse_bindings, resolve_period
from ..loader.loader import LoadRequest
from ..templates.algos import decile_edges, psi_from_edges
from ..worker.runner import feedback_labels

log = logging.getLogger("data2flow_analytics.models")
S = repo.S
MODEL_COLS = """m.id, m.organization_id, m.analysis_id, m.template_key, m.template_version, m.binding_hash, m.artifact_version, m.status, m.uri,
  m.train_from, m.train_to, m.params, m.metrics, m.trained_at, m.created_at, m.updated_at"""


def _view(row: dict) -> dict:
    return to_jsonable({"modelId": str(row["id"]), "analysisId": str(row["analysis_id"]), "analysisName": row.get("analysis_name"),
                        "templateKey": row["template_key"], "version": row["artifact_version"], "status": row["status"],
                        "trainFrom": iso(row["train_from"]), "trainTo": iso(row["train_to"]), "metrics": row.get("metrics") or {},
                        "trainedAt": iso(row.get("trained_at"))})


class ModelService:
    def __init__(self, deps: Deps, executor=None):
        self.deps = deps
        self.executor = executor

    # ---- 학습 요청(API-ANA-36) ----
    def request_training(self, org: int, analysis_id: int, body: dict | None, reason: str = "MANUAL") -> tuple[dict, bool]:
        body = body or {}
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            analysis = repo.get_analysis(conn, org, analysis_id)
            if analysis is None:
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
            reg = self.deps.registry.get(analysis["template_key"], analysis["template_version"])
            if not reg.template.trainable:
                raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("analysisId", "Trainable", "학습형 템플릿이 아닙니다")])
            running = conn.execute(f"SELECT id, artifact_version FROM {S}.model_artifacts WHERE analysis_id = %s AND status = 'TRAINING'",
                                   (analysis_id,)).fetchone()
            if running:
                return {"modelId": str(running["id"]), "version": running["artifact_version"]}, False
            if body.get("trainFrom") and body.get("trainTo"):
                start, end = parse_iso(body["trainFrom"]), parse_iso(body["trainTo"])
                if start >= end:
                    raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("trainFrom", "Order", "시작이 끝보다 앞서야 합니다")])
            else:
                start, end = resolve_period(analysis["period"], now)
            params = {**(analysis["params"] or {}), **(body.get("params") or {})}
            reg.template.parse_params(params)
            version = conn.execute(f"SELECT coalesce(max(artifact_version), 0) + 1 AS v FROM {S}.model_artifacts WHERE analysis_id = %s",
                                   (analysis_id,)).fetchone()["v"]
            digest = hashlib.sha256(canonical_json(analysis["bindings"]).encode()).hexdigest()
            row = conn.execute(
                f"""INSERT INTO {S}.model_artifacts (organization_id, analysis_id, template_key, template_version, binding_hash,
                       artifact_version, status, train_from, train_to, params, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'TRAINING', %s, %s, %s, %s, %s) RETURNING id, artifact_version""",
                (org, analysis_id, reg.key, reg.version, digest, version, start, end, repo.j(params), now, now)).fetchone()
            repo.insert_job(conn, org, "TRAIN", row["id"], now, {"reason": reason})
        return {"modelId": str(row["id"]), "version": row["artifact_version"]}, True

    # ---- 학습 작업 처리 ----
    def process(self, job: dict) -> str:
        deps = self.deps
        now = deps.clock.now()
        with deps.pool.connection() as conn:
            model = conn.execute(f"SELECT * FROM {S}.model_artifacts WHERE id = %s", (job["ref_id"],)).fetchone()
            if model is None or model["status"] != "TRAINING":
                repo.finish_job(conn, job["id"], now)
                return "SKIPPED"
            analysis = repo.get_analysis(conn, model["organization_id"], model["analysis_id"], include_archived=True)
            labels = feedback_labels(conn, model["organization_id"], analysis["id"])
        try:
            reg = deps.registry.get(model["template_key"], model["template_version"])
            template = reg.template
            params = template.parse_params(model["params"])
            sources = parse_bindings(analysis["bindings"])
            # 가상 데이터는 학습에서 항상 뺀다(ANA-08.03)
            req = LoadRequest(model["organization_id"], sources, model["train_from"], model["train_to"],
                              _resolution(template, analysis), analysis["quality_filter"], False)
            data = deps.loader.load(req, template)
            if data.total_points() == 0:
                raise BusinessError(ErrorCode.ANALYSIS_INSUFFICIENT_DATA, {"reason": "학습 기간에 데이터가 없습니다"})
            from ..worker.executor import Job

            work = Job(template.key, template.version, data, params.model_dump(mode="json"), _seed(model), now, deps.settings.timezone,
                       None, labels, mode="fit")
            trained = self.executor.execute(work, template.requirements_for(params).timeout_sec, lambda: False)
            if trained is None:
                raise BusinessError(ErrorCode.INVALID_REQUEST)
            metrics = dict(trained.metrics)
            target = next(iter(data.frames.values()))
            if target.shape[1]:
                metrics["baselineDeciles"] = decile_edges(target.iloc[:, 0].to_numpy(dtype=float))
            if analysis["include_virtual"]:
                metrics["warnings"] = ["VIRTUAL_EXCLUDED_FROM_TRAINING"]
            uri = deps.store.put(f"models/{model['organization_id']}/{model['analysis_id']}/v{model['artifact_version']}.pkl",
                                 pickle.dumps(trained.payload))
            with deps.pool.connection() as conn:
                conn.execute(f"""UPDATE {S}.model_artifacts SET status = 'CANDIDATE', uri = %s, metrics = %s, trained_at = %s, updated_at = %s
                                 WHERE id = %s""", (uri, repo.j(to_jsonable(metrics)), now, now, model["id"]))
                repo.finish_job(conn, job["id"], now)
                self._auto_promote(conn, model["id"], template, now)
            return "CANDIDATE"
        except Exception as exc:  # noqa: BLE001
            log.exception("training failed model=%s", model["id"])
            with deps.pool.connection() as conn:
                conn.execute(f"UPDATE {S}.model_artifacts SET status = 'FAILED', metrics = %s, updated_at = %s WHERE id = %s",
                             (repo.j({"error": str(exc)[:500]}), now, model["id"]))
                repo.finish_job(conn, job["id"], now)
            return "FAILED"

    def _auto_promote(self, conn, model_id: int, template, now: datetime) -> None:
        """BR-ANA-12: 활성 모델이 없거나 새 모델이 같거나 좋으면 자동 적용, 나쁘면 CANDIDATE로 둔다."""
        model = conn.execute(f"SELECT * FROM {S}.model_artifacts WHERE id = %s FOR UPDATE", (model_id,)).fetchone()
        active = conn.execute(f"SELECT * FROM {S}.model_artifacts WHERE analysis_id = %s AND status = 'ACTIVE' FOR UPDATE",
                              (model["analysis_id"],)).fetchone()
        if active is None or template.compare_models(model["metrics"] or {}, active["metrics"] or {}):
            if active:
                conn.execute(f"UPDATE {S}.model_artifacts SET status = 'RETIRED', updated_at = %s WHERE id = %s", (now, active["id"]))
            conn.execute(f"UPDATE {S}.model_artifacts SET status = 'ACTIVE', updated_at = %s WHERE id = %s", (now, model_id))
        else:
            metrics = {**(model["metrics"] or {}), "note": "기존보다 성능이 낮음", "worseThanActive": True}
            conn.execute(f"UPDATE {S}.model_artifacts SET metrics = %s, updated_at = %s WHERE id = %s", (repo.j(metrics), now, model_id))

    # ---- 적용(API-ANA-36) ----
    def activate(self, org: int, model_id: int, force: bool) -> dict:
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            model = conn.execute(f"SELECT * FROM {S}.model_artifacts WHERE id = %s AND organization_id = %s FOR UPDATE",
                                 (model_id, org)).fetchone()
            if model is None:
                raise BusinessError(ErrorCode.ML_MODEL_NOT_FOUND)
            if model["status"] in ("TRAINING", "FAILED"):
                raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("modelId", "State", "학습이 끝난 모델만 적용합니다")])
            active = conn.execute(f"SELECT * FROM {S}.model_artifacts WHERE analysis_id = %s AND status = 'ACTIVE' FOR UPDATE",
                                  (model["analysis_id"],)).fetchone()
            previous = None
            if active and active["id"] != model_id:
                template = self.deps.registry.get(model["template_key"], model["template_version"]).template
                if not force and not template.compare_models(model["metrics"] or {}, active["metrics"] or {}):
                    raise BusinessError(ErrorCode.MODEL_WORSE_THAN_ACTIVE)
                conn.execute(f"UPDATE {S}.model_artifacts SET status = 'RETIRED', updated_at = %s WHERE id = %s", (now, active["id"]))
                previous = active["id"]
            conn.execute(f"UPDATE {S}.model_artifacts SET status = 'ACTIVE', updated_at = %s WHERE id = %s", (now, model_id))
        return {"modelId": str(model_id), "analysisId": str(model["analysis_id"]), "status": "ACTIVE",
                "previousModelId": str(previous) if previous else None}

    def list(self, org: int, page: int | None, size: int | None, analysis_id: int | None = None) -> dict:
        p, s = page_args(page, size)
        cond = " AND m.analysis_id = %(aid)s" if analysis_id else ""
        params = {"org": org, "aid": analysis_id, "limit": s, "offset": (p - 1) * s}
        with self.deps.pool.connection() as conn:
            total = conn.execute(f"SELECT count(*) AS n FROM {S}.model_artifacts m WHERE m.organization_id = %(org)s{cond}", params).fetchone()["n"]
            rows = conn.execute(f"""SELECT {MODEL_COLS}, a.name AS analysis_name FROM {S}.model_artifacts m
                                    JOIN {S}.analyses a ON a.id = m.analysis_id
                                    WHERE m.organization_id = %(org)s{cond} ORDER BY m.created_at DESC, m.id DESC
                                    LIMIT %(limit)s OFFSET %(offset)s""", params).fetchall()
        return listing([_view(r) for r in rows], total, p, s)

    # ---- 드리프트·재학습 점검(매일, ANA-07.03·07.04) ----
    def daily_check(self) -> dict:
        deps = self.deps
        now = deps.clock.now()
        today = now.date().isoformat()
        out = {"drift": 0, "retrain": 0}
        with deps.pool.connection() as conn:
            models = conn.execute(f"""SELECT m.*, a.retrain_policy, a.bindings, a.quality_filter, a.template_key AS a_key, a.params AS a_params
                                      FROM {S}.model_artifacts m JOIN {S}.analyses a ON a.id = m.analysis_id
                                      WHERE m.status = 'ACTIVE' AND a.status = 'ACTIVE'""").fetchall()
        for m in models:
            metrics = m["metrics"] or {}
            reason = None
            edges = metrics.get("baselineDeciles") or []
            drift = metrics.get("drift") or {}
            if edges and drift.get("checkedOn") != today:
                recent = self._recent_values(m, now)
                value = psi_from_edges(edges, recent) if recent.size else 0.0
                metrics["drift"] = {"psi": round(value, 6), "checkedOn": today, "detected": value > 0.2,
                                    "detectedAt": iso(now) if value > 0.2 else drift.get("detectedAt")}
                with deps.pool.connection() as conn:
                    conn.execute(f"UPDATE {S}.model_artifacts SET metrics = %s, updated_at = %s WHERE id = %s",
                                 (repo.j(to_jsonable(metrics)), now, m["id"]))
                if value > 0.2:
                    out["drift"] += 1
                    ev.safe_publish(deps.publisher, ev.make_event(deps.clock, ev.DRIFT, m["organization_id"], {
                        "analysisId": str(m["analysis_id"]), "modelId": str(m["id"]), "psi": round(value, 6), "detectedAt": iso(now)}))
                    if m["retrain_policy"] == "ON_DRIFT":
                        reason = "DRIFT"
            if m["retrain_policy"] == "SCHEDULE" and m["trained_at"] and now - m["trained_at"] >= timedelta(days=7):
                reason = reason or "SCHEDULE"
            if reason is None and m["retrain_policy"] in ("ON_DRIFT", "SCHEDULE") and metrics.get("mape"):
                recent_mape = self._recent_mape(m, now)
                if recent_mape is not None and recent_mape >= 1.5 * float(metrics["mape"]):
                    reason = "DEGRADED"
            if reason and not self._training_pending(m["analysis_id"]):
                self.request_training(m["organization_id"], m["analysis_id"], None, reason)
                out["retrain"] += 1
        return out

    def _training_pending(self, analysis_id: int) -> bool:
        with self.deps.pool.connection() as conn:
            row = conn.execute(f"SELECT 1 FROM {S}.model_artifacts WHERE analysis_id = %s AND status = 'TRAINING'", (analysis_id,)).fetchone()
        return row is not None

    def _recent_data(self, m: dict, now: datetime):
        template = self.deps.registry.get(m["template_key"], m["template_version"]).template
        sources = parse_bindings(m["bindings"])
        req = LoadRequest(m["organization_id"], sources, now - timedelta(days=7), now, _resolution(template, {"resolution": "AUTO"}),
                          m["quality_filter"], False)
        return template, self.deps.loader.load(req, template)

    def _recent_values(self, m: dict, now: datetime) -> np.ndarray:
        _, data = self._recent_data(m, now)
        frame = next(iter(data.frames.values()), None)
        if frame is None or not frame.shape[1]:
            return np.array([])
        return frame.iloc[:, 0].dropna().to_numpy(dtype=float)

    def _recent_mape(self, m: dict, now: datetime) -> float | None:
        template, data = self._recent_data(m, now)
        evaluate = getattr(template, "evaluate_recent", None)
        if evaluate is None:
            return None
        try:
            return evaluate(data, template.parse_params(m["params"]))
        except Exception:  # noqa: BLE001
            return None


def _resolution(template, analysis: dict) -> str:
    res = analysis.get("resolution", "AUTO")
    if res == "AUTO":
        res = template.requirements.recommended_resolution
    return res if res in ("RAW", "1m", "1h", "1d") else "1h"


def _seed(model: dict) -> int:
    return int(hashlib.sha256(f"{model['analysis_id']}:{model['artifact_version']}".encode()).hexdigest()[:8], 16)
