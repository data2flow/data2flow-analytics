"""에너지 일 집계 작업(ENE-01.01·01.02, TC-ENE-006): energy_daily 출처별 행, EVT-ENE-01, 두 인스턴스 동시 실행에도 한 번만(Testcontainers PostgreSQL 18)."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from data2flow_analytics.energy.daily import EnergyDaily
from tests.conftest import ORG, insert_telemetry

DAY = date(2026, 10, 4)
START = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)  # 2026-10-04 00:00+09:00
CONFIG = {
    "organizationId": str(ORG), "timezone": "Asia/Seoul", "configVersion": 3,
    "tariffs": [{"tariffId": "1", "scopeSpace": None, "validFrom": "2026-01-01", "mode": "FLAT", "flatRateKrwPerKwh": 150},
                {"tariffId": "2", "scopeSpace": {"id": "10", "name": "광주캠퍼스"}, "validFrom": "2026-10-01", "mode": "TOU",
                 "touSchedule": {"seasons": [{"name": "ALL", "months": list(range(1, 13)), "bands": [
                     {"dayType": "WEEKDAY", "from": "00:00", "to": "24:00", "rate": 200},
                     {"dayType": "WEEKEND_HOLIDAY", "from": "00:00", "to": "12:00", "rate": 100},
                     {"dayType": "WEEKEND_HOLIDAY", "from": "12:00", "to": "24:00", "rate": 50}]}]}}],
    "emissionFactors": [{"emissionFactorId": "1", "validFrom": "2020-01-01", "factorTco2PerMwh": 0.4567, "system": True}],
    "meterSources": [
        {"sourceId": "401", "spaceId": "31", "siteId": "10", "timezone": "Asia/Seoul", "kind": "METERED", "deviceId": "2001",
         "metricKey": "energy", "cumulative": True},
        {"sourceId": "402", "spaceId": "32", "siteId": "20", "timezone": "Asia/Seoul", "kind": "METERED", "deviceId": "2002",
         "metricKey": "power", "cumulative": False},
        {"sourceId": "403", "spaceId": "32", "siteId": "20", "timezone": "Asia/Seoul", "kind": "ESTIMATED", "deviceId": "2003",
         "ratedPowerW": 1200}],
    "holidays": {"10": []},
}


class Action:
    """action 가동 시간 대역: 2003은 그날 8시간 가동"""

    def __init__(self, fail: bool = False):
        self.fail = fail

    def on_seconds(self, org, day, device_ids):
        if self.fail:
            raise ConnectionError("action down")
        return {d: 8 * 3600 for d in device_ids if d == 2003}


class EnergyCore:
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


@pytest.fixture
def energy(deps):
    deps.loader.core = EnergyCore()
    with deps.pool.connection() as conn:
        # 2001: 누적 kWh 10분마다 0.5씩, 정오(현지)에 계량기 리셋. 날짜 경계 앞뒤 한 점씩 더
        idx = pd.date_range(START - timedelta(minutes=10), START + timedelta(days=1, minutes=10), freq="10min")
        values = [100.0 + 0.5 * i for i in range(len(idx))]
        reset = idx.get_loc(pd.Timestamp(START + timedelta(hours=12)))
        values = values[:reset] + [0.2 + 0.5 * (i - reset) for i in range(reset, len(idx))]
        insert_telemetry(conn, 2001, "energy", pd.Series(values, index=idx))
        # 2002: 1000W 1분마다, 현지 18시~19시는 수신 공백(추정)
        idx = pd.date_range(START, START + timedelta(days=1), freq="1min")
        series = pd.Series(1000.0, index=idx)
        series[(idx > START + timedelta(hours=18)) & (idx < START + timedelta(hours=19))] = None
        insert_telemetry(conn, 2002, "power", series)
        # 품질 이상 값은 쓰지 않는다
        insert_telemetry(conn, 2002, "power", pd.Series([999999.0], index=[START + timedelta(hours=18, minutes=30)]), quality=1)
    return deps


def _rows(deps):
    with deps.pool.connection() as conn:
        return conn.execute("""SELECT space_id, source_kind, kwh, krw, tco2, includes_estimate FROM data2flow_analytics.energy_daily
                               WHERE organization_id = %s AND day = %s ORDER BY space_id, source_kind""", (ORG, DAY)).fetchall()


@pytest.mark.spec("ENE-01.01")
def test_ENE_01_01_TC_ENE_006_daily_rows_event_and_single_writer(energy, clock):
    """[ENE-01.01][AT-ENE-02.1] 공간 × 출처 행, 누적 리셋 0, 공백 구간 추정, 사이트 TOU·조직 단가, 탄소. 두 인스턴스가 동시에 돌아도 한 번만 쓰고 이벤트 한 번"""
    clock.set(datetime(2026, 10, 4, 16, 59, tzinfo=UTC))  # 현지 01:59 — 아직
    assert EnergyDaily(energy, Action()).tick() == []
    clock.set(datetime(2026, 10, 4, 17, 0, tzinfo=UTC))  # 현지 02:00
    jobs = [EnergyDaily(energy, Action()), EnergyDaily(energy, Action())]
    results: list = []
    barrier = threading.Barrier(2)

    def run(job):
        barrier.wait()
        results.append(job.compute(ORG, DAY))

    threads = [threading.Thread(target=run, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1

    rows = _rows(energy)
    assert [(r["space_id"], r["source_kind"]) for r in rows] == [(31, "METERED"), (32, "ESTIMATED"), (32, "METERED")]
    meter, estimate, power = rows
    # 144칸 × 0.5kWh에서 리셋 구간 하나(0) = 71.5kWh. 2026-10-04는 일요일 → 사이트 10의 TOU 주말 요금(오전 100, 오후 50)
    assert meter["kwh"] == pytest.approx(71.5, abs=1e-6) and not meter["includes_estimate"]
    assert float(meter["krw"]) == pytest.approx(0.5 * 72 * 100 + 0.5 * 71 * 50 - 0.5 * 50, abs=1e-6)
    assert meter["tco2"] == pytest.approx(71.5 / 1000 * 0.4567, abs=1e-9)
    # 1000W × 24h(공백 구간도 양끝 표본 사이 적분) = 24kWh, 사이트 20은 사이트 단가가 없어 조직 정액 150원
    assert power["kwh"] == pytest.approx(24.0, abs=1e-6) and power["includes_estimate"]
    assert float(power["krw"]) == pytest.approx(24.0 * 150, abs=1e-4)

    # 추정: 정격 1.2kW × 8시간(대기 전력 없음) = 9.6kWh, 출처 ESTIMATED는 측정과 따로 저장(BR-ENE-01)
    assert estimate["kwh"] == pytest.approx(9.6, abs=1e-9) and estimate["includes_estimate"]
    assert float(estimate["krw"]) == pytest.approx(9.6 * 150, abs=1e-4)

    events = energy.publisher.of_type("energy.daily.updated")
    assert len(events) == 1 and events[0].payload == {"days": ["2026-10-04"], "spaceIds": ["31", "32"], "recomputed": False}

    # 이미 쓴 날은 tick이 건너뛰고, 다시 계산 요청은 다시 쓴다
    assert EnergyDaily(energy, Action()).tick() == []
    again = EnergyDaily(energy, Action()).compute(ORG, DAY, recomputed=True)
    assert again == {"organizationId": ORG, "day": "2026-10-04", "spaces": 2, "rows": 3}
    assert len(_rows(energy)) == 3
    assert energy.publisher.of_type("energy.daily.updated")[-1].payload["recomputed"] is True


@pytest.mark.spec("ENE-01.01")
def test_ENE_01_01_tick_runs_due_org_once_and_caches_config(energy, clock):
    """[ENE-01.01] 02:00이 지나면 tick이 전날을 한 번 쓰고, 설정 묶음은 5분 캐시한다"""
    clock.set(datetime(2026, 10, 4, 18, 0, tzinfo=UTC))
    job = EnergyDaily(energy, Action())
    assert [r["day"] for r in job.tick()] == ["2026-10-04"]
    assert job.tick() == []
    assert energy.loader.core.calls == 1
    clock.advance(timedelta(minutes=5))
    job.tick()
    assert energy.loader.core.calls == 2
    job.invalidate(ORG)
    job.tick()
    assert energy.loader.core.calls == 3


@pytest.mark.spec("ENE-01.01")
def test_ENE_01_01_estimated_rows_skipped_when_runtime_unavailable(energy):
    """[ENE-01.01] action에 닿지 않으면 그날 추정 행은 쓰지 않고 측정 행만 쓴다"""
    out = EnergyDaily(energy, Action(fail=True)).compute(ORG, DAY)
    assert out["rows"] == 2
    assert [r["source_kind"] for r in _rows(energy)] == ["METERED", "METERED"]
