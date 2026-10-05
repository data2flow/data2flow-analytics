"""forecast 예측(ANA-02.08). 기본 Holt-Winters, 설명 변수가 있으면 SARIMAX, 그래디언트 부스팅(GRADIENT_BOOSTING) 방식도 제공.

마지막 20% 구간에서 여러 시작점으로 검증 오차(MAE·MAPE)를 내고, 같은 오차로 80%·95% 예측 구간을 만든다(분할 등각 방식).
"""

from __future__ import annotations

import warnings
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from data2flow_analytics.templates.algos import is_constant, mae, mape, median_step
from data2flow_analytics.templates.base import (
    Category,
    InputData,
    Kind,
    Metric,
    Requirements,
    Result,
    RoleSpec,
    RunContext,
    Template,
    TemplateParams,
    TrainedModel,
    chart,
    table,
    time_axis,
    ts_points,
    value_axis,
)


class Params(TemplateParams):
    method: Literal["HOLT_WINTERS", "SARIMAX", "GRADIENT_BOOSTING"] = Field(
        "HOLT_WINTERS", description="예측 방식. 설명 변수(covariates)가 있으면 SARIMAX가 그것을 쓴다")
    horizonHours: int = Field(48, ge=1, le=336, description="예측할 길이(시간). 길수록 신뢰구간이 넓어진다")
    seasonality: Literal["AUTO", "DAILY", "WEEKLY", "NONE"] = Field(
        "AUTO", description="계절 주기. AUTO는 검증 오차가 작은 쪽(일 또는 주)을 고른다")


def _fill(s: pd.Series) -> pd.Series:
    s = s.copy()
    s = s.interpolate(limit=6, limit_direction="both")
    return s.ffill().bfill()


