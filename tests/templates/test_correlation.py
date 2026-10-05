"""ANA-02.11 상관 분석."""

from __future__ import annotations

import pytest

from data2flow_analytics.templates.builtin.correlation.template import CAVEAT
from data2flow_analytics.templates.builtin.correlation.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-02.11")
def test_ANA_02_11_TC_ANA_070_lagged():
    """[ANA-02.11][AT-ANA-17.9] 외기 → 실내 2시간 지연(r=0.8) 30일: 최대 시차 2h ± 10분, 계수 0.8 ± 0.05, 행렬 대칭·대각 1"""
    df = synth.lagged(6, lag="2h", r=0.8)
    res = T.run(make_input({"target": {"outdoor": df["outdoor"], "indoor": df["indoor"]}}), T.parse_params({}), ctx(6))
    pair = res.tables[0]["rows"][0]
    assert abs(pair["bestLagMinutes"] - 120) <= 10 and abs(pair["bestR"] - 0.8) <= 0.05 and pair["leader"] == "outdoor"
    m = res.charts[0]["heatmap"]["values"]
    assert m[0][0] == 1.0 and m[1][1] == 1.0 and m[0][1] == m[1][0]
    assert CAVEAT in res.caveats


@pytest.mark.spec("ANA-02.11")
def test_ANA_02_11_spearman_and_reverse_lag():
    """[ANA-02.11] SPEARMAN, 역방향 시차(B가 먼저)도 찾는다"""
    df = synth.lagged(6, lag="1h", r=0.8)
    res = T.run(make_input({"target": {"indoor": df["indoor"], "outdoor": df["outdoor"]}}), T.parse_params({"method": "SPEARMAN"}), ctx(6))
    pair = res.tables[0]["rows"][0]
    assert abs(pair["bestLagMinutes"] + 60) <= 10 and pair["leader"] == "outdoor"
