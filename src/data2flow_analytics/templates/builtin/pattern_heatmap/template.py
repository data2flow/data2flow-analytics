"""pattern-heatmap 시간 패턴(ANA-02.07). 요일 × 시간대 평균. 4주 미만이면 '참고용'(BR-ANA-24)."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

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

DAYS = ["월", "화", "수", "목", "금", "토", "일"]


class Params(TemplateParams):
    aggregation: Literal["MEAN", "MEDIAN", "MAX"] = Field("MEAN", description="칸 값을 내는 방법. MEAN은 평균, MEDIAN은 튀는 값에 덜 흔들리는 중앙값")


class PatternHeatmap(Template):
    key = "pattern-heatmap"
    version = "1.0.0"
    name = "시간 패턴"
    kind = Kind.GENERAL
    category = Category.SPACE_USAGE
    roles = [RoleSpec("target", "series", 1, 1, label="볼 값")]
    params_model = Params
    requirements = Requirements(min_period_days=7, min_points=168, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1m", max_period_days=366)
    fast = True
    outputs = ["heatmap", "line", "table"]
    fixed_caveats = ["평균이라 드문 사건은 묻힙니다. 드문 사건은 anomaly-detect로 찾으세요."]

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("target")
        col = frame.columns[0]
        s = frame[col].dropna()
        local = s.index.tz_convert(ctx.timezone)
        grouped = pd.Series(s.to_numpy(), index=pd.MultiIndex.from_arrays([local.dayofweek, local.hour])).groupby(level=[0, 1])
        agg = {"MEAN": grouped.mean, "MEDIAN": grouped.median, "MAX": grouped.max}[params.aggregation]()
        counts = grouped.count()
        stds = grouped.std()
        values, samples, devs = [], [], []
        for d in range(7):
            values.append([_num(agg.get((d, h))) for h in range(24)])
            samples.append([int(counts.get((d, h), 0)) for h in range(24)])
            devs.append([_num(stds.get((d, h))) for h in range(24)])
        hourly = pd.Series(s.to_numpy(), index=local.hour).groupby(level=0).mean()
        span_days = (data.period_to - data.period_from).total_seconds() / 86400
        flags = ["REFERENCE_ONLY"] if span_days < 28 else []
        unit = data.unit(col)
        flat = [(values[d][h], d, h) for d in range(7) for h in range(24) if values[d][h] is not None]
        peak = max(flat) if flat else (None, 0, 0)
        low = min(flat) if flat else (None, 0, 0)
        headline = (f"{DAYS[peak[1]]}요일 {peak[2]}시가 가장 높고, {DAYS[low[1]]}요일 {low[2]}시가 가장 낮습니다"
                    if flat else "표시할 데이터가 없습니다")
        if flags:
            headline = "참고용(4주 미만): " + headline
        charts = [
            chart("heatmap", "heatmap", "요일 × 시간대 " + {"MEAN": "평균", "MEDIAN": "중앙값", "MAX": "최대"}[params.aggregation],
                  x_axis={"type": "category", "label": "시"}, y_axis={"type": "category", "label": "요일"},
                  heatmap={"xLabels": [str(h) for h in range(24)], "yLabels": DAYS, "values": values, "samples": samples,
                           "std": devs, "unit": unit}),
            chart("hourly", "line", "시간대별 평균", x_axis={"type": "category", "label": "시"}, y_axis={"type": "value", "label": "값", "unit": unit},
                  series=[{"key": "hourly", "label": "시간대별 평균", "data": [[int(h), _num(v)] for h, v in hourly.items()]}]),
        ]
        rows = [{"day": DAYS[d], "hour": h, "value": values[d][h], "samples": samples[d][h], "std": devs[d][h]}
                for d in range(7) for h in range(24)]
        tables = [table("cells", "칸별 값", [("day", "요일", "string"), ("hour", "시", "number"), ("value", "값", "number"),
                                             ("samples", "표본 수", "number"), ("std", "편차", "number")], rows)]
        metrics = [Metric("peakValue", "가장 높은 칸", peak[0], unit), Metric("lowValue", "가장 낮은 칸", low[0], unit),
                   Metric("weeks", "주 수", round(span_days / 7, 2))]
        return Result(headline=headline, level="WARN" if flags else "OK", metrics=metrics, charts=charts, tables=tables,
                      caveats=list(self.fixed_caveats) + (["4주 미만 데이터라 참고용입니다."] if flags else []), flags=flags,
                      provenance={"algorithm": "요일×시간대 집계", "timezone": ctx.timezone, "aggregation": params.aggregation})


def _num(v) -> float | None:
    if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v):
        return None
    return float(v)


TEMPLATE = PatternHeatmap()
