"""기준선 학습 작업(ENE-01.04, 내부 API API-ENE-81 → EVT-ENE-02). core-api가 권한 범위의 공간 트리와 기온 원천을 넘긴다.

- 요청: 작업 큐에 BASELINE 작업(ref_id = core 버전 ID)을 넣고 202. 같은 버전의 작업이 살아 있으면 그 작업을 돌려준다
- 처리: 학습 기간의 `energy_daily`를 날마다 롤업해 범위 노드의 일 kWh를 내고(가상 공간 제외), 회귀면 기온 원천의 `telemetry_1h` 시간 평균으로
  도일을 붙여 학습한다(baseline.py). 결과는 `baseline_models`(key = `bl-{versionId}`)에 쓰고 EVT-ENE-02를 낸다
- 실패: 30일 미만 INSUFFICIENT_DATA(neededDays), 기온 원천이 없거나 기온이 하루도 없으면 NO_WEATHER, 그 밖은 INTERNAL_ERROR
- 지표(API-ENE-85)의 기준선 비교는 `predict()`로 날마다 예상 kWh를 낸다(날씨 보정, TC-ENE-023)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..analysis import repository as repo
from ..common.errors import BusinessError, ErrorCode, FieldError
from ..common.jsonutil import to_jsonable
from ..deps import Deps
from ..events import publisher as ev
from ..loader.loader import PIPE
from .baseline import COOLING_BASE_C, HEATING_BASE_C, METHODS, Day, InsufficientData, Model, daily_means, degree_days, fit
from .rollup import Node, own_usage, portfolio, rollup

log = logging.getLogger("data2flow_analytics.energy")

S = "data2flow_analytics"
JOB = "BASELINE"
MAX_DAYS = 730
NO_WEATHER = "NO_WEATHER"
INSUFFICIENT = "INSUFFICIENT_DATA"


class NoWeather(Exception):  # noqa: N818
    """회귀에 쓸 기온이 없다"""


def model_key(version_id: int) -> str:
    return f"bl-{version_id}"


def _bad(field: str, message: str) -> BusinessError:
    return BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(field, "Invalid", message)])


def _date(body: dict, key: str) -> date:
    try:
        return date.fromisoformat(str(body[key])[:10])
    except (KeyError, ValueError) as exc:
        raise _bad(key, "YYYY-MM-DD 날짜가 필요합니다") from exc


def _nodes(spaces: list[dict]) -> list[Node]:
    return [Node(int(s["id"]), None if s.get("parentId") is None else int(s["parentId"]), s.get("name"), s.get("kind"),
                 None if s.get("areaM2") is None else float(s["areaM2"]), bool(s.get("coversDescendants"))) for s in spaces]


class EnergyBaselines:
    def __init__(self, deps: Deps):
        self.deps = deps

    # ---- 요청(API-ENE-81) ----
    def request(self, org: int, body: dict) -> dict:
        try:
            version = int(body["versionId"])
            assert version > 0
        except (KeyError, ValueError, TypeError, AssertionError) as exc:
            raise _bad("versionId", "양의 정수가 필요합니다") from exc
        method = body.get("method")
        if method not in METHODS:
            raise _bad("method", ", ".join(METHODS))
        start, end = _date(body, "trainFrom"), _date(body, "trainTo")
        if end < start or (end - start).days + 1 > MAX_DAYS:
            raise _bad("trainTo", f"기간은 1~{MAX_DAYS}일입니다")
        try:
            spaces = [dict(s) for s in body.get("spaces") or []]
            nodes = _nodes(spaces)
        except (KeyError, ValueError, TypeError) as exc:
            raise _bad("spaces", "공간은 id·parentId·areaM2·coversDescendants·virtual로 줍니다") from exc
        scope = body.get("scopeSpaceId")
        scope = None if scope in (None, "") else int(scope)
        if scope is not None and scope not in {n.id for n in nodes}:
            raise BusinessError(ErrorCode.RESOURCE_NOT_FOUND)
        temp = body.get("temperatureSource") or None
        if temp is not None and (not temp.get("deviceId") or not temp.get("metricKey")):
            raise _bad("temperatureSource", "deviceId·metricKey가 필요합니다")
        params = body.get("params") or {}
        payload = {"versionId": version, "scopeSpaceId": scope, "method": method, "trainFrom": start.isoformat(), "trainTo": end.isoformat(),
                   "params": {"coolingBaseC": float(params.get("coolingBaseC", COOLING_BASE_C)),
                              "heatingBaseC": float(params.get("heatingBaseC", HEATING_BASE_C)),
                              "splitWeekday": bool(params.get("splitWeekday", True))},
                   "excludeVirtual": body.get("excludeVirtual", True) is not False, "spaces": spaces,
                   "temperatureSource": None if temp is None else {"deviceId": int(temp["deviceId"]), "metricKey": str(temp["metricKey"])},
                   "timezone": body.get("timezone") or self.deps.settings.timezone}
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            for job in repo.jobs_for(conn, JOB, version):
                if job["organization_id"] == org and job["status"] in ("READY", "LOCKED"):
                    return {"jobId": str(job["id"]), "versionId": str(version), "modelKey": model_key(version)}
            job = repo.insert_job(conn, org, JOB, version, now, payload)
        return {"jobId": str(job["id"]), "versionId": str(version), "modelKey": model_key(version)}

    # ---- 작업 처리 ----
    def process(self, job: dict) -> str:
        org, p = int(job["organization_id"]), job["payload"]
        version = int(p["versionId"])
        try:
            model = self.train(org, p)
        except InsufficientData as exc:
            return self._failed(job, org, version, INSUFFICIENT, needed=exc.needed_days, have=exc.have_days)
        except NoWeather:
            return self._failed(job, org, version, NO_WEATHER)
        except Exception:  # noqa: BLE001
            log.exception("baseline training failed version=%s", version)
            return self._failed(job, org, version, ErrorCode.INTERNAL_ERROR.name)
        now = self.deps.clock.now()
        key = model_key(version)
        scope = {"scopeSpaceId": p.get("scopeSpaceId"), "spaceIds": [int(s["id"]) for s in p.get("spaces") or []],
                 "temperatureSource": p.get("temperatureSource"), "params": p["params"], "timezone": p.get("timezone"),
                 "trainFrom": p["trainFrom"], "trainTo": p["trainTo"], "days": model.days, "warnings": model.warnings}
        with self.deps.pool.connection() as conn:
            conn.execute(f"""INSERT INTO {S}.baseline_models (organization_id, key, scope, method, coefficients, r2, residuals, trained_at, created_at)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (organization_id, key) DO UPDATE SET scope = EXCLUDED.scope, method = EXCLUDED.method,
                               coefficients = EXCLUDED.coefficients, r2 = EXCLUDED.r2, residuals = EXCLUDED.residuals, trained_at = EXCLUDED.trained_at""",
                         (org, key, repo.j(to_jsonable(scope)), model.method, repo.j(model.coefficients), model.r2, repo.j(model.residuals), now, now))
            repo.finish_job(conn, job["id"], now)
        self._publish(org, {"versionId": version, "status": "READY", "r2": model.r2, "coefficients": {k: round(v, 6) for k, v in model.coefficients.items()},
                            "residualsRef": key, "warning": ",".join(model.warnings) or None, "error": None, "neededDays": None,
                            "trainedDays": model.days})
        return "READY"

    def _failed(self, job: dict, org: int, version: int, error: str, *, needed: int | None = None, have: int | None = None) -> str:
        with self.deps.pool.connection() as conn:
            repo.finish_job(conn, job["id"], self.deps.clock.now())
        self.publish_failed(org, version, error, needed=needed, have=have)
        return "FAILED"

    def publish_failed(self, org: int, version: int, error: str, *, needed: int | None = None, have: int | None = None) -> None:
        self._publish(org, {"versionId": version, "status": "FAILED", "r2": None, "coefficients": None, "residualsRef": None, "warning": None,
                            "error": error, "neededDays": needed, "trainedDays": have})

    def _publish(self, org: int, payload: dict) -> None:
        ev.safe_publish(self.deps.publisher, ev.make_event(self.deps.clock, ev.ENERGY_BASELINE_TRAINED, org, payload))

    # ---- 학습 ----
    def train(self, org: int, p: dict) -> Model:
        start, end = date.fromisoformat(p["trainFrom"]), date.fromisoformat(p["trainTo"])
        spaces = p.get("spaces") or []
        if p.get("excludeVirtual", True):
            spaces = [s for s in spaces if not s.get("virtual")]
        nodes = _nodes(spaces)
        scope = p.get("scopeSpaceId")
        kwh = self.daily_kwh(org, nodes, scope, start, end)
        params = p["params"]
        if p["method"] == "PERIOD_AVERAGE":
            days = [Day(d, v, weekend=d.weekday() >= 5) for d, v in sorted(kwh.items())]
        else:
            temps = self.daily_temperatures(org, p.get("temperatureSource"), p.get("timezone") or "UTC", start, end)
            if not temps:
                raise NoWeather
            days = []
            for d, v in sorted(kwh.items()):
                if d in temps:
                    cdd, hdd = degree_days(temps[d], params["coolingBaseC"], params["heatingBaseC"])
                    days.append(Day(d, v, cdd, hdd, d.weekday() >= 5))
        return fit(days, p["method"], split_weekday=params.get("splitWeekday", True))

    def daily_kwh(self, org: int, nodes: list[Node], scope: int | None, start: date, end: date) -> dict[date, float]:
        """범위 노드의 날짜별 롤업 kWh. 그날 행이 하나도 없으면 빠진다"""
        ids = [n.id for n in nodes]
        if not ids or (scope is not None and scope not in ids):
            return {}
        with self.deps.pool.connection() as conn:
            rows = conn.execute(f"""SELECT space_id, day, source_kind, kwh, krw, tco2 FROM {S}.energy_daily
                                    WHERE organization_id = %s AND space_id = ANY(%s) AND day BETWEEN %s AND %s""",
                                (org, ids, start, end)).fetchall()
        by_day: dict[date, list[dict]] = {}
        for r in rows:
            by_day.setdefault(r["day"], []).append(r)
        out = {}
        for d, day_rows in by_day.items():
            rolled = rollup(nodes, own_usage(day_rows))
            out[d] = (rolled[scope] if scope is not None else portfolio(nodes, rolled)).usage.kwh
        return out

    def daily_temperatures(self, org: int, source: dict | None, tz: str, start: date, end: date) -> dict[date, float]:
        """기온 원천의 사이트 시간대 날짜별 평균(`telemetry_1h` avg, 가상 제외)"""
        if not source:
            return {}
        zone = ZoneInfo(tz)
        lo = datetime.combine(start, time(0), zone)
        hi = datetime.combine(end + timedelta(days=1), time(0), zone)
        with self.deps.pool.connection() as conn:
            rows = conn.execute(f"""SELECT bucket, avg FROM {PIPE}.telemetry_1h WHERE organization_id = %s AND device_id = %s AND metric_key = %s
                                    AND bucket >= %s AND bucket < %s AND count > 0 AND NOT is_virtual""",
                                (org, int(source["deviceId"]), source["metricKey"], lo, hi)).fetchall()
        return daily_means(((r["bucket"], r["avg"]) for r in rows), tz)

    # ---- 모델 조회(core 버전 상세의 잔차) ----
    def get(self, org: int, key: str) -> dict:
        with self.deps.pool.connection() as conn:
            row = conn.execute(f"SELECT * FROM {S}.baseline_models WHERE organization_id = %s AND key = %s", (org, key)).fetchone()
        if row is None:
            raise BusinessError(ErrorCode.RESOURCE_NOT_FOUND)
        return to_jsonable({"key": row["key"], "method": row["method"], "coefficients": row["coefficients"], "r2": row["r2"],
                            "residuals": row["residuals"] or [], "warnings": (row["scope"] or {}).get("warnings") or [], "trainedAt": row["trained_at"]})

    # ---- 지표의 기준선 비교 ----
    def load(self, org: int, key: str) -> tuple[Model, dict] | None:
        with self.deps.pool.connection() as conn:
            row = conn.execute(f"SELECT * FROM {S}.baseline_models WHERE organization_id = %s AND key = %s", (org, key)).fetchone()
        if row is None:
            return None
        scope = row["scope"] or {}
        model = Model(row["method"], date.fromisoformat(scope.get("trainFrom") or "1970-01-01"), date.fromisoformat(scope.get("trainTo") or "1970-01-01"),
                      {k: float(v) for k, v in (row["coefficients"] or {}).items()}, row["r2"])
        return model, scope

    def predict(self, org: int, key: str, days: list[date]) -> dict[date, float] | None:
        """날마다 기준선 예상 kWh. 모델이 없으면 None, 회귀인데 그날 기온이 없으면 그날은 빠진다"""
        loaded = self.load(org, key)
        if loaded is None or not days:
            return None
        model, scope = loaded
        params = scope.get("params") or {}
        split = params.get("splitWeekday", True)
        if model.method == "PERIOD_AVERAGE":
            return {d: model.predict(weekend=split and d.weekday() >= 5) for d in days}
        temps = self.daily_temperatures(org, scope.get("temperatureSource"), scope.get("timezone") or "UTC", min(days), max(days))
        out = {}
        for d in days:
            if d in temps:
                cdd, hdd = degree_days(temps[d], params.get("coolingBaseC", COOLING_BASE_C), params.get("heatingBaseC", HEATING_BASE_C))
                out[d] = model.predict(cdd, hdd, split and d.weekday() >= 5)
        return out
