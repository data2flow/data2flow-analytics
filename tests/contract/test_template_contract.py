"""템플릿 계약 테스트(`pytest -k contract`, design/testing/backend.md §4.6).

등록된 모든 템플릿에 대해: manifest 스키마(TC-ANA-008), 설명서 9항목(TC-ANA-027), 결과·ChartSpec 스키마(TC-ANA-114·117),
최소 데이터 조건 = 설명서 수치(TC-ANA-158), 단정 표현 금지(TC-ANA-159), 해석 주의 문구(TC-ANA-165), 결정성(TC-ANA-180).
"""

from __future__ import annotations

import re

import pytest

from data2flow_analytics.common.jsonutil import content_hash, to_jsonable
from data2flow_analytics.schemas import chartspec_validator, manifest_validator, result_validator
from data2flow_analytics.templates.guide import missing_items
from data2flow_analytics.templates.registry import build_registry
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input
from tests.fixtures.samples import provenance, sample

REGISTRY = build_registry()
KEYS = REGISTRY.keys()
FORBIDDEN = ("정확히", "반드시", "확실히", "틀림없이")
_CACHE: dict[str, dict] = {}


def _result(key: str, seed: int = 7) -> dict:
    if (key, seed) not in _CACHE:
        reg = REGISTRY.get(key)
        data, params = sample(key)
        res = reg.template.run(data, reg.template.parse_params(params), ctx(seed)).to_json()
        res["provenance"] = {**provenance(data, seed), **res["provenance"], "template": f"{key}@{reg.version}"}
        _CACHE[(key, seed)] = to_jsonable(res)
    return _CACHE[(key, seed)]


def test_contract_thirteen_templates_registered():
    """[ANA-02][AT-ANA-01.1] 기본 템플릿 13종이 등록된다(갤러리 카드 13장)"""
    assert KEYS == ["anomaly-detect", "battery-life", "change-point", "comfort-index", "correlation", "daily-profile-cluster", "forecast",
                    "intervention-impact", "occupancy-estimate", "pattern-heatmap", "sensor-health", "space-benchmark", "threshold-eta"]
    assert REGISTRY.rejected == {}


@pytest.mark.spec("ANA-01.02")
@pytest.mark.parametrize("key", KEYS)
def test_contract_ANA_01_02_TC_ANA_008_manifest_schema(key):
    """[ANA-01.02][TC-ANA-008] manifest가 template-manifest.v1을 만족, paramsSchema의 모든 필드에 default·description"""
    t = REGISTRY.get(key).template
    manifest = to_jsonable(t.manifest())
    assert list(manifest_validator().iter_errors(manifest)) == []
    for name, prop in manifest["paramsSchema"].get("properties", {}).items():
        assert "description" in prop, name
        assert "default" in prop or name in manifest["paramsSchema"].get("required", []), name
    assert all(r["min"] <= r["max"] for r in manifest["roles"])


@pytest.mark.spec("ANA-01.06")
@pytest.mark.parametrize("key", KEYS)
def test_contract_ANA_01_06_TC_ANA_027_guide_complete(key):
    """[ANA-01.06][TC-ANA-027] GUIDE.md 9항목(요약, 언제 쓰나 ≥3, 안 되나, 필요 데이터, 파라미터 전부, 읽는 법, 주의점, 예시, 알고리즘·참고 문헌)"""
    reg = REGISTRY.get(key)
    assert missing_items(reg.guide, list(reg.template.params_model.model_fields)) == []
    assert reg.template.guide_path.parent.name == key.replace("-", "_")


@pytest.mark.spec("ANA-08.01")
@pytest.mark.parametrize("key", KEYS)
def test_contract_ANA_08_01_TC_ANA_158_min_requirements(key):
    """[ANA-08.01][TC-ANA-158] requirements(minPeriodDays, minPoints, missingWarn, missingFail)가 API 응답과 설명서 수치와 같다"""
    reg = REGISTRY.get(key)
    numbers = reg.guide.data_numbers()
    req = reg.template.requirements.to_json()
    for k in ("minPeriodDays", "minPoints", "missingWarn", "missingFail"):
        assert numbers[k] == pytest.approx(req[k]), k


@pytest.mark.spec("ANA-05.01")
@pytest.mark.parametrize("key", KEYS)
def test_contract_ANA_05_01_TC_ANA_114_result_schema(key):
    """[ANA-05.01][TC-ANA-114·037·044·062·071] 모든 템플릿 결과가 result.v1을 통과(summary·charts·tables·provenance)"""
    res = _result(key)
    errors = [f"{list(e.path)} {e.message}" for e in result_validator().iter_errors(res)]
    assert errors == []


