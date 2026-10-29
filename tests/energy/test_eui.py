"""BR-ENE-06 EUI·재실 시간당 사용량(ENE-01.02)."""

from __future__ import annotations

import pytest

from data2flow_analytics.energy.compute import eui, per_occupied_hour


@pytest.mark.spec("ENE-01.02")
def test_ENE_01_02_TC_ENE_011_eui_and_occupied_hour():
    """[ENE-01.02][AT-ENE-03.2] 500㎡에 6,000kWh면 12, 면적이 없으면 None과 사유, 재실 시간당 분모의 출처"""
    assert eui(6000, 500).value == 12
    assert (eui(6000, None).value, eui(6000, None).reason) == (None, "NO_AREA")
    assert eui(6000, 0).value is None
    by_occ = per_occupied_hour(600, 200, 300)
    assert (by_occ.value, by_occ.denominator_source) == (3, "OCCUPANCY_EST")
    by_sched = per_occupied_hour(600, None, 300)
    assert (by_sched.value, by_sched.denominator_source) == (2, "OPERATING_SCHEDULE")
    assert per_occupied_hour(600, 0, None).reason == "NO_OCCUPANCY"