class Forecast(Template):
    key = "forecast"
    version = "1.0.0"
    name = "예측"
    kind = Kind.GENERAL
    category = Category.PREDICTION
    roles = [RoleSpec("target", "series", 1, 1, label="예측할 값"),
             RoleSpec("covariates", "series", 0, 5, required=False, label="설명 변수")]
    params_model = Params
    requirements = Requirements(min_period_days=14, min_points=336, missing_warn=0.10, missing_fail=0.20,
                                recommended_resolution="1h", max_period_days=180)
    fast = True
    trainable = True
    predictive = True
    outputs = ["line", "band", "table"]
    fixed_caveats = ["예측값은 추정입니다. 방학, 공휴일, 장비 교체처럼 패턴이 바뀐 직후에는 정확도가 떨어집니다."]

    # ---- 모델 ----
    def _periods(self, y: pd.Series, params: Params) -> list[int]:
        step = median_step(y.index)
        daily = max(1, int(round(pd.Timedelta(days=1) / step)))
        weekly = max(1, int(round(pd.Timedelta(days=7) / step)))
        n = len(y)
        if params.seasonality == "NONE" or daily < 2:
            return [0]
        if params.seasonality == "DAILY":
            return [daily]
        if params.seasonality == "WEEKLY":
            return [weekly if n * 0.8 >= 2 * weekly else daily]
        cands = [daily]
        if n * 0.8 >= 2 * weekly and weekly <= 400:
            cands.append(weekly)
        return cands

    def _fit_predict(self, method: str, y: pd.Series, h: int, period: int, cov: pd.DataFrame | None, seed: int) -> np.ndarray:
        values = y.to_numpy(dtype=float)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if method == "SARIMAX" and cov is not None and cov.shape[1]:
                from statsmodels.tsa.statespace.sarimax import SARIMAX

                season = period if 0 < period <= 24 else 0
                exog = cov.loc[y.index].to_numpy(dtype=float)
                future = _future_exog(cov.loc[y.index], h, median_step(y.index))
                model = SARIMAX(values, exog=exog, order=(1, 0, 1),
                                seasonal_order=(1, 0, 1, season) if season else (0, 0, 0, 0), trend="c")
                fit = model.fit(disp=False, maxiter=30)
                return np.asarray(fit.forecast(h, exog=future), dtype=float)
            if method == "GRADIENT_BOOSTING":
                return _gbm_forecast(y, h, period, seed)
            from statsmodels.tsa.holtwinters import ExponentialSmoothing

            if period and len(values) >= 2 * period:
                model = ExponentialSmoothing(values, trend=None, seasonal="add", seasonal_periods=period,
                                             initialization_method="estimated")
            else:
                model = ExponentialSmoothing(values, trend="add", damped_trend=True, initialization_method="estimated")
            fit = model.fit(optimized=True)
            return np.asarray(fit.forecast(h), dtype=float)

    def _validate(self, method: str, y: pd.Series, h: int, period: int, cov, seed: int):
        """마지막 20% 구간에서 여러 시작점으로 h걸음 예측 오차를 모은다."""
        n = len(y)
        hold = max(h, int(n * 0.2))
        start = n - hold
        n_origins = 4 if method == "HOLT_WINTERS" else 1
        gap = max(1, (hold - h) // max(1, n_origins - 1))
        origins = sorted({start + k * gap for k in range(n_origins) if start + k * gap + 1 <= n - 1})
        actual_all, pred_all, steps_all = [], [], []
        for o in origins:
            hh = min(h, n - o)
            if hh <= 0 or o < max(10, 2 * period if period else 10):
                continue
            pred = self._fit_predict(method, y.iloc[:o], hh, period, cov, seed)
            actual_all.append(y.iloc[o:o + hh].to_numpy(dtype=float))
            pred_all.append(pred[:hh])
            steps_all.append(np.arange(1, hh + 1))
        if not actual_all:
            return None
        return np.concatenate(actual_all), np.concatenate(pred_all), np.concatenate(steps_all)

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("target")
        col = frame.columns[0]
        y = _fill(frame[col].dropna().asfreq(median_step(frame[col].dropna().index)))
        unit = data.unit(col)
        if is_constant(y.to_numpy()):
            return _unpredictable(data, col, y, "변화 없음", unit, self.fixed_caveats)
        cov = data.role("covariates")
        cov = cov.reindex(y.index).interpolate(limit_direction="both") if cov.shape[1] else None
        step = median_step(y.index)
        h = max(1, int(round(pd.Timedelta(hours=params.horizonHours) / step)))
        method = params.method if not (params.method == "SARIMAX" and cov is None) else "HOLT_WINTERS"
        model = ctx.model if isinstance(ctx.model, dict) and ctx.model.get("method") == method else None
        if model is None:
            model = self._select(method, y, h, cov, params, ctx)
        ctx.checkpoint()
        period = model["period"]
        pred = self._fit_predict(method, y, h, period, cov, ctx.seed)
        q80, q95, relative = model["q80"], model["q95"], model["relative"]
        scale = np.abs(pred) if relative else np.ones_like(pred)
        lower80, upper80 = pred - q80 * scale, pred + q80 * scale
        lower95, upper95 = pred - q95 * scale, pred + q95 * scale
        future_idx = pd.date_range(y.index[-1] + step, periods=h, freq=step)
        history = y.iloc[-min(len(y), 14 * max(1, int(pd.Timedelta(days=1) / step))):]
        fc = pd.Series(pred, index=future_idx)
        charts = [chart("forecast", "band", "실측과 예측", x_axis=time_axis(), y_axis=value_axis("값", unit), series=[
            {"key": "actual", "label": "실측", "data": ts_points(history), "style": "line"},
            {"key": "forecast", "label": "예측(추정)", "data": ts_points(fc), "style": "dashed"},
        ], bands=[
            {"key": "pi95", "label": "95% 구간", "level": 0.95, "x": [p[0] for p in ts_points(fc)],
             "lower": np.round(lower95, 6).tolist(), "upper": np.round(upper95, 6).tolist()},
            {"key": "pi80", "label": "80% 구간", "level": 0.8, "x": [p[0] for p in ts_points(fc)],
             "lower": np.round(lower80, 6).tolist(), "upper": np.round(upper80, 6).tolist()},
        ])]
        if model.get("validation"):
            v = model["validation"]
            charts.append(chart("validation", "line", "검증 구간 예측과 실측", x_axis={"type": "value", "label": "예측 걸음"},
                                y_axis=value_axis("값", unit), series=[
                                    {"key": "actual", "label": "실측", "data": [[i, a] for i, a in enumerate(v["actual"])]},
                                    {"key": "predicted", "label": "검증 예측", "data": [[i, p] for i, p in enumerate(v["predicted"])]}]))
        rows = [{"time": t, "forecast": round(float(p), 6), "lower80": round(float(a), 6), "upper80": round(float(b), 6),
                 "lower95": round(float(c), 6), "upper95": round(float(d), 6)}
                for t, p, a, b, c, d in zip(future_idx, pred, lower80, upper80, lower95, upper95, strict=True)]
        tables = [table("forecast", "예측값", [("time", "시각", "datetime"), ("forecast", "예측(추정)", "number"),
                                               ("lower80", "80% 하한", "number"), ("upper80", "80% 상한", "number"),
                                               ("lower95", "95% 하한", "number"), ("upper95", "95% 상한", "number")], rows)]
        val_mape = model["mape"]
        level = "WARN" if (val_mape is not None and val_mape >= 0.3) else "OK"
        headline = (f"앞으로 {params.horizonHours}시간 예측(추정): 평균 {np.mean(pred):.1f}{unit or ''}, "
                    f"최고 {np.max(pred):.1f}{unit or ''} 예상")
        caveats = list(self.fixed_caveats)
        if level == "WARN":
            caveats.append("검증 오차(MAPE)가 30% 이상이라 참고용으로만 보세요.")
        metrics = [Metric("mae", "검증 MAE", round(model["mae"], 6) if model["mae"] is not None else None, unit),
                   Metric("mape", "검증 MAPE", round(val_mape, 6) if val_mape is not None else None, "ratio", level),
                   Metric("forecastMean", "예측 평균(추정)", round(float(np.mean(pred)), 6), unit),
                   Metric("forecastMax", "예측 최고(추정)", round(float(np.max(pred)), 6), unit)]
        return Result(headline=headline, level=level, metrics=metrics, charts=charts, tables=tables, caveats=caveats,
                      flags=["ESTIMATE"], evidence={"validation": {"mae": model["mae"], "mape": val_mape, "points": model["points"]}},
                      provenance={"algorithm": {"HOLT_WINTERS": "Holt-Winters(가법 계절)", "SARIMAX": "SARIMAX",
                                                "GRADIENT_BOOSTING": "그래디언트 부스팅(재귀 예측)"}[method], "method": method,
                                  "seasonalPeriod": period, "horizonSteps": h})

    def _select(self, method: str, y: pd.Series, h: int, cov, params: Params, ctx: RunContext) -> dict:
        cands = []
        for period in self._periods(y, params):
            ctx.checkpoint()
            val = self._validate(method, y, h, period, cov, ctx.seed)
            if val is not None:
                cands.append((mape(val[0], val[1]), period, val[0], val[1]))
        if not cands:
            return {"method": method, "period": self._periods(y, params)[0], "mae": None, "mape": None, "q80": 0.0, "q95": 0.0,
                    "relative": False, "points": 0, "validation": None}
        # 주 주기를 쓸 수 있으면 우선한다. 검증 구간에 주말이 없을 수 있어서, 일 주기가 15% 넘게 좋을 때만 일 주기를 고른다
        cands.sort(key=lambda c: -c[1])
        chosen = cands[0]
        for cand in cands[1:]:
            if not np.isnan(cand[0]) and (np.isnan(chosen[0]) or cand[0] < 0.85 * chosen[0]):
                chosen = cand
        m, period, actual, pred = chosen
        relative = bool(np.all(y.to_numpy() > 0))
        err = np.abs(actual - pred) / (np.abs(pred) if relative else 1.0)
        n = len(err)
        q80 = float(np.quantile(err, min(1.0, 0.8 * (n + 1) / n)))
        q95 = float(np.quantile(err, min(1.0, 0.95 * (n + 1) / n)))
        # 모형 기반 폭(계절 칸 평균의 추정 오차 포함)과 비교해 큰 쪽을 쓴다
        sigma, k = _slot_sigma(y, period, relative)
        if sigma is not None:
            inflate = float(np.sqrt(1 + 1 / max(1.0, k)))
            q80 = max(q80, 1.2816 * sigma * inflate)
            q95 = max(q95, 1.96 * sigma * inflate)
        return {"method": method, "period": period, "mae": mae(actual, pred), "mape": m, "q80": q80, "q95": q95,
                "relative": relative, "points": int(n),
                "validation": {"actual": np.round(actual, 6).tolist(), "predicted": np.round(pred, 6).tolist()}}

    def evaluate_recent(self, data: InputData, params: Params) -> float | None:
        """최근 데이터의 예측 오차(MAPE). 학습 때보다 1.5배 나빠지면 재학습한다(ANA-07.03, TC-ANA-151)."""
        return _evaluate_recent(self, data, params)

    def fit(self, data: InputData, params: Params, ctx: RunContext) -> TrainedModel | None:
        frame = data.role("target")
        col = frame.columns[0]
        s = frame[col].dropna()
        y = _fill(s.asfreq(median_step(s.index)))
        cov = data.role("covariates")
        cov = cov.reindex(y.index).interpolate(limit_direction="both") if cov.shape[1] else None
        h = max(1, int(round(pd.Timedelta(hours=params.horizonHours) / median_step(y.index))))
        method = params.method if not (params.method == "SARIMAX" and cov is None) else "HOLT_WINTERS"
        model = self._select(method, y, h, cov, params, ctx)
        model.pop("validation", None)
        metrics = {"mae": model["mae"], "mape": model["mape"], "validationPoints": model["points"]}
        return TrainedModel(payload=model, metrics=metrics, params=params.model_dump())


def _evaluate_recent(template: Forecast, data: InputData, params: Params) -> float | None:
    frame = data.role("target")
    if not frame.shape[1]:
        return None
    s = frame.iloc[:, 0].dropna()
    if len(s) < 24:
        return None
    y = _fill(s.asfreq(median_step(s.index)))
    h = max(1, min(48, int(len(y) * 0.3)))
    period = max(1, int(round(pd.Timedelta(days=1) / median_step(y.index))))
    pred = template._fit_predict("HOLT_WINTERS", y.iloc[:-h], h, period if len(y) - h >= 2 * period else 0, None, 0)
    return mape(y.iloc[-h:].to_numpy(dtype=float), pred)


def _slot_sigma(y: pd.Series, period: int, relative: bool) -> tuple[float | None, float]:
    """계절 칸(같은 주기 위치) 평균에서 벗어난 정도의 로버스트 σ와 칸당 평균 표본 수."""
    if not period or len(y) < 2 * period:
        return None, 0.0
    pos = np.arange(len(y)) % period
    values = y.to_numpy(dtype=float)
    means = pd.Series(values).groupby(pos).transform("mean").to_numpy()
    counts = pd.Series(values).groupby(pos).transform("count").to_numpy()
    resid = (values - means) / (np.abs(means) if relative else 1.0)
    k = float(np.mean(counts))
    if k <= 1:
        return None, k
    sigma = float(1.4826 * np.median(np.abs(resid - np.median(resid)))) * float(np.sqrt(k / (k - 1)))
    return sigma, k


def _future_exog(cov: pd.DataFrame, h: int, step: pd.Timedelta) -> np.ndarray:
    """미래 설명 변수: 과거 같은 요일·시간대 평균(모르는 미래 값을 계절 평균으로 대신한다)."""
    idx = pd.date_range(cov.index[-1] + step, periods=h, freq=step)
    keys = cov.index.dayofweek * 24 + cov.index.hour
    prof = cov.groupby(keys).mean()
    fkeys = idx.dayofweek * 24 + idx.hour
    out = prof.reindex(fkeys).to_numpy(dtype=float)
    fill = cov.mean().to_numpy(dtype=float)
    out = np.where(np.isnan(out), fill, out)
    return out


def _gbm_forecast(y: pd.Series, h: int, period: int, seed: int) -> np.ndarray:
    """히스토그램 그래디언트 부스팅(scikit-learn, LightGBM과 같은 방식) 재귀 예측."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    lags = sorted({1, 2, 3, *(p for p in (period, 2 * period) if p)})
    values = list(y.to_numpy(dtype=float))
    rows, target = [], []
    for i in range(max(lags), len(values)):
        rows.append([values[i - lag] for lag in lags] + [i % period if period else 0])
        target.append(values[i])
    model = HistGradientBoostingRegressor(max_iter=100, learning_rate=0.05, max_leaf_nodes=15, random_state=seed)
    model.fit(np.asarray(rows), np.asarray(target))
    out = []
    for _ in range(h):
        i = len(values)
        feat = [values[i - lag] for lag in lags] + [i % period if period else 0]
        pred = float(model.predict(np.asarray([feat]))[0])
        values.append(pred)
        out.append(pred)
    return np.asarray(out)


def _unpredictable(data: InputData, col: str, y: pd.Series, reason: str, unit, caveats) -> Result:
    return Result(headline=f"예측 불가: {reason}", level="WARN", metrics=[Metric("predictable", "예측 가능", False)],
                  charts=[chart("actual", "line", "실측", x_axis=time_axis(), y_axis=value_axis("값", unit),
                                series=[{"key": col, "label": data.label(col), "data": ts_points(y)}])],
                  tables=[], caveats=list(caveats), flags=["ESTIMATE", "UNPREDICTABLE"],
                  evidence={"reason": reason}, provenance={"algorithm": "없음(예측 불가)", "unpredictableReason": reason})


TEMPLATE = Forecast()
