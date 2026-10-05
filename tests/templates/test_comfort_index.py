"""ANA-02.01 쾌적도(BR-ANA-22)."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.templates.builtin.comfort_index.template import TEMPLATE as T
from data2flow_analytics.templates.builtin.comfort_index.template import grade_of
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


def _room_with_bad_hours(seed=11, days=7, hours=4):
    df = synth.room(seed, days=days)
    hour = df.index.hour
    bad = hour < hours
    df.loc[bad, "temp"] = 27.0
    df.loc[bad, "humidity"] = 65.0
    df.loc[bad, "co2"] = 1300.0
    return df


def _data(df, extras=None):
    return make_input({"temp": {"temp": df["temp"]}, "humidity": {"humidity": df["humidity"]}, "co2": {"co2": df["co2"]}}, extras=extras)


@pytest.mark.spec("ANA-02.01")
def test_ANA_02_01_TC_ANA_035_default_baseline():
    """[ANA-02.01][AT-ANA-16.1] 목표 환경 없음 + 27℃·65%·1,300ppm 하루 4시간: baseline=DEFAULT, 이탈 비율 4/24 ± 0.001, 기여도 합 1.0"""
    res = T.run(_data(_room_with_bad_hours()), T.parse_params({}), ctx(11))
    assert res.provenance["baseline"] == "DEFAULT"
    ratio = next(m.value for m in res.metrics if m.key == "outsideRatio")
    assert abs(ratio - 4 / 24) <= 0.001
    bars = res.charts[1]["series"][0]["data"]
    assert len(bars) == 3 and abs(sum(v for _, v in bars) - 1.0) < 1e-9
    assert any("기본 기준" in c for c in res.caveats)


@pytest.mark.spec("ANA-02.01")
def test_ANA_02_01_TC_ANA_036_target_env_overrides():
    """[ANA-02.01][TC-ANA-036] 공간 목표(22~24℃)가 있으면 그것을 쓰고 SPACE_TARGET. 등급 경계 80/60/40 ± 0.01"""
    df = synth.room(11, days=2)
    df["temp"] = 24.5
    res = T.run(_data(df, {"spaceTargets": {"temperature": {"min": 22, "max": 24}}}), T.parse_params({}), ctx(11))
    assert res.provenance["baseline"] == "SPACE_TARGET"
    assert res.provenance["targets"]["temp"] == [22, 24]
    assert next(m.value for m in res.metrics if m.key == "outsideRatio") == 1.0
    assert grade_of(80.0)[0] == "GOOD" and grade_of(79.99)[0] == "FAIR"
    assert grade_of(60.0)[0] == "FAIR" and grade_of(59.99)[0] == "POOR"
    assert grade_of(40.0)[0] == "POOR" and grade_of(39.99)[0] == "BAD"
    default = T.run(_data(df, {"spaceTargets": {"temperature": {"min": 22, "max": 24}}}), T.parse_params({"useSpaceTargets": False}), ctx(1))
    assert default.provenance["baseline"] == "DEFAULT"


@pytest.mark.spec("ANA-02.01")
def test_ANA_02_01_optional_items_and_thi():
    """[ANA-02.01] 선택 항목(TVOC·PM2.5)도 감점하고 평균 불쾌지수(THI)를 보여 준다"""
    df = synth.room(3, days=1)
    data = make_input({"temp": {"temp": df["temp"]}, "humidity": {"humidity": df["humidity"]}, "co2": {"co2": df["co2"]},
                       "pm25": {"pm25": pd.Series(45.0, index=df.index)}})
    res = T.run(data, T.parse_params({}), ctx(3))
    assert next(m.value for m in res.metrics if m.key == "score") == pytest.approx(95.0)
    assert any(m.key == "thiMean" for m in res.metrics)
