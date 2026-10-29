"""기준선 학습(ENE-01.04, API-ENE-81 → EVT-ENE-02)과 지표의 기준선 비교(API-ENE-85, TC-ENE-023) — Testcontainers PostgreSQL 18."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from data2flow_analytics.worker.executor import InlineExecutor
from data2flow_analytics.worker.main import Worker
from tests.conftest import HEADERS, ORG

SITE, BLDG, R1, SIM = 1, 10, 101, 109
TEMP = {"deviceId": 900, "metricKey": "temperature"}
SPACES = [{"id": SITE, "parentId": None, "name": "캠퍼스", "kind": "SITE"},
          {"id": BLDG, "parentId": SITE, "name": "본관", "kind": "BUILDING"},
          {"id": R1, "parentId": BLDG, "name": "101", "kind": "ROOM"},
          {"id": SIM, "parentId": BLDG, "name": "가상실", "kind": "ROOM", "virtual": True}]
START = date(2026, 8, 1)
KST = ZoneInfo("Asia/Seoul")


def _cdd(i: int) -> float:
    return float(i % 7)  # 0~6


def _seed(deps, days: int, start: date = START, factor: float = 1.0, temps: bool = True):
    with deps.pool.connection() as conn:
        for i in range(days):
            d = start + timedelta(days=i)
            cdd = _cdd((d - START).days)
            conn.execute("""INSERT INTO data2flow_analytics.energy_daily (organization_id, space_id, day, source_kind, kwh, includes_estimate)
                            VALUES (%s, %s, %s, 'METERED', %s, false)""", (ORG, R1, d, (50 + 8 * cdd) * factor))
            conn.execute("""INSERT INTO data2flow_analytics.energy_daily (organization_id, space_id, day, source_kind, kwh, includes_estimate)
                            VALUES (%s, %s, %s, 'METERED', 5000, false)""", (ORG, SIM, d))
            if temps:
                midnight = datetime.combine(d, time(0), KST).astimezone(UTC)
                for h in range(24):  # KST 날짜의 시간 평균 = 24 + CDD
                    conn.execute("""INSERT INTO data2flow_pipeline.telemetry_1h (device_id, metric_key, bucket, organization_id, count, count_all, avg)
                                    VALUES (%s, %s, %s, %s, 1, 1, %s)""",
                                 (TEMP["deviceId"], TEMP["metricKey"], midnight + timedelta(hours=h), ORG, 24 + cdd + (h - 11.5) / 10))


def _train(client, version=12, **over):
    body = {"versionId": version, "scopeSpaceId": BLDG, "method": "DEGREE_DAY_REGRESSION", "trainFrom": "2026-08-01", "trainTo": "2026-09-29",
            "params": {"splitWeekday": False}, "excludeVirtual": True, "spaces": SPACES, "temperatureSource": TEMP, "timezone": "Asia/Seoul", **over}
    return client.post("/internal/analytics/energy/baselines/train", json=body, headers=HEADERS)


def _events(deps):
    return [e.payload for e in deps.publisher.of_type("energy.baseline.trained")]


@pytest.fixture
def worker(deps):
    return Worker(deps, InlineExecutor(deps.registry), None)


def test_regression_trains_and_publishes_ready(client, deps, worker):
    _seed(deps, 60)
    res = _train(client)
    assert res.status_code == 202, res.text
    body = res.json()["response"]
    assert body["versionId"] == "12" and body["modelKey"] == "bl-12"
    assert _train(client).json()["response"]["jobId"] == body["jobId"]  # 같은 버전은 작업 하나
    assert worker.drain() == ["READY"]
    [ev] = _events(deps)
    assert ev["status"] == "READY" and ev["versionId"] == 12 and ev["trainedDays"] == 60 and ev["residualsRef"] == "bl-12"
    assert ev["coefficients"]["cdd"] == pytest.approx(8) and ev["coefficients"]["intercept"] == pytest.approx(50)  # 가상실 5000 kWh는 빠진다
    assert ev["r2"] == pytest.approx(1.0) and ev["warning"] is None
    with deps.pool.connection() as conn:
        row = conn.execute("SELECT * FROM data2flow_analytics.baseline_models WHERE organization_id = %s AND key = 'bl-12'", (ORG,)).fetchone()
    assert row["method"] == "DEGREE_DAY_REGRESSION" and len(row["residuals"]) == 60
    assert row["scope"]["temperatureSource"] == TEMP and SIM in row["scope"]["spaceIds"]


def test_period_average_needs_no_weather(client, deps, worker):
    _seed(deps, 35, temps=False)
    assert _train(client, 13, method="PERIOD_AVERAGE", temperatureSource=None, trainTo="2026-09-04").status_code == 202
    assert worker.drain() == ["READY"]
    assert _events(deps)[0]["coefficients"]["intercept"] == pytest.approx(sum(50 + 8 * _cdd(i) for i in range(35)) / 35)


def test_failures_no_weather_and_insufficient(client, deps, worker):
    _seed(deps, 25)
    _train(client, 14, temperatureSource=None)
    _train(client, 15)
    assert worker.drain() == ["FAILED", "FAILED"]
    no_weather, short = _events(deps)
    assert no_weather["versionId"] == 14 and no_weather["error"] == "NO_WEATHER"
    assert short["versionId"] == 15 and short["error"] == "INSUFFICIENT_DATA" and short["neededDays"] == 5 and short["trainedDays"] == 25


def test_validation(client):
    assert _train(client, method="MAGIC").status_code == 400
    assert _train(client, versionId=0).status_code == 400
    assert _train(client, trainTo="2026-07-01").status_code == 400
    assert _train(client, temperatureSource={"deviceId": 900}).status_code == 400
    assert _train(client, scopeSpaceId=999).status_code == 404


def test_dead_job_publishes_failed(deps, worker, clock):
    with deps.pool.connection() as conn:
        conn.execute("""INSERT INTO data2flow_analytics.jobs (organization_id, type, ref_id, status, locked_by, locked_until, attempts, available_at,
                        payload) VALUES (%s, 'BASELINE', 16, 'LOCKED', 'w', %s, 3, %s, '{}')""", (ORG, clock.now() - timedelta(minutes=1), clock.now()))
    assert worker.reap()["dead"] == 1
    assert _events(deps) == [{"versionId": 16, "status": "FAILED", "r2": None, "coefficients": None, "residualsRef": None, "warning": None,
                              "error": "INTERNAL_ERROR", "neededDays": None, "trainedDays": None}]


def test_tc_ene_023_metrics_compare_with_baseline(client, deps, worker):
    _seed(deps, 60)
    _train(client)
    worker.drain()
    _seed(deps, 3, start=date(2026, 10, 1), factor=0.9)  # 실제는 날씨 보정 예상보다 10% 적다
    res = client.post("/internal/analytics/energy/metrics", headers=HEADERS,
                      json={"spaces": SPACES, "scopeSpaceId": R1, "from": "2026-10-01", "to": "2026-10-03", "compare": "BASELINE", "baselineKey": "bl-12"})
    assert res.status_code == 200, res.text
    body = res.json()["response"]
    expected = sum(50 + 8 * _cdd((date(2026, 10, 1) - START).days + i) for i in range(3))
    assert body["compare"]["kind"] == "BASELINE" and body["compare"]["kwh"] == pytest.approx(expected, rel=1e-4)
    assert body["compare"]["diffRatio"] == pytest.approx(-0.1, abs=1e-3)
    assert all(s["baselineKwh"] is not None for s in body["series"])
    missing = client.post("/internal/analytics/energy/metrics", headers=HEADERS,
                          json={"spaces": SPACES, "from": "2026-10-01", "to": "2026-10-03", "compare": "BASELINE", "baselineKey": "bl-404"})
    assert missing.json()["response"]["compare"]["reason"] == "NO_BASELINE"
