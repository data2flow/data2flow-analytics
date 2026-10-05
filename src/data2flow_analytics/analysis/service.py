"""분석 유스케이스: 템플릿 카탈로그·설정, 충분성 확인, 분석 정의, 실행 요청·조회·취소·비교, 실시간 켜기, 이상 피드백.

core-api가 사용자 권한·공간 범위를 확인한 뒤 내부 API로 부른다(ANA-api §2, ADR-021). 여기서는 조직 범위(X-ORG-ID)를 강제한다.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime

from ..clock import iso, parse_iso
from ..common.errors import BusinessError, ErrorCode, FieldError
from ..common.jsonutil import canonical_json, to_jsonable
from ..deps import Deps
from ..loader.bindings import RESOLUTIONS, binding_spaces, parse_bindings, resolve_period, validate_roles
from ..loader.loader import LoadRequest
from ..loader.sufficiency import CheckResult, check, limit_violation
from ..templates.registry import Registered
from ..templates.search import evaluate_runnable, keyword_search
from ..worker.executor import InlineExecutor
from ..worker.runner import RunRunner
from . import repository as repo
from . import runs as runs_mod
from . import schedule as sched
from . import views

log = logging.getLogger("data2flow_analytics.service")
QUALITY = ("NORMAL_ONLY", "INCLUDE_ALL")
TRIGGERS = ("MANUAL", "SCHEDULE", "API")
RETRAIN = ("MANUAL", "SCHEDULE", "ON_DRIFT")


def page_args(page: int | None, size: int | None) -> tuple[int, int]:
    """오프셋 페이징(api-rules §3.4): page 1부터, size 기본 20·최대 100. 범위 밖은 경계값."""
    p = 1 if page is None or page < 1 else page
    s = 20 if size is None else min(100, max(1, size))
    return p, s


def listing(items: list, total: int, page: int, size: int) -> dict:
    return {"page": page, "size": size, "totalPages": (total + size - 1) // size if size else 0, "responses": items, "totalCount": total}


class AnalyticsService:
    def __init__(self, deps: Deps, sync_threads: int = 4):
        self.deps = deps
        self._sync_pool = ThreadPoolExecutor(max_workers=sync_threads, thread_name_prefix="sync-run")
        self._sync_lock = threading.Lock()

    @property
    def now(self) -> datetime:
        return self.deps.clock.now()

    # ================================================================ 템플릿(API-ANA-30, 17·18)
    def _enabled(self, org: int) -> dict[str, bool]:
        with self.deps.pool.connection() as conn:
            return repo.template_settings(conn, org)

    def list_templates(self, org: int, *, category: str | None = None, kind: str | None = None, include_disabled: bool = False,
                       keyword: str | None = None, view: str | None = None, semantics: list[str] | None = None,
                       page: int | None = None, size: int | None = None) -> dict:
        settings = self._enabled(org)
        regs = [r for r in self.deps.registry.current()
                if (include_disabled or settings.get(r.key, True))
                and (not category or r.template.category.value == category) and (not kind or r.template.kind.value == kind)]
        if keyword:
            items = keyword_search(regs, keyword)
        elif view == "runnable":
            items = evaluate_runnable(regs, set(semantics or []))
        else:
            items = [views.template_summary(r, settings.get(r.key, True)) for r in regs]
        p, s = page_args(page, size)
        return listing(items[(p - 1) * s: p * s], len(items), p, s)

    def get_template(self, org: int, key: str, version: str | None = None) -> dict:
        reg = self.deps.registry.get(key, version)
        enabled = self._enabled(org).get(key, True)
        return views.template_detail(reg, self.deps.registry.versions(key), enabled)

    def template_settings(self, org: int) -> dict:
        with self.deps.pool.connection() as conn:
            settings = repo.template_settings(conn, org)
            usage = repo.template_usage(conn, org)
        items = [self._setting_item(r, settings, usage) for r in self.deps.registry.current()]
        return listing(items, len(items), 1, max(1, len(items)))

    @staticmethod
    def _setting_item(reg: Registered, settings: dict, usage: dict) -> dict:
        u = usage.get(reg.key) or {}
        return {"key": reg.key, "name": reg.template.name, "version": reg.version, "enabled": settings.get(reg.key, True),
                "analysisCount": int(u.get("analyses") or 0), "scheduleCount": int(u.get("schedules") or 0),
                "realtimeCount": int(u.get("realtimes") or 0)}

    def set_template_enabled(self, org: int, user: int | None, key: str, enabled: bool) -> dict:
        reg = self.deps.registry.get(key)
        now = self.now
        with self.deps.pool.connection() as conn:
            repo.set_template_enabled(conn, org, key, enabled, user, now)
            paused = repo.pause_template_usage(conn, org, key, now) if not enabled else []
            settings = repo.template_settings(conn, org)
            usage = repo.template_usage(conn, org)
        item = self._setting_item(reg, settings, usage)
        item["pausedAnalysisIds"] = [str(i) for i in paused]
        return item

    def _require_enabled(self, org: int, key: str) -> None:
        if not self._enabled(org).get(key, True):
            raise BusinessError(ErrorCode.TEMPLATE_DISABLED)

    # ================================================================ 충분성(API-ANA-31)
    def check_sufficiency(self, org: int, key: str, body: dict, request_id: str | None = None) -> dict:
        reg = self.deps.registry.get(key, body.get("version"))
        template = reg.template
        params = template.parse_params(body.get("params"))
        sources = parse_bindings(body.get("bindings"))
        validate_roles(sources, template.roles_for(params))
        template.validate_inputs(body.get("bindings") or [], params)
        resolution = body.get("resolution", "AUTO")
        if resolution not in RESOLUTIONS:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("resolution", "Enum", "AUTO·RAW·1m·1h·1d")])
        quality = body.get("qualityFilter", "NORMAL_ONLY")
        if quality not in QUALITY:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("qualityFilter", "Enum", "NORMAL_ONLY·INCLUDE_ALL")])
        start, end = resolve_period(body.get("period"), self.now)
        result = self._check(org, template, params, sources, start, end, resolution, quality, bool(body.get("includeVirtual")), request_id)
        return result.to_json()

    def _check(self, org, template, params, sources, start, end, resolution, quality, include_virtual, request_id) -> CheckResult:
        req = LoadRequest(org, sources, start, end, "RAW", quality, include_virtual, request_id)
        stats = self.deps.loader.stats(req)
        extra = template.extra_issues(start, end, params)
        return check(stats, template.requirements_for(params), start, end, resolution, include_virtual, extra)

    # ================================================================ 분석 정의(API-ANA-32)
    def _definition(self, org: int, user: int | None, body: dict, existing: dict | None) -> dict:
        name = (body.get("name") or (existing or {}).get("name") or "").strip()
        if not (1 <= len(name) <= 100):
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("name", "Size", "이름은 1~100자")])
        key = body.get("templateKey") or (existing or {}).get("template_key")
        if not key:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("templateKey", "NotBlank", "템플릿을 고르세요")])
        reg = self.deps.registry.get(key, body.get("templateVersion") or None)
        self._require_enabled(org, key)
        template = reg.template
        params = template.parse_params(body.get("params"))
        dataset_id = body.get("datasetId")
        dataset_version = None
        bindings, period = body.get("bindings"), body.get("period")
        quality = body.get("qualityFilter", "NORMAL_ONLY")
        include_virtual = bool(body.get("includeVirtual", False))
        if dataset_id:
            from .datasets import get_dataset_row

            with self.deps.pool.connection() as conn:
                ds = get_dataset_row(conn, org, int(dataset_id))
            same = existing is not None and existing.get("dataset_id") == ds["id"]
            dataset_version = existing["dataset_version"] if same else ds["version"]
            with self.deps.pool.connection() as conn:
                ver = conn.execute(f"SELECT * FROM {repo.S}.dataset_versions WHERE dataset_id = %s AND version = %s",
                                   (ds["id"], dataset_version)).fetchone()
            bindings, period, quality, include_virtual = ver["bindings"], ver["period"], ver["quality_filter"], ver["include_virtual"]
            dataset_id = ds["id"]
        sources = parse_bindings(bindings)
        validate_roles(sources, template.roles_for(params))
        template.validate_inputs(bindings or [], params)
        resolve_period(period, self.now)
        resolution = body.get("resolution", "AUTO")
        if resolution not in RESOLUTIONS:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("resolution", "Enum", "AUTO·RAW·1m·1h·1d")])
        if quality not in QUALITY:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("qualityFilter", "Enum", "NORMAL_ONLY·INCLUDE_ALL")])
        schedule = body.get("schedule") or None
        sched.validate(schedule)
        state = body.get("scheduleState")
        if state not in (None, "ACTIVE", "PAUSED"):
            raise BusinessError(ErrorCode.ANALYSIS_SCHEDULE_INVALID)
        if schedule is None:
            state = None
        elif state is None:
            prev = (existing or {}).get("schedule_state")
            state = prev if prev else "ACTIVE"
        retrain = body.get("retrainPolicy")
        if retrain is not None and retrain not in RETRAIN:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("retrainPolicy", "Enum", "MANUAL·SCHEDULE·ON_DRIFT")])
        owner = body.get("ownerUserId") or (existing or {}).get("owner_user_id") or user
        if owner is None:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("ownerUserId", "NotNull", "소유자가 필요합니다")])
        spaces = body.get("spaceScopeIds")
        spaces = [int(s) for s in spaces] if spaces is not None else binding_spaces(sources)
        return {"org": org, "template_key": key, "template_version": reg.version, "dataset_id": dataset_id,
                "dataset_version": dataset_version, "name": name, "bindings": repo.j(bindings), "period": repo.j(period),
                "resolution": resolution, "quality_filter": quality, "include_virtual": include_virtual,
                "params": repo.j(params.model_dump(mode="json")), "schedule": repo.j(schedule), "schedule_state": state,
                "recipients": repo.j(body.get("recipients")), "retrain_policy": retrain, "owner": int(owner), "spaces": spaces,
                "user": user, "now": self.now}

    def _analysis_view(self, conn, row: dict) -> dict:
        nxt = sched.next_run(row.get("schedule"), self.now, self.deps.settings.timezone) if row.get("schedule_state") == "ACTIVE" else None
        newer = None
        if row.get("dataset_id"):
            cur = conn.execute(f"SELECT version FROM {repo.S}.datasets WHERE id = %s", (row["dataset_id"],)).fetchone()
            if cur and cur["version"] != row["dataset_version"]:
                newer = cur["version"]
        return views.analysis_view(row, nxt, newer)

    def create_analysis(self, org: int, user: int | None, body: dict) -> dict:
        row = self._definition(org, user, body, None)
        with self.deps.pool.connection() as conn:
            saved = repo.insert_analysis(conn, row)
            return self._analysis_view(conn, saved)

    def update_analysis(self, org: int, user: int | None, analysis_id: int, body: dict) -> dict:
        base = body.get("baseVersion")
        if base is None:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("baseVersion", "NotNull", "baseVersion이 필요합니다")])
        with self.deps.pool.connection() as conn:
            existing = repo.get_analysis(conn, org, analysis_id)
        if existing is None:
            raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
        row = self._definition(org, user, body, existing)
        row.update({"id": analysis_id, "base_version": int(base)})
        with self.deps.pool.connection() as conn:
            saved = repo.update_analysis(conn, row)
            if saved is None:
                raise BusinessError(ErrorCode.VERSION_CONFLICT)
            return self._analysis_view(conn, saved)

    def get_analysis(self, org: int, analysis_id: int) -> dict:
        with self.deps.pool.connection() as conn:
            row = repo.get_analysis(conn, org, analysis_id)
            if row is None:
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
            return self._analysis_view(conn, row)

    def list_analyses(self, org: int, user: int | None, *, template_key: str | None = None, owner: str | None = None,
                      schedule_state: str | None = None, realtime: bool | None = None, keyword: str | None = None,
                      space_ids: list[int] | None = None, page: int | None = None, size: int | None = None) -> dict:
        p, s = page_args(page, size)
        filters = {"templateKey": template_key, "scheduleState": schedule_state, "realtime": realtime, "keyword": keyword,
                   "ownerUserId": user if owner == "me" else None, "spaceIds": space_ids}
        with self.deps.pool.connection() as conn:
            rows, total = repo.list_analyses(conn, org, filters, p, s)
        now = self.now
        items = [views.analysis_list_item(r, sched.next_run(r.get("schedule"), now, self.deps.settings.timezone)
                                          if r.get("schedule_state") == "ACTIVE" else None) for r in rows]
        return listing(items, total, p, s)

    def delete_analysis(self, org: int, user: int | None, analysis_id: int) -> None:
        with self.deps.pool.connection() as conn:
            if not repo.archive_analysis(conn, org, analysis_id, user, self.now):
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)

    # ================================================================ 실행(API-ANA-33·34)
    def _definition_for_run(self, conn, org: int, analysis: dict) -> tuple[list, dict, str, bool]:
        if analysis.get("dataset_id"):
            ver = conn.execute(f"SELECT * FROM {repo.S}.dataset_versions WHERE dataset_id = %s AND version = %s",
                               (analysis["dataset_id"], analysis["dataset_version"])).fetchone()
            return ver["bindings"], ver["period"], ver["quality_filter"], ver["include_virtual"]
        return analysis["bindings"], analysis["period"], analysis["quality_filter"], analysis["include_virtual"]

    def request_run(self, org: int, user: int | None, body: dict, request_id: str | None = None) -> tuple[int, dict]:
        try:
            analysis_id = int(str(body.get("analysisId")))
        except ValueError as exc:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("analysisId", "Pattern", "숫자 ID")]) from exc
        trigger = body.get("trigger", "MANUAL")
        mode = str(body.get("mode", "ASYNC")).upper()
        if trigger not in TRIGGERS or mode not in ("SYNC", "ASYNC"):
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("trigger", "Enum", "MANUAL·SCHEDULE·API / SYNC·ASYNC")])
        with self.deps.pool.connection() as conn:
            analysis = repo.get_analysis(conn, org, analysis_id)
            if analysis is None:
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
            bindings, period, quality, include_virtual = self._definition_for_run(conn, org, analysis)
        if trigger == "SCHEDULE" and analysis.get("schedule_state") != "ACTIVE":
            raise BusinessError(ErrorCode.ANALYSIS_RUN_STATE_CONFLICT)
        self._require_enabled(org, analysis["template_key"])
        reg = self.deps.registry.get(analysis["template_key"], analysis["template_version"])
        template = reg.template
        params = template.parse_params(analysis["params"])
        sources = parse_bindings(bindings)
        now = self.now
        start, end = resolve_period(period, now)
        result = self._check(org, template, params, sources, start, end, analysis["resolution"], quality, include_virtual, request_id)
        violation = limit_violation(result)
        if violation:
            fix = violation.get("fix") or {}
            raise BusinessError(ErrorCode.ANALYSIS_LIMIT_EXCEEDED, {"suggest": fix.get("value", "1h")}, detail=violation["message"],
                                extra={"check": result.to_json()})
        if result.level == "FAIL":
            raise BusinessError(ErrorCode.ANALYSIS_INSUFFICIENT_DATA, {"reason": result.reason}, extra={"check": result.to_json()})
        if result.level == "WARN" and body.get("acknowledgeWarnings") is False:
            raise BusinessError(ErrorCode.ANALYSIS_WARNING_NOT_ACKNOWLEDGED, extra={"check": result.to_json()})
        resolution = result.stats["effectiveResolution"]
        params_json = params.model_dump(mode="json")
        seed = body.get("seed")
        if seed is None:
            # BR-ANA-10: 같은 템플릿 버전·바인딩·확정 기간·파라미터면 같은 시드 → 같은 결과
            basis = canonical_json({"t": f"{template.key}@{template.version}", "b": bindings, "p": params_json, "from": iso(start),
                                    "to": iso(end), "r": resolution, "q": quality, "v": include_virtual,
                                    "d": [analysis.get("dataset_id"), analysis.get("dataset_version")]})
            seed = int(hashlib.sha256(basis.encode()).hexdigest()[:8], 16)
        provenance = {"bindings": bindings, "period": {"from": iso(start), "to": iso(end)}, "resolution": resolution,
                      "requestedResolution": analysis["resolution"], "qualityFilter": quality, "virtual": include_virtual,
                      "seed": int(seed), "datasetId": views.sid(analysis.get("dataset_id")), "datasetVersion": analysis.get("dataset_version"),
                      "params": params_json, "points": result.stats["points"], "missingRate": result.stats["missingRate"],
                      "requestId": request_id, "resultRetentionDays": body.get("resultRetentionDays")}
        sync = mode == "SYNC" and template.fast and result.stats["points"] <= self.deps.settings.sync_points_limit
        timeout = template.requirements_for(params).timeout_sec
        with self.deps.pool.connection() as conn:
            run, job = runs_mod.admit(conn, org, self.deps.settings.org_run_limit, {
                "org": org, "analysis_id": analysis_id, "template_version": template.version, "trigger": trigger,
                "period_from": start, "period_to": end, "provenance": repo.j(provenance), "requested_by": user},
                now, sync_worker=f"{self.deps.settings.instance_id}-sync" if sync else None, lease_sec=timeout + 60)
            est = runs_mod.estimated_start(conn, org, run, self.deps.settings.org_run_limit, now)
        RunRunner(self.deps, None)._publish_run(run, request_id)
        if sync and job is not None:
            future = self._sync_pool.submit(RunRunner(self.deps, InlineExecutor(self.deps.registry)).process, job)
            try:
                future.result(timeout=self.deps.settings.sync_wait_sec)
            except FutureTimeout:
                log.info("sync run continues in background run=%s", run["id"])
            else:
                view = self.get_run(org, run["id"])
                if view["run"]["status"] == "SUCCEEDED":
                    return 200, view
        return 202, to_jsonable({"runId": str(run["id"]), "status": run["status"], "queuePosition": run.get("queue_position"),
                                 "estimatedStartAt": iso(est)})

    def get_run(self, org: int, run_id: int) -> dict:
        with self.deps.pool.connection() as conn:
            run = repo.get_run(conn, org, run_id)
            if run is None:
                raise BusinessError(ErrorCode.ANALYSIS_RUN_NOT_FOUND)
            est = runs_mod.estimated_start(conn, org, run, self.deps.settings.org_run_limit, self.now)
            result = repo.get_result(conn, org, run_id) if run["status"] == "SUCCEEDED" else None
        out: dict = {"run": views.run_view(run, est)}
        if run["status"] == "SUCCEEDED":
            if result is None:
                raise BusinessError(ErrorCode.RESOURCE_NOT_FOUND, extra={"resultExpired": True, "run": out["run"]})
            out["result"] = views.result_view(result, run["provenance"] and _public_provenance(run["provenance"]))
        return out

    def list_runs(self, org: int, analysis_id: int, page: int | None, size: int | None) -> dict:
        p, s = page_args(page, size)
        with self.deps.pool.connection() as conn:
            if repo.get_analysis(conn, org, analysis_id, include_archived=True) is None:
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
            rows, total = repo.list_runs(conn, org, analysis_id, p, s)
            now = self.now
            items = [views.run_view(r, runs_mod.estimated_start(conn, org, r, self.deps.settings.org_run_limit, now)) for r in rows]
        return listing(items, total, p, s)

    def cancel_run(self, org: int, run_id: int) -> dict:
        """QUEUED·PENDING은 바로, RUNNING도 바로 CANCELLED로 바꾸고 워커는 다음 체크포인트(1초 안)에서 멈춘다(ANA-04.04)."""
        now = self.now
        with self.deps.pool.connection() as conn:
            run = repo.get_run(conn, org, run_id, for_update=True)
            if run is None:
                raise BusinessError(ErrorCode.ANALYSIS_RUN_NOT_FOUND)
            runs_mod.ensure_transition(run["status"], "CANCELLED")
            saved = repo.update_run(conn, run_id, now, expect=("QUEUED", "PENDING", "RUNNING"), status="CANCELLED", finished_at=now,
                                    queue_position=None)
            if saved is None:
                raise BusinessError(ErrorCode.ANALYSIS_RUN_STATE_CONFLICT)
            for job in repo.jobs_for(conn, "RUN", run_id):
                if job["status"] == "READY":
                    repo.finish_job(conn, job["id"], now)
            promoted = runs_mod.promote(conn, org, self.deps.settings.org_run_limit, now)
        runner = RunRunner(self.deps, None)
        runner._publish_run(saved)
        for p in promoted:
            runner._publish_run(p)
        return views.run_view(saved)

    def compare_runs(self, org: int, analysis_id: int, base_id: int, target_id: int) -> dict:
        base, target = self.get_run(org, base_id), self.get_run(org, target_id)
        if base["run"]["analysisId"] != str(analysis_id) or target["run"]["analysisId"] != str(analysis_id):
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("target", "SameAnalysis", "같은 분석의 실행끼리만 비교합니다")])
        if "result" not in base or "result" not in target:
            raise BusinessError(ErrorCode.ANALYSIS_RUN_STATE_CONFLICT)
        bm = {m["key"]: m for m in base["result"]["summary"]["metrics"]}
        metrics = []
        for m in target["result"]["summary"]["metrics"]:
            b = bm.get(m["key"])
            if b is None or not isinstance(m.get("value"), (int, float)) or not isinstance(b.get("value"), (int, float)) \
                    or isinstance(m.get("value"), bool):
                continue
            diff = m["value"] - b["value"]
            metrics.append({"key": m["key"], "label": m["label"], "base": b["value"], "target": m["value"], "diff": round(diff, 6),
                            "diffPct": round(diff / b["value"], 6) if b["value"] else None,
                            "direction": "UP" if diff > 0 else ("DOWN" if diff < 0 else "SAME")})
        charts = []
        for label, side in (("기준", base), ("비교", target)):
            for c in side["result"]["charts"][:1]:
                charts.append({**c, "id": f"{label}-{c['id']}", "title": f"{c['title']} ({label})"})
        return {"metrics": metrics, "versionMismatch": base["run"]["templateVersion"] != target["run"]["templateVersion"],
                "charts": charts}

    def set_ai_commentary(self, org: int, run_id: int, body: dict) -> None:
        if not body.get("commentaryId"):
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("commentaryId", "NotBlank", "해설 ID가 필요합니다")])
        value = {"commentaryId": str(body["commentaryId"]), "generatedAt": body.get("generatedAt") or iso(self.now)}
        with self.deps.pool.connection() as conn:
            if not repo.set_ai_commentary(conn, org, run_id, value):
                raise BusinessError(ErrorCode.ANALYSIS_RUN_NOT_FOUND)

    # ================================================================ 실시간(API-ANA-35, 11)
    def set_realtime(self, org: int, analysis_id: int, enabled: bool) -> dict:
        with self.deps.pool.connection() as conn:
            analysis = repo.get_analysis(conn, org, analysis_id)
            if analysis is None:
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
            reg = self.deps.registry.get(analysis["template_key"], analysis["template_version"])
            model = conn.execute(f"SELECT id FROM {repo.S}.model_artifacts WHERE analysis_id = %s AND status = 'ACTIVE'",
                                 (analysis_id,)).fetchone()
            if enabled:
                if not reg.template.realtime:
                    raise BusinessError(ErrorCode.ANALYSIS_REALTIME_NOT_SUPPORTED)
                self._require_enabled(org, analysis["template_key"])
                params = reg.template.parse_params(analysis["params"])
                if reg.template.realtime_requires_model(params) and model is None:
                    raise BusinessError(ErrorCode.ANALYSIS_MODEL_REQUIRED)
            repo.set_realtime(conn, org, analysis_id, enabled, self.now)
        return {"analysisId": str(analysis_id), "realtime": enabled, "activeModelId": views.sid(model["id"]) if model else None}

    def realtime_events(self, org: int, analysis_id: int, start: str | None, end: str | None, page: int | None, size: int | None,
                        device_ids: list[int] | None = None) -> dict:
        p, s = page_args(page, size if size is not None else 50)
        where = ["organization_id = %(org)s", "analysis_id = %(aid)s"]
        params: dict = {"org": org, "aid": analysis_id, "limit": s, "offset": (p - 1) * s}
        if start:
            where.append("occurred_at >= %(from)s")
            params["from"] = _iso_param("from", start)
        if end:
            where.append("occurred_at < %(to)s")
            params["to"] = _iso_param("to", end)
        if device_ids is not None:
            where.append("device_id = ANY(%(devices)s)")
            params["devices"] = device_ids
        sql_where = " AND ".join(where)
        with self.deps.pool.connection() as conn:
            if repo.get_analysis(conn, org, analysis_id, include_archived=True) is None:
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
            total = conn.execute(f"SELECT count(*) AS n FROM {repo.S}.realtime_events WHERE {sql_where}", params).fetchone()["n"]
            rows = conn.execute(f"SELECT * FROM {repo.S}.realtime_events WHERE {sql_where} ORDER BY occurred_at DESC, id DESC "
                                "LIMIT %(limit)s OFFSET %(offset)s", params).fetchall()
        items = [to_jsonable({"realtimeEventId": str(r["id"]), "type": r["type"], "deviceId": str(r["device_id"]),
                              "metricKey": r["metric_key"], "occurredAt": iso(r["occurred_at"]), "payload": r["payload"]}) for r in rows]
        return listing(items, total, p, s)

    # ================================================================ 이상 피드백(API-ANA-15)
    def create_feedback(self, org: int, user: int | None, body: dict) -> dict:
        run_id, event_id = body.get("runId"), body.get("realtimeEventId")
        if (run_id is None) == (event_id is None):
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("runId", "OneOf", "runId와 realtimeEventId 중 하나만")])
        verdict = body.get("verdict")
        if verdict not in ("TRUE_POSITIVE", "FALSE_POSITIVE"):
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("verdict", "Enum", "TRUE_POSITIVE·FALSE_POSITIVE")])
        if not body.get("occurredAt") or not body.get("seriesKey") or user is None:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("occurredAt", "NotNull", "시각·시계열·사용자가 필요합니다")])
        occurred = _iso_param("occurredAt", str(body["occurredAt"]))
        with self.deps.pool.connection() as conn:
            if run_id is not None:
                if repo.get_run(conn, org, int(run_id)) is None:
                    raise BusinessError(ErrorCode.ANALYSIS_RUN_NOT_FOUND)
            else:
                found = conn.execute(f"SELECT id FROM {repo.S}.realtime_events WHERE id = %s AND organization_id = %s",
                                     (int(event_id), org)).fetchone()
                if found is None:
                    raise BusinessError(ErrorCode.RESOURCE_NOT_FOUND)
            row = conn.execute(
                f"""INSERT INTO {repo.S}.anomaly_feedback (organization_id, run_id, realtime_event_id, occurred_at, series_key, verdict,
                       user_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (org, int(run_id) if run_id is not None else None, int(event_id) if event_id is not None else None, occurred,
                 str(body["seriesKey"])[:200], verdict, int(user), self.now)).fetchone()
        return to_jsonable({"feedbackId": str(row["id"]), "runId": views.sid(row["run_id"]), "realtimeEventId": views.sid(row["realtime_event_id"]),
                            "occurredAt": iso(row["occurred_at"]), "seriesKey": row["series_key"], "verdict": row["verdict"],
                            "createdAt": iso(row["created_at"])})


def _iso_param(field: str, value: str):
    try:
        return parse_iso(value)
    except ValueError as exc:
        raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(field, "Pattern", "ISO-8601 시각")]) from exc


def _public_provenance(prov: dict) -> dict:
    return {k: v for k, v in prov.items() if k not in ("params", "requestId", "resultRetentionDays", "requestedResolution")}


__all__ = ["AnalyticsService", "listing", "page_args"]
