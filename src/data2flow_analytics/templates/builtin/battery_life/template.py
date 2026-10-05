"""battery-life 배터리 교체 예측(ANA-02.06, BR-ANA-23). 로버스트 감소 추세로 교체 기준에 닿는 날을 계산한다."""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from data2flow_analytics.templates.algos import theil_sen
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

MIN_DAYS = 30


class Params(TemplateParams):
    replaceAt: float = Field(20.0, ge=0, le=100, description="교체 기준(%). 배터리가 이 값에 닿는 날을 교체 예상일로 본다")
    recommendWithinDays: int = Field(30, ge=1, le=365, description="교체 예상일이 이 기간(일) 안이면 교체 권장 목록에 넣는다")


class BatteryLife(Template):
    key = "battery-life"
    version = "1.0.0"
    name = "배터리 교체 예측"
    kind = Kind.DOMAIN
    category = Category.ASSET_HEALTH
    roles = [RoleSpec("battery", "multi_series", 1, 50, "battery", label="배터리")]
    params_model = Params
    requirements = Requirements(min_period_days=MIN_DAYS, min_points=30, missing_warn=0.30, missing_fail=0.80,
                                recommended_resolution="1h", max_period_days=366)
    fast = True
    predictive = True
    outputs = ["line", "table"]
    fixed_caveats = ["교체 예상일은 최근 감소 추세가 이어진다고 가정한 추정입니다. 온도·전송 주기가 바뀌면 달라집니다."]

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("battery")
        rows, series = [], []
        now = pd.Timestamp(data.period_to)
        for col in frame.columns:
            ctx.checkpoint()
            s = frame[col].dropna()
            label = data.label(col)
            series.append({"key": col, "label": label, "data": ts_points(s.resample("6h").mean() if len(s) else s, 3)})
            row = {"seriesKey": col, "label": label, "current": round(float(s.iloc[-1]), 3) if len(s) else None}
            span = (s.index[-1] - s.index[0]) / pd.Timedelta(days=1) if len(s) > 1 else 0
            if span < MIN_DAYS - 0.5:
                row.update({"predictable": False, "reason": f"{MIN_DAYS}일 미만"})
                rows.append(row)
                continue
            x = np.asarray((s.index - s.index[-1]) / pd.Timedelta(days=1), dtype=float)
            y = s.to_numpy(dtype=float)
            slope, intercept, lo, hi = theil_sen(x, y)
            sigma = robust_sigma(y - (intercept + slope * x))
            if abs(slope) < 1e-6 or float(np.nanmax(y) - np.nanmin(y)) < 1e-9 or slope >= 0:
                row.update({"predictable": False, "reason": "변화 없음"})
                rows.append(row)
                continue
            current = intercept
            days_left = max(0.0, (current - params.replaceAt) / -slope)
            rel_ci = (hi - lo) / (2 * abs(slope)) if slope else float("inf")
            confidence = "HIGH" if rel_ci < 0.2 else ("MEDIUM" if rel_ci < 0.5 else "LOW")
            replace_at = now + pd.Timedelta(days=days_left)
            row.update({"predictable": True, "slopePerDay": round(slope, 6), "daysLeft": round(days_left, 2),
                        "replaceDate": replace_at.date().isoformat(), "confidence": confidence, "noise": round(sigma, 6),
                        "recommend": days_left <= params.recommendWithinDays or current <= params.replaceAt})
            rows.append(row)
        predictable = [r for r in rows if r.get("predictable")]
        recommend = sorted([r for r in predictable if r.get("recommend")], key=lambda r: r["daysLeft"])
        if recommend:
            headline = f"{len(recommend)}대 교체 권장(가장 빠른 예상일 {recommend[0]['replaceDate']}, 추정)"
        elif predictable:
            soonest = min(predictable, key=lambda r: r["daysLeft"])
            headline = f"교체가 급한 기기는 없습니다(가장 빠른 예상일 {soonest['replaceDate']}, 추정)"
        else:
            headline = "예측 불가: 배터리 값이 변하지 않거나 데이터가 부족합니다"
        charts = [chart("battery", "line", "배터리 추이", x_axis=time_axis(), y_axis=value_axis("배터리", "%"), series=series,
                        thresholds=[{"value": params.replaceAt, "label": "교체 기준"}])]
        cols = [("label", "기기", "string"), ("current", "현재(%)", "number"), ("slopePerDay", "하루 감소", "number"),
                ("daysLeft", "남은 일수(추정)", "number"), ("replaceDate", "교체 예상일(추정)", "date"), ("confidence", "신뢰도", "string"),
                ("reason", "예측 불가 사유", "string")]
        tables = [table("devices", "기기별 교체 예상", cols, rows),
                  table("recommend", "교체 권장 목록", cols[:5], recommend)]
        metrics = [Metric("recommend", "교체 권장", len(recommend), None, "WARN" if recommend else "OK"),
                   Metric("unpredictable", "예측 불가", len(rows) - len(predictable))]
        flags = ["ESTIMATE"] + (["UNPREDICTABLE"] if not predictable else [])
        return Result(headline=headline, level="WARN" if recommend else "OK", metrics=metrics, charts=charts, tables=tables,
                      caveats=list(self.fixed_caveats), flags=flags,
                      evidence={"threshold": params.replaceAt, "contributors": [{"seriesKey": r["seriesKey"], "weight": 1.0} for r in recommend]},
                      provenance={"algorithm": "Theil-Sen 감소 추세"})


TEMPLATE = BatteryLife()
