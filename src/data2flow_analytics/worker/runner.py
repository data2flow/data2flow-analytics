"""RUN 작업 처리: LOAD → COMPUTE → SAVE(ANA-04.02). 단계마다 EVT-ANA-01을 발행한다.

- 업무 오류(데이터 부족 등)는 바로 FAILED(재시도 없음), 시간 초과는 TIMEOUT(ANALYSIS_TIMEOUT), 취소는 부분 결과를 저장하지 않는다.
- 예상하지 못한 오류는 작업을 다시 READY로 돌려 재시도하고, 3번째 실패에서 작업 DEAD·실행 FAILED(domain-model Job).
- 일정 실행(trigger=SCHEDULE)이 3번 연속 실패하면 일정 STOPPED_BY_FAILURE + EVT-ANA-05(BR-ANA-11).
"""

from __future__ import annotations

import logging
import pickle
from datetime import datetime, timedelta

from ..analysis import repository as repo
from ..analysis import runs as runs_mod
from ..common.errors import BusinessError, ErrorCode
from ..common.jsonutil import content_hash, to_jsonable
from ..deps import Deps
from ..events import publisher as ev
from ..loader.bindings import parse_bindings
from ..loader.loader import LoadRequest
from ..schemas import result_validator
from ..templates.base import CancelledError
from .executor import Job, RunTimeoutError, TemplateFailure

log = logging.getLogger("data2flow_analytics.runner")
MAX_ATTEMPTS = 3


