"""KPI 계산(API-ANA-37, BR-ANA-20: 대시보드 위젯과 리포트가 같은 계산을 부른다).

- SENSOR_AVAILABILITY: 공간 기기의 일별 데이터 품질 완전성(data_quality_daily.completeness) 평균(%)
- COMPLIANCE_RATE: 공간 목표 환경(API-DEV-126) 안에 든 1시간 평균의 비율(%)
- UTILIZATION_RATE: occupancy-estimate 규칙으로 본 운영 시간 사용 비율(%)
- ALARM_RATE·EQUIPMENT_RUNTIME: 알람(RUL)·장비(ACT) 연동이 필요해 M7에서 계산한다(available=false, ANA-09.03 M7 배치)
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from .clock import parse_iso
from .common.errors import BusinessError, ErrorCode, FieldError
from .deps import Deps
from .loader.bindings import Source
from .loader.loader import PIPE, LoadRequest

KEYS = ("COMPLIANCE_RATE", "UTILIZATION_RATE", "ALARM_RATE", "SENSOR_AVAILABILITY", "EQUIPMENT_RUNTIME")
ACTIVITY_KEYS = ("activity", "pir", "motion")


class KpiService:
    def __init__(self, deps: Deps):
        self.deps = deps

    def compute(self, org: int, body: dict) -> dict:
        try:
            space_id = int(str(body.get("spaceId")))
            start, end = parse_iso(body["from"]), parse_iso(body["to"])
        except (KeyError, ValueError, TypeError) as exc:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("spaceId", "NotNull", "spaceId·from·to가 필요합니다")]) from exc
        keys = body.get("keys") or list(KEYS)
        bad = [k for k in keys if k not in KEYS]
        if bad or start >= end:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("keys", "Enum", ", ".join(KEYS))])
        core = self.deps.loader.core
        devices = core.space_devices(space_id) if core else []
        out = []
        for key in keys:
            fn = {"SENSOR_AVAILABILITY": self._availability, "COMPLIANCE_RATE": self._compliance,
                  "UTILIZATION_RATE": self._utilization}.get(key)
            if fn is None:
                out.append({"key": key, "value": None, "unit": None, "available": False, "reason": "알람·장비 연동(M7) 뒤 계산합니다"})
                continue
            value, reason = fn(org, space_id, devices, start, end)
            out.append({"key": key, "value": None if value is None else round(value, 4), "unit": "%", "available": value is not None,
                        "reason": reason})
        return {"kpis": out}

    def _availability(self, org, space_id, devices, start: datetime, end: datetime):
        if not devices:
            return None, "공간에 기기가 없습니다"
        with self.deps.pool.connection() as conn:
            row = conn.execute(f"""SELECT avg(completeness) AS v FROM {PIPE}.data_quality_daily WHERE organization_id = %s
                                   AND device_id = ANY(%s) AND day >= %s::date AND day < %s::date""",
                               (org, devices, start, end)).fetchone()
        return (float(row["v"]), None) if row and row["v"] is not None else (None, "데이터 품질 집계가 없습니다")

    def _compliance(self, org, space_id, devices, start, end):
        core = self.deps.loader.core
        targets = core.space_targets(space_id) if core else None
        if not targets:
            return None, "공간 목표 환경이 없습니다"
        if not devices:
            return None, "공간에 기기가 없습니다"
        rates = []
        with self.deps.pool.connection() as conn:
            for metric, rng in targets.items():
                rows = conn.execute(f"""SELECT bucket, avg(avg) AS v FROM {PIPE}.telemetry_1h WHERE organization_id = %s AND device_id = ANY(%s)
                                        AND metric_key = %s AND bucket >= %s AND bucket < %s AND count > 0 AND NOT is_virtual
                                        GROUP BY bucket""", (org, devices, metric, start, end)).fetchall()
                vals = np.array([float(r["v"]) for r in rows if r["v"] is not None])
                if vals.size == 0:
                    continue
                ok = np.ones(vals.size, dtype=bool)
                if rng.get("min") is not None:
                    ok &= vals >= float(rng["min"])
                if rng.get("max") is not None:
                    ok &= vals <= float(rng["max"])
                rates.append(float(ok.mean()))
        if not rates:
            return None, "목표 항목의 측정값이 없습니다"
        return 100 * float(np.mean(rates)), None

    def _utilization(self, org, space_id, devices, start, end):
        from .templates.builtin.occupancy_estimate.template import TEMPLATE as OCC

        sources = [Source("co2", "SPACE_AGGREGATE", "co2", None, space_id), Source("activity", "SPACE_AGGREGATE", "activity", None, space_id)]
        data = self.deps.loader.load(LoadRequest(org, sources, start, end, "1m"), OCC)
        if sum(1 for f in data.frames.values() if f.notna().any().any()) < 2:
            return None, "재실 신호(CO2·활동)가 부족합니다"
        from .templates.base import RunContext

        res = OCC.run(data, OCC.parse_params({}), RunContext(seed=0, now=end, timezone=self.deps.settings.timezone))
        value = next(m.value for m in res.metrics if m.key == "utilizationPercent")
        return (float(value), None) if value is not None else (None, "운영 시간 데이터가 없습니다")
