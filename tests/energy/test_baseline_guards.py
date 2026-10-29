"""기준선 가드(ENE-01.04, TC-ENE-024): 낮은 적합도, 최소 30일, 가상 공간 제외."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from data2flow_analytics.energy.baseline import Day, InsufficientData, fit

START = date(2026, 6, 1)


def _days(n: int, virtual: int = 0) -> list[Day]:
    rng = np.random.default_rng(3)
    out = [Day(START + timedelta(days=i), 50 + 8 * (c := float(rng.uniform(0, 5))), cdd=c) for i in range(n)]
    out += [Day(START + timedelta(days=i), 9999.0, cdd=1.0, virtual=True) for i in range(virtual)]
    return out


def test_tc_ene_024_noise_is_low_fit():
    rng = np.random.default_rng(11)
    days = [Day(START + timedelta(days=i), float(rng.normal(100, 30)), cdd=float(rng.uniform(0, 5))) for i in range(60)]
    m = fit(days, "DEGREE_DAY_REGRESSION", split_weekday=False)
    assert m.r2 < 0.5
    assert m.warnings == ["LOW_FIT"]


@pytest.mark.parametrize(("n", "needed"), [(25, 5), (29, 1)])
def test_tc_ene_024_minimum_days(n, needed):
    with pytest.raises(InsufficientData) as e:
        fit(_days(n), "DEGREE_DAY_REGRESSION")
    assert e.value.needed_days == needed
    assert e.value.have_days == n


def test_tc_ene_024_thirty_days_train():
    assert fit(_days(30), "DEGREE_DAY_REGRESSION").days == 30


def test_tc_ene_024_virtual_rows_excluded():
    with pytest.raises(InsufficientData):
        fit(_days(20, virtual=20), "PERIOD_AVERAGE")
    m = fit(_days(30, virtual=10), "DEGREE_DAY_REGRESSION", split_weekday=False)
    assert m.days == 30
    assert m.coefficients["cdd"] == pytest.approx(8)
    assert m.r2 == pytest.approx(1.0)