class RunRunner:
    def __init__(self, deps: Deps, executor):
        self.deps = deps
        self.executor = executor

    # ------------------------------------------------------------------ 상태 이벤트
    def _publish_run(self, run: dict, request_id: str | None = None) -> None:
        payload = {"runId": str(run["id"]), "analysisId": str(run["analysis_id"]), "status": run["status"],
                   "progress": run.get("progress"), "trigger": run["trigger"], "stage": run.get("stage")}
        if run.get("error_code"):
            payload["errorCode"] = run["error_code"]
        if run.get("finished_at"):
            payload["finishedAt"] = run["finished_at"]
        event = ev.make_event(self.deps.clock, ev.RUN_STATUS.format(status=run["status"].lower()), run["organization_id"],
                              to_jsonable(payload), request_id)
        ev.safe_publish(self.deps.publisher, event)

    def _stage(self, run_id: int, stage: str, progress: int) -> dict | None:
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            run = repo.update_run(conn, run_id, now, expect=("RUNNING",), stage=stage, progress=progress)
        if run:
            self._publish_run(run)
        return run

    def _cancelled(self, run_id: int) -> bool:
        with self.deps.pool.connection() as conn:
            return repo.run_status(conn, run_id) == "CANCELLED"

    # ------------------------------------------------------------------ 처리
    def process(self, job: dict) -> str:
        """작업 하나를 끝까지 처리하고 결과 상태를 돌려준다."""
        deps = self.deps
        run_id = job["ref_id"]
        now = deps.clock.now()
        with deps.pool.connection() as conn:
            run = repo.get_run_any_org(conn, run_id)
            if run is None or run["status"] not in ("PENDING", "RUNNING"):
                repo.finish_job(conn, job["id"], now)
                return run["status"] if run else "MISSING"
            analysis = repo.get_analysis(conn, run["organization_id"], run["analysis_id"], include_archived=True)
            run = repo.update_run(conn, run_id, now, expect=("PENDING", "RUNNING"), status="RUNNING", stage="LOAD", progress=5,
                                  started_at=now, error_code=None, error_message=None, error_detail=None)
        self._publish_run(run)
        prov = dict(run["provenance"] or {})
        try:
            reg = deps.registry.get(analysis["template_key"], run["template_version"])
            template = reg.template
            params = template.parse_params(prov.get("params"))
            sources = parse_bindings(prov.get("bindings"))
            req = LoadRequest(run["organization_id"], sources, run["period_from"], run["period_to"], prov["resolution"],
                              prov.get("qualityFilter", "NORMAL_ONLY"), bool(prov.get("virtual")), prov.get("requestId"))
            data = deps.loader.load(req, template)
            prov["points"] = data.total_points()
            frames_missing = _missing_rate(data, run["period_from"], run["period_to"])
            prov["missingRate"] = round(frames_missing, 6) if frames_missing is not None else prov.get("missingRate")
            if data.total_points() == 0:
                raise BusinessError(ErrorCode.ANALYSIS_INSUFFICIENT_DATA, {"reason": "선택한 기간에 데이터가 없습니다"})
            if self._stage(run_id, "COMPUTE", 30) is None:
                return self._abandon(job, run_id)
            model, labels = self._model_and_labels(run["organization_id"], analysis, template)
            work = Job(template.key, template.version, data, params.model_dump(mode="json"), int(prov["seed"]), deps.clock.now(),
                       deps.settings.timezone, model, labels)
            result = self.executor.execute(work, template.requirements_for(params).timeout_sec, lambda: self._cancelled(run_id))
            if self._stage(run_id, "SAVE", 90) is None:
                return self._abandon(job, run_id)
            return self._save(job, run, analysis, template, prov, result)
        except CancelledError:
            return self._abandon(job, run_id)
        except RunTimeoutError:
            return self._fail(job, run, analysis, "TIMEOUT", ErrorCode.ANALYSIS_TIMEOUT, {}, None, prov)
        except BusinessError as exc:
            return self._fail(job, run, analysis, "FAILED", exc.code, exc.args_, exc.detail, prov)
        except TemplateFailure as exc:
            return self._retry_or_fail(job, run, analysis, str(exc), exc.detail, prov)
        except Exception as exc:  # noqa: BLE001 — DB·네트워크 같은 일시 오류는 재시도
            import traceback

            return self._retry_or_fail(job, run, analysis, str(exc) or type(exc).__name__, traceback.format_exc(), prov)

    def _model_and_labels(self, org: int, analysis: dict, template) -> tuple[object, list]:
        if not template.trainable:
            return None, []
        with self.deps.pool.connection() as conn:
            row = conn.execute(f"SELECT uri FROM {repo.S}.model_artifacts WHERE analysis_id = %s AND organization_id = %s "
                               "AND status = 'ACTIVE'", (analysis["id"], org)).fetchone()
            labels = feedback_labels(conn, org, analysis["id"])
        model = None
        if row and row["uri"]:
            try:
                model = pickle.loads(self.deps.store.get(row["uri"]))  # noqa: S301 — 우리 저장소에 우리가 쓴 모델만 읽는다
            except Exception:  # noqa: BLE001
                log.warning("active model unreadable analysis=%s", analysis["id"])
        return model, labels

    def _save(self, job: dict, run: dict, analysis: dict, template, prov: dict, result: dict) -> str:
        deps = self.deps
        now = deps.clock.now()
        provenance = dict(prov)
        provenance.update(result.get("provenance") or {})
        for k in ("params", "requestId", "resultRetentionDays"):
            provenance.pop(k, None)
        provenance = to_jsonable({**provenance, "template": f"{template.key}@{template.version}"})
        body = {k: result.get(k) for k in ("summary", "charts", "tables", "evidence", "caveats")}
        body["provenance"] = provenance
        result_validator().validate(to_jsonable(body))
        digest = content_hash(body)
        retention = int(prov.get("resultRetentionDays") or 365)
        with deps.pool.connection() as conn:
            saved = repo.update_run(conn, run["id"], now, expect=("RUNNING",), status="SUCCEEDED", stage="SAVE", progress=100,
                                    finished_at=now, provenance={**prov, **provenance})
            if saved is None:  # 그사이 취소됨: 부분 결과를 저장하지 않는다
                repo.finish_job(conn, job["id"], now)
                return "CANCELLED"
            repo.insert_result(conn, run["id"], run["organization_id"], body, digest, now + timedelta(days=retention), now)
            repo.finish_job(conn, job["id"], now)
            if run["trigger"] == "SCHEDULE":
                repo.bump_schedule_failures(conn, analysis["id"], False, now)
            promoted = runs_mod.promote(conn, run["organization_id"], deps.settings.org_run_limit, now)
        self._publish_run(saved)
        for p in promoted:
            self._publish_run(p)
        return "SUCCEEDED"

    def _fail(self, job: dict, run: dict, analysis: dict, status: str, code: ErrorCode, args: dict, detail: str | None,
              prov: dict) -> str:
        deps = self.deps
        now = deps.clock.now()
        stopped = None
        with deps.pool.connection() as conn:
            saved = repo.update_run(conn, run["id"], now, expect=("RUNNING", "PENDING"), status=status, finished_at=now,
                                    error_code=code.name, error_message=code.message("ko", **args), error_detail=detail,
                                    provenance=prov)
            repo.finish_job(conn, job["id"], now)
            if saved and run["trigger"] == "SCHEDULE":
                stopped = repo.bump_schedule_failures(conn, analysis["id"], True, now)
            promoted = runs_mod.promote(conn, run["organization_id"], deps.settings.org_run_limit, now)
        if saved:
            self._publish_run(saved)
        for p in promoted:
            self._publish_run(p)
        if stopped and stopped["schedule_state"] == "STOPPED_BY_FAILURE" and stopped["consecutive_failures"] == 3:
            ev.safe_publish(deps.publisher, ev.make_event(deps.clock, ev.SCHEDULE_STOPPED, stopped["organization_id"], {
                "analysisId": str(stopped["id"]), "ownerUserId": str(stopped["owner_user_id"]),
                "consecutiveFailures": stopped["consecutive_failures"]}))
        return status if saved else "CANCELLED"

    def _retry_or_fail(self, job: dict, run: dict, analysis: dict, message: str, detail: str, prov: dict) -> str:
        deps = self.deps
        now = deps.clock.now()
        if job["attempts"] >= MAX_ATTEMPTS:
            with deps.pool.connection() as conn:
                repo.dead_job(conn, job["id"], detail or message, now)
            job = {**job, "status": "DEAD"}
            return self._fail_dead(job, run, analysis, detail or message, prov)
        with deps.pool.connection() as conn:
            repo.retry_job(conn, job["id"], detail or message, now + timedelta(seconds=30 * job["attempts"]), now)
            back = repo.update_run(conn, run["id"], now, expect=("RUNNING",), status="PENDING", stage=None, progress=0)
        if back:
            self._publish_run(back)
        return "RETRY"

    def _fail_dead(self, job: dict, run: dict, analysis: dict, detail: str, prov: dict) -> str:
        deps = self.deps
        now = deps.clock.now()
        with deps.pool.connection() as conn:
            saved = repo.update_run(conn, run["id"], now, expect=("RUNNING", "PENDING"), status="FAILED", finished_at=now,
                                    error_code=ErrorCode.INTERNAL_ERROR.name, error_message="분석 중 오류가 발생했습니다. 3번 시도했지만 실패했습니다",
                                    error_detail=detail[:8000], provenance=prov)
            stopped = repo.bump_schedule_failures(conn, analysis["id"], True, now) if saved and run["trigger"] == "SCHEDULE" else None
            promoted = runs_mod.promote(conn, run["organization_id"], deps.settings.org_run_limit, now)
        if saved:
            self._publish_run(saved)
        for p in promoted:
            self._publish_run(p)
        if stopped and stopped["schedule_state"] == "STOPPED_BY_FAILURE" and stopped["consecutive_failures"] == 3:
            ev.safe_publish(deps.publisher, ev.make_event(deps.clock, ev.SCHEDULE_STOPPED, stopped["organization_id"], {
                "analysisId": str(stopped["id"]), "ownerUserId": str(stopped["owner_user_id"]), "consecutiveFailures": 3}))
        return "FAILED"

    def _abandon(self, job: dict, run_id: int) -> str:
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            repo.finish_job(conn, job["id"], now)
            run = repo.get_run_any_org(conn, run_id)
            promoted = runs_mod.promote(conn, run["organization_id"], self.deps.settings.org_run_limit, now) if run else []
        for p in promoted:
            self._publish_run(p)
        return "CANCELLED"


