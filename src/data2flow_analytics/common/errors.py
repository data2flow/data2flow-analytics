"""오류 코드(api-rules.md §4, ANA domain-model 오류 표). 코드 하나는 HTTP 상태 하나에만 대응한다.

resultMessage는 Accept-Language(ko·en·ja·zh)로 현지화하고, 번역이 없으면 ja·zh → en → ko 순서로 폴백한다(ADR-037).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class _Code:
    status: int
    ko: str
    en: str
    ja: str | None = None
    zh: str | None = None


class ErrorCode(Enum):
    # 공통(api-rules.md §4.2)
    INVALID_REQUEST = _Code(400, "입력값을 확인하세요.", "Check the input.", "入力値を確認してください。", "请检查输入值。")
    AUTH_TOKEN_INVALID = _Code(401, "인증 정보가 올바르지 않습니다.", "Invalid authentication.", "認証情報が正しくありません。", "认证信息无效。")
    PERMISSION_DENIED = _Code(403, "권한이 없습니다.", "Permission denied.", "権限がありません。", "没有权限。")
    RESOURCE_NOT_FOUND = _Code(404, "대상을 찾을 수 없습니다.", "Resource not found.", "対象が見つかりません。", "找不到资源。")
    VERSION_CONFLICT = _Code(409, "다른 사용자가 먼저 바꿨습니다. 새로 고친 뒤 다시 시도하세요.",
                             "Someone else changed it first. Refresh and try again.")
    INTERNAL_ERROR = _Code(500, "처리 중 오류가 발생했습니다.", "An internal error occurred.", "内部エラーが発生しました。", "发生内部错误。")
    SERVICE_UNAVAILABLE = _Code(503, "잠시 후 다시 시도하세요.", "Try again later.")
    # ANA(spec/detail/ANA/domain-model.md 오류 코드)
    TEMPLATE_NOT_FOUND = _Code(404, "템플릿을 찾을 수 없습니다", "Template not found", "テンプレートが見つかりません", "找不到模板")
    TEMPLATE_DISABLED = _Code(409, "비활성화된 템플릿입니다", "The template is disabled", "無効化されたテンプレートです", "模板已停用")
    ANALYSIS_NOT_FOUND = _Code(404, "분석을 찾을 수 없습니다", "Analysis not found", "分析が見つかりません", "找不到分析")
    ANALYSIS_BINDING_INVALID = _Code(400, "{role} 역할에 {min}~{max}개의 {semantic} 데이터를 연결하세요",
                                     "Connect {min}–{max} {semantic} series to the {role} role")
    ANALYSIS_PARAMS_INVALID = _Code(400, "입력값을 확인하세요: {field}", "Check the input: {field}")
    ANALYSIS_INSUFFICIENT_DATA = _Code(409, "데이터가 부족합니다: {reason}", "Not enough data: {reason}")
    ANALYSIS_WARNING_NOT_ACKNOWLEDGED = _Code(400, "데이터 경고를 확인한 뒤 실행하세요", "Acknowledge the data warnings before running")
    ANALYSIS_LIMIT_EXCEEDED = _Code(409, "데이터가 너무 많습니다. 집계 단위를 {suggest} 이상으로 올리세요",
                                    "Too much data. Raise the resolution to {suggest} or coarser")
    ANALYSIS_TIMEOUT = _Code(408, "분석 시간이 초과되었습니다. 기간을 줄이거나 집계 단위를 올리세요",
                             "The analysis timed out. Shorten the period or raise the resolution")
    ANALYSIS_RUN_NOT_FOUND = _Code(404, "실행 기록을 찾을 수 없습니다", "Run not found", "実行記録が見つかりません", "找不到运行记录")
    ANALYSIS_RUN_STATE_CONFLICT = _Code(409, "이미 끝난 실행입니다", "The run has already finished")
    ANALYSIS_MODEL_REQUIRED = _Code(409, "먼저 모델을 학습하세요", "Train a model first")
    ANALYSIS_REALTIME_NOT_SUPPORTED = _Code(400, "이 템플릿은 실시간 적용을 지원하지 않습니다", "This template does not support real-time mode")
    ML_MODEL_NOT_FOUND = _Code(404, "분석 모델을 찾을 수 없습니다", "Analysis model not found")
    MODEL_WORSE_THAN_ACTIVE = _Code(409, "현재 모델보다 성능이 낮습니다. 강제 적용하려면 확인하세요",
                                    "The model is worse than the active one. Confirm to force it")
    ANALYSIS_SCHEDULE_INVALID = _Code(400, "일정 형식이 올바르지 않습니다", "The schedule format is invalid")
    DATASET_NOT_FOUND = _Code(404, "데이터셋을 찾을 수 없습니다", "Dataset not found")
    DATASET_IN_USE = _Code(409, "분석이 쓰고 있는 데이터셋은 지울 수 없습니다", "The dataset is used by an analysis")
    INTERVENTION_POINT_REQUIRED = _Code(400, "개입 시점을 지정하세요", "Specify the intervention point")
    ANALYSIS_EXPORT_TOO_LARGE = _Code(409, "내보낼 데이터가 너무 많습니다. 기간을 줄이세요", "Too much data to export. Shorten the period")

    @property
    def status(self) -> int:
        return self.value.status

    def message(self, lang: str = "ko", **args: Any) -> str:
        c = self.value
        text = {"ko": c.ko, "en": c.en, "ja": c.ja or c.en, "zh": c.zh or c.en}.get(lang, c.ko)
        return text.format_map(_SafeArgs(args))


class _SafeArgs(dict):
    """빠진 치환 인자는 '{이름}' 그대로 둔다."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class FieldError:
    field: str
    code: str
    message: str

    def to_json(self) -> dict:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass
class BusinessError(Exception):
    """서비스 어디서나 던지는 업무 예외. API 계층이 공통 오류 응답으로 바꾼다."""

    code: ErrorCode
    args_: dict[str, Any] = field(default_factory=dict)
    errors: list[FieldError] = field(default_factory=list)
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code.name}: {self.code.message('ko', **self.args_)}"


def pick_language(accept_language: str | None) -> str:
    """Accept-Language에서 지원 언어(ko·en·ja·zh)를 고른다. 없으면 ko."""
    if not accept_language:
        return "ko"
    for part in accept_language.split(","):
        tag = part.split(";")[0].strip().lower()
        primary = tag.split("-")[0]
        if primary in ("ko", "en", "ja", "zh"):
            return primary
    return "ko"
