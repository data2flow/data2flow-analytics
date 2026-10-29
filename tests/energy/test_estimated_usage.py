"""BR-ENE-04 추정 사용량, BR-ENE-01 출처 우선(ENE-01.01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data2flow_analytics.energy.compute import ESTIMATED, METERED, estimated_usage, pick_source

DAY = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.spec("ENE-01.01")
def test_ENE_01_01_TC_ENE_002_rated_and_standby():
    """[ENE-01.01][AT-ENE-02.1] 정격 1.2kW 8시간 + 대기 5W 16시간 = 9.68kWh, 출처 ESTIMATED"""
    running = [(DAY + timedelta(hours=9), DAY + timedelta(hours=13)), (DAY + timedelta(hours=12), DAY + timedelta(hours=17)),
               (DAY - timedelta(hours=2), DAY - timedelta(hours=1))]
    kwh = estimated_usage(1200, 5, running, DAY, DAY + timedelta(days=1))
    assert kwh == pytest.approx(9.68, abs=1e-9)
    usage = pick_source(None, kwh)
    assert usage.source == ESTIMATED and usage.comparison is None


@pytest.mark.spec("ENE-01.01")
def test_ENE_01_01_TC_ENE_003_metered_wins():
    """[ENE-01.01][AT-ENE-02.1] 측정 10.0과 추정 9.68이 함께 있으면 합계는 측정, 추정은 comparison으로만"""
    usage = pick_source(10.0, 9.68)
    assert (usage.kwh, usage.source, usage.comparison) == (10.0, METERED, 9.68)
    assert pick_source(None, None).kwh is None
