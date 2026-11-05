"""재실 추정 가상 센서(ENE-02.02, TC-ENE-039): 5분마다 occupancy_est 0/1을 pipeline API-ING-25에 source=DERIVED·estimated=true로 보낸다
(Testcontainers PostgreSQL 18, pipeline은 httpx.MockTransport)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from data2flow_analytics.energy.daily import EnergyDaily
from data2flow_analytics.energy.occupancy import OccupancySensors, floor_bucket
from data2flow_analytics.loader.pipeline_client import PipelineClient
from tests.conftest import ORG, insert_telemetry

NOW = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)  # 현지 11:00
OCC = {"spaceId": "31", "kind": "OCCUPANCY", "enabled": True, "params": {"inputs": ["pir", "co2"]}, "timezone": "Asia/Seoul",
       "outputDeviceId": "9001", "inputs": [{"signal": "pir", "metricKey": "activity", "deviceIds": ["4001"]},
                                            {"signal": "co2", "metricKey": "co2", "deviceIds": ["3001"]}]}
ONLY_CO2 = {"spaceId": "32", "kind": "OCCUPANCY", "enabled": True, "params": {"inputs": ["pir", "co2"]}, "timezone": "Asia/Seoul",
            "outputDeviceId": "9002", "inputs": [{"signal": "pir", "metricKey": "activity", "deviceIds": []},
                                                 {"signal": "co2", "metricKey": "co2", "deviceIds": ["3001"]}]}
CONFIG = {"organizationId": str(ORG), "timezone": "Asia/Seoul", "configVersion": 1, "tariffs": [], "emissionFactors": [], "meterSources": [],
          "holidays": {}, "virtualSensors": [OCC, ONLY_CO2,
                                             {"spaceId": "33", "kind": "ACH", "enabled": True, "timezone": "Asia/Seoul", "inputs": []}]}


class Core:
    def space_devices(self, space_id, request_id=None):
        return []

    def space_targets(self, space_id, request_id=None):
        return None

    def energy_organizations(self):
        return [ORG]

    def energy_config(self, org):
        return CONFIG


class Pipeline:
    """pipeline API-ING-25 자리: 받은 요청 본문을 모은다"""

    def __init__(self, status: int = 200):
        self.bodies: list[dict] = []
        self.status = status
        self.client = PipelineClient("http://pipeline", transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/pipeline/derived-points"
        assert request.headers["X-CALLER-SERVICE"] == "data2flow-analytics"
        body = json.loads(request.content)
        self.bodies.append(body)
        return httpx.Response(self.status, json={"header": {"isSuccessful": True}, "response": {"accepted": len(body["points"]), "rejected": []}})


def _signals(occupied_from: datetime) -> tuple[pd.Series, pd.Series]:
    """24시간: occupied_from 전까지 비어 있음(CO2 430, PIR 0), 그 뒤 재실(CO2 상승, PIR 활동)"""
    idx = pd.date_range(NOW - timedelta(hours=24), NOW, freq="1min")
    rng = np.random.default_rng(7)
    occ = idx >= occupied_from
    minutes = np.where(occ, (idx - occupied_from).total_seconds() / 60, 0)
    co2 = pd.Series(430 + np.minimum(minutes * 8, 700) + rng.normal(0, 3, len(idx)), index=idx)
    pir = pd.Series(np.where(occ, rng.integers(1, 4, len(idx)), 0).astype(float), index=idx)
    return co2, pir


@pytest.fixture
def occupied(deps):
    deps.loader.core = Core()
    co2, pir = _signals(NOW - timedelta(hours=1))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 3001, "co2", co2)
        insert_telemetry(conn, 4001, "activity", pir)
    return deps


@pytest.mark.spec("ENE-02.02")
def test_TC_ENE_039_occupancy_sent_as_derived_point(occupied, clock):
    """[TC-ENE-039] 재실 중인 공간은 occupancy_est=1이 공간 가상 기기로 source=DERIVED·estimated=true와 함께 간다. 신호 1개뿐인 공간은
    INPUT_MISSING(보내지 않음), EVT-ENE-03은 공간마다 한 번, 같은 구간은 다시 계산하지 않는다"""
    clock.set(NOW + timedelta(minutes=2, seconds=10))
    pipe = Pipeline()
    job = OccupancySensors(occupied, EnergyDaily(occupied), sink=pipe.client, enabled=True)
    out = job.tick()
    assert [(r["spaceId"], r["status"], r["value"], r["sent"]) for r in out] == [(31, "OK", 1.0, True), (32, "INPUT_MISSING", None, False)]
    assert out[1]["missing"] == ["pir"]

    assert len(pipe.bodies) == 1
    body = pipe.bodies[0]
    assert body["orgId"] == ORG and body["source"] == "DERIVED"
    assert body["points"] == [{"deviceId": 9001, "metricKey": "occupancy_est", "ts": "2026-10-04T02:00:00Z", "value": 1.0, "estimated": True,
                               "meta": {"method": "rule-based", "signals": ["pir", "co2"]}}]

    events = occupied.publisher.of_type("energy.virtual-sensor.updated")
    assert [e.payload for e in events] == [
        {"spaceId": 31, "kind": "OCCUPANCY", "metricKeys": ["occupancy_est"], "computedFrom": "2026-10-04T01:55:00Z",
         "computedTo": "2026-10-04T02:00:00Z", "status": "OK", "missing": None},
        {"spaceId": 32, "kind": "OCCUPANCY", "metricKeys": ["occupancy_est"], "computedFrom": "2026-10-04T01:55:00Z",
         "computedTo": "2026-10-04T02:00:00Z", "status": "INPUT_MISSING", "missing": ["pir"]}]
    with occupied.pool.connection() as conn:
        rows = conn.execute("""SELECT space_id, status, value, sent FROM data2flow_analytics.virtual_sensor_ticks ORDER BY space_id""").fetchall()
    assert [(r["space_id"], r["status"], r["sent"]) for r in rows] == [(31, "OK", True), (32, "INPUT_MISSING", False)]

    # 다른 인스턴스가 같은 구간을 다시 돌아도 한 번만
    assert OccupancySensors(occupied, EnergyDaily(occupied), sink=pipe.client, enabled=True).tick() == []
    assert len(pipe.bodies) == 1


@pytest.mark.spec("ENE-02.02")
def test_ENE_02_02_vacant_space_sends_zero(deps, clock):
    """[ENE-02.02] 하루 내내 비어 있으면 0을 보낸다"""
    deps.loader.core = Core()
    co2, pir = _signals(NOW + timedelta(hours=5))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 3001, "co2", co2)
        insert_telemetry(conn, 4001, "activity", pir)
    clock.set(NOW)
    pipe = Pipeline()
    out = OccupancySensors(deps, EnergyDaily(deps), sink=pipe.client, enabled=True).tick()
    assert out[0]["value"] == 0.0 and pipe.bodies[0]["points"][0]["value"] == 0.0


@pytest.mark.spec("ENE-02.02")
def test_ENE_02_02_pipeline_failure_keeps_going(occupied, clock):
    """[ENE-02.02] pipeline이 실패해도 계산 기록과 이벤트는 남고(sent=false), 다음 구간은 다시 계산한다. 실시간이 꺼져 있으면 돌지 않는다"""
    clock.set(NOW)
    pipe = Pipeline(status=503)
    job = OccupancySensors(occupied, EnergyDaily(occupied), sink=pipe.client, enabled=True)
    assert job.tick()[0]["sent"] is False
    clock.set(NOW + timedelta(minutes=5))
    assert job.tick()[0]["bucket"] == "2026-10-04T02:05:00Z"
    assert OccupancySensors(occupied, EnergyDaily(occupied), sink=pipe.client, enabled=False).tick() == []


def test_floor_bucket():
    assert floor_bucket(datetime(2026, 10, 4, 2, 7, 59, 5, tzinfo=UTC)) == datetime(2026, 10, 4, 2, 5, tzinfo=UTC)
