"""JSON 정규화와 재현성 해시(BR-ANA-10, ANA test-plan "결정성").

결과 JSON은 키를 정렬하고 부동소수를 유효 숫자 12자리로 맞춘 뒤 SHA-256을 낸다. 같은 입력·시드면 해시가 같아야 한다.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from ..clock import iso


def to_jsonable(value: Any) -> Any:
    """numpy·pandas 값을 JSON 기본 타입으로 바꾼다. NaN·무한대는 null."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else iso(value.to_pydatetime(warn=False))
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [to_jsonable(v) for v in value.tolist()]
    if value is pd.NaT:
        return None
    return str(value)


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        return float(f"{value:.12g}")
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_normalize(to_jsonable(value)), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False)
