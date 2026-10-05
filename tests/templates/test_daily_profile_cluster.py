"""ANA-02.12 하루 패턴 군집."""

from __future__ import annotations

import pytest
from sklearn.metrics import adjusted_rand_score

from data2flow_analytics.templates.builtin.daily_profile_cluster.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-02.12")
def test_ANA_02_12_TC_ANA_073_three_profiles():
    """[ANA-02.12][AT-ANA-17.10] 전력량 60일 평일·주말·휴일: k 자동 = 3, ARI ≥ 0.9, 실루엣 표시, 같은 시드면 군집 번호까지 같다"""
    s, labels = synth.daily_profiles(31, days=60, holidays=6)
    data = make_input({"target": {"kwh": s}})
    res = T.run(data, T.parse_params({}), ctx(31))
    metrics = {m.key: m.value for m in res.metrics}
    assert metrics["clusters"] == 3 and metrics["silhouette"] > 0.5
    assert adjusted_rand_score(labels, res.evidence["labels"]) >= 0.9
    again = T.run(data, T.parse_params({}), ctx(31))
    assert again.evidence["labels"] == res.evidence["labels"]


@pytest.mark.spec("ANA-02.12")
def test_ANA_02_12_TC_ANA_074_min_30_days():
    """[ANA-02.12][TC-ANA-074] 최소 기간 30일(25일이면 충분성 FAIL '최소 30일')"""
    assert T.requirements.min_period_days == 30
    s, _ = synth.daily_profiles(1, days=35)
    res = T.run(make_input({"target": {"kwh": s}}), T.parse_params({"normalize": True, "maxClusters": 4}), ctx(1))
    assert res.charts[1]["type"] == "calendar"
