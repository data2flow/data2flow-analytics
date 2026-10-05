"""ANA-09.01 개입 효과 검증(BR-ANA-19)."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.templates.builtin.intervention_impact.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


def _run(effect, seed=29, **kw):
    df, at = synth.intervention(seed, effect=effect, **kw)
    data = make_input({"target": {"pm25": df["target"]}, "covariates": {"out": df["outdoor"]}})
    return T.run(data, T.parse_params({"interventionAt": at.isoformat()}), ctx(seed))


def _m(res, key):
    return next(m.value for m in res.metrics if m.key == key)


@pytest.mark.spec("ANA-09.01")
def test_ANA_09_01_TC_ANA_166_effect_detected():
    """[ANA-09.01][AT-ANA-12.1] 개입 전후 14일, 주입 효과 −20%(노이즈 10%): 효과 −20% ± 4%p, 95% 구간이 0을 포함하지 않음, 매칭 비교"""
    res = _run(-0.2)
    assert abs(_m(res, "effect") + 0.2) <= 0.04
    assert _m(res, "ciHigh") < 0 and _m(res, "verdict") == "DECREASE"
    assert res.evidence["matchedOn"] == ["요일 유형", "시간대", "외기 온도"]


@pytest.mark.spec("ANA-09.01")
def test_ANA_09_01_TC_ANA_167_unclear():
    """[ANA-09.01][TC-ANA-167] 주입 효과 0 → 신뢰구간이 0 포함 → '효과 불명확'(BR-ANA-19)"""
    res = _run(0.0)
    assert _m(res, "ciLow") <= 0 <= _m(res, "ciHigh") and _m(res, "verdict") == "UNCLEAR"
    assert res.headline.startswith("효과 불명확")


@pytest.mark.spec("ANA-09.01")
def test_ANA_09_01_TC_ANA_168_insufficient_after():
    """[ANA-09.01][AT-ANA-12.2] 개입 후 3일 → '개입 후 최소 7일 데이터가 필요합니다', 개입 시점 없음 → INTERVENTION_POINT_REQUIRED"""
    df, at = synth.intervention(29, effect=-0.2, days_before=14, days_after=3)
    issues = T.extra_issues(df.index[0].to_pydatetime(), (df.index[-1] + pd.Timedelta(hours=1)).to_pydatetime(),
                            T.parse_params({"interventionAt": at.isoformat()}))
    assert issues[0]["severity"] == "FAIL" and issues[0]["message"].startswith("개입 후 최소 7일 데이터가 필요합니다")
    with pytest.raises(BusinessError) as exc:
        T.run(make_input({"target": {"pm25": df["target"]}}), T.parse_params({"interventionAt": at.isoformat()}), ctx(1))
    assert exc.value.code.name == "ANALYSIS_INSUFFICIENT_DATA"
    with pytest.raises(BusinessError) as exc:
        T.validate_inputs([], T.parse_params({}))
    assert exc.value.code.name == "INTERVENTION_POINT_REQUIRED" and exc.value.code.status == 400
