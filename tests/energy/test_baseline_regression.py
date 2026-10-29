"""기준선 회귀(ENE-01.04, TC-ENE-020·021): 합성 데이터에서 계수를 되찾는다."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from data2flow_analytics.energy.baseline import Day, fit

START = date(2026, 6, 1)


def test_tc_ene_020_cdd_only_regression():
    rng = np.random.default_rng(42)
    days = []
    for i in range(60):
        cdd = float(rng.uniform(0, 8))
        days.append(Day(START + timedelta(days=i), 50 + 8 * cdd + float(rng.normal(0, 2)), cdd=cdd))
    m = fit(days, "DEGREE_DAY_REGRESSION", split_weekday=False)
    assert 7.8 <= m.coefficients["cdd"] <= 8.2
    assert 48 <= m.coefficients["intercept"] <= 52
    assert m.coefficients["hdd"] == 0.0
    assert m.r2 >= 0.9
    assert m.warnings == []
    assert m.days == 60 and m.train_from == START and m.train_to == START + timedelta(days=59)
    assert len(m.residuals) == 60


def test_tc_ene_021_cdd_hdd_weekend_regression():
    rng = np.random.default_rng(7)
    days = []
    for i in range(90):
        d = START + timedelta(days=i)
        cdd, hdd = (float(rng.uniform(0, 6)), 0.0) if i % 2 else (0.0, float(rng.uniform(0, 6)))
        weekend = d.weekday() >= 5
        days.append(Day(d, 40 + 6 * cdd + 4 * hdd + (10 if weekend else 0) + float(rng.normal(0, 1)), cdd, hdd, weekend))
    c = fit(days, "DEGREE_DAY_REGRESSION").coefficients
    assert c["intercept"] == pytest.approx(40, rel=0.05)
    assert c["cdd"] == pytest.approx(6, rel=0.05)
    assert c["hdd"] == pytest.approx(4, rel=0.05)
    assert c["weekend"] == pytest.approx(10, rel=0.05)


def test_period_average_weekday_and_weekend():
    days = [Day(START + timedelta(days=i), 120.0 if (START + timedelta(days=i)).weekday() >= 5 else 100.0,
                weekend=(START + timedelta(days=i)).weekday() >= 5) for i in range(35)]
    m = fit(days, "PERIOD_AVERAGE")
    assert m.coefficients == {"intercept": 100.0, "cdd": 0.0, "hdd": 0.0, "weekend": 20.0}
    assert m.r2 == 1.0
    assert m.predict(weekend=True) == 120.0
    assert m.to_json()["method"] == "PERIOD_AVERAGE"


def test_unknown_method():
    with pytest.raises(ValueError):
        fit([], "MAGIC")
