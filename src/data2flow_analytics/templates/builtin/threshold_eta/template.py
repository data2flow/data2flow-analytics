"""threshold-eta 기준 도달 시각 예측(ANA-02.09, 실시간 ANA-06.01).

최근 구간의 로버스트 선형 추세(Theil-Sen)로 기준값에 닿는 시각을 계산한다. 신뢰도는 추세 대비 잔차 잡음의 크기로 정한다.
"""

from __future__ import annotations

from collections import deque
from typing import Literal

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

MIN_MINUTES = 10


class Params(TemplateParams):
    threshold: float = Field(1000.0, description="기준값. 이 값에 언제 닿는지 예측한다(예: CO2 1000ppm)")
    direction: Literal["UP", "DOWN"] = Field("UP", description="UP은 올라가서 닿는 경우, DOWN은 내려가서 닿는 경우(예: 배터리 20%)")
    windowMinutes: int = Field(30, ge=5, le=720, description="추세를 계산할 최근 구간(분). 짧으면 빠르게 반응하고 흔들림도 커진다")
    horizonMinutes: int = Field(240, ge=5, le=10080, description="이 시간 안에 닿을 때만 도달 예상으로 알린다(분)")


def estimate_eta(times: pd.DatetimeIndex, values: np.ndarray, params: Params) -> dict:
    """도달 예상(minutesLeft, etaAt, confidence) 또는 예측 불가 사유."""
    if len(values) < 3 or (times[-1] - times[0]) < pd.Timedelta(minutes=MIN_MINUTES):
        return {"predictable": False, "reason": f"최근 데이터가 {MIN_MINUTES}분 미만"}
    x = (times - times[-1]) / pd.Timedelta(minutes=1)
    x = np.asarray(x, dtype=float)
    slope, intercept, lo, hi = theil_sen(x, values)
    current = intercept  # x=0(마지막 시각)의 추세값
    resid = values - (intercept + slope * x)
    sigma = robust_sigma(resid)
    step_min = float(np.median(np.diff(x))) if len(x) > 1 else 1.0
    if abs(slope) < 1e-12:
        return {"predictable": False, "reason": "변화 없음", "slope": 0.0, "current": current}
    noise_ratio = sigma / (abs(slope) * step_min) if slope else float("inf")
    confidence = "HIGH" if noise_ratio < 0.5 else ("MEDIUM" if noise_ratio < 1.5 else "LOW")
    sign = 1 if params.direction == "UP" else -1
    gap = params.threshold - current
    out = {"predictable": True, "slope": slope, "slopeLow": lo, "slopeHigh": hi, "current": current, "sigma": sigma,
           "confidence": confidence, "noiseRatio": noise_ratio}
    if sign * gap <= 0:
        out.update({"reached": True, "minutesLeft": 0.0})
        return out
    if sign * slope <= 0:
        out.update({"reached": False, "minutesLeft": None, "reason": "추세가 기준과 반대 방향(도달 예상 없음)"})
        return out
    minutes = gap / slope
    out.update({"reached": False, "minutesLeft": float(minutes)})
    return out


