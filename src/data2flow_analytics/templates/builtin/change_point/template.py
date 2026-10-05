"""change-point 변화 구간 탐지(ANA-02.13). PELT(ruptures) + 요일 효과 제거.

14일 이상이면 하루 평균으로, 짧으면 시간 평균(시간대 효과 제거)으로 본다. 벌점은 BIC 형태(σ²·log n)에 배수를 곱한다.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
import ruptures as rpt
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
    robust_sigma,
    table,
    time_axis,
    ts_points,
    value_axis,
)


class Params(TemplateParams):
    costModel: Literal["MEAN", "MEAN_VARIANCE"] = Field("MEAN", description="찾을 변화. MEAN은 평균 수준, MEAN_VARIANCE는 평균과 흔들림 크기")
    penalty: float = Field(4.0, ge=0.5, le=20, description="벌점 배수. 크면 큰 변화만, 작으면 작은 변화까지 찾는다(오탐도 늘어남)")
    minSegmentDays: float = Field(3.0, ge=0.5, le=30, description="한 구간의 최소 길이(일). 이보다 짧은 변화는 무시한다")


class ChangePoint(Template):
    key = "change-point"
    version = "1.0.0"
    name = "변화 구간 탐지"
    kind = Kind.GENERAL
    category = Category.GENERAL
    roles = [RoleSpec("target", "series", 1, 1, label="볼 값")]
    params_model = Params
    requirements = Requirements(min_period_days=7, min_points=48, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1h", max_period_days=730)
    fast = True
    outputs = ["line", "table"]
    fixed_caveats = ["변화 시점은 데이터가 달라진 때이지 원인이 아닙니다. 그 무렵의 작업 기록과 함께 확인하세요."]

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("target")
        col = frame.columns[0]
        s = frame[col].dropna()
        unit = data.unit(col)
        span_days = (s.index[-1] - s.index[0]) / pd.Timedelta(days=1) if len(s) > 1 else 0
        local = s.index.tz_convert(ctx.timezone)
        if span_days >= 14:
            buckets = pd.Series(s.to_numpy(), index=local.normalize()).groupby(level=0)
            unit_len = pd.Timedelta(days=1)
        else:
            buckets = pd.Series(s.to_numpy(), index=local.floor("h")).groupby(level=0)
            unit_len = pd.Timedelta(hours=1)
        counts = buckets.count()
        # 처음·끝의 일부만 있는 칸은 하루 주기 때문에 평균이 치우치므로 뺀다(칸 표본 수가 중앙값의 80% 미만)
        full = counts >= 0.8 * float(counts.median())
        agg = buckets.mean()[full]
        group_key = agg.index.dayofweek if unit_len == pd.Timedelta(days=1) else agg.index.hour
        y = agg.to_numpy(dtype=float)
        # 주기 효과(요일 또는 시간대)를 수준 이동에 강한 방식으로 뺀다: 이동 중앙값 대비 차이의 같은 칸 중앙값
        width = 7 if unit_len == pd.Timedelta(days=1) else 24
        rolling = pd.Series(y).rolling(width, center=True, min_periods=max(2, width // 2)).median().to_numpy()
        effect = pd.Series(y - rolling).groupby(np.asarray(group_key)).median()
        enough_cycles = len(y) >= 3 * width
        adj = y - (effect.reindex(np.asarray(group_key)).fillna(0).to_numpy() if enough_cycles else 0.0)
        ctx.checkpoint()
        sigma = robust_sigma(np.diff(adj)) / np.sqrt(2) if len(adj) > 2 else 1.0
        sigma = sigma or 1e-9
        min_size = max(2, int(round(pd.Timedelta(days=params.minSegmentDays) / unit_len)))
        n = len(adj)
        cps: list[int] = []
        if n >= 2 * min_size:
            model = "l2" if params.costModel == "MEAN" else "normal"
            algo = rpt.Pelt(model=model, min_size=min_size, jump=1).fit(adj.reshape(-1, 1))
            pen = params.penalty * sigma ** 2 * np.log(n) if model == "l2" else params.penalty * np.log(n)
            cps = [c for c in algo.predict(pen=pen) if c < n]
            # 효과 크기가 잡음보다 작은 변화는 버린다
            bounds = [0, *cps, n]
            kept = []
            for i, c in enumerate(cps):
                left, right = adj[bounds[i]:c], adj[c:bounds[i + 2]]
                diff = abs(np.mean(right) - np.mean(left))
                se = sigma * np.sqrt(1 / len(left) + 1 / len(right))
                var_change = params.costModel == "MEAN_VARIANCE" and (
                    max(np.std(left), np.std(right)) > 2 * max(1e-9, min(np.std(left), np.std(right))))
                if diff > 4 * se or var_change:
                    kept.append(c)
            cps = kept
        bounds = [0, *cps, n]
        segments = []
        for i in range(len(bounds) - 1):
            a, b = bounds[i], bounds[i + 1]
            seg_start = agg.index[a].tz_convert("UTC")
            seg_end = (agg.index[b - 1] + unit_len).tz_convert("UTC")
            raw = s[(s.index >= seg_start) & (s.index < seg_end)]
            segments.append({"segment": i + 1, "from": seg_start, "to": seg_end, "mean": round(float(raw.mean()), 6),
                             "variance": round(float(raw.var()), 6) if len(raw) > 1 else None, "points": int(len(raw))})
        change_rows = []
        for i, c in enumerate(cps):
            at = agg.index[c].tz_convert("UTC")
            before, after = segments[i]["mean"], segments[i + 1]["mean"]
            change_rows.append({"time": at, "before": before, "after": after, "change": round(after - before, 6),
                                "changePct": round((after - before) / before, 6) if before else None})
        regions = [{"from": seg["from"], "to": seg["to"], "label": f"구간 {seg['segment']}", "severity": "INFO"} for seg in segments]
        charts = [chart("segments", "line", "변화 구간", x_axis=time_axis(), y_axis=value_axis("값", unit),
                        series=[{"key": col, "label": data.label(col), "data": ts_points(s)}], regions=regions,
                        markers=[{"x": r["time"], "y": r["after"], "label": "변화 시점", "severity": "WARN"} for r in change_rows])]
        tables = [table("changes", "변화 시점", [("time", "시각", "datetime"), ("before", "이전 평균", "number"), ("after", "이후 평균", "number"),
                                                  ("change", "변화량", "number"), ("changePct", "변화율", "number")], change_rows),
                  table("segments", "구간별 평균·분산", [("segment", "구간", "number"), ("from", "시작", "datetime"), ("to", "끝", "datetime"),
                                                       ("mean", "평균", "number"), ("variance", "분산", "number"), ("points", "건수", "number")], segments)]
        if cps:
            first = change_rows[0]
            headline = f"변화 시점 {len(cps)}곳: 첫 변화 이후 평균이 {first['changePct'] * 100:+.0f}% 달라졌습니다" if first["changePct"] is not None \
                else f"변화 시점 {len(cps)}곳"
        else:
            headline = "뚜렷한 변화 구간 없음"
        return Result(headline=headline, level="WARN" if cps else "OK",
                      metrics=[Metric("changePoints", "변화 시점 수", len(cps)), Metric("segments", "구간 수", len(segments))],
                      charts=charts, tables=tables, caveats=list(self.fixed_caveats),
                      evidence={"threshold": params.penalty, "noiseSigma": sigma, "contributors": [{"seriesKey": col, "weight": 1.0}]},
                      provenance={"algorithm": f"PELT({'l2' if params.costModel == 'MEAN' else 'normal'})",
                                  "unit": "day" if unit_len == pd.Timedelta(days=1) else "hour"})


TEMPLATE = ChangePoint()
