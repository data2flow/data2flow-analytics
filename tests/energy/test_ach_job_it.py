"""가상 센서 ACH 일 계산(ENE-02.01, TC-ENE-033~035의 작업 쪽): ach_estimates, virtual_sensor_runs, EVT-ENE-03, 조회 API(Testcontainers PostgreSQL 18)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from data2flow_analytics.energy.daily import EnergyDaily
from data2flow_analytics.energy.virtual import VirtualSensors
from tests.conftest import HEADERS, ORG, insert_telemetry

DAY = date(2026, 10, 4)
START = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)  # 2026-10-04 00:00+09:00
SENSOR = {"spaceId": "31", "kind": "ACH", "enabled": True, "params": {}, "timezone": "Asia/Seoul", "volumeM3": 150,
          "inputs": [{"metricKey": "co2", "deviceIds": ["3001"]}]}
CONFIG = {"organizationId": str(ORG), "timezone": "Asia/Seoul", "configVersion": 1, "tariffs": [], "emissionFactors": [],
          "meterSources": [], "holidays": {},
          "virtualSensors": [SENSOR,
                             {"spaceId": "32", "kind": "ACH", "enabled": True, "timezone": "Asia/Seoul", "inputs": []},
                             {"spaceId": "33", "kind": "ACH", "enabled": False, "timezone": "Asia/Seoul",
                              "inputs": [{"metricKey": "co2", "deviceIds": ["3001"]}]}]}


class VirtualCore:
    def __init__(self):
        self.calls = 0

    def space_devices(self, space_id, request_id=None):
        return []

    def space_targets(self, space_id, request_id=None):
        return None

    def energy_organizations(self):
        return [ORG]

    def energy_config(self, org):
        self.calls += 1
        return CONFIG


def _co2() -> pd.Series:
    """현지 09~17시 재실로 450→1420ppm, 17시 퇴실 후 ACH 2.0으로 감쇠(외기 420ppm)"""
    idx = pd.date_range(START, START + timedelta(days=1) - timedelta(minutes=1), freq="1min")
    values = []
    for t in idx:
        h = (t - START).total_seconds() / 3600
        if h < 9:
            values.append(450.0)
        elif h < 17:
            values.append(450.0 + (h - 9) / 8 * 970)
        else:
            values.append(420.0 + 1000.0 * math.exp(-2.0 * (h - 17)))
    return pd.Series(values, index=idx)


@pytest.fixture
def virtual(deps):
    deps.loader.core = VirtualCore()
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 3001, "co2", _co2())
        # 품질 이상·가상 값은 쓰지 않는다
        insert_telemetry(conn, 3001, "co2", pd.Series([5000.0], index=[START + timedelta(hours=18, minutes=0, seconds=30)]), quality=1)
    return deps


def _runs(deps):
    with deps.pool.connection() as conn:
        return conn.execute("""SELECT space_id, status, reason FROM data2flow_analytics.virtual_sensor_runs
                               WHERE organization_id = %s AND day = %s ORDER BY space_id""", (ORG, DAY)).fetchall()


@pytest.mark.spec("ENE-02.01")
def test_ENE_02_01_ach_tick_writes_estimate_event_once(virtual, clock):
    """[ENE-02.01][BR-ENE-09] 02:00 전에는 계산하지 않고, 지나면 전날 ACH를 한 번 쓰고 EVT-ENE-03을 낸다. 입력 없는 공간은 INPUT_MISSING, 꺼진 공간은 건너뛴다"""
    clock.set(datetime(2026, 10, 4, 16, 59, tzinfo=UTC))  # 현지 10-05 01:59
    job = VirtualSensors(virtual, EnergyDaily(virtual))
    assert job.tick() == []
    clock.set(datetime(2026, 10, 4, 17, 0, tzinfo=UTC))
    out = job.tick()
    assert [(r["spaceId"], r["status"]) for r in out] == [(31, "OK"), (32, "INPUT_MISSING")]
    assert out[0]["ach"] == pytest.approx(2.0, rel=0.05)

    with virtual.pool.connection() as conn:
        row = conn.execute("""SELECT ach, ci_low, ci_high, decay_from, decay_to, r2_fit FROM data2flow_analytics.ach_estimates
                              WHERE organization_id = %s AND space_id = 31 AND day = %s""", (ORG, DAY)).fetchone()
        assert conn.execute("SELECT count(*) AS n FROM data2flow_analytics.ach_estimates").fetchone()["n"] == 1
    assert row["ci_low"] <= row["ach"] <= row["ci_high"] and row["r2_fit"] > 0.99
    assert START + timedelta(hours=16, minutes=55) <= row["decay_from"] <= START + timedelta(hours=17, minutes=10)
    assert [(r["space_id"], r["status"]) for r in _runs(virtual)] == [(31, "OK"), (32, "INPUT_MISSING")]

    events = virtual.publisher.of_type("energy.virtual-sensor.updated")
    assert [e.payload for e in events] == [
        {"spaceId": 31, "kind": "ACH", "metricKeys": ["ach", "ventilation_m3h"], "computedFrom": "2026-10-04T00:00:00+09:00",
         "computedTo": "2026-10-05T00:00:00+09:00", "status": "OK", "missing": None},
        {"spaceId": 32, "kind": "ACH", "metricKeys": ["ach", "ventilation_m3h"], "computedFrom": "2026-10-04T00:00:00+09:00",
         "computedTo": "2026-10-05T00:00:00+09:00", "status": "INPUT_MISSING", "missing": ["co2"]}]

    # 다른 인스턴스가 다시 돌아도 한 번만
    assert VirtualSensors(virtual, EnergyDaily(virtual)).tick() == []
    assert len(virtual.publisher.of_type("energy.virtual-sensor.updated")) == 2


@pytest.mark.spec("ENE-02.01")
def test_ENE_02_01_no_decay_records_reason_without_estimate(deps, clock):
    """[ENE-02.01][TC-ENE-034] 감쇠 구간이 없는 날은 ach_estimates를 쓰지 않고 사유만 남긴다"""
    deps.loader.core = VirtualCore()
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 3001, "co2", pd.Series(430.0, index=pd.date_range(START, periods=1440, freq="1min")))
    clock.set(datetime(2026, 10, 4, 18, 0, tzinfo=UTC))
    out = VirtualSensors(deps, EnergyDaily(deps)).tick()
    assert out[0]["status"] == "OK" and out[0]["ach"] is None and out[0]["reason"]
    with deps.pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM data2flow_analytics.ach_estimates").fetchone()["n"] == 0
    assert _runs(deps)[0]["reason"] == out[0]["reason"]


@pytest.mark.spec("ENE-02.01")
def test_ENE_02_01_ach_api_and_reload(virtual, clock, client, services):
    """[ENE-02.01] core API-ENE-21이 부르는 ACH 조회와 API-ENE-82 reload"""
    services.deps.loader.core = virtual.loader.core
    clock.set(datetime(2026, 10, 4, 17, 0, tzinfo=UTC))
    services.virtual.tick()
    res = client.get("/internal/analytics/energy/spaces/31/ach", params={"from": "2026-10-01", "to": "2026-10-31"}, headers=HEADERS)
    assert res.status_code == 200, res.text
    items = res.json()["response"]["items"]
    assert len(items) == 1 and items[0]["day"] == "2026-10-04" and items[0]["ach"] == pytest.approx(2.0, rel=0.05)
    assert set(items[0]["decayWindow"]) == {"from", "to"} and items[0]["fitR2"] > 0.99
    assert client.get("/internal/analytics/energy/spaces/31/ach", params={"from": "x", "to": "2026-10-31"},
                      headers=HEADERS).status_code == 400

    calls = virtual.loader.core.calls
    services.virtual.energy.config(ORG)
    assert virtual.loader.core.calls == calls  # 캐시
    assert client.post("/internal/analytics/energy/virtual-sensors/reload", json={"spaceId": "31"}, headers=HEADERS).status_code == 204
    services.virtual.energy.config(ORG)
    assert virtual.loader.core.calls == calls + 1
