"""intervention-impact 개입 효과 검증(ANA-09.01, BR-ANA-19).

개입 시점 전후를 요일 유형·시간대(외기 온도가 있으면 온도 구간까지) 같은 칸끼리 맞춰 비교하고, 날 단위 부트스트랩(시드 고정)으로
효과 크기의 95% 신뢰구간을 낸다. 구간이 0을 포함하면 '효과 불명확'이다.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
from pydantic import Field

from data2flow_analytics.common.errors import BusinessError, ErrorCode
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
    time_axis,
    ts_points,
    value_axis,
)

CAVEAT = "전후 비교는 같은 조건끼리 맞췄지만 다른 변화(재실 인원, 계절)가 섞일 수 있습니다. 효과는 추정이며 원인을 단정하지 않습니다."


class Params(TemplateParams):
    interventionAt: datetime | None = Field(None, description="개입 시점(ISO-8601 UTC). 작업 지시 완료·플로우 배포 이력에서 고른다. 없으면 실행할 수 없다")
    minDaysEachSide: int = Field(7, ge=3, le=60, description="개입 전과 후에 각각 필요한 최소 일수")
    tempBinWidth: float = Field(3.0, gt=0, le=20, description="외기 온도로 맞출 때 온도 구간 폭(℃). 설명 변수가 없으면 쓰지 않는다")
    bootstrap: int = Field(1000, ge=200, le=5000, description="신뢰구간을 낼 부트스트랩 반복 수. 크면 구간이 안정되고 느려진다")


class InterventionImpact(Template):
    key = "intervention-impact"
    version = "1.0.0"
    name = "개입 효과 검증"
    kind = Kind.GENERAL
    category = Category.ENV_QUALITY
    roles = [RoleSpec("target", "series", 1, 1, label="효과를 볼 값"),
             RoleSpec("covariates", "series", 0, 1, required=False, label="외기 온도(맞춤 조건)")]
    params_model = Params
    requirements = Requirements(min_period_days=14, min_points=200, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1h", max_period_days=180)
    fast = True
    outputs = ["line", "bar", "table"]
    fixed_caveats = [CAVEAT]

    def validate_inputs(self, bindings: list[dict], params: Params) -> None:
        if params.interventionAt is None:
            raise BusinessError(ErrorCode.INTERVENTION_POINT_REQUIRED)

    def extra_issues(self, period_from: datetime, period_to: datetime, params: Params) -> list[dict]:
        if params.interventionAt is None:
            return []
        at = pd.Timestamp(params.interventionAt)
        before = (at - pd.Timestamp(period_from)) / pd.Timedelta(days=1)
        after = (pd.Timestamp(period_to) - at) / pd.Timedelta(days=1)
        issues = []
        if after < params.minDaysEachSide:
            issues.append({"code": "AFTER_TOO_SHORT", "severity": "FAIL",
                           "message": f"개입 후 최소 {params.minDaysEachSide}일 데이터가 필요합니다(현재 {max(0.0, after):.0f}일)"})
        if before < params.minDaysEachSide:
            issues.append({"code": "BEFORE_TOO_SHORT", "severity": "FAIL",
                           "message": f"개입 전 최소 {params.minDaysEachSide}일 데이터가 필요합니다(현재 {max(0.0, before):.0f}일)"})
        return issues

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        if params.interventionAt is None:
            raise BusinessError(ErrorCode.INTERVENTION_POINT_REQUIRED)
        at = pd.Timestamp(params.interventionAt)
        frame = data.role("target")
        col = frame.columns[0]
        y = frame[col].dropna().resample("1h").mean().dropna()
        issues = self.extra_issues(y.index[0].to_pydatetime() if len(y) else data.period_from,
                                   (y.index[-1] + pd.Timedelta(hours=1)).to_pydatetime() if len(y) else data.period_to, params)
        if issues:
            raise BusinessError(ErrorCode.ANALYSIS_INSUFFICIENT_DATA, {"reason": issues[0]["message"]})
        local = y.index.tz_convert(ctx.timezone)
        daytype = (local.dayofweek >= 5).astype(int)
        cell = np.asarray(daytype) * 24 + np.asarray(local.hour)
        cov = data.role("covariates")
        if cov.shape[1]:
            temp = cov.iloc[:, 0].resample("1h").mean().reindex(y.index).interpolate(limit_direction="both")
            tbin = np.floor(temp.to_numpy() / params.tempBinWidth).astype(int)
            tbin = tbin - tbin.min()
            cell = cell * 1000 + tbin
        after = np.asarray(y.index >= at)
        day = np.asarray(local.normalize().asi8)
        _, day_idx = np.unique(day, return_inverse=True)
        _, cell_idx = np.unique(cell, return_inverse=True)
        n_days, n_cells = day_idx.max() + 1, cell_idx.max() + 1
        values = y.to_numpy(dtype=float)
        sums = np.zeros((n_days, n_cells))
        cnts = np.zeros((n_days, n_cells))
        np.add.at(sums, (day_idx, cell_idx), values)
        np.add.at(cnts, (day_idx, cell_idx), 1)
        day_after = np.zeros(n_days, dtype=bool)
        day_after[day_idx[after]] = True
        before_days, after_days = np.flatnonzero(~day_after), np.flatnonzero(day_after)

        def effect(wb: np.ndarray, wa: np.ndarray) -> tuple[float, float, float]:
            sb, cb = wb @ sums[before_days], wb @ cnts[before_days]
            sa, ca = wa @ sums[after_days], wa @ cnts[after_days]
            ok = (cb > 0) & (ca > 0)
            if not ok.any():
                return float("nan"), float("nan"), float("nan")
            mb, ma = sb[ok] / cb[ok], sa[ok] / ca[ok]
            w = ca[ok]
            base = float(np.sum(w * mb))
            return float(np.sum(w * (ma - mb)) / base) if base else float("nan"), float(np.sum(w * mb) / w.sum()), float(np.sum(w * ma) / w.sum())

        point, mean_before, mean_after = effect(np.ones(len(before_days)), np.ones(len(after_days)))
        rng = ctx.rng()
        boots = []
        for _ in range(params.bootstrap):
            wb = np.bincount(rng.integers(0, len(before_days), len(before_days)), minlength=len(before_days)).astype(float)
            wa = np.bincount(rng.integers(0, len(after_days), len(after_days)), minlength=len(after_days)).astype(float)
            boots.append(effect(wb, wa)[0])
        ctx.checkpoint()
        boots = np.asarray([b for b in boots if not np.isnan(b)])
        lo, hi = (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))) if boots.size else (float("nan"), float("nan"))
        # 날 수가 적은 부트스트랩은 구간이 좁게 나오므로(소표본 편향) 반폭을 1.25배 넓힌다
        lo, hi = point - 1.25 * (point - lo), point + 1.25 * (hi - point)
        unclear = not (lo > 0 or hi < 0)
        verdict = "UNCLEAR" if unclear else ("DECREASE" if point < 0 else "INCREASE")
        if unclear:
            headline = f"효과 불명확: 변화 {point * 100:+.1f}%(95% 구간 {lo * 100:+.1f}% ~ {hi * 100:+.1f}%가 0을 포함)"
        else:
            headline = f"개입 뒤 평균이 약 {abs(point) * 100:.1f}% {'줄었습니다' if point < 0 else '늘었습니다'}(95% 구간 {lo * 100:+.1f}% ~ {hi * 100:+.1f}%, 추정)"
        unit = data.unit(col)
        charts = [chart("series", "line", "개입 전후", x_axis=time_axis(), y_axis=value_axis("값", unit),
                        series=[{"key": col, "label": data.label(col), "data": ts_points(y)}],
                        markers=[{"x": at, "y": None, "label": "개입", "severity": "INFO"}],
                        regions=[{"from": y.index[0], "to": at, "label": "개입 전", "severity": "INFO"},
                                 {"from": at, "to": y.index[-1], "label": "개입 후", "severity": "INFO"}]),
                  chart("effect", "bar", "효과 크기(95% 구간)", x_axis={"type": "category", "label": ""}, y_axis=value_axis("변화율"),
                        series=[{"key": "effect", "label": "효과", "data": [["효과", round(point, 6)]]}],
                        bands=[{"key": "ci95", "label": "95% 구간", "level": 0.95, "lower": [round(lo, 6)], "upper": [round(hi, 6)]}])]
        tables = [table("effect", "효과 요약", [("meanBefore", "개입 전 평균", "number"), ("meanAfter", "개입 후 평균(같은 조건)", "number"),
                                                ("effect", "변화율", "number"), ("ciLow", "95% 하한", "number"), ("ciHigh", "95% 상한", "number"),
                                                ("verdict", "판정", "string")],
                        [{"meanBefore": round(mean_before, 6), "meanAfter": round(mean_after, 6), "effect": round(point, 6),
                          "ciLow": round(lo, 6), "ciHigh": round(hi, 6), "verdict": {"UNCLEAR": "효과 불명확", "DECREASE": "감소",
                                                                                     "INCREASE": "증가"}[verdict]}])]
        metrics = [Metric("effect", "효과 크기(추정)", round(point, 6), "ratio", "WARN" if unclear else "OK"),
                   Metric("ciLow", "95% 하한", round(lo, 6), "ratio"), Metric("ciHigh", "95% 상한", round(hi, 6), "ratio"),
                   Metric("verdict", "판정", verdict)]
        return Result(headline=headline, level="WARN" if unclear else "OK", metrics=metrics, charts=charts, tables=tables,
                      caveats=list(self.fixed_caveats), flags=["ESTIMATE"],
                      evidence={"baseline": round(mean_before, 6), "threshold": 0.0, "ci95": [lo, hi],
                                "matchedOn": ["요일 유형", "시간대"] + (["외기 온도"] if cov.shape[1] else []),
                                "contributors": [{"seriesKey": col, "weight": 1.0}]},
                      provenance={"algorithm": "조건 맞춤 전후 비교 + 날 단위 부트스트랩", "interventionAt": at,
                                  "bootstrap": params.bootstrap})


TEMPLATE = InterventionImpact()
