"""ANA-02.09 기준 도달 시각 예측."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.templates.builtin.threshold_eta.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


def _metric(res, key):
    return next(m.value for m in res.metrics if m.key == key)


@pytest.mark.spec("ANA-02.09")
def test_ANA_02_09_TC_ANA_064_linear_rise():
    """[ANA-02.09][AT-ANA-17.7] 현재 850ppm, 분당 +15, 기준 1000: minutesLeft = 10 ± 1, UP, HIGH. 하강 추세면 '도달 예상 없음'"""
    res = T.run(make_input({"target": {"co2": synth.ramp(2, start=850, slope=15)}}), T.parse_params({"threshold": 1000}), ctx(2))
    assert abs(_metric(res, "minutesLeft") - 10) <= 1
    assert _metric(res, "confidence") == "HIGH" and res.tables[0]["rows"][0]["direction"] == "UP"
    down = T.run(make_input({"target": {"co2": synth.ramp(2, start=850, slope=-15)}}), T.parse_params({"threshold": 1000}), ctx(2))
    assert "도달 예상 없음" in down.headline and _metric(down, "minutesLeft") is None


@pytest.mark.spec("ANA-02.09")
def test_ANA_02_09_TC_ANA_065_noisy_low_confidence():
    """[ANA-02.09][TC-ANA-065] 노이즈 σ가 기울기의 3배면 LOW, 10분 미만이면 '예측 불가'(BR-ANA-23)"""
    noisy = T.run(make_input({"target": {"co2": synth.ramp(5, start=850, slope=15, noise=45)}}), T.parse_params({}), ctx(5))
    assert _metric(noisy, "confidence") == "LOW"
    short = T.run(make_input({"target": {"co2": synth.ramp(5, minutes=8)}}), T.parse_params({}), ctx(5))
    assert short.headline.startswith("예측 불가") and "UNPREDICTABLE" in short.flags


@pytest.mark.spec("ANA-02.09")
def test_ANA_02_09_reached_and_beyond_horizon_and_down_direction():
    """[ANA-02.09] 이미 기준 이상이면 도달, 지평 밖이면 알리지 않음, DOWN 방향(배터리 20%)"""
    above = T.run(make_input({"target": {"co2": synth.ramp(1, start=1100, slope=5)}}), T.parse_params({"threshold": 1000}), ctx(1))
    assert above.level == "CRITICAL"
    far = T.run(make_input({"target": {"co2": synth.ramp(1, start=500, slope=1)}}), T.parse_params({"threshold": 1000, "horizonMinutes": 60}), ctx(1))
    assert far.level == "OK" and "도달 예상 없음" in far.headline
    battery = T.run(make_input({"target": {"b": synth.ramp(1, start=30, slope=-0.5, noise=0.05)}}),
                    T.parse_params({"threshold": 20, "direction": "DOWN"}), ctx(1))
    assert abs(_metric(battery, "minutesLeft") - 20) <= 1


@pytest.mark.spec("ANA-06.01")
def test_ANA_06_01_realtime_eta_events():
    """[ANA-06.01] 실시간: 분당 +15ppm이면 도달 예상 이벤트, 남은 분이 같으면 다시 내지 않는다"""
    hist = synth.ramp(2, start=700, slope=15, minutes=20)
    params = T.parse_params({"threshold": 1000})
    state = T.realtime_init(hist, params, None, 0)
    t = hist.index[-1]
    events = []
    for k in range(1, 6):
        ev = T.infer(state, t + pd.Timedelta(minutes=k), 700 + 15 * k, params)
        if ev:
            events.append(ev)
    assert events and events[-1]["type"] == "ETA" and abs(events[-1]["minutesLeft"] - 15) <= 1.5
    assert T.infer(state, t + pd.Timedelta(minutes=5, seconds=1), 775, params) is None