@pytest.mark.spec("ANA-05.02")
@pytest.mark.parametrize("key", KEYS)
def test_contract_ANA_05_02_TC_ANA_117_chartspec_schema(key):
    """[ANA-05.02][TC-ANA-117] charts[]가 chartspec.v1을 통과, id 고유, band lower ≤ upper, 출력 타입 선언과 일치"""
    res = _result(key)
    ids = [c["id"] for c in res["charts"]]
    assert len(ids) == len(set(ids))
    declared = set(REGISTRY.get(key).template.outputs)
    for c in res["charts"]:
        assert list(chartspec_validator().iter_errors(c)) == []
        assert c["type"] in declared, (key, c["type"])
        for band in c.get("bands", []):
            assert all(lo is None or hi is None or lo <= hi for lo, hi in zip(band["lower"], band["upper"], strict=True))


@pytest.mark.spec("ANA-08.02")
@pytest.mark.parametrize("key", KEYS)
def test_contract_ANA_08_02_TC_ANA_159_no_assertive_words(key):
    """[ANA-08.02][TC-ANA-159] headline·metrics label·caveats에 '정확히·반드시·확실히·틀림없이' 0건, 예측형은 구간 또는 '추정' 표시"""
    res = _result(key)
    texts = [res["summary"]["headline"], *[m["label"] for m in res["summary"]["metrics"]], *res["caveats"]]
    assert not [t for t in texts for w in FORBIDDEN if w in t]
    if REGISTRY.get(key).template.predictive:
        has_band = any(c["type"] == "band" for c in res["charts"])
        assert has_band or "ESTIMATE" in res["summary"].get("flags", []) or "추정" in res["summary"]["headline"]


@pytest.mark.spec("ANA-08.05")
@pytest.mark.parametrize("key", ["correlation", "occupancy-estimate", "intervention-impact"])
def test_contract_ANA_08_05_TC_ANA_165_caveats_required(key):
    """[ANA-08.05][TC-ANA-165] 오해하기 쉬운 결과에는 템플릿에 정의된 해석 주의 문구가 caveats에 있다"""
    t = REGISTRY.get(key).template
    assert t.fixed_caveats and set(t.fixed_caveats) <= set(_result(key)["caveats"])


@pytest.mark.spec("ANA-02.10")
def test_contract_ANA_02_10_TC_ANA_068_occupancy_no_headcount():
    """[ANA-02.10][TC-ANA-068] 결과 JSON 어디에도 인원 수 필드(count, people, headcount)가 없고, caveats에 '추정치'(BR-ANA-25)"""
    res = _result("occupancy-estimate")

    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)

    assert not [k for k in keys(res) if re.search(r"count|people|headcount", k, re.I)]
    assert any("추정치" in c for c in res["caveats"])


@pytest.mark.spec("ANA-10.02")
@pytest.mark.parametrize("key", KEYS)
def test_contract_ANA_10_02_TC_ANA_180_deterministic(key):
    """[ANA-10.02][TC-ANA-180·037] 같은 템플릿 버전·입력·파라미터·시드로 2회 → 정규화한 결과 JSON SHA-256 동일(BR-ANA-10)"""
    reg = REGISTRY.get(key)
    hashes = set()
    for _ in range(2):
        data, params = sample(key)
        res = reg.template.run(data, reg.template.parse_params(params), ctx(7)).to_json()
        hashes.add(content_hash(res))
    assert len(hashes) == 1


@pytest.mark.spec("ANA-01.05")
@pytest.mark.parametrize("key", ["anomaly-detect", "forecast", "pattern-heatmap", "correlation", "change-point", "daily-profile-cluster"])
def test_contract_ANA_01_05_TC_ANA_023_general_templates_any_metric(key):
    """[ANA-01.05][TC-ANA-023] 범용 템플릿 6종은 온도·소음(LAeq)·전력(kWh) 합성 시계열 어디에도 예외 없이 실행되고 단위를 그대로 전달"""
    reg = REGISTRY.get(key)
    for unit, base, amp in (("℃", 23.0, 2.0), ("dB", 45.0, 8.0), ("kWh", 3.0, 1.5)):
        s = synth.series(7, days=35, step="1h", base=base, daily_amp=amp, weekend_delta=-amp / 3, noise=amp / 10).series
        roles = {"target": {"a": s, "b": s.shift(2).bfill() * 1.1}} if key == "correlation" else {"target": {"a": s}}
        res = reg.template.run(make_input(roles, units={"a": unit, "b": unit}), reg.template.parse_params({}), ctx(7)).to_json()
        assert res["summary"]["headline"]
        y_units = [c.get("yAxis", {}).get("unit") for c in res["charts"]]
        if key in ("anomaly-detect", "forecast", "change-point"):
            assert unit in y_units
