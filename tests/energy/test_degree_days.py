"""도일(ENE-01.04, TC-ENE-022): 냉방 24℃·난방 18℃ 기준, 사이트 시간대(KST) 날짜별 시간 평균."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from data2flow_analytics.energy.baseline import daily_means, degree_days


@pytest.mark.parametrize(("mean", "cdd", "hdd"), [(28.0, 4.0, 0.0), (24.0, 0.0, 0.0), (21.0, 0.0, 0.0), (18.0, 0.0, 0.0), (10.5, 0.0, 7.5)])
def test_tc_ene_022_degree_days(mean, cdd, hdd):
    assert degree_days(mean) == (cdd, hdd)


def test_custom_bases():
    assert degree_days(22.0, cooling_base=20, heating_base=23) == (2.0, 1.0)


def test_tc_ene_022_daily_means_follow_kst():
    # KST 10/1 00시 = UTC 9/30 15시. 그 앞 시각은 KST 9/30
    base = datetime(2026, 9, 30, 15, tzinfo=UTC)
    hourly = [(base + timedelta(hours=h), 20.0 + h) for h in range(24)]
    hourly.append((base - timedelta(hours=1), 99.0))
    hourly.append((base + timedelta(hours=2), None))
    means = daily_means(hourly, "Asia/Seoul")
    assert means[date(2026, 10, 1)] == pytest.approx(20 + 11.5)
    assert means[date(2026, 9, 30)] == 99.0
