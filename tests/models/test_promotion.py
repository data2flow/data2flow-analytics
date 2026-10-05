"""모델 비교(BR-ANA-12)와 피드백의 학습 반영(ANA-07.05)."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.templates.builtin.anomaly_detect.template import TEMPLATE as ANOMALY
from data2flow_analytics.templates.builtin.anomaly_detect.template import _exclude_false_positive_points
from data2flow_analytics.templates.builtin.forecast.template import TEMPLATE as FORECAST
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-07.03")
def test_ANA_07_03_TC_ANA_149_compare_models():
    """[ANA-07.03][AT-ANA-06.1] 예측: MAE 1.1 vs 활성 0.8 → 나쁨, 같거나 좋으면 적용. 이상 탐지: 정밀도로 비교, 라벨 없으면 적용"""
    assert FORECAST.compare_models({"mae": 1.1}, {"mae": 0.8}) is False
    assert FORECAST.compare_models({"mae": 0.8}, {"mae": 0.8}) is True
    assert FORECAST.compare_models({"mae": None}, {"mae": 0.8}) is True
    assert ANOMALY.compare_models({"precision": 0.5}, {"precision": 0.9}) is False
    assert ANOMALY.compare_models({"precision": None}, {"precision": 0.9}) is True


@pytest.mark.spec("ANA-07.05")
def test_ANA_07_05_TC_ANA_156_feedback_in_training():
    """[ANA-07.05][AT-ANA-06.1] 오탐 지점은 학습에서 정상 데이터로 남고, 맞음 지점은 학습에서 빠지며, 표시 전체가 정밀도·재현율 라벨이 된다"""
    df, _ = synth.multivariate(5, days=21)
    df, truth = synth.combo_anomaly(df, day=15)
    frame = pd.DataFrame({c: df[c] for c in df})
    t_true, t_fp = truth["start"] + pd.Timedelta(hours=2), df.index[100]
    labels = [{"occurredAt": t_true.isoformat(), "verdict": "TRUE_POSITIVE", "seriesKey": "x"},
              {"occurredAt": t_fp.isoformat(), "verdict": "FALSE_POSITIVE", "seriesKey": "x"}]
    cleaned = _exclude_false_positive_points(frame, labels, mark_normal=True)
    assert cleaned.loc[t_true].isna().all() and cleaned.loc[t_fp].notna().all()
    trained = ANOMALY.fit(make_input({"target": {c: df[c] for c in df}}), ANOMALY.parse_params({"method": "ISOLATION_FOREST"}),
                          ctx(5, labels=labels))
    assert trained.metrics["precision"] == 1.0 and trained.metrics["recall"] == 1.0 and trained.metrics["anomalies"] == 1
    reused = ANOMALY.run(make_input({"target": {c: df[c] for c in df}}), ANOMALY.parse_params({"method": "ISOLATION_FOREST"}),
                         ctx(5, model=trained.payload))
    assert len(reused.tables[0]["rows"]) == 1
