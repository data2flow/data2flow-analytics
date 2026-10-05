"""ANA-02.13 변화 구간 탐지."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.templates.builtin.change_point.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-02.13")
def test_ANA_02_13_TC_ANA_076_filter_replacement():
    """[ANA-02.13][AT-ANA-17.11] PM2.5 60일, 30일차 이후 평균 40% 감소: 변화점 1개가 30일차 ± 1일, 구간별 평균·분산 2행, 오탐 0"""
    s, changes = synth.level_shift_series(19, 30, 0.6)
    res = T.run(make_input({"target": {"pm25": s}}), T.parse_params({}), ctx(19))
    rows = res.tables[0]["rows"]
    assert len(rows) == 1
    assert abs(pd.Timestamp(rows[0]["time"]) - changes[0]) <= pd.Timedelta(days=1)
    assert len(res.tables[1]["rows"]) == 2 and rows[0]["changePct"] < -0.3


@pytest.mark.spec("ANA-02.13")
def test_ANA_02_13_TC_ANA_077_no_change():
    """[ANA-02.13][TC-ANA-077] 계절성만 → 변화점 0개, '뚜렷한 변화 구간 없음'"""
    s, _ = synth.level_shift_series(19, [], 0.6)
    res = T.run(make_input({"target": {"pm25": s}}), T.parse_params({}), ctx(19))
    assert res.headline == "뚜렷한 변화 구간 없음" and res.tables[0]["rows"] == []


@pytest.mark.spec("ANA-02.13")
def test_ANA_02_13_short_hourly_and_variance_model():
    """[ANA-02.13] 14일 미만은 시간 단위로 보고, MEAN_VARIANCE 비용도 실행된다"""
    s, _ = synth.level_shift_series(3, 5, 0.5, days=10)
    res = T.run(make_input({"target": {"pm25": s}}), T.parse_params({"minSegmentDays": 1}), ctx(3))
    assert res.provenance["unit"] == "hour" and len(res.tables[0]["rows"]) >= 1
    res = T.run(make_input({"target": {"pm25": s}}), T.parse_params({"costModel": "MEAN_VARIANCE", "minSegmentDays": 1}), ctx(3))
    assert res.provenance["algorithm"] == "PELT(normal)"
