"""correlation 상관 분석(ANA-02.11). 상관 행렬, 시차 상관, 산점도. 상관이 인과가 아님을 항상 표시한다(ANA-08.05)."""

from __future__ import annotations

from itertools import combinations
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from data2flow_analytics.templates.algos import median_step
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
    chart,
    table,
)

CAVEAT = "상관은 인과가 아닙니다. 같이 움직여도 한쪽이 다른 쪽의 원인이라는 뜻은 아닙니다."


class Params(TemplateParams):
    method: Literal["PEARSON", "SPEARMAN"] = Field("PEARSON", description="상관 계수. PEARSON은 직선 관계, SPEARMAN은 순위가 같이 움직이는지")
    maxLagHours: float = Field(6.0, ge=0, le=72, description="찾아볼 최대 시차(시간). 0이면 같은 시각끼리만 비교한다")


def _corr(a: np.ndarray, b: np.ndarray, method: str) -> float:
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 3:
        return float("nan")
    x, y = a[ok], b[ok]
    if method == "SPEARMAN":
        x, y = pd.Series(x).rank().to_numpy(), pd.Series(y).rank().to_numpy()
    if np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


class Correlation(Template):
    key = "correlation"
    version = "1.0.0"
    name = "상관 분석"
    kind = Kind.GENERAL
    category = Category.GENERAL
    roles = [RoleSpec("target", "series", 2, 20, label="비교할 값")]
    params_model = Params
    requirements = Requirements(min_period_days=3, min_points=72, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1m", max_period_days=366)
    fast = True
    outputs = ["heatmap", "line", "scatter", "table"]
    fixed_caveats = [CAVEAT]

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("target")
        step = median_step(frame.index)
        frame = frame.sort_index()
        grid = frame.resample(step).mean() if len(frame) else frame
        cols = list(grid.columns)
        labels = [data.label(c) for c in cols]
        matrix = [[1.0 if i == j else None for j in range(len(cols))] for i in range(len(cols))]
        lag_rows, lag_series = [], []
        max_lag = int(round(pd.Timedelta(hours=params.maxLagHours) / step)) if step > pd.Timedelta(0) else 0
        for i, j in combinations(range(len(cols)), 2):
            ctx.checkpoint()
            a, b = grid[cols[i]].to_numpy(float), grid[cols[j]].to_numpy(float)
            r0 = _corr(a, b, params.method)
            matrix[i][j] = matrix[j][i] = None if np.isnan(r0) else round(r0, 6)
            best_lag, best_r, curve = 0, r0, []
            for lag in range(-max_lag, max_lag + 1):
                # lag>0: b가 a보다 lag만큼 늦게 따라온다(corr(a_t, b_{t+lag}))
                if lag >= 0:
                    r = _corr(a[: len(a) - lag] if lag else a, b[lag:], params.method)
                else:
                    r = _corr(a[-lag:], b[: len(b) + lag], params.method)
                curve.append([round(lag * step / pd.Timedelta(minutes=1), 3), None if np.isnan(r) else round(r, 6)])
                if not np.isnan(r) and (np.isnan(best_r) or abs(r) > abs(best_r)):
                    best_lag, best_r = lag, r
            lag_minutes = best_lag * step / pd.Timedelta(minutes=1)
            lag_rows.append({"a": labels[i], "b": labels[j], "aKey": cols[i], "bKey": cols[j], "r": None if np.isnan(r0) else round(r0, 6),
                             "bestLagMinutes": round(lag_minutes, 3), "bestR": None if np.isnan(best_r) else round(best_r, 6),
                             "leader": labels[i] if best_lag > 0 else (labels[j] if best_lag < 0 else "동시")})
            lag_series.append({"key": f"{cols[i]}~{cols[j]}", "label": f"{labels[i]} → {labels[j]}", "data": curve})
        strongest = max(lag_rows, key=lambda r: abs(r["bestR"] or 0)) if lag_rows else None
        if strongest and strongest["bestR"] is not None:
            lag_txt = f", {abs(strongest['bestLagMinutes']):.0f}분 시차" if strongest["bestLagMinutes"] else ""
            headline = f"{strongest['a']}와 {strongest['b']}의 상관 {strongest['bestR']:+.2f}{lag_txt}가 가장 뚜렷합니다"
        else:
            headline = "계산할 수 있는 상관이 없습니다"
        sample = grid.dropna()
        if len(sample) > 2000:
            sample = sample.iloc[np.linspace(0, len(sample) - 1, 2000).astype(int)]
        charts = [chart("matrix", "heatmap", "상관 행렬", x_axis={"type": "category", "label": ""}, y_axis={"type": "category", "label": ""},
                        heatmap={"xLabels": labels, "yLabels": labels, "values": matrix, "unit": None}),
                  chart("lagged", "line", "시차 상관", x_axis={"type": "value", "label": "시차(분)"}, y_axis={"type": "value", "label": "상관"},
                        series=lag_series)]
        if len(cols) >= 2:
            charts.append(chart("scatter", "scatter", f"{labels[0]} 대 {labels[1]}",
                                x_axis={"type": "value", "label": labels[0], "unit": data.unit(cols[0])},
                                y_axis={"type": "value", "label": labels[1], "unit": data.unit(cols[1])},
                                series=[{"key": "points", "label": "측정값",
                                         "data": [[round(float(x), 6), round(float(y), 6)] for x, y in zip(sample[cols[0]], sample[cols[1]], strict=True)]}]))
        tables = [table("pairs", "짝별 상관", [("a", "A", "string"), ("b", "B", "string"), ("r", "상관(같은 시각)", "number"),
                                               ("bestLagMinutes", "가장 강한 시차(분)", "number"), ("bestR", "그때 상관", "number"),
                                               ("leader", "먼저 움직이는 쪽", "string")], lag_rows)]
        metrics = [Metric("strongestR", "가장 강한 상관", strongest["bestR"] if strongest else None),
                   Metric("strongestLagMinutes", "그때 시차(분)", strongest["bestLagMinutes"] if strongest else None, "min")]
        return Result(headline=headline, metrics=metrics, charts=charts, tables=tables, caveats=list(self.fixed_caveats),
                      evidence={"matrix": matrix, "labels": labels}, provenance={"algorithm": f"{params.method} 상관 + 시차 상관",
                                                                                 "maxLagHours": params.maxLagHours})


TEMPLATE = Correlation()
