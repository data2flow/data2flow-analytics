"""daily-profile-cluster 하루 패턴 군집(ANA-02.12). KMeans(시드 고정) + 실루엣으로 군집 수 자동 결정.

군집 번호는 처음 나타난 날짜 순으로 다시 매겨, 같은 시드로 다시 실행하면 번호까지 같다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

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
    maxClusters: int = Field(6, ge=2, le=10, description="군집 수의 최대값. 실루엣 점수가 가장 높은 수를 2부터 이 값까지에서 고른다")
    normalize: bool = Field(False, description="true면 하루 평균을 빼고 모양만 비교한다. false면 크기와 모양을 함께 본다")


class DailyProfileCluster(Template):
    key = "daily-profile-cluster"
    version = "1.0.0"
    name = "하루 패턴 군집"
    kind = Kind.GENERAL
    category = Category.SPACE_USAGE
    roles = [RoleSpec("target", "series", 1, 1, label="볼 값")]
    params_model = Params
    requirements = Requirements(min_period_days=30, min_points=720, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1h", max_period_days=730)
    fast = True
    outputs = ["line", "calendar", "table"]
    fixed_caveats = ["군집은 비슷한 날끼리 묶은 것이고 이름(평일형·행사형)은 해석입니다. 대표 곡선으로 확인하세요."]

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("target")
        col = frame.columns[0]
        s = frame[col].dropna()
        local = s.index.tz_convert(ctx.timezone)
        hourly = pd.Series(s.to_numpy(), index=local.floor("h")).groupby(level=0).mean()
        table_ = pd.DataFrame({"day": hourly.index.normalize(), "hour": hourly.index.hour, "v": hourly.to_numpy()})
        pivot = table_.pivot_table(index="day", columns="hour", values="v", aggfunc="mean").reindex(columns=range(24))
        pivot = pivot[pivot.notna().sum(axis=1) >= 20].interpolate(axis=1, limit_direction="both")
        days = list(pivot.index)
        x = pivot.to_numpy(dtype=float)
        if params.normalize:
            x = x - x.mean(axis=1, keepdims=True)
        scale = np.std(x) or 1.0
        xs = x / scale
        cands = []
        upper = min(params.maxClusters, max(2, len(days) // 4))
        for k in range(2, upper + 1):
            ctx.checkpoint()
            km = KMeans(n_clusters=k, n_init=10, random_state=ctx.seed).fit(xs)
            if len(set(km.labels_)) < 2:
                continue
            cands.append((float(silhouette_score(xs, km.labels_)), k, km.labels_))
        # 실루엣이 가장 높은 값에서 0.15 안쪽(그리고 0.5 이상)인 후보 중 가장 잘게 나눈 것을 고른다.
        # 작은 군집(휴일 등)이 큰 군집에 묻히는 것을 막는다
        best = None
        if cands:
            top = max(c[0] for c in cands)
            ok = [c for c in cands if c[0] >= max(0.5, top - 0.15)] or [max(cands, key=lambda c: c[0])]
            best = max(ok, key=lambda c: c[1])
        if best is None:
            labels = np.zeros(len(days), dtype=int)
            sil, k = None, 1
        else:
            sil, k, raw = best
            order: dict[int, int] = {}
            for lab in raw:
                order.setdefault(int(lab), len(order))
            labels = np.array([order[int(lab)] for lab in raw])
        clusters = []
        for c in range(k):
            members = [d for d, lab in zip(days, labels, strict=True) if lab == c]
            centroid = pivot.to_numpy(dtype=float)[labels == c].mean(axis=0)
            weekday_share = float(np.mean([d.dayofweek < 5 for d in members])) if members else 0.0
            clusters.append({"cluster": c + 1, "size": len(members), "weekdayShare": round(weekday_share, 4),
                             "mean": round(float(np.mean(centroid)), 6), "peakHour": int(np.argmax(centroid)),
                             "examples": [d.date().isoformat() for d in members[:5]], "centroid": np.round(centroid, 6).tolist()})
        series = [{"key": f"c{c['cluster']}", "label": f"군집 {c['cluster']}({c['size']}일)",
                   "data": [[h, v] for h, v in enumerate(c["centroid"])]} for c in clusters]
        charts = [chart("profiles", "line", "군집별 대표 곡선", x_axis={"type": "category", "label": "시"},
                        y_axis={"type": "value", "label": "값", "unit": data.unit(col)}, series=series),
                  chart("calendar", "calendar", "날짜별 군집", calendar={"days": [[d.date().isoformat(), int(lab) + 1, int(lab) + 1]
                                                                             for d, lab in zip(days, labels, strict=True)]})]
        tables = [table("clusters", "군집 요약", [("cluster", "군집", "number"), ("size", "날 수", "number"),
                                                  ("weekdayShare", "평일 비율", "number"), ("mean", "평균", "number"),
                                                  ("peakHour", "최고 시각", "number")],
                        [{k_: v for k_, v in c.items() if k_ not in ("centroid", "examples")} for c in clusters]),
                  table("days", "날짜별 군집", [("date", "날짜", "date"), ("weekday", "요일", "string"), ("cluster", "군집", "number")],
                        [{"date": d.date().isoformat(), "weekday": DAYS[d.dayofweek], "cluster": int(lab) + 1}
                         for d, lab in zip(days, labels, strict=True)])]
        headline = f"{len(days)}일을 하루 패턴 {k}가지로 묶었습니다"
        metrics = [Metric("clusters", "군집 수", k), Metric("silhouette", "실루엣 점수", round(sil, 6) if sil is not None else None),
                   Metric("days", "날 수", len(days))]
        return Result(headline=headline, metrics=metrics, charts=charts, tables=tables, caveats=list(self.fixed_caveats),
                      evidence={"silhouette": sil, "labels": [int(lab) + 1 for lab in labels]},
                      provenance={"algorithm": "KMeans + 실루엣", "timezone": ctx.timezone})


TEMPLATE = DailyProfileCluster()
