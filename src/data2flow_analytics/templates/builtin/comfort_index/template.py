"""comfort-index 쾌적도(ANA-02.01, BR-ANA-22).

온도·습도·CO2(선택: TVOC, PM2.5)가 공간 목표 환경(DEV-01.04)을 벗어난 정도를 감점해 0~100점으로 매긴다.
목표 환경이 없으면 기본 기준(온도 20~26℃, 습도 40~60%, CO2 1,000ppm)을 쓰고 결과에 그 사실을 남긴다.
"""

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
    time_axis,
    ts_points,
    value_axis,
)

DEFAULT_TARGETS = {"temp": (20.0, 26.0), "humidity": (40.0, 60.0), "co2": (None, 1000.0), "tvoc": (None, 500.0), "pm25": (None, 35.0)}
#: 단위당 감점과 상한(항목별)
PENALTY = {"temp": (15.0, 40.0), "humidity": (1.5, 20.0), "co2": (0.04, 40.0), "tvoc": (0.02, 15.0), "pm25": (0.5, 15.0)}
LABELS = {"temp": "온도", "humidity": "습도", "co2": "CO2", "tvoc": "TVOC", "pm25": "PM2.5"}
SEMANTIC_OF = {"temp": "temperature", "humidity": "humidity", "co2": "co2", "tvoc": "tvoc", "pm25": "pm25"}
GRADES = [(80.0, "GOOD", "쾌적"), (60.0, "FAIR", "보통"), (40.0, "POOR", "나쁨"), (-1.0, "BAD", "매우 나쁨")]


class Params(TemplateParams):
    method: Literal["SIMPLE"] = Field("SIMPLE", description="계산 방식. SIMPLE은 목표 범위 이탈 감점(THI·CO2 등급). PMV/PPD·적응형은 M7에서 더한다")
    useSpaceTargets: bool = Field(True, description="true면 공간 목표 환경을 쓰고, 없을 때만 기본 기준을 쓴다. false면 항상 기본 기준")


def grade_of(score: float) -> tuple[str, str]:
    for bound, code, label in GRADES:
        if score >= bound:
            return code, label
    return "BAD", "매우 나쁨"


def thi(temp: np.ndarray, rh: np.ndarray) -> np.ndarray:
    """불쾌지수 THI = 0.81T + 0.01RH(0.99T − 14.3) + 46.3"""
    return 0.81 * temp + 0.01 * rh * (0.99 * temp - 14.3) + 46.3


