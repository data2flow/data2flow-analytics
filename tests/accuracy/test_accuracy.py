"""템플릿별 합성 데이터 정확도(M6 완료 확인: 주입 이상 전부 탐지·오탐 1개 이하, MAPE 10% 이하).

- PR 기준(고정 시드 1~3): 템플릿 13종 모두 아래 기준을 통과해야 한다.
- 야간 기준(`pytest -m accuracy`, 시드 30~50): TC-ANA-043·048·063·078. 결과를 accuracy-report.json으로 남기고 직전보다 2%p 넘게
  떨어지면 실패한다(ANA test-plan "합성 데이터와 정확도 기준").
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import accuracy_score, adjusted_rand_score, f1_score

from data2flow_analytics.templates.algos import mape
from data2flow_analytics.templates.registry import build_registry
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input

REG = build_registry()
PR_SEEDS = (1, 2, 3)
REPORT = Path(os.getenv("ACCURACY_REPORT", "accuracy-report.json"))


def T(key):
    return REG.get(key).template


def _run(key, roles, seed, **params):
    t = T(key)
    return t.run(make_input(roles), t.parse_params(params), ctx(seed))


def _match(rows, labels, tol):
    """주입 라벨이 탐지 구간과 겹치면 찾은 것. 어느 라벨과도 겹치지 않는 탐지는 오탐."""
    found = [(pd.Timestamp(r["time"]), pd.Timestamp(r["end"])) for r in rows]
    hit = sum(any(a - tol <= lab["end"] and lab["start"] <= b + tol for a, b in found) for lab in labels)
    fp = sum(not any(a - tol <= lab["end"] and lab["start"] <= b + tol for lab in labels) for a, b in found)
    return hit, fp


# ---------------------------------------------------------------- PR 기준(13종)
@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_anomaly_detect_statistical(seed):
    """[ANA-02.02] 급등·급락·수준 이동 혼합 20건(30일, 10분): 모두 찾고 오탐 ≤ 1"""
    lab = synth.mixed_anomalies(seed, days=30, step="10min", n=20)
    rows = _run("anomaly-detect", {"target": {"x": lab.series}}, seed).tables[0]["rows"]
    hit, fp = _match(rows, lab.labels, pd.Timedelta(minutes=20))
    assert hit == len(lab.labels) and fp <= 1, (hit, fp)


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_anomaly_detect_isolation_forest(seed):
    """[ANA-02.03] 다변량 조합 이상 1건: 찾고(IoU ≥ 0.5) 오탐 ≤ 1"""
    df, _ = synth.multivariate(seed, days=21)
    df, truth = synth.combo_anomaly(df, day=15)
    rows = _run("anomaly-detect", {"target": {c: df[c] for c in df}}, seed, method="ISOLATION_FOREST").tables[0]["rows"]
    ious = []
    for r in rows:
        a, b = pd.Timestamp(r["time"]), pd.Timestamp(r["end"])
        inter = max(pd.Timedelta(0), min(b, truth["end"]) - max(a, truth["start"]))
        ious.append(inter / (max(b, truth["end"]) - min(a, truth["start"])))
    assert max(ious) >= 0.5 and sum(i == 0 for i in ious) <= 1


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_forecast(seed):
    """[ANA-02.08] 48시간 예측 MAPE ≤ 10%"""
    s = synth.forecastable(seed, days=21)
    res = _run("forecast", {"target": {"x": s.iloc[:-48]}}, seed, horizonHours=48)
    assert mape(s.iloc[-48:].to_numpy(), np.array([r["forecast"] for r in res.tables[0]["rows"]])) <= 0.10


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_threshold_eta(seed):
    """[ANA-02.09] 도달까지 남은 분 오차 ≤ 1분"""
    slope = 10 + 5 * seed
    res = _run("threshold-eta", {"target": {"x": synth.ramp(seed, start=850, slope=slope)}}, seed, threshold=1000)
    assert abs(next(m.value for m in res.metrics if m.key == "minutesLeft") - 150 / slope) <= 1


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_change_point(seed):
    """[ANA-02.13] 주입 변화점 1~3개를 ±1일 안에서 모두 찾고 오탐 ≤ 1"""
    days = [15, 30, 45][: seed]
    s, changes = synth.level_shift_series(seed, days, 0.6)
    found = [pd.Timestamp(r["time"]) for r in _run("change-point", {"target": {"x": s}}, seed).tables[0]["rows"]]
    assert all(any(abs(f - c) <= pd.Timedelta(days=1) for f in found) for c in changes)
    assert sum(not any(abs(f - c) <= pd.Timedelta(days=1) for c in changes) for f in found) <= 1


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_daily_profile_cluster(seed):
    """[ANA-02.12] 평일·주말·휴일 3종: ARI ≥ 0.9"""
    s, labels = synth.daily_profiles(seed, days=60)
    assert adjusted_rand_score(labels, _run("daily-profile-cluster", {"target": {"x": s}}, seed).evidence["labels"]) >= 0.9


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_pattern_heatmap(seed):
    """[ANA-02.07] 요일×시간 평균이 기준 계산과 1e-9 이내"""
    s = synth.occupancy_pattern(seed, weeks=4, step="5min")
    hm = _run("pattern-heatmap", {"target": {"x": s}}, seed).charts[0]["heatmap"]["values"]
    local = s.index.tz_convert("Asia/Seoul")
    ref = pd.Series(s.to_numpy(), index=pd.MultiIndex.from_arrays([local.dayofweek, local.hour])).groupby(level=[0, 1]).mean()
    assert max(abs(hm[d][h] - v) for (d, h), v in ref.items()) <= 1e-9


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_correlation(seed):
    """[ANA-02.11] 시차 ±10분, 상관 ±0.05"""
    df = synth.lagged(seed, lag="2h", r=0.8)
    pair = _run("correlation", {"target": {"a": df["outdoor"], "b": df["indoor"]}}, seed).tables[0]["rows"][0]
    assert abs(pair["bestLagMinutes"] - 120) <= 10 and abs(pair["bestR"] - 0.8) <= 0.05


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_comfort_index(seed):
    """[ANA-02.01] 목표 이탈 시간 비율 ± 0.001"""
    df = synth.room(seed, days=7)
    bad = df.index.hour < 3 + seed
    df.loc[bad, ["temp", "humidity", "co2"]] = [27.0, 65.0, 1300.0]
    res = _run("comfort-index", {"temp": {"t": df["temp"]}, "humidity": {"h": df["humidity"]}, "co2": {"c": df["co2"]}}, seed)
    assert abs(next(m.value for m in res.metrics if m.key == "outsideRatio") - (3 + seed) / 24) <= 0.001


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_sensor_health(seed):
    """[ANA-02.04·02.05] 주입 고장(값 멈춤·누락·드리프트) 3대를 모두 '의심'으로, 정상 기기 오탐 ≤ 1"""
    f = synth.fleet(seed, devices=6)
    f = synth.offset(synth.drop_rate(synth.stuck(f, 1, 6), 3, 0.3, seed), 5, 0.7)
    rows = _run("sensor-health", {"devices": f}, seed, peerCompare=True).tables[0]["rows"]
    suspect = {r["label"] for r in rows if r["status"] == "SUSPECT"}
    assert {"dev1", "dev3", "dev5"} <= suspect and len(suspect - {"dev1", "dev3", "dev5"}) <= 1


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_battery_life(seed):
    """[ANA-02.06] 남은 일수 ± 5%"""
    slope = -0.1 * (seed + 1)
    res = _run("battery-life", {"battery": {"b": synth.battery(seed, days=60, slope=slope)}}, seed)
    row = res.tables[0]["rows"][0]
    truth = (80 + slope * 60 - 20) / -slope
    assert abs(row["daysLeft"] - truth) <= truth * 0.05


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_occupancy_estimate(seed):
    """[ANA-02.10] 재실 정확도 ≥ 0.85, F1 ≥ 0.8, 활용률 오차 ≤ 3%p"""
    df, labels = synth.occupancy(seed, days=14)
    t = T("occupancy-estimate")
    data = make_input({"co2": {"c": df["co2"]}, "activity": {"a": df["activity"]}})
    occ, _ = t.estimate(data)
    assert accuracy_score(labels, occ) >= 0.85 and f1_score(labels, occ) >= 0.8
    local = labels.index.tz_convert("Asia/Seoul")
    m = local.hour * 60 + local.minute
    truth = labels[(m >= 540) & (m < 1080) & (local.dayofweek < 5)].mean() * 100
    util = next(x.value for x in t.run(data, t.parse_params({}), ctx(seed)).metrics if x.key == "utilizationPercent")
    assert abs(util - truth) <= 3


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_intervention_impact(seed):
    """[ANA-09.01] 효과 −20% ± 4%p, 95% 구간이 0을 포함하지 않음"""
    df, at = synth.intervention(seed, effect=-0.2)
    res = _run("intervention-impact", {"target": {"x": df["target"]}, "covariates": {"o": df["outdoor"]}}, seed, interventionAt=at.isoformat())
    m = {x.key: x.value for x in res.metrics}
    assert abs(m["effect"] + 0.2) <= 0.04 and m["ciHigh"] < 0


@pytest.mark.parametrize("seed", PR_SEEDS)
def test_accuracy_space_benchmark(seed):
    """[ANA-09.02] ㎡당 에너지 순위가 손 계산과 같다"""
    rooms = {sid: synth.room(seed + i, days=7) for i, sid in enumerate(("1", "2", "3"))}
    kwh = {"1": 3.0, "2": 1.0, "3": 2.0}
    area = {"1": 50.0, "2": 10.0, "3": 40.0}
    roles: dict = {"temp": {}, "humidity": {}, "co2": {}, "energy": {}}
    for sid, df in rooms.items():
        roles["temp"][f"t{sid}"], roles["humidity"][f"h{sid}"], roles["co2"][f"c{sid}"] = df["temp"], df["humidity"], df["co2"]
        roles["energy"][f"e{sid}"] = pd.Series(kwh[sid], index=df.index)
    data = make_input(roles)
    for key, info in list(data.series.items()):
        data.series[key] = type(info)(**{**info.__dict__, "space_id": int(key[1:])})
    t = T("space-benchmark")
    rows = {r["spaceId"]: r for r in t.run(data, t.parse_params({"spaces": [{"spaceId": s, "areaM2": a} for s, a in area.items()]}),
                                           ctx(seed)).tables[0]["rows"]}
    per = {s: kwh[s] * len(rooms[s]) / area[s] for s in area}
    expected = {s: i + 1 for i, s in enumerate(sorted(per, key=per.get))}
    assert {s: rows[s]["energyPerM2Rank"] for s in rows} == expected


# ---------------------------------------------------------------- 야간 기준(-m accuracy)
def _report(name: str, metrics: dict) -> None:
    data = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    previous = data.get(name, {})
    for key, value in metrics.items():
        if key in previous and isinstance(value, float) and key.startswith(("recall", "precision", "iou", "coverage")):
            assert value >= previous[key] - 0.02, f"{name}.{key} {value:.3f} < 직전 {previous[key]:.3f} - 2%p"
    data[name] = metrics
    REPORT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.accuracy
def test_nightly_TC_ANA_043_anomaly_accuracy():
    """[ANA-02.02][TC-ANA-043] 시드 50개 × 이상 20개: 평균 재현율 ≥ 0.9, 정밀도 ≥ 0.8, 시드별 최저 재현율 ≥ 0.75"""
    recalls, precisions = [], []
    for seed in range(50):
        lab = synth.mixed_anomalies(seed, days=30, step="10min", n=20)
        rows = _run("anomaly-detect", {"target": {"x": lab.series}}, seed).tables[0]["rows"]
        hit, fp = _match(rows, lab.labels, pd.Timedelta(minutes=20))
        recalls.append(hit / len(lab.labels))
        precisions.append((len(rows) - fp) / len(rows) if rows else 1.0)
    _report("anomaly-detect", {"recall": float(np.mean(recalls)), "precision": float(np.mean(precisions)), "recallMin": float(min(recalls))})
    assert np.mean(recalls) >= 0.9 and np.mean(precisions) >= 0.8 and min(recalls) >= 0.75


@pytest.mark.accuracy
def test_nightly_TC_ANA_048_anomaly_ml_accuracy():
    """[ANA-02.03][TC-ANA-048] 시드 30개 다변량: 구간 재현율 ≥ 0.9, IoU 평균 ≥ 0.6"""
    found, ious = 0, []
    for seed in range(30):
        df, _ = synth.multivariate(seed, days=21)
        df, truth = synth.combo_anomaly(df, day=15)
        rows = _run("anomaly-detect", {"target": {c: df[c] for c in df}}, seed, method="ISOLATION_FOREST").tables[0]["rows"]
        best = 0.0
        for r in rows:
            a, b = pd.Timestamp(r["time"]), pd.Timestamp(r["end"])
            inter = max(pd.Timedelta(0), min(b, truth["end"]) - max(a, truth["start"]))
            best = max(best, inter / (max(b, truth["end"]) - min(a, truth["start"])))
        found += best > 0
        ious.append(best)
    _report("anomaly-detect-ml", {"recall": found / 30, "iou": float(np.mean(ious))})
    assert found / 30 >= 0.9 and np.mean(ious) >= 0.6


@pytest.mark.accuracy
def test_nightly_TC_ANA_063_forecast_accuracy():
    """[ANA-02.08][TC-ANA-063] 시드 30개 × 계절 패턴 3종: MAPE 중앙값 ≤ 8%, 95% 구간 포함률 평균 0.93 ± 0.03"""
    mapes, cover = [], []
    for pattern in range(3):
        for seed in range(30):
            s = synth.forecastable(seed, days=21, pattern=pattern)
            rows = _run("forecast", {"target": {"x": s.iloc[:-48]}}, seed, horizonHours=48).tables[0]["rows"]
            actual = s.iloc[-48:].to_numpy()
            mapes.append(mape(actual, np.array([r["forecast"] for r in rows])))
            lo, hi = np.array([r["lower95"] for r in rows]), np.array([r["upper95"] for r in rows])
            cover.append(float(np.mean((actual >= lo) & (actual <= hi))))
    _report("forecast", {"mapeMedian": float(np.median(mapes)), "mapeMax": float(max(mapes)), "coverage95": float(np.mean(cover))})
    assert np.median(mapes) <= 0.08 and abs(np.mean(cover) - 0.93) <= 0.03


@pytest.mark.accuracy
def test_nightly_TC_ANA_078_change_point_accuracy():
    """[ANA-02.13][TC-ANA-078] 시드 30개 × 변화점 1~3개: 재현율 ≥ 0.9(±1일), 오탐 평균 ≤ 0.2개"""
    recalls, fps = [], []
    for seed in range(30):
        rng = np.random.default_rng(seed)
        days = sorted(rng.choice(np.arange(8, 53, 8), size=int(rng.integers(1, 4)), replace=False).tolist())
        s, changes = synth.level_shift_series(seed, days, 0.6)
        found = [pd.Timestamp(r["time"]) for r in _run("change-point", {"target": {"x": s}}, seed).tables[0]["rows"]]
        recalls.append(sum(any(abs(f - c) <= pd.Timedelta(days=1) for f in found) for c in changes) / len(changes))
        fps.append(sum(not any(abs(f - c) <= pd.Timedelta(days=1) for c in changes) for f in found))
    _report("change-point", {"recall": float(np.mean(recalls)), "falsePositivesMean": float(np.mean(fps))})
    assert np.mean(recalls) >= 0.9 and np.mean(fps) <= 0.2
