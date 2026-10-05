"""평가 지표(ANA-07.02)와 드리프트 PSI(ANA-07.04)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error

from data2flow_analytics.templates.algos import decile_edges, mae, mape, psi, psi_from_edges
from data2flow_analytics.templates.builtin.anomaly_detect.template import _Event, _label_metrics
from tests.fixtures import synth


@pytest.mark.spec("ANA-07.02")
def test_ANA_07_02_TC_ANA_147_metrics_match_sklearn():
    """[ANA-07.02][AT-ANA-06.1] MAE·MAPE가 scikit-learn과 1e-9까지 일치, 실제값 0은 MAPE에서 뺀다. 라벨 없으면 정밀도·재현율 None"""
    rng = np.random.default_rng(1)
    a = rng.uniform(1, 100, 200)
    p = a + rng.normal(0, 5, 200)
    assert abs(mae(a, p) - mean_absolute_error(a, p)) < 1e-9
    assert abs(mape(a, p) - mean_absolute_percentage_error(a, p)) < 1e-9
    a0, p0 = np.append(a, 0.0), np.append(p, 3.0)
    assert abs(mape(a0, p0) - mean_absolute_percentage_error(a, p)) < 1e-9
    idx = pd.date_range("2026-10-01", periods=10, freq="1h", tz="UTC")
    events = [_Event("x", 2, 2, "SPIKE", 5, 1, 0, index=idx), _Event("x", 6, 6, "SPIKE", 5, 1, 0, index=idx)]
    assert _label_metrics(events, idx, []) == (None, None)
    labels = [{"occurredAt": idx[2].isoformat(), "verdict": "TRUE_POSITIVE"}, {"occurredAt": idx[6].isoformat(), "verdict": "FALSE_POSITIVE"},
              {"occurredAt": idx[9].isoformat(), "verdict": "TRUE_POSITIVE"}]
    assert _label_metrics(events, idx, labels) == (0.5, 0.5)


@pytest.mark.spec("ANA-07.04")
def test_ANA_07_04_TC_ANA_152_psi():
    """[ANA-07.04][AT-ANA-14.1] 학습 평균 22℃·최근 28℃ → PSI > 0.2, 같은 분포 → PSI < 0.1(10분위)"""
    train, recent = synth.shifted(23, delta=6)
    same_train, same_recent = synth.shifted(23, delta=0)
    assert psi(train, recent) > 0.2 and psi(same_train, same_recent) < 0.1
    edges = decile_edges(train)
    assert psi_from_edges(edges, recent) > 0.2 and psi_from_edges(edges, same_recent) < 0.1
    assert psi_from_edges([], recent) == 0.0 and psi(np.array([1.0]), recent) == 0.0
