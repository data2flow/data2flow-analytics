"""BR-ENE-03 전력 적분·누적 차분(ENE-01.01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data2flow_analytics.energy.compute import diff_cumulative, integrate_power, total

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _minutes(values, step=1):
    return [(T0 + timedelta(minutes=i * step), v) for i, v in enumerate(values)]


@pytest.mark.spec("ENE-01.01")
def test_ENE_01_01_TC_ENE_001_trapezoid_and_cumulative():
    """[ENE-01.01][AT-ENE-02.1] 1000W 1시간 1.000kWh, 0→1200W 선형 0.600kWh, 20분 공백은 추정, 누적 리셋 구간은 0"""
    kwh, estimated = total(integrate_power(_minutes([1000.0] * 61)))
    assert kwh == pytest.approx(1.0, abs=1e-6) and not estimated
    kwh, _ = total(integrate_power(_minutes([1200.0 * i / 60 for i in range(61)])))
    assert kwh == pytest.approx(0.6, abs=1e-6)
    gap = integrate_power([(T0, 600.0), (T0 + timedelta(minutes=1), 600.0), (T0 + timedelta(minutes=21), 600.0)])
    assert [i.estimated for i in gap] == [False, True]
    assert gap[1].kwh == pytest.approx(0.2, abs=1e-6)
    kwh, _ = total(diff_cumulative(_minutes([100.0, 100.5, 0.2, 0.7])))
    assert kwh == pytest.approx(1.0, abs=1e-6)


@pytest.mark.spec("ENE-01.01")
def test_ENE_01_01_power_15_minutes_is_not_a_gap():
    """[ENE-01.01] 간격이 정확히 15분이면 추정이 아니다(15분을 넘을 때만), 같은 시각 표본은 건너뛴다"""
    out = integrate_power([(T0, 100.0), (T0, 100.0), (T0 + timedelta(minutes=15), 100.0)])
    assert len(out) == 1 and not out[0].estimated
