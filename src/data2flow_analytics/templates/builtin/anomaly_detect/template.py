"""anomaly-detect 이상 탐지(ANA-02.02 통계, ANA-02.03 ML 다변량, ANA-06.01 실시간)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field
from sklearn.covariance import MinCovDet
from sklearn.ensemble import IsolationForest

from data2flow_analytics.common.errors import BusinessError, ErrorCode
from data2flow_analytics.templates.algos import (
    SeasonalProfile,
    fit_profile,
    group_runs,
    median_step,
    rolling_robust_sigma,
    steps_for,
)
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
    TrainedModel,
    chart,
    robust_sigma,
    table,
    time_axis,
    ts_points,
    value_axis,
)

STAT_THRESHOLDS = {1: 6.0, 2: 5.25, 3: 4.5, 4: 3.75, 5: 3.0}
ML_THRESHOLDS = {1: 7.0, 2: 6.0, 3: 5.0, 4: 4.0, 5: 3.0}
KIND_LABELS = {"SPIKE": "급등", "DROP": "급락", "LEVEL_SHIFT": "수준 이동", "VARIANCE": "변동성 증가"}


class Params(TemplateParams):
    method: Literal["STATISTICAL", "ISOLATION_FOREST"] = Field(
        "STATISTICAL", description="탐지 방식. STATISTICAL은 계절 제거 후 로버스트 z-score, ISOLATION_FOREST는 여러 측정 항목의 조합 이상")
    sensitivity: int = Field(3, ge=1, le=5, description="민감도 1~5. 높을수록 더 많이 탐지하고 오탐도 늘어난다")
    period: Literal["AUTO", "DAILY", "WEEKLY"] = Field("AUTO", description="제거할 주기. AUTO는 14일 이상이면 일·주 주기, 아니면 일 주기")
    minDurationMinutes: int = Field(0, ge=0, le=1440, description="최소 지속 시간(분). 이보다 짧은 튐은 무시한다")
    cooldownMinutes: int = Field(10, ge=0, le=1440, description="실시간 모드에서 같은 시계열의 이상 이벤트를 다시 내기 전 쉬는 시간(분)")


@dataclass
class _Event:
    series_key: str
    i0: int
    i1: int
    kind: str
    score: float
    value: float
    baseline: float
    contributors: list[dict] = field(default_factory=list)
    index: pd.DatetimeIndex | None = None


class AnomalyDetect(Template):
    key = "anomaly-detect"
    version = "1.0.0"
    name = "이상 탐지"
    kind = Kind.GENERAL
    category = Category.GENERAL
    roles = [RoleSpec("target", "series", 1, 50, label="분석할 값")]
    params_model = Params
    requirements = Requirements(min_period_days=7, min_points=100, missing_warn=0.10, missing_fail=0.20,
                                recommended_resolution="RAW", max_period_days=180)
    fast = True
    realtime = True
    trainable = True
    outputs = ["line", "scatter", "table"]
    fixed_caveats = ["이상은 '평소와 다름'이지 '잘못됨'이 아닙니다. 행사나 방학처럼 평소와 다른 날도 이상으로 잡힙니다."]

    def requirements_for(self, params: Params) -> Requirements:
        if params.method == "ISOLATION_FOREST":
            return Requirements(min_period_days=14, min_points=200, missing_warn=0.10, missing_fail=0.20,
                                recommended_resolution="RAW", max_period_days=180)
        return self.requirements

    def validate_inputs(self, bindings: list[dict], params: Params) -> None:
        if params.method == "ISOLATION_FOREST":
            count = sum(len(b.get("sources") or []) for b in bindings if b.get("role") == "target")
            if count < 2:
                raise BusinessError(ErrorCode.ANALYSIS_BINDING_INVALID, {"role": "target", "min": 2, "max": 50, "semantic": "수치"})

    def realtime_requires_model(self, params: Params) -> bool:
        return params.method == "ISOLATION_FOREST"

    # ------------------------------------------------------------------ 실행
    def run(self, data: InputData, params: Params, ctx: RunContext) -> Result:
        frame = data.role("target")
        if params.method == "ISOLATION_FOREST":
            events, scores = self._run_ml(frame, params, ctx)
        else:
            events, scores = self._run_stat(frame, params, ctx)
        return self._result(data, frame, events, scores, params)

    def _run_stat(self, frame: pd.DataFrame, params: Params, ctx: RunContext):
        threshold = STAT_THRESHOLDS[params.sensitivity]
        events: list[_Event] = []
        scores: dict[str, pd.Series] = {}
        for col in frame.columns:
            ctx.checkpoint()
            s = frame[col].dropna()
            if len(s) < 10:
                continue
            profile = fit_profile(s, ctx.timezone, params.period)
            base = profile.baseline(s.index)
            resid = s.to_numpy() - base
            resid = resid - np.median(resid)
            sigma = robust_sigma(resid) or 1e-9
            z = resid / sigma
            scores[col] = pd.Series(z, index=s.index)
            step = median_step(s.index)
            window = steps_for(pd.Timedelta(hours=1), step, minimum=12)
            # 변동성 증가: 잔차 1차 차분의 이동 로버스트 σ가 전체의 2배를 넘는 구간
            d = np.diff(resid, prepend=resid[0])
            d_sigma = robust_sigma(d[1:]) or 1e-9
            var_ratio = rolling_robust_sigma(d, window) / d_sigma
            var_mask = np.nan_to_num(var_ratio) > 2.0
            var_runs = [(a, b) for a, b in group_runs(var_mask, max_gap=window // 2) if b - a + 1 >= window]
            in_var = np.zeros(len(s), dtype=bool)
            pad = window // 2
            for i, (a, b) in enumerate(var_runs):
                a, b = max(0, a - pad), min(len(s) - 1, b + pad)
                var_runs[i] = (a, b)
                in_var[a:b + 1] = True
                peak = float(np.nanmax(var_ratio[a:b + 1]))
                events.append(_Event(col, a, b, "VARIANCE", round(peak, 4), float(s.iloc[a]), float(base[a]),
                                     [{"seriesKey": col, "weight": 1.0}]))
            flagged = (np.abs(z) > threshold) & ~in_var
            min_points = steps_for(pd.Timedelta(minutes=params.minDurationMinutes), step, minimum=1) if params.minDurationMinutes else 1
            level_points = max(6, steps_for(pd.Timedelta(minutes=30), step, minimum=1))
            for a, b in group_runs(flagged, max_gap=2):
                n = b - a + 1
                if n < min_points:
                    continue
                seg = z[a:b + 1]
                peak_i = a + int(np.argmax(np.abs(seg)))
                sign = float(np.sign(np.median(seg)))
                if n >= level_points:
                    kind = "LEVEL_SHIFT"
                else:
                    kind = "SPIKE" if z[peak_i] > 0 else "DROP"
                if kind == "LEVEL_SHIFT" and sign == 0:
                    kind = "SPIKE" if z[peak_i] > 0 else "DROP"
                events.append(_Event(col, a, b, kind, round(float(abs(z[peak_i])), 4), float(s.iloc[peak_i]),
                                     float(base[peak_i]), [{"seriesKey": col, "weight": 1.0}]))
            for ev in events:
                if ev.series_key == col and ev.index is None:
                    ev.index = s.index
        return events, scores

    # ---- ML(Isolation Forest, ANA-02.03) ----
    def _features(self, frame: pd.DataFrame, profiles: dict[str, SeasonalProfile], sigmas: dict[str, float]) -> pd.DataFrame:
        cols = {}
        for col in frame.columns:
            s = frame[col]
            base = profiles[col].baseline(s.index)
            cols[col] = (s.to_numpy() - base) / sigmas[col]
        feats = pd.DataFrame(cols, index=frame.index)
        return feats.interpolate(limit=3, limit_direction="both").dropna()

    def fit(self, data: InputData, params: Params, ctx: RunContext) -> TrainedModel | None:
        frame = data.role("target")
        labels = ctx.labels or []
        frame = _exclude_false_positive_points(frame, labels, mark_normal=True)
        model = self._fit_ml(frame, params, ctx)
        events, _ = self._score_ml(frame, params, ctx, model)
        metrics = {"anomalies": len(events), "precision": None, "recall": None}
        precision, recall = _label_metrics(events, frame.index, labels)
        if precision is not None:
            metrics.update({"precision": precision, "recall": recall})
        return TrainedModel(payload=model, metrics=metrics, params=params.model_dump())

    def compare_models(self, new_metrics: dict, active_metrics: dict) -> bool:
        new_p, old_p = new_metrics.get("precision"), active_metrics.get("precision")
        if new_p is None or old_p is None:
            return True
        return float(new_p) >= float(old_p)

    def _fit_ml(self, frame: pd.DataFrame, params: Params, ctx: RunContext) -> dict:
        profiles, sigmas = {}, {}
        for col in frame.columns:
            s = frame[col].dropna()
            profiles[col] = fit_profile(s, ctx.timezone, params.period)
            r = s.to_numpy() - profiles[col].baseline(s.index)
            sigmas[col] = robust_sigma(r) or 1.0
        feats = self._features(frame, profiles, sigmas)
        x = feats.to_numpy()
        mcd = MinCovDet(random_state=ctx.seed, support_fraction=0.9).fit(x)
        prec = np.linalg.pinv(mcd.covariance_)
        w = np.linalg.cholesky(prec + 1e-9 * np.eye(prec.shape[0]))
        xw = (x - mcd.location_) @ w
        forest = IsolationForest(n_estimators=200, random_state=ctx.seed, n_jobs=1).fit(xw)
        raw = -forest.score_samples(xw)
        med = float(np.median(raw))
        sig = robust_sigma(raw) or 1e-9
        # 조건부 기여도: 각 항목을 나머지 항목으로 선형 예측한 잔차
        coefs = {}
        for j, col in enumerate(feats.columns):
            others = np.delete(x, j, axis=1)
            design = np.column_stack([np.ones(len(x)), others])
            beta, *_ = np.linalg.lstsq(design, x[:, j], rcond=None)
            res = x[:, j] - design @ beta
            coefs[col] = (beta, robust_sigma(res) or 1.0)
        return {"profiles": {c: p.to_json() for c, p in profiles.items()}, "sigmas": sigmas, "location": mcd.location_,
                "whiten": w, "forest": forest, "scoreMedian": med, "scoreSigma": sig, "columns": list(feats.columns),
                "coefs": coefs}

    def _score_ml(self, frame: pd.DataFrame, params: Params, ctx: RunContext, model: dict):
        profiles = {c: SeasonalProfile.from_json(p) for c, p in model["profiles"].items()}
        cols = [c for c in model["columns"] if c in frame.columns]
        if len(cols) != len(model["columns"]):
            model = self._fit_ml(frame, params, ctx)
            profiles = {c: SeasonalProfile.from_json(p) for c, p in model["profiles"].items()}
            cols = model["columns"]
        feats = self._features(frame[cols], profiles, model["sigmas"])
        x = feats.to_numpy()
        xw = (x - model["location"]) @ model["whiten"]
        raw = -model["forest"].score_samples(xw)
        z = (raw - model["scoreMedian"]) / model["scoreSigma"]
        step = median_step(feats.index)
        window = steps_for(pd.Timedelta(hours=1), step, minimum=3)
        zs = pd.Series(z).rolling(window, center=True, min_periods=1).median().to_numpy()
        threshold = ML_THRESHOLDS[params.sensitivity]
        min_minutes = params.minDurationMinutes or 30
        min_points = steps_for(pd.Timedelta(minutes=min_minutes), step, minimum=1)
        events = []
        for a, b in group_runs(zs > threshold, max_gap=window):
            if b - a + 1 < min_points:
                continue
            contrib = {}
            for j, col in enumerate(cols):
                beta, sd = model["coefs"][col]
                others = np.delete(x[a:b + 1], j, axis=1)
                design = np.column_stack([np.ones(len(others)), others])
                contrib[col] = float(np.mean(np.abs(x[a:b + 1, j] - design @ beta)) / sd)
            total = sum(contrib.values()) or 1.0
            contributors = sorted(({"seriesKey": c, "weight": round(v / total, 4)} for c, v in contrib.items()),
                                  key=lambda c: -c["weight"])
            peak = a + int(np.argmax(zs[a:b + 1]))
            ev = _Event("|".join(cols), a, b, "COMBINATION", round(float(zs[peak]), 4), float("nan"), float("nan"), contributors)
            ev.index = feats.index
            events.append(ev)
        return events, {"combined": pd.Series(zs, index=feats.index)}

    def _run_ml(self, frame: pd.DataFrame, params: Params, ctx: RunContext):
        model = ctx.model if isinstance(ctx.model, dict) and "forest" in ctx.model else None
        if model is None:
            model = self._fit_ml(_exclude_false_positive_points(frame, ctx.labels, mark_normal=True), params, ctx)
        ctx.checkpoint()
        return self._score_ml(frame, params, ctx, model)

    # ---- 결과 ----
    def _result(self, data: InputData, frame: pd.DataFrame, events: list[_Event], scores: dict[str, pd.Series],
                params: Params) -> Result:
        threshold = (ML_THRESHOLDS if params.method == "ISOLATION_FOREST" else STAT_THRESHOLDS)[params.sensitivity]
        rows, markers, regions = [], [], []
        events.sort(key=lambda e: (e.index[e.i0], e.series_key))
        for n, ev in enumerate(events, start=1):
            idx = ev.index
            start, end = idx[ev.i0], idx[ev.i1]
            row = {"id": str(n), "time": start, "end": end, "seriesKey": ev.series_key,
                   "label": data.label(ev.series_key) if "|" not in ev.series_key else "여러 항목",
                   "value": None if np.isnan(ev.value) else round(ev.value, 6), "score": ev.score, "kind": ev.kind,
                   "kindLabel": KIND_LABELS.get(ev.kind, "조합 이상"),
                   "baseline": None if np.isnan(ev.baseline) else round(ev.baseline, 6), "threshold": threshold,
                   "contributors": ev.contributors}
            rows.append(row)
            if ev.i0 == ev.i1 and ev.kind in ("SPIKE", "DROP"):
                markers.append({"x": start, "y": row["value"], "label": KIND_LABELS[ev.kind], "severity": "CRITICAL",
                                "seriesKey": ev.series_key})
            else:
                regions.append({"from": start, "to": end, "label": KIND_LABELS.get(ev.kind, "조합 이상"), "severity": "WARN"})
        series = [{"key": c, "label": data.label(c), "data": ts_points(frame[c])} for c in frame.columns]
        unit = data.unit(frame.columns[0]) if len(frame.columns) else None
        charts = [chart("raw", "line", "원본과 이상 지점", x_axis=time_axis(), y_axis=value_axis("값", unit), series=series,
                        markers=markers, regions=regions)]
        score_series = [{"key": k, "label": f"{data.label(k) if k in data.series else '조합'} 점수", "data": ts_points(v, 4)}
                        for k, v in scores.items()]
        charts.append(chart("score", "line", "이상 점수 추이", x_axis=time_axis(), y_axis=value_axis("점수"), series=score_series,
                            thresholds=[{"value": threshold, "label": "임계값"}]))
        tables = [table("anomalies", "이상 목록", [("time", "시각", "datetime"), ("end", "끝", "datetime"), ("label", "대상", "string"),
                                                  ("value", "값", "number"), ("score", "점수", "number"), ("kindLabel", "유형", "string"),
                                                  ("baseline", "기준선", "number"), ("threshold", "임계값", "number")], rows)]
        total = len(events)
        by_kind = {k: sum(1 for e in events if e.kind == k) for k in ("SPIKE", "DROP", "LEVEL_SHIFT", "VARIANCE", "COMBINATION")}
        days = max(1, round((data.period_to - data.period_from).total_seconds() / 86400))
        headline = f"최근 {days}일 중 이상 {total}건" if total else f"최근 {days}일 동안 평소와 다른 값이 보이지 않습니다"
        metrics = [Metric("anomalies", "이상 건수", total, None, "WARN" if total else "OK")]
        metrics += [Metric(f"anomalies{k.title().replace('_', '')}", f"{KIND_LABELS.get(k, '조합 이상')} 건수", v) for k, v in by_kind.items() if v]
        contributors = rows[0]["contributors"] if rows else [{"seriesKey": c, "weight": round(1 / max(1, len(frame.columns)), 4)} for c in frame.columns]
        evidence = {"baseline": "계절(일·주) 로버스트 중앙값" if params.method == "STATISTICAL" else "학습 구간 이상 점수 분포",
                    "threshold": threshold, "contributors": contributors,
                    "method": params.method, "sensitivity": params.sensitivity}
        return Result(headline=headline, level="WARN" if total else "OK", metrics=metrics, charts=charts, tables=tables,
                      evidence=evidence, caveats=list(self.fixed_caveats),
                      provenance={"algorithm": "STL형 계절 제거 + MAD z-score" if params.method == "STATISTICAL"
                                  else "Isolation Forest(로버스트 백색화)", "method": params.method})

    # ------------------------------------------------------------------ 실시간(ANA-06.01)
    def realtime_init(self, history: pd.Series, params: Params, model, seed: int):
        s = history.dropna()
        if len(s) < 30:
            raise BusinessError(ErrorCode.ANALYSIS_INSUFFICIENT_DATA, {"reason": "실시간 기준을 만들 최근 데이터가 부족합니다"})
        profile = fit_profile(s, "Asia/Seoul", params.period)
        resid = s.to_numpy() - profile.baseline(s.index)
        offset = float(np.median(resid))
        sigma = robust_sigma(resid - offset) or 1e-9
        return {"profile": profile, "offset": offset, "sigma": sigma, "run": 0, "lastEmit": None, "lastScore": 0.0}

    def infer(self, state: dict, at: pd.Timestamp, value: float, params: Params) -> dict | None:
        base = state["profile"].value_at(at) + state["offset"]
        z = (value - base) / state["sigma"]
        threshold = STAT_THRESHOLDS[params.sensitivity]
        if abs(z) <= threshold:
            state["run"] = 0
            return None
        state["run"] += 1
        kind = "LEVEL_SHIFT" if state["run"] >= 6 else ("SPIKE" if z > 0 else "DROP")
        last = state["lastEmit"]
        if last is not None and (at - last) < pd.Timedelta(minutes=params.cooldownMinutes) and abs(z) < 1.5 * state["lastScore"]:
            return None
        state["lastEmit"], state["lastScore"] = at, abs(z)
        return {"type": "ANOMALY", "value": float(value), "score": round(float(abs(z)), 4), "kind": kind,
                "evidence": {"baseline": round(base, 6), "threshold": threshold, "contributors": []}}


def _exclude_false_positive_points(frame: pd.DataFrame, labels: list[dict], mark_normal: bool) -> pd.DataFrame:
    """오탐(FALSE_POSITIVE) 표시 지점은 다음 학습에서 정상 데이터로 쓴다(ANA-07.05). 맞음(TRUE_POSITIVE) 지점은 학습에서 뺀다."""
    if not labels:
        return frame
    out = frame.copy()
    step = median_step(frame.index)
    for lab in labels:
        if lab.get("verdict") == "TRUE_POSITIVE":
            t = pd.Timestamp(lab["occurredAt"])
            mask = (out.index >= t - step) & (out.index <= t + step)
            out.loc[mask] = np.nan
    return out


def _label_metrics(events: list[_Event], index: pd.DatetimeIndex, labels: list[dict]):
    """라벨이 있으면 정밀도·재현율(ANA-07.02). 없으면 (None, None)."""
    if not labels:
        return None, None
    windows = []
    for ev in events:
        idx = ev.index
        windows.append((idx[ev.i0], idx[ev.i1]))
    tp_labels = [pd.Timestamp(lab["occurredAt"]) for lab in labels if lab.get("verdict") == "TRUE_POSITIVE"]
    fp_labels = [pd.Timestamp(lab["occurredAt"]) for lab in labels if lab.get("verdict") == "FALSE_POSITIVE"]
    step = median_step(index)

    def hit(t: pd.Timestamp) -> bool:
        return any(a - step <= t <= b + step for a, b in windows)

    detected_tp = sum(1 for t in tp_labels if hit(t))
    detected_fp = sum(1 for t in fp_labels if hit(t))
    precision = detected_tp / (detected_tp + detected_fp) if (detected_tp + detected_fp) else None
    recall = detected_tp / len(tp_labels) if tp_labels else None
    return precision, recall


TEMPLATE = AnomalyDetect()
