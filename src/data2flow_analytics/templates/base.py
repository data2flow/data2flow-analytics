"""템플릿 플러그인 계약(design/analytics-service.md §4).

템플릿은 측정 항목 이름 대신 **입력 역할**을 선언하고(ANA-01.05), 공통 로더가 만든 정렬된 DataFrame을 받는다. 템플릿 코드는 DB를 모른다.
결과는 공통 렌더러용 형식(ANA-api §4 Result·ChartSpec)으로 돌려준다.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from ..common.errors import BusinessError, ErrorCode
from ..common.jsonutil import to_jsonable


class Category(enum.StrEnum):
    ENV_QUALITY = "ENV_QUALITY"
    SPACE_USAGE = "SPACE_USAGE"
    ASSET_HEALTH = "ASSET_HEALTH"
    PREDICTION = "PREDICTION"
    GENERAL = "GENERAL"


class Kind(enum.StrEnum):
    GENERAL = "GENERAL"
    DOMAIN = "DOMAIN"


@dataclass(frozen=True)
class RoleSpec:
    """입력 역할(domain-model RoleSpec). type: series | multi_series | event | group"""

    name: str
    type: str = "series"
    min: int = 1
    max: int = 1
    semantic: str | None = None
    required: bool = True
    label: str = ""

    def to_json(self) -> dict:
        out = {"name": self.name, "type": self.type, "min": self.min, "max": self.max, "required": self.required,
               "label": self.label or self.name}
        if self.semantic:
            out["semantic"] = self.semantic
        return out


@dataclass(frozen=True)
class Requirements:
    """최소 데이터 조건(BR-ANA-04, ANA-08.01)과 실행 한도(BR-ANA-07·09)."""

    min_period_days: float
    min_points: int
    missing_warn: float = 0.10
    missing_fail: float = 0.20
    recommended_resolution: str = "1h"
    max_points: int = 5_000_000
    max_series: int = 50
    max_period_days: int | None = 366
    timeout_sec: int = 600

    def to_json(self) -> dict:
        return {
            "minPeriodDays": self.min_period_days,
            "minPoints": self.min_points,
            "missingWarn": self.missing_warn,
            "missingFail": self.missing_fail,
            "maxPoints": self.max_points,
            "maxSeries": self.max_series,
            "maxPeriodDays": self.max_period_days,
            "recommendedResolution": self.recommended_resolution,
            "timeoutSec": self.timeout_sec,
        }


@dataclass(frozen=True)
class SeriesInfo:
    """DataFrame 열 하나(시계열 하나)의 출처."""

    key: str
    role: str
    label: str
    kind: str = "DEVICE_METRIC"
    device_id: int | None = None
    space_id: int | None = None
    metric_key: str | None = None
    unit: str | None = None
    semantic: str | None = None

    def to_json(self) -> dict:
        return to_jsonable({"seriesKey": self.key, "role": self.role, "label": self.label, "kind": self.kind,
                            "deviceId": str(self.device_id) if self.device_id is not None else None,
                            "spaceId": str(self.space_id) if self.space_id is not None else None,
                            "metricKey": self.metric_key, "unit": self.unit})


@dataclass
class InputData:
    """공통 로더가 만든 입력. frames[역할]은 UTC DatetimeIndex, 열은 시계열 키."""

    frames: dict[str, pd.DataFrame]
    series: dict[str, SeriesInfo]
    period_from: datetime
    period_to: datetime
    resolution: str | None = "1h"  # None이면 원본(RAW)
    quality: dict[str, pd.DataFrame] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def role(self, name: str) -> pd.DataFrame:
        frame = self.frames.get(name)
        if frame is None:
            return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
        return frame

    def has_role(self, name: str) -> bool:
        frame = self.frames.get(name)
        return frame is not None and frame.shape[1] > 0

    def series_of(self, role: str) -> list[SeriesInfo]:
        frame = self.role(role)
        return [self.series[c] for c in frame.columns if c in self.series]

    def label(self, key: str) -> str:
        info = self.series.get(key)
        return info.label if info else key

    def unit(self, key: str) -> str | None:
        info = self.series.get(key)
        return info.unit if info else None

    @property
    def step(self) -> pd.Timedelta | None:
        for frame in self.frames.values():
            if len(frame.index) > 2:
                diffs = np.diff(frame.index.values).astype("timedelta64[ns]").astype(np.int64)
                if len(diffs):
                    return pd.Timedelta(int(np.median(diffs)), unit="ns")
        return None

    def total_points(self) -> int:
        return int(sum(int(f.notna().sum().sum()) for f in self.frames.values()))


class CancelledError(Exception):
    """협조적 취소(ANA-04.04). 체크포인트에서 던진다."""


@dataclass
class RunContext:
    seed: int
    now: datetime
    timezone: str = "Asia/Seoul"
    model: Any = None  # 활성 모델(학습형). 없으면 None
    labels: list[dict] = field(default_factory=list)  # 이상 피드백 라벨(ANA-07.05)
    cancel_check: Callable[[], bool] = lambda: False
    progress: Callable[[int], None] = lambda p: None

    def rng(self) -> np.random.Generator:
        return np.random.default_rng(self.seed)

    def checkpoint(self) -> None:
        if self.cancel_check():
            raise CancelledError()


@dataclass
class Metric:
    key: str
    label: str
    value: Any
    unit: str | None = None
    level: str | None = None

    def to_json(self) -> dict:
        out = {"key": self.key, "label": self.label, "value": to_jsonable(self.value), "unit": self.unit}
        if self.level:
            out["level"] = self.level
        return out


@dataclass
class Result:
    """템플릿 결과. to_json()이 ANA-api §4.1 모양을 만든다(provenance는 실행기가 채운다)."""

    headline: str
    level: str = "OK"
    metrics: list[Metric] = field(default_factory=list)
    charts: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    evidence: dict | None = None
    caveats: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)  # 템플릿이 덧붙이는 출처(방식, 기준 등)

    def to_json(self) -> dict:
        summary: dict[str, Any] = {"headline": self.headline, "level": self.level, "metrics": [m.to_json() for m in self.metrics]}
        if self.flags:
            summary["flags"] = list(self.flags)
        return to_jsonable({
            "summary": summary,
            "charts": self.charts,
            "tables": self.tables,
            "evidence": self.evidence,
            "caveats": self.caveats,
            "provenance": self.provenance,
        })


@dataclass
class TrainedModel:
    """학습 결과(ANA-07.01). payload는 피클로 저장소에 올린다."""

    payload: Any
    metrics: dict
    params: dict = field(default_factory=dict)


class TemplateParams(BaseModel):
    """파라미터 모델의 기본. 모든 필드에 기본값과 설명을 둔다(TC-ANA-008)."""

    model_config = ConfigDict(extra="forbid")


class Template(ABC):
    key: ClassVar[str]
    version: ClassVar[str] = "1.0.0"
    name: ClassVar[str]
    kind: ClassVar[Kind] = Kind.GENERAL
    category: ClassVar[Category] = Category.GENERAL
    roles: ClassVar[list[RoleSpec]]
    params_model: ClassVar[type[TemplateParams]] = TemplateParams
    requirements: ClassVar[Requirements]
    fast: ClassVar[bool] = True
    realtime: ClassVar[bool] = False
    trainable: ClassVar[bool] = False
    sample_image_url: ClassVar[str | None] = None
    #: 결과에 항상 넣는 해석 주의 문구(ANA-08.05)
    fixed_caveats: ClassVar[list[str]] = []
    #: 품질 코드가 정상이 아닌 값도 필요한 템플릿(센서 건강의 범위 초과율)
    needs_quality: ClassVar[bool] = False
    #: 출력 ChartSpec 타입(TC-ANA-008 outputs)
    outputs: ClassVar[list[str]] = []
    #: 예측형(BR-ANA-23, TC-ANA-159: 신뢰구간 또는 "추정" 표시 필수)
    predictive: ClassVar[bool] = False

    guide_path: Path | None = None  # 레지스트리가 채운다

    # ---- 계약 ----
    def params_schema(self) -> dict:
        schema = self.params_model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        return schema

    def parse_params(self, raw: dict | None) -> TemplateParams:
        from pydantic import ValidationError

        try:
            return self.params_model.model_validate(raw or {})
        except ValidationError as exc:
            first = exc.errors()[0]
            fld = ".".join(str(p) for p in first.get("loc", ())) or "params"
            raise BusinessError(ErrorCode.ANALYSIS_PARAMS_INVALID, {"field": fld},
                                errors=_field_errors(exc)) from exc

    def requirements_for(self, params: TemplateParams) -> Requirements:
        return self.requirements

    def roles_for(self, params: TemplateParams) -> list[RoleSpec]:
        return self.roles

    def validate_inputs(self, bindings: list[dict], params: TemplateParams) -> None:
        """BR-ANA-02 밖의 템플릿 고유 검사(예: ML 방식은 측정 항목 2개 이상). 어기면 BusinessError."""
        return None

    def extra_issues(self, period_from: datetime, period_to: datetime, params: TemplateParams) -> list[dict]:
        """충분성 확인에 더할 템플릿 고유 이슈(예: 개입 후 7일). [{code, severity, message}]"""
        return []

    @abstractmethod
    def run(self, data: InputData, params: TemplateParams, ctx: RunContext) -> Result: ...

    def fit(self, data: InputData, params: TemplateParams, ctx: RunContext) -> TrainedModel | None:
        return None

    def compare_models(self, new_metrics: dict, active_metrics: dict) -> bool:
        """새 모델이 활성 모델과 같거나 좋으면 True(BR-ANA-12). 기본은 MAE가 작거나 같을 때."""
        new, old = new_metrics.get("mae"), active_metrics.get("mae")
        if new is None or old is None:
            return True
        return float(new) <= float(old)

    # ---- 실시간(ANA-06) ----
    def realtime_init(self, history: pd.Series, params: TemplateParams, model: Any, seed: int) -> Any:
        raise BusinessError(ErrorCode.ANALYSIS_REALTIME_NOT_SUPPORTED)

    def infer(self, state: Any, at: pd.Timestamp, value: float, params: TemplateParams) -> dict | None:
        raise BusinessError(ErrorCode.ANALYSIS_REALTIME_NOT_SUPPORTED)

    def realtime_requires_model(self, params: TemplateParams) -> bool:
        return False

    # ---- 표시 ----
    def manifest(self) -> dict:
        return {
            "key": self.key,
            "version": self.version,
            "name": self.name,
            "kind": self.kind.value,
            "category": self.category.value,
            "roles": [r.to_json() for r in self.roles],
            "requirements": self.requirements.to_json(),
            "paramsSchema": self.params_schema(),
            "fast": self.fast,
            "realtime": self.realtime,
            "trainable": self.trainable,
            "sampleImageUrl": self.sample_image_url,
            "outputs": list(self.outputs),
        }


def _field_errors(exc) -> list:
    from ..common.errors import FieldError

    out = []
    for err in exc.errors():
        fld = ".".join(str(p) for p in err.get("loc", ())) or "params"
        out.append(FieldError(field=f"params.{fld}", code=str(err.get("type", "invalid")), message=str(err.get("msg", ""))))
    return out


# ---------------------------------------------------------------------------
# ChartSpec 도우미(ANA-api §4.2). 새 템플릿이 이 타입만 쓰면 프론트엔드 변경이 필요 없다(ANA-05.02)
# ---------------------------------------------------------------------------
CHART_TYPES = ("line", "band", "scatter", "bar", "heatmap", "calendar", "gauge", "table", "timeline")


def ts_points(series: pd.Series, digits: int = 6) -> list[list]:
    """시계열을 [[ISO 시각, 값], …]으로. 누락은 null(gaps=true면 끊어 그림)."""
    out = []
    for t, v in series.items():
        val = None if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v) else round(float(v), digits)
        out.append([to_jsonable(pd.Timestamp(t)), val])
    return out


def chart(chart_id: str, chart_type: str, title: str, *, x_axis: dict | None = None, y_axis: dict | None = None,
          series: list[dict] | None = None, **extra: Any) -> dict:
    if chart_type not in CHART_TYPES:
        raise ValueError(f"unknown chart type {chart_type}")
    out: dict[str, Any] = {"id": chart_id, "type": chart_type, "title": title}
    if x_axis:
        out["xAxis"] = x_axis
    if y_axis:
        out["yAxis"] = y_axis
    out["series"] = series or []
    for k in ("bands", "markers", "regions", "thresholds"):
        out[k] = extra.pop(k, [])
    out["gaps"] = extra.pop("gaps", True)
    out.update(extra)
    return out


def table(table_id: str, title: str, columns: list[tuple[str, str, str]], rows: list[dict]) -> dict:
    return {
        "id": table_id,
        "title": title,
        "columns": [{"key": k, "label": label, "type": t} for k, label, t in columns],
        "rows": to_jsonable(rows),
    }


def time_axis(label: str = "시각") -> dict:
    return {"type": "time", "label": label}


def value_axis(label: str = "값", unit: str | None = None) -> dict:
    return {"type": "value", "label": label, "unit": unit}


def robust_sigma(values: np.ndarray) -> float:
    """MAD 기반 표준편차 추정(1.4826 × MAD)."""
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if v.size == 0:
        return float("nan")
    mad = float(np.median(np.abs(v - np.median(v))))
    return 1.4826 * mad
