"""날씨 보정(ENE-01.04, TC-ENE-023): 기준선 모델로 올해 8월 날씨의 예상값을 내고 실제와 비교한다."""

from __future__ import annotations

from datetime import date

import pytest

from data2flow_analytics.energy.baseline import Model, normalized_diff


def test_tc_ene_023_august_normalized_diff():
    m = Model("DEGREE_DAY_REGRESSION", date(2025, 6, 1), date(2025, 9, 30),
              {"intercept": 50.0, "cdd": 8.0, "hdd": 0.0, "weekend": 0.0}, 0.95)
    # 8월 31일, 일 CDD 합 250 → 50·31 + 8·250 = 3,550
    predicted = 50.0 * 31 + sum(m.coefficients["cdd"] * c for c in [250 / 31] * 31)
    assert predicted == pytest.approx(3550, rel=0.02)
    assert normalized_diff(3195, predicted) == pytest.approx(-0.10, abs=0.005)


def test_no_prediction():
    assert normalized_diff(10, 0) is None
