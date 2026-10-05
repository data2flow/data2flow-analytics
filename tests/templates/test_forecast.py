"""ANA-02.08 예측."""

from __future__ import annotations

import numpy as np
import pytest

from data2flow_analytics.templates.algos import mae, mape
from data2flow_analytics.templates.builtin.forecast.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


def _forecast(seed, pattern=0, method="HOLT_WINTERS"):
    s = synth.forecastable(seed, days=21, pattern=pattern)
    train, test = s.iloc[:-48], s.iloc[-48:]
    res = T.run(make_input({"target": {"co2": train}}), T.parse_params({"method": method, "horizonHours": 48}), ctx(seed))
    rows = res.tables[0]["rows"]
    return res, test.to_numpy(), rows


@pytest.mark.spec("ANA-02.08")
def test_ANA_02_08_TC_ANA_060_seasonal_mape():
    """[ANA-02.08][AT-ANA-17.6] CO2 1h 21일(일+주 주기, 노이즈 5%) 48시간 예측: MAPE ≤ 10%, MAE ≤ 40ppm, 95% 구간 0.88~0.99·80% 0.72~0.88(시드 3개 합산)"""
    cov95, cov80 = [], []
    for seed in (13, 14, 15):
        res, actual, rows = _forecast(seed)
        pred = np.array([r["forecast"] for r in rows])
        assert mape(actual, pred) <= 0.10
        assert mae(actual, pred) <= 40
        lo95, hi95 = np.array([r["lower95"] for r in rows]), np.array([r["upper95"] for r in rows])
        lo80, hi80 = np.array([r["lower80"] for r in rows]), np.array([r["upper80"] for r in rows])
        cov95.extend(((actual >= lo95) & (actual <= hi95)).tolist())
        cov80.extend(((actual >= lo80) & (actual <= hi80)).tolist())
        keys = {m.key for m in res.metrics}
        assert {"mae", "mape"} <= keys and "ESTIMATE" in res.flags
    assert 0.88 <= np.mean(cov95) <= 0.99
    assert 0.72 <= np.mean(cov80) <= 0.88


@pytest.mark.spec("ANA-02.08")
def test_ANA_02_08_TC_ANA_061_unpredictable():
    """[ANA-02.08][TC-ANA-061] 14일 같은 값 → '예측 불가'(사유 '변화 없음'), 최소 기간 14일(BR-ANA-23)"""
    s = synth.forecastable(1, days=14) * 0 + 500
    res = T.run(make_input({"target": {"co2": s}}), T.parse_params({}), ctx(1))
    assert res.headline == "예측 불가: 변화 없음" and "UNPREDICTABLE" in res.flags
    assert T.requirements.min_period_days == 14


@pytest.mark.spec("ANA-02.08")
def test_ANA_02_08_other_methods_and_covariates():
    """[ANA-02.08] SARIMAX(설명 변수)·GRADIENT_BOOSTING도 예측과 구간을 낸다. 설명 변수 없이 SARIMAX면 HOLT_WINTERS"""
    s = synth.forecastable(3, days=15)
    cov = synth.forecastable(4, days=15) / 50
    res = T.run(make_input({"target": {"co2": s}, "covariates": {"out": cov}}), T.parse_params({"method": "SARIMAX", "horizonHours": 12}), ctx(3))
    assert res.provenance["method"] == "SARIMAX" and len(res.tables[0]["rows"]) == 12
    res = T.run(make_input({"target": {"co2": s}}), T.parse_params({"method": "SARIMAX", "horizonHours": 6}), ctx(3))
    assert res.provenance["method"] == "HOLT_WINTERS"
    res = T.run(make_input({"target": {"co2": s}}), T.parse_params({"method": "GRADIENT_BOOSTING", "horizonHours": 6}), ctx(3))
    assert res.provenance["method"] == "GRADIENT_BOOSTING" and len(res.tables[0]["rows"]) == 6
    fitted = T.fit(make_input({"target": {"co2": s}}), T.parse_params({"horizonHours": 24}), ctx(3))
    assert fitted.metrics["mape"] is not None and fitted.payload["period"] in (24, 168)
    assert T.evaluate_recent(make_input({"target": {"co2": s.iloc[-7 * 24:]}}), T.parse_params({})) < 0.2
