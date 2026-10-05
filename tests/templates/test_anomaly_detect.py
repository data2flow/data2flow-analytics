"""ANA-02.02 이상 탐지(통계)·ANA-02.03 ML 방식·ANA-05.03 설명 가능성."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.templates.builtin.anomaly_detect.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


def _run(series, seed=42, **params):
    return T.run(make_input({"target": {"co2": series}}), T.parse_params(params), ctx(seed))


def _hits(rows, labels, tol=pd.Timedelta(minutes=5)):
    found = [(pd.Timestamp(r["time"]), pd.Timestamp(r["end"])) for r in rows]
    return sum(any(a - tol <= lab["start"] <= b + tol for a, b in found) for lab in labels)


@pytest.mark.spec("ANA-02.02")
def test_ANA_02_02_TC_ANA_039_injected_spikes():
    """[ANA-02.02][AT-ANA-17.1] CO2 14일(1분) 급등 3건 +800ppm, 민감도 3: 모두 SPIKE로 찾고 오탐 ≤ 1, 근거 포함"""
    lab = synth.series(42, days=14, step="1min").spike(n=3, delta=800)
    rows = _run(lab.series, sensitivity=3).tables[0]["rows"]
    assert _hits(rows, lab.labels) == 3
    assert len(rows) - 3 <= 1
    injected = [r for r in rows if any(abs(pd.Timestamp(r["time"]) - lb["start"]) <= pd.Timedelta(minutes=5) for lb in lab.labels)]
    assert len(injected) == 3
    for r in injected:
        assert r["kind"] == "SPIKE" and r["value"] is not None and r["score"] > r["threshold"]
        assert r["baseline"] is not None and r["contributors"][0]["weight"] == 1.0


@pytest.mark.spec("ANA-02.02")
def test_ANA_02_02_TC_ANA_040_kinds():
    """[ANA-02.02][TC-ANA-040] 급락은 DROP, 6시간 +200 수준 이동은 LEVEL_SHIFT 1건(시작 ±5분), 분산 3배 구간은 VARIANCE"""
    lab = synth.series(7, days=14, step="1min").spike(1, -500).level_shift(6, 200).variance(6, 3.0)
    rows = _run(lab.series, seed=7).tables[0]["rows"]
    kinds = {r["kind"] for r in rows}
    assert {"DROP", "LEVEL_SHIFT", "VARIANCE"} <= kinds
    shift = next(lb for lb in lab.labels if lb["kind"] == "LEVEL_SHIFT")
    level_rows = [r for r in rows if r["kind"] == "LEVEL_SHIFT"]
    assert len(level_rows) == 1
    assert abs(pd.Timestamp(level_rows[0]["time"]) - shift["start"]) <= pd.Timedelta(minutes=5)


@pytest.mark.spec("ANA-02.02")
def test_ANA_02_02_TC_ANA_041_sensitivity_monotonic():
    """[ANA-02.02][AT-ANA-17.2] 탐지 수 s1 ≤ s3 ≤ s5 그리고 s5 > s1, 민감도 0·6은 ANALYSIS_PARAMS_INVALID"""
    lab = synth.series(42, days=14, step="1min").spike(3, 800)
    counts = [len(_run(lab.series, sensitivity=s).tables[0]["rows"]) for s in (1, 3, 5)]
    assert counts[0] <= counts[1] <= counts[2] and counts[2] > counts[0]
    for bad in (0, 6):
        with pytest.raises(BusinessError) as exc:
            T.parse_params({"sensitivity": bad})
        assert exc.value.code.name == "ANALYSIS_PARAMS_INVALID"
        assert exc.value.args_["field"] == "sensitivity"


@pytest.mark.spec("ANA-02.02")
def test_ANA_02_02_TC_ANA_042_seasonality_not_flagged():
    """[ANA-02.02][TC-ANA-042] 이상 없이 강한 일(3σ)·주(1.5σ) 계절성만 있는 30일: 오탐률 ≤ 0.1%(포인트 기준)"""
    s = synth.seasonal(3, days=30, daily=3.0, weekly=1.5)
    rows = _run(s, seed=3).tables[0]["rows"]
    flagged_points = sum(1 + (pd.Timestamp(r["end"]) - pd.Timestamp(r["time"])) // pd.Timedelta(minutes=1) for r in rows)
    assert flagged_points / len(s) <= 0.001


@pytest.mark.spec("ANA-05.03")
def test_ANA_05_03_TC_ANA_121_evidence():
    """[ANA-05.03][AT-ANA-03.1] 이상마다 기준선·임계값·점수·기여 시계열(weight 합 1.0)이 있고 score > threshold"""
    lab = synth.series(42, days=14, step="5min").spike(3, 800)
    res = _run(lab.series)
    assert res.evidence["threshold"] == 4.5 and res.evidence["baseline"]
    for r in res.tables[0]["rows"]:
        assert r["score"] > r["threshold"]
        assert abs(sum(c["weight"] for c in r["contributors"]) - 1.0) < 1e-9
    assert any(c["markers"] for c in res.charts if c["id"] == "raw")


@pytest.mark.spec("ANA-02.03")
def test_ANA_02_03_TC_ANA_045_multivariate_combo():
    """[ANA-02.03][AT-ANA-17.3] 온도·습도·CO2 21일, 하루 동안 각자 정상 범위인 조합 이상: 구간 1개, IoU ≥ 0.5, 기여 1위가 주입 항목"""
    df, _ = synth.multivariate(5, days=21)
    df, truth = synth.combo_anomaly(df, day=15)
    data = make_input({"target": {c: df[c] for c in df}})
    res = T.run(data, T.parse_params({"method": "ISOLATION_FOREST"}), ctx(5))
    rows = res.tables[0]["rows"]
    assert len(rows) == 1
    a, b = pd.Timestamp(rows[0]["time"]), pd.Timestamp(rows[0]["end"])
    inter = max(pd.Timedelta(0), min(b, truth["end"]) - max(a, truth["start"]))
    union = max(b, truth["end"]) - min(a, truth["start"])
    assert inter / union >= 0.5
    assert rows[0]["contributors"][0]["seriesKey"] in truth["injected"]


@pytest.mark.spec("ANA-02.03")
def test_ANA_02_03_TC_ANA_046_requires_two_metrics_14days():
    """[ANA-02.03][TC-ANA-046] 측정 항목 1개 → ANALYSIS_BINDING_INVALID, ML 방식 최소 기간 14일"""
    params = T.parse_params({"method": "ISOLATION_FOREST"})
    with pytest.raises(BusinessError) as exc:
        T.validate_inputs([{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "1", "metricKey": "co2"}]}], params)
    assert exc.value.code.name == "ANALYSIS_BINDING_INVALID"
    assert T.requirements_for(params).min_period_days == 14
    assert T.requirements_for(T.parse_params({})).min_period_days == 7


@pytest.mark.spec("ANA-06.01")
def test_ANA_06_01_realtime_infer_spike_and_cooldown():
    """[ANA-06.01] 실시간: 평소보다 5σ 높은 값은 SPIKE, 쿨다운 안의 같은 크기 이상은 다시 내지 않는다"""
    s = synth.series(1, days=7, step="1min").series
    params = T.parse_params({"cooldownMinutes": 10})
    state = T.realtime_init(s, params, None, 0)
    at = s.index[-1] + pd.Timedelta(minutes=1)
    base = state["profile"].value_at(at) + state["offset"]
    assert T.infer(state, at, base, params) is None
    ev = T.infer(state, at, base + 8 * state["sigma"], params)
    assert ev["kind"] == "SPIKE" and ev["score"] > 4.5 and ev["evidence"]["threshold"] == 4.5
    assert T.infer(state, at + pd.Timedelta(minutes=1), base + 8 * state["sigma"], params) is None
    assert T.infer(state, at + pd.Timedelta(minutes=2), base - 20 * state["sigma"], params)["kind"] in ("DROP", "SPIKE")
    with pytest.raises(BusinessError):
        T.realtime_init(s.iloc[:5], params, None, 0)
