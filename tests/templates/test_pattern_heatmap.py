"""ANA-02.07 시간 패턴."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from data2flow_analytics.templates.builtin.pattern_heatmap.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-02.07")
def test_ANA_02_07_TC_ANA_057_matrix_matches_reference():
    """[ANA-02.07][AT-ANA-17.4] 소음 5주: 7×24 행렬이 pandas groupby(dayofweek, hour).mean()과 1e-9 이내, 칸 표본 수 = 5×60"""
    s = synth.occupancy_pattern(8, weeks=5)
    res = T.run(make_input({"target": {"noise": s}}), T.parse_params({}), ctx(8))
    local = s.index.tz_convert("Asia/Seoul")
    ref = pd.Series(s.to_numpy(), index=pd.MultiIndex.from_arrays([local.dayofweek, local.hour])).groupby(level=[0, 1]).mean()
    hm = res.charts[0]["heatmap"]
    for (d, h), v in ref.items():
        assert abs(hm["values"][d][h] - v) <= 1e-9
    assert {hm["samples"][d][h] for d in range(7) for h in range(24)} == {300}
    assert res.flags == []
    assert np.argmax([hm["values"][0][h] for h in range(24)]) in range(9, 18)


@pytest.mark.spec("ANA-02.07")
def test_ANA_02_07_TC_ANA_058_reference_only_under_4weeks():
    """[ANA-02.07][AT-ANA-17.5] 3주 → 결과는 만들고 flags=REFERENCE_ONLY, '참고용(4주 미만)'(BR-ANA-24)"""
    s = synth.occupancy_pattern(8, weeks=3, step="10min")
    res = T.run(make_input({"target": {"noise": s}}), T.parse_params({"aggregation": "MEDIAN"}), ctx(8))
    assert res.flags == ["REFERENCE_ONLY"] and res.headline.startswith("참고용(4주 미만)")
    assert T.run(make_input({"target": {"noise": s}}), T.parse_params({"aggregation": "MAX"}), ctx(8)).charts[0]["type"] == "heatmap"
