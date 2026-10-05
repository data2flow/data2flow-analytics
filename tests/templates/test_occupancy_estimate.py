"""ANA-02.10 재실·활용률 추정(BR-ANA-25)."""

from __future__ import annotations

import pytest
from sklearn.metrics import accuracy_score, f1_score

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.templates.builtin.occupancy_estimate.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-02.10")
def test_ANA_02_10_TC_ANA_067_rule_based_accuracy():
    """[ANA-02.10][AT-ANA-17.8] CO2+activity 14일(라벨): 정확도 ≥ 0.85, F1 ≥ 0.8, 활용률 오차 ≤ 3%p"""
    df, labels = synth.occupancy(17, days=14)
    data = make_input({"co2": {"co2": df["co2"]}, "activity": {"act": df["activity"]}})
    occ, _ = T.estimate(data)
    assert accuracy_score(labels, occ) >= 0.85 and f1_score(labels, occ) >= 0.8
    res = T.run(data, T.parse_params({}), ctx(17))
    local = labels.index.tz_convert("Asia/Seoul")
    minutes = local.hour * 60 + local.minute
    truth = labels[(minutes >= 540) & (minutes < 1080) & (local.dayofweek < 5)].mean() * 100
    util = next(m.value for m in res.metrics if m.key == "utilizationPercent")
    assert abs(util - truth) <= 3.0
    assert "ESTIMATE" in res.flags and res.charts[0]["type"] == "timeline"


@pytest.mark.spec("ANA-02.10")
def test_ANA_02_10_needs_two_signals_and_other_signals():
    """[ANA-02.10] 재실 신호 2개 미만이면 ANALYSIS_BINDING_INVALID, 소음·조도 조합도 추정한다"""
    with pytest.raises(BusinessError):
        T.validate_inputs([{"role": "co2", "sources": [{"metricKey": "co2", "deviceId": "1"}]}], T.parse_params({}))
    df, _ = synth.occupancy(2, days=7)
    noise = 35 + 15 * (df["activity"] > 0)
    lux = 50 + 300 * (df["activity"].rolling(5, min_periods=1).max() > 0)
    res = T.run(make_input({"noise": {"n": noise}, "illuminance": {"l": lux}}), T.parse_params({"weekdaysOnly": False}), ctx(2))
    assert next(m.value for m in res.metrics if m.key == "utilizationPercent") > 0
    assert T.runnable_with({"co2", "noise"}, True) == (True, [])
    assert T.runnable_with({"co2"}, True)[0] is False
