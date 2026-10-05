"""occupancy-estimate 재실·활용률 추정(ANA-02.10, BR-ANA-25).

적외선 활동, CO2 증가율·수준, 소음, 조도 가운데 2개 이상으로 규칙 기반 재실 여부를 추정한다. 인원 수는 어디에도 내지 않는다
(재실 여부 0/1과 활용률 %만, NFR-13.02). 실제 재실 라벨이 쌓이면 분류 모델로 바꿀 수 있게 판정 근거를 남긴다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from data2flow_analytics.common.errors import BusinessError, ErrorCode
from data2flow_analytics.templates.algos import group_runs, median_step, steps_for
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

SIGNALS = ("co2", "activity", "noise", "illuminance")
CAVEAT = "재실 여부와 활용률은 센서로 본 추정치입니다. 예약 기록 같은 실제 재실 기록이 있으면 함께 확인하세요."
DAYS = ["월", "화", "수", "목", "금", "토", "일"]


class Params(TemplateParams):
    operatingStart: str = Field("09:00", pattern=r"^\d{2}:\d{2}$", description="운영 시작 시각(조직 시간대). 활용률의 분모가 되는 운영 시간의 시작")
    operatingEnd: str = Field("18:00", pattern=r"^\d{2}:\d{2}$", description="운영 끝 시각(조직 시간대)")
    weekdaysOnly: bool = Field(True, description="true면 평일만 운영 시간으로 센다")
    minOccupiedMinutes: int = Field(5, ge=1, le=120, description="이보다 짧은 재실 판정은 버린다(분)")


class OccupancyEstimate(Template):
    key = "occupancy-estimate"
    version = "1.0.0"
    name = "재실·활용률 추정"
    kind = Kind.DOMAIN
    category = Category.SPACE_USAGE
    roles = [RoleSpec("co2", "series", 0, 1, "co2", required=False, label="CO2"),
             RoleSpec("activity", "series", 0, 1, "activity", required=False, label="적외선 활동"),
             RoleSpec("noise", "series", 0, 1, "noise", required=False, label="소음"),
             RoleSpec("illuminance", "series", 0, 1, "illuminance", required=False, label="조도")]
    params_model = Params
    requirements = Requirements(min_period_days=7, min_points=500, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1m", max_period_days=180)
    fast = True
    outputs = ["timeline", "heatmap", "table"]
    fixed_caveats = [CAVEAT]

    def validate_inputs(self, bindings: list[dict], params: Params) -> None:
        bound = {b.get("role") for b in bindings if b.get("sources")}
        if len(bound & set(SIGNALS)) < 2:
            raise BusinessError(ErrorCode.ANALYSIS_BINDING_INVALID, {"role": "co2·activity·noise·illuminance", "min": 2, "max": 4,
                                                                     "semantic": "재실 신호"})

    def runnable_with(self, semantics: set[str], has_numeric: bool):
        have = [s for s in SIGNALS if s in semantics]
        if len(have) >= 2:
            return True, []
        return False, [{"role": s, "semantic": s} for s in SIGNALS if s not in semantics][: 2 - len(have)]

    def estimate(self, data: InputData) -> tuple[pd.Series, dict[str, pd.Series]]:
        frames = {r: data.role(r).iloc[:, 0].dropna().sort_index() for r in SIGNALS if data.has_role(r)}
        base = frames.get("activity", next(iter(frames.values())))
        index = base.index
        step = median_step(index)
        win = steps_for(pd.Timedelta(minutes=10), step, minimum=1)
        evidence: dict[str, pd.Series] = {}
        weights: dict[str, float] = {}
        if "activity" in frames:
            act = frames["activity"].reindex(index).fillna(0)
            win_act = steps_for(pd.Timedelta(minutes=5), step, minimum=1)
            evidence["activity"] = ((act > 0).astype(float).rolling(win_act, center=True, min_periods=1).sum() >= 2).astype(float)
            weights["activity"] = 2.0
        if "co2" in frames:
            co2 = frames["co2"].reindex(index, method="nearest", tolerance=pd.Timedelta(minutes=15)).interpolate(limit_direction="both")
            slope = (co2 - co2.shift(win)) / (win * step / pd.Timedelta(minutes=1))
            level = co2 > float(np.nanpercentile(co2, 5)) + 250
            evidence["co2"] = ((slope.fillna(0) > 3.0) | level).astype(float)
            weights["co2"] = 1.0 if "activity" in frames else 2.0
        if "noise" in frames:
            noise = frames["noise"].reindex(index, method="nearest", tolerance=pd.Timedelta(minutes=15))
            evidence["noise"] = (noise > float(np.nanpercentile(noise, 10)) + 8).astype(float)
            weights["noise"] = 1.0
        if "illuminance" in frames:
            lux = frames["illuminance"].reindex(index, method="nearest", tolerance=pd.Timedelta(minutes=15))
            evidence["illuminance"] = (lux > float(np.nanpercentile(lux, 10)) + 100).astype(float)
            weights["illuminance"] = 1.0
        total = sum(weights.values())
        score = sum(evidence[k] * w for k, w in weights.items()) / total
        occ = (score >= 0.5).to_numpy()
        # 짧은 끊김은 메우고 짧은 재실은 버린다
        gap = steps_for(pd.Timedelta(minutes=5), step, minimum=1)
        filled = np.zeros_like(occ)
        for a, b in group_runs(occ, max_gap=gap):
            filled[a:b + 1] = True
        return pd.Series(filled.astype(int), index=index), evidence

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        occ, _ = self.estimate(data)
        step = median_step(occ.index)
        min_pts = steps_for(pd.Timedelta(minutes=params.minOccupiedMinutes), step, minimum=1)
        arr = occ.to_numpy().astype(bool)
        cleaned = np.zeros_like(arr)
        for a, b in group_runs(arr, max_gap=1):
            if b - a + 1 >= min_pts:
                cleaned[a:b + 1] = True
        occ = pd.Series(cleaned.astype(int), index=occ.index)
        local = occ.index.tz_convert(ctx.timezone)
        minutes = local.hour * 60 + local.minute
        sh, sm = map(int, params.operatingStart.split(":"))
        eh, em = map(int, params.operatingEnd.split(":"))
        operating = (minutes >= sh * 60 + sm) & (minutes < eh * 60 + em)
        if params.weekdaysOnly:
            operating &= local.dayofweek < 5
        op = occ[np.asarray(operating)]
        utilization = float(op.mean()) if len(op) else None
        grid = pd.Series(occ.to_numpy(), index=pd.MultiIndex.from_arrays([local.dayofweek, local.hour])).groupby(level=[0, 1]).mean()
        heat = [[None if pd.isna(grid.get((d, h))) else round(float(grid.get((d, h))) * 100, 3) for h in range(24)] for d in range(7)]
        items = []
        for a, b in group_runs(cleaned, max_gap=1):
            items.append({"from": occ.index[a], "to": occ.index[b] + step, "label": "사용 중", "state": "OCCUPIED"})
        spaces = sorted({str(i.space_id) for i in data.series.values() if i.space_id is not None}) or ["대상"]
        ranking = [{"rank": 1, "space": spaces[0] if len(spaces) == 1 else ",".join(spaces),
                    "utilizationPercent": None if utilization is None else round(utilization * 100, 3),
                    "occupiedHours": round(float(occ.sum()) * step / pd.Timedelta(hours=1), 3)}]
        headline = (f"운영 시간 활용률 약 {utilization * 100:.0f}%(추정)" if utilization is not None else "운영 시간 데이터가 없습니다")
        charts = [chart("timeline", "timeline", "재실 타임라인(추정)", timeline={"items": items[-500:]}),
                  chart("utilization", "heatmap", "요일 × 시간대 활용률(%)", x_axis={"type": "category", "label": "시"},
                        y_axis={"type": "category", "label": "요일"},
                        heatmap={"xLabels": [str(h) for h in range(24)], "yLabels": DAYS, "values": heat, "unit": "%"})]
        tables = [table("ranking", "공간별 활용률 순위(추정)", [("rank", "순위", "number"), ("space", "공간", "string"),
                                                           ("utilizationPercent", "활용률(%)", "number"), ("occupiedHours", "사용 시간", "number")],
                        ranking)]
        metrics = [Metric("utilizationPercent", "활용률(추정)", None if utilization is None else round(utilization * 100, 4), "%"),
                   Metric("occupiedHours", "사용 시간(추정)", ranking[0]["occupiedHours"], "h")]
        return Result(headline=headline, metrics=metrics, charts=charts, tables=tables, caveats=list(self.fixed_caveats),
                      flags=["ESTIMATE"], evidence={"rule": "활동·CO2 증가율·소음·조도 가중 판정(0.5 이상이면 재실)"},
                      provenance={"algorithm": "규칙 기반 재실 판정", "operatingHours": f"{params.operatingStart}-{params.operatingEnd}",
                                  "weekdaysOnly": params.weekdaysOnly})


TEMPLATE = OccupancyEstimate()
