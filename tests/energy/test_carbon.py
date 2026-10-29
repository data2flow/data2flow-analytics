"""BR-ENE-05 탄소(ENE-01.02)."""

from __future__ import annotations

from datetime import date

import pytest

from data2flow_analytics.energy.compute import EmissionFactor, carbon, round_carbon


@pytest.mark.spec("ENE-01.02")
def test_ENE_01_02_TC_ENE_010_carbon_by_period():
    """[ENE-01.02][AT-ENE-03.3] 0.4567에서 6,000kWh = 2.7402 → 2.74 tCO2eq, 날짜마다 그 기간의 계수"""
    factors = [EmissionFactor(date(2020, 1, 1), 0.4567), EmissionFactor(date(2026, 10, 1), 0.5)]
    assert round_carbon(carbon([(date(2026, 9, 15), 6000.0)], factors)) == 2.74
    assert carbon([(date(2026, 9, 30), 1000.0), (date(2026, 10, 1), 1000.0)], factors) == pytest.approx(0.4567 + 0.5)
    assert carbon([(date(2019, 1, 1), 1000.0)], factors) is None
