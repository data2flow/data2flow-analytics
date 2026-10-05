"""파라미터 스키마(ANA-03.02)."""

from __future__ import annotations

import pytest

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.templates.registry import build_registry

REG = build_registry()


@pytest.mark.spec("ANA-03.02")
def test_ANA_03_02_TC_ANA_083_params_schema():
    """[ANA-03.02][AT-ANA-02.4] 민감도 9 → ANALYSIS_PARAMS_INVALID field=sensitivity, 타입 불일치·모르는 필드도 같은 코드, 기본값 자동 채움"""
    t = REG.get("anomaly-detect").template
    for bad, field in (({"sensitivity": 9}, "sensitivity"), ({"sensitivity": "high"}, "sensitivity"), ({"nope": 1}, "nope"),
                       ({"method": "MAGIC"}, "method")):
        with pytest.raises(BusinessError) as exc:
            t.parse_params(bad)
        assert exc.value.code.name == "ANALYSIS_PARAMS_INVALID" and exc.value.args_["field"] == field
        assert exc.value.errors[0].field == f"params.{field}"
    assert t.parse_params({}).model_dump() == {"method": "STATISTICAL", "sensitivity": 3, "period": "AUTO", "minDurationMinutes": 0,
                                               "cooldownMinutes": 10}


@pytest.mark.spec("ANA-03.02")
@pytest.mark.parametrize("key", REG.keys())
def test_ANA_03_02_TC_ANA_083_defaults_valid(key):
    """[ANA-03.02] 13종 모두 기본값만으로 파라미터가 유효하다(폼 자동 생성의 기본값)"""
    t = REG.get(key).template
    params = t.parse_params({})
    assert t.parse_params(params.model_dump(mode="json")) == params
    assert t.params_schema()["$schema"].endswith("2020-12/schema")
