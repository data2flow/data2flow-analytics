"""ENE-02.01 환기율(ACH) 추정 — TC-ENE-033~035(AT-ENE-06.1, BR-ENE-09)"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from data2flow_analytics.energy import ach

T0 = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)


def curve(minutes: int = 120, start: float = 780.0, rate: float = 2.0, sigma: float = 0.0, seed: int = 7):
    rng = np.random.default_rng(seed)
    times = [T0 + timedelta(minutes=m) for m in range(minutes + 1)]
    values = [420 + start * np.exp(-rate * m / 60) + (rng.normal(0, sigma) if sigma else 0.0) for m in range(minutes + 1)]
    return times, values


def test_tc_ene_033_clean_decay_gives_ach_2():
    times, values = curve()
    r = ach.estimate(times, values)
    assert r.estimate is not None
    assert r.estimate.ach == pytest.approx(2.0, abs=0.2)
    assert r.estimate.ci_low <= 2.0 <= r.estimate.ci_high
    assert r.estimate.r2 >= 0.99
    assert r.estimate.decay_from == T0


def test_tc_ene_033_noise_sigma_5_stays_within_tolerance():
    times, values = curve(sigma=5.0, seed=7)
    r = ach.estimate(times, values)
    assert r.estimate is not None
    assert r.estimate.ach == pytest.approx(2.0, abs=0.2)
    assert r.estimate.ci_low < r.estimate.ach < r.estimate.ci_high


def _flat_after(times, values, minute: int):
    """minute 뒤로는 값이 그대로(재실이 다시 시작돼 감쇠가 멈춤)"""
    return times, [v if i <= minute else values[minute] + (i - minute) * 2 for i, v in enumerate(values)]


def _step(times, values, minute: int, jump: float):
    return times, [v + (jump if i >= minute else 0.0) for i, v in enumerate(values)]


@pytest.mark.parametrize(
    ("name", "make", "reason"),
    [
        ("진폭 150ppm", lambda: curve(start=150.0), ach.LOW_AMPLITUDE),
        ("단조 감소 25분", lambda: _flat_after(*curve(), minute=25), ach.TOO_SHORT),
        ("20분째 +30ppm 튐으로 30분 미만으로 나뉨", lambda: _step(*curve(minutes=45, rate=0.5), minute=20, jump=30.0), ach.TOO_SHORT),
        ("잡음 σ=80", lambda: curve(sigma=80.0, seed=7), ach.LOW_FIT),
    ],
)
def test_tc_ene_034_rejected_windows(name, make, reason):
    times, values = make()
    r = ach.estimate(times, values)
    assert r.estimate is None, name
    assert r.reason == reason, name


def test_tc_ene_035_ventilation_is_ach_times_volume():
    volume = ach.volume_m3(50.0, 3.0)
    assert volume == 150.0
    assert ach.ventilation_m3h(2.0, volume) == 300.0
    assert ach.volume_m3(50.0, None) is None
    assert ach.ventilation_m3h(2.0, None) is None


def test_segments_split_on_sustained_rise_and_stop_at_floor():
    times, values = _step(*curve(minutes=90), minute=40, jump=60.0)
    segs = ach.segments(times, values)
    assert len(segs) == 2
    assert segs[0][0] == 0
    assert segs[0][1] < 40 <= segs[1][0] + 2
    # 꼬리(C − C_out < 50ppm)는 구간에 넣지 않는다
    clean_t, clean_v = curve(minutes=120)
    (a, b), = ach.segments(clean_t, clean_v)
    assert clean_v[b] - 420 >= 50 - 5