class ThresholdEta(Template):
    key = "threshold-eta"
    version = "1.0.0"
    name = "기준 도달 시각 예측"
    kind = Kind.GENERAL
    category = Category.PREDICTION
    roles = [RoleSpec("target", "series", 1, 1, label="지켜볼 값")]
    params_model = Params
    requirements = Requirements(min_period_days=0.0833, min_points=10, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="RAW", max_period_days=31)
    fast = True
    realtime = True
    predictive = True
    outputs = ["line", "gauge", "table"]
    fixed_caveats = ["도달 시각은 최근 추세가 이어진다고 가정한 추정입니다. 환기·재실 변화가 생기면 달라집니다."]

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("target")
        col = frame.columns[0]
        s = frame[col].dropna()
        unit = data.unit(col)
        window = s[s.index > s.index[-1] - pd.Timedelta(minutes=params.windowMinutes)] if len(s) else s
        est = estimate_eta(window.index, window.to_numpy(dtype=float), params) if len(window) else {"predictable": False,
                                                                                                    "reason": "데이터 없음"}
        thresholds = [{"value": params.threshold, "label": "기준값"}]
        series = [{"key": col, "label": data.label(col), "data": ts_points(s.iloc[-min(len(s), 720):])}]
        if not est["predictable"]:
            return Result(headline=f"예측 불가: {est['reason']}", level="WARN", metrics=[Metric("predictable", "예측 가능", False)],
                          charts=[chart("trend", "line", "최근 값과 기준", x_axis=time_axis(), y_axis=value_axis("값", unit),
                                        series=series, thresholds=thresholds)],
                          caveats=list(self.fixed_caveats), flags=["ESTIMATE", "UNPREDICTABLE"], evidence={"reason": est["reason"]},
                          provenance={"algorithm": "Theil-Sen 로버스트 추세", "unpredictableReason": est["reason"]})
        last = window.index[-1]
        minutes = est.get("minutesLeft")
        path = []
        if minutes is not None and minutes > 0:
            span = min(minutes, params.horizonMinutes) if minutes <= params.horizonMinutes else params.horizonMinutes
            pts = np.linspace(0, span, 12)
            path = [[last + pd.Timedelta(minutes=float(m)), est["current"] + est["slope"] * m] for m in pts]
        series.append({"key": "path", "label": "예측 경로(추정)", "data": [[t, round(v, 6)] for t, v in path], "style": "dashed"})
        conf_label = {"HIGH": "높음", "MEDIUM": "보통", "LOW": "낮음"}[est["confidence"]]
        if est.get("reached"):
            headline, level = "이미 기준값에 닿았습니다", "CRITICAL"
        elif minutes is None:
            headline, level = "도달 예상 없음(추세가 기준과 반대 방향)", "OK"
        elif minutes > params.horizonMinutes:
            headline, level = f"{params.horizonMinutes}분 안에는 도달 예상 없음(약 {minutes:.0f}분 뒤, 신뢰도 {conf_label})", "OK"
        else:
            headline, level = f"약 {minutes:.0f}분 뒤 도달 예상(신뢰도 {conf_label})", "WARN"
        eta_at = last + pd.Timedelta(minutes=minutes) if minutes is not None else None
        metrics = [Metric("minutesLeft", "남은 시간(추정)", round(minutes, 2) if minutes is not None else None, "min", level),
                   Metric("etaAt", "도달 예상 시각(추정)", eta_at),
                   Metric("confidence", "신뢰도", est["confidence"]),
                   Metric("slopePerMinute", "분당 변화", round(est["slope"], 6), unit),
                   Metric("current", "현재 추세값", round(est["current"], 6), unit)]
        charts = [chart("trend", "line", "최근 값과 예측 경로", x_axis=time_axis(), y_axis=value_axis("값", unit), series=series,
                        thresholds=thresholds),
                  chart("eta", "gauge", "남은 시간(분)", gauge={"value": round(minutes, 2) if minutes is not None else None, "min": 0,
                                                              "max": params.horizonMinutes, "ranges": [
                                                                  {"from": 0, "to": min(30, params.horizonMinutes), "severity": "CRITICAL"},
                                                                  {"from": min(30, params.horizonMinutes), "to": params.horizonMinutes,
                                                                   "severity": "OK"}]})]
        tables = [table("eta", "도달 예측", [("threshold", "기준값", "number"), ("direction", "방향", "string"),
                                              ("minutesLeft", "남은 시간(분)", "number"), ("etaAt", "도달 예상 시각", "datetime"),
                                              ("confidence", "신뢰도", "string")],
                        [{"threshold": params.threshold, "direction": params.direction,
                          "minutesLeft": round(minutes, 2) if minutes is not None else None, "etaAt": eta_at,
                          "confidence": est["confidence"]}])]
        return Result(headline=headline, level=level, metrics=metrics, charts=charts, tables=tables, caveats=list(self.fixed_caveats),
                      flags=["ESTIMATE"], evidence={"baseline": round(est["current"], 6), "threshold": params.threshold,
                                                    "slope": est["slope"], "slopeInterval95": [est["slopeLow"], est["slopeHigh"]],
                                                    "noiseRatio": est["noiseRatio"], "contributors": [{"seriesKey": col, "weight": 1.0}]},
                      provenance={"algorithm": "Theil-Sen 로버스트 추세", "windowMinutes": params.windowMinutes})

    # ---- 실시간 ----
    def realtime_init(self, history: pd.Series, params: Params, model, seed: int):
        state = {"points": deque(), "lastMinutes": None, "lastConfidence": None}
        cutoff = history.index[-1] - pd.Timedelta(minutes=params.windowMinutes) if len(history) else None
        for t, v in history.dropna().items():
            if cutoff is None or t > cutoff:
                state["points"].append((pd.Timestamp(t), float(v)))
        return state

    def infer(self, state: dict, at: pd.Timestamp, value: float, params: Params) -> dict | None:
        pts: deque = state["points"]
        pts.append((pd.Timestamp(at), float(value)))
        while pts and pts[0][0] <= at - pd.Timedelta(minutes=params.windowMinutes):
            pts.popleft()
        times = pd.DatetimeIndex([p[0] for p in pts])
        est = estimate_eta(times, np.array([p[1] for p in pts]), params)
        if not est["predictable"] or est.get("minutesLeft") is None:
            state["lastMinutes"] = None
            return None
        minutes = est["minutesLeft"]
        if minutes > params.horizonMinutes:
            return None
        rounded = int(round(minutes))
        if state["lastMinutes"] == rounded and state["lastConfidence"] == est["confidence"]:
            return None
        state["lastMinutes"], state["lastConfidence"] = rounded, est["confidence"]
        return {"type": "ETA", "value": float(value), "threshold": params.threshold, "direction": params.direction,
                "etaAt": at + pd.Timedelta(minutes=minutes), "minutesLeft": round(minutes, 2), "confidence": est["confidence"]}


TEMPLATE = ThresholdEta()
