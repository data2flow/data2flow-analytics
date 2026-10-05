"""입력 역할 바인딩(domain-model Binding, BR-ANA-02).

바인딩: [{role, sources:[{kind: DEVICE_METRIC|SPACE_AGGREGATE|DERIVED_METRIC, deviceId?, spaceId?, metricKey, agg?, label?, semantic?}]}]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..clock import parse_iso
from ..common.errors import BusinessError, ErrorCode, FieldError
from ..templates.base import RoleSpec
from .core_client import METRIC_SEMANTIC

KINDS = ("DEVICE_METRIC", "SPACE_AGGREGATE", "DERIVED_METRIC")
AGGS = ("avg", "max", "min")
RESOLUTIONS = ("AUTO", "RAW", "1m", "1h", "1d")


@dataclass(frozen=True)
class Source:
    role: str
    kind: str
    metric_key: str
    device_id: int | None = None
    space_id: int | None = None
    agg: str = "avg"
    label: str | None = None
    semantic: str | None = None

    @property
    def key(self) -> str:
        if self.device_id is not None and self.kind != "SPACE_AGGREGATE":
            return f"dev:{self.device_id}:{self.metric_key}"
        return f"space:{self.space_id}:{self.metric_key}:{self.agg}"

    @property
    def display(self) -> str:
        if self.label:
            return self.label
        if self.device_id is not None and self.kind != "SPACE_AGGREGATE":
            return f"기기 {self.device_id} {self.metric_key}"
        return f"공간 {self.space_id} {self.metric_key}({self.agg})"

    @property
    def inferred_semantic(self) -> str | None:
        return self.semantic or METRIC_SEMANTIC.get(self.metric_key.lower())


def _int(value, field: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError as exc:
        raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(field, "Pattern", "숫자 ID여야 합니다")]) from exc


def parse_bindings(raw: list[dict] | None) -> list[Source]:
    if not isinstance(raw, list):
        raise BusinessError(ErrorCode.ANALYSIS_BINDING_INVALID, {"role": "bindings", "min": 1, "max": 50, "semantic": ""})
    out: list[Source] = []
    for i, b in enumerate(raw):
        role = (b or {}).get("role")
        if not role:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(f"bindings[{i}].role", "NotBlank", "역할을 지정하세요")])
        for j, s in enumerate(b.get("sources") or []):
            kind = s.get("kind", "DEVICE_METRIC")
            fld = f"bindings[{i}].sources[{j}]"
            if kind not in KINDS:
                raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(f"{fld}.kind", "Enum", "알 수 없는 연결 종류")])
            metric = s.get("metricKey")
            device_id = _int(s.get("deviceId"), f"{fld}.deviceId")
            space_id = _int(s.get("spaceId"), f"{fld}.spaceId")
            agg = (s.get("agg") or "avg").lower()
            if not metric or agg not in AGGS:
                raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(f"{fld}.metricKey", "NotBlank", "측정 항목을 지정하세요")])
            if kind == "DEVICE_METRIC" and device_id is None:
                raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(f"{fld}.deviceId", "NotNull", "기기를 지정하세요")])
            if kind == "SPACE_AGGREGATE" and space_id is None:
                raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(f"{fld}.spaceId", "NotNull", "공간을 지정하세요")])
            if kind == "DERIVED_METRIC" and device_id is None and space_id is None:
                raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError(f"{fld}.deviceId", "NotNull", "기기나 공간을 지정하세요")])
            out.append(Source(role, kind, str(metric), device_id, space_id, agg, s.get("label"), s.get("semantic")))
    return out


def validate_roles(sources: list[Source], roles: list[RoleSpec]) -> None:
    """역할 개수(min~max)와 의미 조건(semantic)을 검사한다(BR-ANA-02, TC-ANA-021)."""
    by_role: dict[str, list[Source]] = {}
    for s in sources:
        by_role.setdefault(s.role, []).append(s)
    known = {r.name for r in roles}
    for name in by_role:
        if name not in known:
            raise BusinessError(ErrorCode.ANALYSIS_BINDING_INVALID, {"role": name, "min": 0, "max": 0, "semantic": "(없는 역할)"})
    for role in roles:
        items = by_role.get(role.name, [])
        lo = role.min if role.required else 0
        if not (lo <= len(items) <= role.max):
            raise BusinessError(ErrorCode.ANALYSIS_BINDING_INVALID,
                                {"role": role.name, "min": max(role.min, 1 if role.required else 0), "max": role.max,
                                 "semantic": role.semantic or "수치"})
        if role.semantic:
            for s in items:
                sem = s.inferred_semantic
                if sem is not None and sem != role.semantic:
                    raise BusinessError(ErrorCode.ANALYSIS_BINDING_INVALID,
                                        {"role": role.name, "min": role.min, "max": role.max, "semantic": role.semantic})


def binding_spaces(sources: list[Source]) -> list[int]:
    return sorted({s.space_id for s in sources if s.space_id is not None})


def resolve_period(period: dict | None, now: datetime) -> tuple[datetime, datetime]:
    """{type: RELATIVE, days} 또는 {type: FIXED, from, to} → (from, to). RELATIVE는 지금(분 단위 내림) 기준."""
    if not isinstance(period, dict):
        raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("period", "NotNull", "기간을 지정하세요")])
    kind = period.get("type", "RELATIVE")
    if kind == "RELATIVE":
        try:
            days = float(period.get("days"))
        except (TypeError, ValueError) as exc:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("period.days", "NotNull", "일수를 지정하세요")]) from exc
        if days <= 0:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("period.days", "Min", "1일 이상")])
        end = now.replace(second=0, microsecond=0)
        return end - timedelta(days=days), end
    if kind == "FIXED":
        try:
            start, end = parse_iso(period["from"]), parse_iso(period["to"])
        except (KeyError, ValueError) as exc:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("period", "Pattern", "ISO-8601 시각")]) from exc
        if start >= end:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("period", "Order", "시작이 끝보다 앞서야 합니다")])
        return start, end
    raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("period.type", "Enum", "RELATIVE 또는 FIXED")])
