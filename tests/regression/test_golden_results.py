"""골든 결과 회귀(TC-ANA-182): 템플릿별 고정 입력 결과를 golden/*.json과 비교한다.

의존성(numpy·scikit-learn·statsmodels) 업그레이드 PR에서 상대 오차 1e-6을 넘으면 실패한다. 최적화기를 쓰는 예측(forecast)은
플랫폼 간 부동소수 차이를 허용해 1e-3으로 본다. 골든을 다시 만들 때는 `UPDATE_GOLDEN=1 pytest tests/regression`.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from data2flow_analytics.common.jsonutil import to_jsonable
from data2flow_analytics.templates.registry import build_registry
from tests.fixtures.inputs import ctx
from tests.fixtures.samples import sample

GOLDEN = Path(__file__).parent / "golden"
REG = build_registry()
LOOSE = {"forecast": 1e-3}


def _compare(a, b, rel: float, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return [f"{path}: keys {sorted(set(a) ^ set(b))}"]
        return [d for k in a for d in _compare(a[k], b[k], rel, f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: len {len(a)} != {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b, strict=True)) for d in _compare(x, y, rel, f"{path}[{i}]")]
    if isinstance(a, float) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return [] if math.isclose(a, b, rel_tol=rel, abs_tol=1e-9) else [f"{path}: {a} != {b}"]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


@pytest.mark.spec("ANA-10.02")
@pytest.mark.parametrize("key", REG.keys())
def test_ANA_10_02_TC_ANA_182_golden_results(key):
    """[ANA-10.02][TC-ANA-182] 골든 결과와 비교(상대 1e-6)"""
    reg = REG.get(key)
    data, params = sample(key)
    result = to_jsonable(reg.template.run(data, reg.template.parse_params(params), ctx(7)).to_json())
    path = GOLDEN / f"{key}.json"
    if os.getenv("UPDATE_GOLDEN") == "1" or not path.exists():
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    golden = json.loads(path.read_text(encoding="utf-8"))
    diffs = _compare(result, golden, LOOSE.get(key, 1e-6))
    assert diffs == [], diffs[:5]