def feedback_labels(conn, org: int, analysis_id: int) -> list[dict]:
    """이상 피드백(ANA-07.05): 실행 결과·실시간 이벤트에 단 '맞음/오탐'."""
    rows = conn.execute(
        f"""SELECT f.occurred_at, f.series_key, f.verdict FROM {repo.S}.anomaly_feedback f
            LEFT JOIN {repo.S}.runs r ON r.id = f.run_id
            WHERE f.organization_id = %s AND (r.analysis_id = %s OR f.realtime_event_id IN
              (SELECT id FROM {repo.S}.realtime_events WHERE analysis_id = %s AND organization_id = %s))
            ORDER BY f.occurred_at""", (org, analysis_id, analysis_id, org)).fetchall()
    return [{"occurredAt": r["occurred_at"].isoformat(), "seriesKey": r["series_key"], "verdict": r["verdict"]} for r in rows]


def _missing_rate(data, start: datetime, end: datetime) -> float | None:
    import numpy as np

    rates = []
    for frame in data.frames.values():
        for col in frame.columns:
            s = frame[col].dropna()
            if len(s) < 3:
                rates.append(1.0)
                continue
            step = float(np.median(np.diff(s.index.values).astype("timedelta64[s]").astype(float)))
            if step <= 0:
                continue
            expected = (end - start).total_seconds() / step
            rates.append(max(0.0, 1 - len(s) / expected))
    return float(np.mean(rates)) if rates else None


__all__ = ["RunRunner", "feedback_labels"]
