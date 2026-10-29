"""BR-ENE-05 비용(ENE-01.02)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from data2flow_analytics.energy.compute import Tariff, Tariffs, cost

KST = timezone(timedelta(hours=9))
TOU = {"seasons": [{"name": "ALL", "months": list(range(1, 13)), "bands": [
    {"dayType": d, "from": f, "to": t, "rate": r} for d in ("WEEKDAY", "WEEKEND_HOLIDAY")
    for f, t, r in (("00:00", "09:00", 100), ("09:00", "18:00", 200), ("18:00", "24:00", 100))]}]}


@pytest.mark.spec("ENE-01.02")
def test_ENE_01_02_TC_ENE_009_period_boundary_tou_and_base_charge():
    """[ENE-01.02][AT-ENE-01.1] 9월 150원, 10월 1일 00:00+09:00부터 160원. TOU 10시 10kWh + 20시 10kWh = 3,000원. 기본요금은 월 범위만"""
    flat = Tariffs([Tariff(date(2026, 1, 1), "FLAT", 150.0, base_charge=1000), Tariff(date(2026, 10, 1), "FLAT", 160.0, base_charge=2000)])
    before = datetime(2026, 9, 30, 23, 0, tzinfo=KST)
    after = datetime(2026, 10, 1, 0, 0, tzinfo=KST)
    # UTC로 넘어와도 현지(Asia/Seoul) 날짜로 기간을 고른다
    assert cost([(before.astimezone(UTC), 1.0)], flat, "Asia/Seoul").krw == 150.0
    assert cost([(after.astimezone(UTC), 1.0)], flat, "Asia/Seoul").krw == 160.0
    monthly = cost([(after, 10.0)], flat, "Asia/Seoul", months=[date(2026, 10, 1)])
    assert (monthly.krw, monthly.base_charge_krw) == (3600.0, 2000.0)

    tou = Tariffs([Tariff(date(2026, 1, 1), "TOU", tou=TOU)])
    out = cost([(datetime(2026, 10, 7, 10, tzinfo=KST), 10.0), (datetime(2026, 10, 7, 20, tzinfo=KST), 10.0)], tou, "Asia/Seoul")
    assert out.krw == pytest.approx(3000.0)


@pytest.mark.spec("ENE-01.02")
def test_ENE_01_02_site_tariff_and_unpriced():
    """[ENE-01.02] 사이트 단가가 있으면 사이트, 없으면 조직. 단가가 없는 날의 사용량은 비용에 넣지 않고 따로 센다"""
    tariffs = Tariffs([Tariff(date(2026, 1, 1), "FLAT", 150.0), Tariff(date(2026, 6, 1), "FLAT", 120.0, scope_space_id=7)])
    at = datetime(2026, 7, 1, 12, tzinfo=KST)
    assert cost([(at, 1.0)], tariffs, "Asia/Seoul", site_id=7).krw == 120.0
    assert cost([(at, 1.0)], tariffs, "Asia/Seoul", site_id=8).krw == 150.0
    out = cost([(datetime(2025, 12, 31, 12, tzinfo=KST), 2.0)], tariffs, "Asia/Seoul")
    assert (out.krw, out.unpriced_kwh) == (None, 2.0)


@pytest.mark.spec("ENE-01.02")
def test_ENE_01_02_tou_weekend_holiday():
    """[ENE-01.02] 주말·공휴일 요금대는 WEEKEND_HOLIDAY(조직 달력의 공휴일 포함)"""
    sched = {"seasons": [{"name": "ALL", "months": list(range(1, 13)), "bands": [
        {"dayType": "WEEKDAY", "from": "00:00", "to": "24:00", "rate": 200}, {"dayType": "WEEKEND_HOLIDAY", "from": "00:00", "to": "24:00", "rate": 50}]}]}
    tariffs = Tariffs([Tariff(date(2026, 1, 1), "TOU", tou=sched)], holidays=frozenset({date(2026, 10, 9)}))
    assert tariffs.rate(datetime(2026, 10, 8, 23, 59)) == 200.0
    assert tariffs.rate(datetime(2026, 10, 9, 12)) == 50.0
    assert tariffs.rate(datetime(2026, 10, 10, 0, 0)) == 50.0