class ComfortIndex(Template):
    key = "comfort-index"
    version = "1.0.0"
    name = "쾌적도"
    kind = Kind.DOMAIN
    category = Category.ENV_QUALITY
    roles = [RoleSpec("temp", "series", 1, 1, "temperature", label="온도"),
             RoleSpec("humidity", "series", 1, 1, "humidity", label="습도"),
             RoleSpec("co2", "series", 1, 1, "co2", label="CO2"),
             RoleSpec("tvoc", "series", 0, 1, "tvoc", required=False, label="TVOC"),
             RoleSpec("pm25", "series", 0, 1, "pm25", required=False, label="PM2.5")]
    params_model = Params
    requirements = Requirements(min_period_days=1, min_points=24, missing_warn=0.10, missing_fail=0.30,
                                recommended_resolution="1m", max_period_days=366)
    fast = True
    outputs = ["line", "bar", "gauge", "table"]
    fixed_caveats = ["쾌적도는 개인차가 큽니다. 기준값은 공간 설정에서 바꿀 수 있습니다."]

    def _targets(self, data: InputData, params: Params) -> tuple[dict, str]:
        space = data.extras.get("spaceTargets") if params.useSpaceTargets else None
        targets = dict(DEFAULT_TARGETS)
        if not space:
            return targets, "DEFAULT"
        used = False
        for role, sem in SEMANTIC_OF.items():
            t = space.get(sem) or space.get(role)
            if t:
                lo, hi = t.get("min"), t.get("max")
                d_lo, d_hi = DEFAULT_TARGETS[role]
                targets[role] = (lo if lo is not None else d_lo, hi if hi is not None else d_hi)
                used = True
        return targets, "SPACE_TARGET" if used else "DEFAULT"

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        targets, baseline = self._targets(data, params)
        roles = [r for r in ("temp", "humidity", "co2", "tvoc", "pm25") if data.has_role(r)]
        base_index = data.role("temp").dropna(how="all").index
        aligned = {}
        for r in roles:
            s = data.role(r).iloc[:, 0].dropna().sort_index()
            aligned[r] = s.reindex(base_index, method="nearest", tolerance=pd.Timedelta(minutes=30)) if len(s) else pd.Series(
                np.nan, index=base_index)
        df = pd.DataFrame(aligned, index=base_index)
        penalties = pd.DataFrame(index=df.index)
        outside = pd.DataFrame(index=df.index)
        for r in roles:
            lo, hi = targets[r]
            v = df[r]
            over = (v - hi).clip(lower=0) if hi is not None else 0 * v
            under = (lo - v).clip(lower=0) if lo is not None else 0 * v
            dev = over + under
            per, cap = PENALTY[r]
            penalties[r] = (dev * per).clip(upper=cap).fillna(0)
            outside[r] = dev > 0
        score = (100 - penalties.sum(axis=1)).clip(0, 100)
        core_cols = [c for c in ("temp", "humidity", "co2") if c in outside]
        any_out = outside[core_cols].any(axis=1) if core_cols else pd.Series(False, index=df.index)
        out_ratio = float(any_out.mean()) if len(any_out) else 0.0
        total_pen = float(penalties.sum().sum())
        contributions = {r: (float(penalties[r].sum()) / total_pen if total_pen > 0 else 0.0) for r in roles}
        mean_score = float(score.mean()) if len(score) else None
        code, label = grade_of(mean_score) if mean_score is not None else ("BAD", "매우 나쁨")
        thi_v = thi(df["temp"].to_numpy(float), df["humidity"].to_numpy(float)) if {"temp", "humidity"} <= set(roles) else None
        hourly = score.resample("1h").mean()
        grade_series = hourly.map(lambda v: None if pd.isna(v) else grade_of(float(v))[0])
        main = max(contributions, key=contributions.get) if total_pen > 0 else None
        headline = f"평균 쾌적도 {mean_score:.0f}점({label})" if mean_score is not None else "계산할 데이터가 없습니다"
        if main:
            headline += f", 주된 감점 원인은 {LABELS[main]}입니다"
        if baseline == "DEFAULT":
            headline += " (공간 목표 환경이 없어 기본 기준 사용)"
        charts = [
            chart("score", "line", "쾌적도 점수 추이", x_axis=time_axis(), y_axis=value_axis("점수"),
                  series=[{"key": "score", "label": "쾌적도(시간 평균)", "data": ts_points(hourly, 3)}],
                  thresholds=[{"value": 80, "label": "쾌적"}, {"value": 60, "label": "보통"}, {"value": 40, "label": "나쁨"}]),
            chart("contributions", "bar", "원인별 감점 비율", x_axis={"type": "category", "label": "항목"}, y_axis=value_axis("비율"),
                  series=[{"key": "contribution", "label": "감점 비율", "data": [[LABELS[r], round(contributions[r], 6)] for r in roles]}]),
            chart("gauge", "gauge", "평균 쾌적도", gauge={"value": round(mean_score, 3) if mean_score is not None else None, "min": 0, "max": 100,
                                                        "ranges": [{"from": 0, "to": 40, "severity": "CRITICAL"},
                                                                   {"from": 40, "to": 60, "severity": "WARN"},
                                                                   {"from": 60, "to": 80, "severity": "INFO"},
                                                                   {"from": 80, "to": 100, "severity": "OK"}]}),
        ]
        rows = [{"item": LABELS[r], "target": _fmt_range(targets[r]), "outsideRatio": round(float(outside[r].mean()), 6),
                 "mean": round(float(df[r].mean()), 6) if df[r].notna().any() else None, "contribution": round(contributions[r], 6)}
                for r in roles]
        tables = [table("items", "항목별 목표 이탈", [("item", "항목", "string"), ("target", "목표", "string"), ("mean", "평균", "number"),
                                                      ("outsideRatio", "이탈 시간 비율", "number"), ("contribution", "감점 비율", "number")], rows),
                  table("grades", "시간별 등급", [("time", "시각", "datetime"), ("score", "점수", "number"), ("grade", "등급", "string")],
                        [{"time": t, "score": None if pd.isna(v) else round(float(v), 3), "grade": g}
                         for (t, v), g in zip(hourly.items(), grade_series, strict=True)])]
        metrics = [Metric("score", "평균 쾌적도", round(mean_score, 6) if mean_score is not None else None, "점",
                          "OK" if code == "GOOD" else "WARN"),
                   Metric("grade", "등급", label),
                   Metric("outsideRatio", "목표 이탈 시간 비율", round(out_ratio, 6), "ratio")]
        if thi_v is not None:
            metrics.append(Metric("thiMean", "평균 불쾌지수(THI)", round(float(np.nanmean(thi_v)), 3)))
        caveats = list(self.fixed_caveats)
        if baseline == "DEFAULT":
            caveats.append("공간 목표 환경이 없어 기본 기준(온도 20~26℃, 습도 40~60%, CO2 1,000ppm)으로 계산했습니다.")
        return Result(headline=headline, level="OK" if code == "GOOD" else "WARN", metrics=metrics, charts=charts, tables=tables,
                      caveats=caveats, evidence={"baseline": baseline, "targets": {LABELS[r]: _fmt_range(targets[r]) for r in roles},
                                                 "contributors": [{"seriesKey": data.role(r).columns[0], "weight": round(contributions[r], 6)}
                                                                  for r in roles]},
                      provenance={"algorithm": "목표 범위 이탈 감점(THI·CO2 등급)", "method": params.method, "baseline": baseline,
                                  "targets": {r: list(targets[r]) for r in roles}, "inputs": {"defaults": baseline == "DEFAULT"}})


def _fmt_range(rng: tuple) -> str:
    lo, hi = rng
    if lo is None:
        return f"≤ {hi:g}"
    if hi is None:
        return f"≥ {lo:g}"
    return f"{lo:g}~{hi:g}"


TEMPLATE = ComfortIndex()
