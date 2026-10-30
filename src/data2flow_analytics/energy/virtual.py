"""가상 센서 일 계산(ENE-02.01 환기율 ACH, BR-ENE-09).

- 설정 묶음(API-ENE-70 `virtualSensors[]`)에서 켜진 ACH 공간마다, 공간 시간대 02:00이 지나면 전날 CO2 원본(품질 정상, 가상 제외)으로
  가장 잘 맞는 감쇠 구간의 ACH를 추정해 `ach_estimates`에 쓴다. 감쇠 구간이 없으면 행을 쓰지 않는다(그날은 갱신하지 않음)
- 공간·종류·날짜마다 `virtual_sensor_runs`를 먼저 넣은 인스턴스만 계산하고 EVT-ENE-03을 한 번 낸다
- CO2 입력 기기가 없으면 INPUT_MISSING(`missing: [co2]`)
- 설정이 바뀌면(API-ENE-82 reload, EVT-ENE-07) 조직 설정 캐시를 지운다
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..deps import Deps
from ..events import publisher as ev
from ..loader.loader import _raw_union
from . import ach
from .daily import EnergyDaily

log = logging.getLogger("data2flow_analytics.energy")

S = "data2flow_analytics"
DUE_HOUR = 2
ACH = "ACH"
CO2 = "co2"
ACH_METRICS = ["ach", "ventilation_m3h"]


def ach_sensors(cfg: dict) -> list[dict]:
    return [v for v in cfg.get("virtualSensors") or [] if v.get("kind") == ACH and v.get("enabled", True)]


def _inputs(sensor: dict, metric: str) -> list[int]:
    for i in sensor.get("inputs") or []:
        if i.get("metricKey") == metric:
            return [int(d) for d in i.get("deviceIds") or []]
    return []


class VirtualSensors:
    def __init__(self, deps: Deps, energy: EnergyDaily):
        self.deps = deps
        self.energy = energy  # 설정 묶음 캐시를 같이 쓴다

    def reload(self, org: int, space_id: int | None = None) -> None:
        """API-ENE-82: 설정 변경 즉시 반영(조직 설정 캐시를 지운다)"""
        self.energy.invalidate(org)

    # ---- 매일 02:00 ----
    def tick(self) -> list[dict]:
        if self.energy.core is None:
            return []
        out = []
        now = self.deps.clock.now()
        for org in self.energy.organizations():
            try:
                cfg = self.energy.config(org)
                for sensor in ach_sensors(cfg):
                    tz = sensor.get("timezone") or cfg.get("timezone") or "UTC"
                    local = now.astimezone(ZoneInfo(tz))
                    if local.hour < DUE_HOUR:
                        continue
                    res = self.compute_ach(org, sensor, local.date() - timedelta(days=1), tz)
                    if res is not None:
                        out.append(res)
            except Exception:  # noqa: BLE001 — 한 조직이 실패해도 다른 조직은 계속
                log.exception("virtual sensor tick failed org=%s", org)
        return out

    def compute_ach(self, org: int, sensor: dict, day: date, tz: str) -> dict | None:
        """한 공간·하루. 이미 계산했으면 None"""
        space = int(sensor["spaceId"])
        zone = ZoneInfo(tz)
        start, end = datetime.combine(day, time(0), zone), datetime.combine(day + timedelta(days=1), time(0), zone)
        devices = _inputs(sensor, CO2)
        outdoor = float((sensor.get("params") or {}).get("outdoorCo2Ppm") or ach.OUTDOOR_PPM)
        result, status, missing = None, "OK", None
        if not devices:
            status, missing = "INPUT_MISSING", [CO2]
        else:
            best: ach.Result | None = None
            for device in devices:
                samples = self.samples(device, CO2, start, end)
                r = ach.estimate([t for t, _ in samples], [v for _, v in samples], outdoor)
                if r.estimate is not None and (best is None or best.estimate is None or r.estimate.r2 > best.estimate.r2):
                    best = r
                elif best is None:
                    best = r
            result = best
        now = self.deps.clock.now()
        est = result.estimate if result is not None else None
        reason = None if est is not None else (result.reason if result is not None else None)
        with self.deps.pool.connection() as conn:
            if conn.execute(f"""INSERT INTO {S}.virtual_sensor_runs (organization_id, space_id, kind, day, status, reason, computed_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING day""",
                            (org, space, ACH, day, status, reason, now)).fetchone() is None:
                return None
            if est is not None:
                conn.execute(f"""INSERT INTO {S}.ach_estimates (organization_id, space_id, day, ach, ci_low, ci_high, decay_from, decay_to, r2_fit,
                                 created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                 ON CONFLICT (organization_id, space_id, day) DO UPDATE SET ach = EXCLUDED.ach, ci_low = EXCLUDED.ci_low,
                                 ci_high = EXCLUDED.ci_high, decay_from = EXCLUDED.decay_from, decay_to = EXCLUDED.decay_to,
                                 r2_fit = EXCLUDED.r2_fit""",
                             (org, space, day, est.ach, est.ci_low, est.ci_high, est.decay_from, est.decay_to, est.r2, now))
        payload = {"spaceId": space, "kind": ACH, "metricKeys": ACH_METRICS, "computedFrom": start.isoformat(), "computedTo": end.isoformat(),
                   "status": status, "missing": missing}
        ev.safe_publish(self.deps.publisher, ev.make_event(self.deps.clock, ev.ENERGY_VIRTUAL_SENSOR, org, payload))
        return {"organizationId": org, "spaceId": space, "day": day.isoformat(), "status": status, "ach": est.ach if est else None, "reason": reason}

    def samples(self, device_id: int, metric: str, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        where = "device_id = %(d)s AND metric_key = %(m)s AND quality = 0 AND value IS NOT NULL AND NOT is_virtual AND time >= %(s)s AND time < %(e)s"
        with self.deps.pool.connection() as conn:
            rows = conn.execute(f"SELECT time, value FROM {_raw_union(where)} t ORDER BY time",
                                {"d": device_id, "m": metric, "s": start, "e": end}).fetchall()
        return [(r["time"], float(r["value"])) for r in rows]

    # ---- 조회(core API-ENE-21) ----
    def ach_series(self, org: int, space_id: int, start: date, end: date) -> list[dict]:
        with self.deps.pool.connection() as conn:
            rows = conn.execute(f"""SELECT day, ach, ci_low, ci_high, decay_from, decay_to, r2_fit FROM {S}.ach_estimates
                                    WHERE organization_id = %s AND space_id = %s AND day >= %s AND day <= %s ORDER BY day""",
                                (org, space_id, start, end)).fetchall()
        return [{"day": r["day"].isoformat(), "ach": r["ach"], "ciLow": r["ci_low"], "ciHigh": r["ci_high"],
                 "decayWindow": {"from": r["decay_from"].isoformat() if r["decay_from"] else None,
                                 "to": r["decay_to"].isoformat() if r["decay_to"] else None},
                 "fitR2": r["r2_fit"]} for r in rows]
