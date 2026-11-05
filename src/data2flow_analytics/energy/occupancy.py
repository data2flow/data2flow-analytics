"""재실 추정 가상 센서(ENE-02.02, UC-ENE-07, BR-ENE-10).

- 설정 묶음(API-ENE-70 `virtualSensors[]`)에서 켜진 OCCUPANCY 공간마다 5분 구간이 끝나면, 지난 24시간의 입력 신호(품질 정상, 가상 제외)로
  occupancy-estimate 템플릿과 같은 규칙(`estimate_frames`)을 돌려 구간 끝 시점의 재실 0/1을 정한다
- 값은 pipeline API-ING-25로 공간 가상 기기(`outputDeviceId`)의 `occupancy_est`에 저장한다(`source=DERIVED`, `estimated=true`).
  인원 숫자는 내지 않는다(0/1만, NFR-13.02)
- 신호가 2개 미만이면 INPUT_MISSING(`missing` = 값이 없는 신호)이고 값을 보내지 않는다
- 공간·종류·구간마다 `virtual_sensor_ticks`를 먼저 넣은 인스턴스만 계산하고 EVT-ENE-03을 한 번 낸다
- 운영과 같은 장비 데이터를 로컬에서 두 번 쓰지 않도록 실시간 설정(`realtime_enabled`)이 켜져 있을 때만 돈다
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from ..deps import Deps
from ..events import publisher as ev
from ..loader.loader import _raw_union
from ..loader.pipeline_client import DerivedSink, PipelineClient
from ..templates.builtin.occupancy_estimate.template import estimate_frames
from .daily import EnergyDaily
from .virtual import S, _utc

log = logging.getLogger("data2flow_analytics.energy")

OCCUPANCY = "OCCUPANCY"
METRIC = "occupancy_est"
STEP = timedelta(minutes=5)
LOOKBACK = timedelta(hours=24)
KEEP = timedelta(days=7)
MIN_SIGNALS = 2
# 입력 신호 → 템플릿 역할
ROLES = {"pir": "activity", "co2": "co2", "noise": "noise", "illuminance": "illuminance"}


def occupancy_sensors(cfg: dict) -> list[dict]:
    return [v for v in cfg.get("virtualSensors") or [] if v.get("kind") == OCCUPANCY and v.get("enabled", True) and v.get("outputDeviceId")]


def floor_bucket(t: datetime) -> datetime:
    return t - timedelta(minutes=t.minute % 5, seconds=t.second, microseconds=t.microsecond)


class OccupancySensors:
    def __init__(self, deps: Deps, energy: EnergyDaily, sink: DerivedSink | None = None, enabled: bool | None = None):
        self.deps = deps
        self.energy = energy  # 설정 묶음 캐시(API-ENE-82 reload로 지운다)
        self.sink = sink if sink is not None else PipelineClient(deps.settings.pipeline_uri)
        self.enabled = deps.settings.realtime_enabled if enabled is None else enabled

    # ---- 5분마다 ----
    def tick(self) -> list[dict]:
        if not self.enabled or self.energy.core is None:
            return []
        out = []
        bucket = floor_bucket(self.deps.clock.now())
        for org in self.energy.organizations():
            try:
                for sensor in occupancy_sensors(self.energy.config(org)):
                    res = self.compute(org, sensor, bucket)
                    if res is not None:
                        out.append(res)
            except Exception:  # noqa: BLE001 — 한 조직이 실패해도 다른 조직은 계속
                log.exception("occupancy tick failed org=%s", org)
        self.purge(bucket - KEEP)
        return out

    def compute(self, org: int, sensor: dict, bucket: datetime) -> dict | None:
        """한 공간·한 구간(끝 시각 bucket). 이미 계산했으면 None"""
        space = int(sensor["spaceId"])
        frames: dict[str, pd.Series] = {}
        missing: list[str] = []
        for item in sensor.get("inputs") or []:
            signal = item.get("signal")
            if signal not in ROLES:
                continue
            series = self.signal(item, bucket - LOOKBACK, bucket)
            if series is None:
                missing.append(signal)
            else:
                frames[ROLES[signal]] = series
        value = None
        if len(frames) >= MIN_SIGNALS:
            occ, _ = estimate_frames(frames)
            occ = occ[occ.index <= bucket]
            value = float(occ.iloc[-1]) if len(occ) else None
        status = "OK" if value is not None else "INPUT_MISSING"
        if status == "INPUT_MISSING" and not missing:
            missing = sorted(set(ROLES) - {s for s, r in ROLES.items() if r in frames})
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            if conn.execute(f"""INSERT INTO {S}.virtual_sensor_ticks (organization_id, space_id, kind, bucket, status, value, computed_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING bucket""",
                            (org, space, OCCUPANCY, bucket, status, value, now)).fetchone() is None:
                return None
        sent = False
        if value is not None:
            point = {"deviceId": sensor["outputDeviceId"], "metricKey": METRIC, "ts": bucket, "value": value,
                     "meta": {"method": "rule-based", "signals": [s for s, r in ROLES.items() if r in frames]}}
            try:
                res = self.sink.derived_points(org, [point])
                sent = res.get("accepted", 0) > 0
                if res.get("rejected"):
                    log.warning("occupancy_est rejected org=%s space=%s: %s", org, space, res["rejected"])
            except Exception:  # noqa: BLE001 — pipeline이 잠시 안 돼도 다음 구간은 계속(이번 값은 빠진다)
                log.exception("derived-points failed org=%s space=%s", org, space)
            if sent:
                with self.deps.pool.connection() as conn:
                    conn.execute(f"""UPDATE {S}.virtual_sensor_ticks SET sent = true
                                     WHERE organization_id = %s AND space_id = %s AND kind = %s AND bucket = %s""", (org, space, OCCUPANCY, bucket))
        payload = {"spaceId": space, "kind": OCCUPANCY, "metricKeys": [METRIC], "computedFrom": _utc(bucket - STEP), "computedTo": _utc(bucket),
                   "status": status, "missing": None if status == "OK" else missing}
        ev.safe_publish(self.deps.publisher, ev.make_event(self.deps.clock, ev.ENERGY_VIRTUAL_SENSOR, org, payload))
        return {"organizationId": org, "spaceId": space, "bucket": _utc(bucket), "status": status, "value": value, "sent": sent,
                "missing": payload["missing"]}

    def signal(self, item: dict, start: datetime, end: datetime) -> pd.Series | None:
        """신호 하나: 입력 기기 중 값이 가장 많은 기기의 시계열(없으면 None)"""
        best: pd.Series | None = None
        for device in item.get("deviceIds") or []:
            where = ("device_id = %(d)s AND metric_key = %(m)s AND quality = 0 AND value IS NOT NULL AND NOT is_virtual "
                     "AND time >= %(s)s AND time <= %(e)s")
            with self.deps.pool.connection() as conn:
                rows = conn.execute(f"SELECT time, value FROM {_raw_union(where)} t ORDER BY time",
                                    {"d": int(device), "m": item.get("metricKey"), "s": start, "e": end}).fetchall()
            if rows and (best is None or len(rows) > len(best)):
                best = pd.Series([float(r["value"]) for r in rows], index=pd.DatetimeIndex([r["time"] for r in rows]))
        return best

    def purge(self, before: datetime) -> None:
        with self.deps.pool.connection() as conn:
            conn.execute(f"DELETE FROM {S}.virtual_sensor_ticks WHERE bucket < %s", (before,))
