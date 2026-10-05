"""sensor-health 센서 건강 진단(ANA-02.04 기본, ANA-02.05 같은 공간 기기 간 비교).

기기별로 수신 누락률, 값 멈춤(같은 값 지속), 범위 초과율(품질 코드 1 비율), 배터리, 신호 세기를 근거 수치와 함께 판정한다.
비교(peerCompare)를 켜면 같은 측정 항목의 다른 기기 중앙값에서 계속 벗어나는 기기를 '드리프트 의심'으로 표시한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from data2flow_analytics.common.errors import BusinessError, ErrorCode
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
    time_axis,
    ts_points,
    value_axis,
)

STATUS_LABEL = {"OK": "정상", "WARN": "주의", "SUSPECT": "의심"}


class Params(TemplateParams):
    peerCompare: bool = Field(False, description="true면 같은 측정 항목 기기끼리 비교해 혼자 벗어나는 기기(드리프트)를 찾는다. 기기 2대 이상 필요")
    stuckHours: float = Field(3.0, ge=0.5, le=168, description="같은 값이 이 시간(시간) 넘게 이어지면 '값 멈춤'으로 본다")
    driftThreshold: float = Field(0.5, gt=0, description="기기 간 비교에서 다른 기기 중앙값과의 차이가 이 값을 계속 넘으면 드리프트 의심(측정 단위)")
    batteryWarn: float = Field(20.0, ge=0, le=100, description="배터리가 이 값(%) 아래면 주의")
    rssiWarn: float = Field(-110.0, ge=-150, le=0, description="신호 세기(RSSI)가 이 값(dBm) 아래면 주의")


def _longest_stuck(s: pd.Series) -> tuple[pd.Timedelta, pd.Timestamp | None, pd.Timestamp | None, float | None]:
    if len(s) < 2:
        return pd.Timedelta(0), None, None, None
    v = s.to_numpy(dtype=float)
    same = np.isclose(v[1:], v[:-1], rtol=0, atol=1e-9)
    best = (pd.Timedelta(0), None, None, None)
    start = None
    for i, flag in enumerate(same):
        if flag and start is None:
            start = i
        if (not flag or i == len(same) - 1) and start is not None:
            end = i + 1 if flag else i
            dur = s.index[end] - s.index[start]
            if dur > best[0]:
                best = (dur, s.index[start], s.index[end], float(v[start]))
            start = None
    return best


class SensorHealth(Template):
    key = "sensor-health"
    version = "1.0.0"
    name = "센서 건강 진단"
    kind = Kind.DOMAIN
    category = Category.ASSET_HEALTH
    roles = [RoleSpec("devices", "multi_series", 1, 50, label="같은 측정 항목의 기기들")]
    params_model = Params
    requirements = Requirements(min_period_days=7, min_points=100, missing_warn=0.30, missing_fail=0.90,
                                recommended_resolution="RAW", max_period_days=90)
    fast = True
    needs_quality = True
    outputs = ["table", "line", "bar"]
    fixed_caveats = ["진단은 데이터 모양으로 본 추정입니다. '의심' 기기는 현장에서 확인하세요."]

    def validate_inputs(self, bindings: list[dict], params: Params) -> None:
        if params.peerCompare:
            count = sum(len(b.get("sources") or []) for b in bindings if b.get("role") == "devices")
            if count < 2:
                raise BusinessError(ErrorCode.ANALYSIS_BINDING_INVALID, {"role": "devices", "min": 2, "max": 50, "semantic": "같은 측정 항목"})

    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("devices")
        quality = data.quality.get("devices")
        status_info = data.extras.get("deviceStatus") or {}
        period = pd.Timestamp(data.period_to) - pd.Timestamp(data.period_from)
        rows = []
        peer_dev: dict[str, pd.Series] = {}
        if params.peerCompare and frame.shape[1] >= 2:
            hourly = frame.resample("1h").mean()
            for col in hourly.columns:
                others = hourly.drop(columns=[col])
                peer_dev[col] = hourly[col] - others.median(axis=1)
        for col in frame.columns:
            ctx.checkpoint()
            s = frame[col].dropna()
            info = data.series.get(col)
            step = median_step(s.index) if len(s) > 2 else pd.Timedelta(minutes=10)
            expected = max(1, int(round(period / step)))
            missing = max(0.0, 1 - len(s) / expected)
            stuck, st_from, st_to, st_val = _longest_stuck(s)
            out_range = None
            if quality is not None and col in quality.columns:
                q = quality[col].dropna()
                out_range = float((q == 1).mean()) if len(q) else 0.0
            dev_key = str(info.device_id) if info and info.device_id is not None else col
            st = status_info.get(dev_key) or status_info.get(col) or {}
            battery, rssi = st.get("battery"), st.get("rssi")
            reasons: list[str] = []
            levels: list[str] = []

            def bump(level: str, text: str, reasons=reasons, levels=levels) -> None:
                reasons.append(text)
                levels.append(level)

            if missing >= 0.2:
                bump("SUSPECT", f"누락률 {missing * 100:.0f}%")
            elif missing >= 0.05:
                bump("WARN", f"누락률 {missing * 100:.0f}%")
            if stuck >= pd.Timedelta(hours=params.stuckHours):
                bump("SUSPECT", f"값 멈춤 {stuck / pd.Timedelta(hours=1):.0f}시간")
            if out_range is not None and out_range >= 0.1:
                bump("SUSPECT", f"범위 초과 {out_range * 100:.0f}%")
            elif out_range is not None and out_range >= 0.02:
                bump("WARN", f"범위 초과 {out_range * 100:.0f}%")
            if battery is not None and battery < params.batteryWarn:
                bump("WARN", f"배터리 {battery:.0f}%")
            if rssi is not None and rssi < params.rssiWarn:
                bump("WARN", f"신호 세기 {rssi:.0f}dBm")
            drift = None
            if col in peer_dev:
                d = peer_dev[col].dropna()
                if len(d):
                    over = (d.abs() > params.driftThreshold)
                    consistent = abs(np.sign(d[over]).mean()) if over.any() else 0
                    drift = float(d.median())
                    if over.mean() >= 0.8 and consistent >= 0.9:
                        bump("SUSPECT", f"드리프트 의심(다른 기기보다 {drift:+.2f})")
            status = "SUSPECT" if "SUSPECT" in levels else ("WARN" if "WARN" in levels else "OK")
            rows.append({"seriesKey": col, "label": data.label(col), "status": status, "statusLabel": STATUS_LABEL[status],
                         "reasons": reasons, "missingRate": round(missing, 6), "received": int(len(s)), "expected": expected,
                         "stuckHours": round(stuck / pd.Timedelta(hours=1), 4), "stuckFrom": st_from, "stuckTo": st_to, "stuckValue": st_val,
                         "outOfRangeRate": None if out_range is None else round(out_range, 6), "battery": battery, "rssi": rssi,
                         "peerDeviation": None if drift is None else round(drift, 6)})
        suspects = [r for r in rows if r["status"] == "SUSPECT"]
        warns = [r for r in rows if r["status"] == "WARN"]
        if suspects:
            headline = f"기기 {len(rows)}대 중 {len(suspects)}대 의심: " + ", ".join(f"{r['label']}({'; '.join(r['reasons'])})" for r in suspects[:3])
        elif warns:
            headline = f"기기 {len(rows)}대 중 {len(warns)}대 주의"
        else:
            headline = f"기기 {len(rows)}대 모두 정상으로 보입니다"
        charts = [chart("missing", "bar", "기기별 누락률", x_axis={"type": "category", "label": "기기"}, y_axis=value_axis("누락률"),
                        series=[{"key": "missingRate", "label": "누락률", "data": [[r["label"], r["missingRate"]] for r in rows]}])]
        if peer_dev:
            charts.append(chart("peer", "line", "다른 기기 중앙값과의 차이", x_axis=time_axis(), y_axis=value_axis("차이"),
                                series=[{"key": k, "label": data.label(k), "data": ts_points(v, 4)} for k, v in peer_dev.items()],
                                thresholds=[{"value": params.driftThreshold, "label": "기준"}, {"value": -params.driftThreshold, "label": "기준"}]))
        tables = [table("devices", "기기별 상태", [("label", "기기", "string"), ("statusLabel", "상태", "string"), ("missingRate", "누락률", "number"),
                                                   ("stuckHours", "같은 값 지속(시간)", "number"), ("outOfRangeRate", "범위 초과율", "number"),
                                                   ("battery", "배터리(%)", "number"), ("rssi", "신호 세기(dBm)", "number"),
                                                   ("peerDeviation", "다른 기기와 차이", "number")], rows)]
        metrics = [Metric("devices", "기기 수", len(rows)), Metric("suspect", "의심", len(suspects), None, "WARN" if suspects else "OK"),
                   Metric("warn", "주의", len(warns))]
        return Result(headline=headline, level="WARN" if suspects or warns else "OK", metrics=metrics, charts=charts, tables=tables,
                      caveats=list(self.fixed_caveats),
                      evidence={"threshold": {"missingSuspect": 0.2, "stuckHours": params.stuckHours, "drift": params.driftThreshold},
                                "contributors": [{"seriesKey": r["seriesKey"], "weight": round(1 / len(suspects), 6)} for r in suspects]},
                      provenance={"algorithm": "누락률·값 멈춤·범위 초과·배터리·신호·기기 간 편차 규칙"})


TEMPLATE = SensorHealth()
