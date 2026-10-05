"""템플릿 레지스트리(ANA-01.02·01.06), 질문 검색(ANA-01.07), 실행 가능성(ANA-01.04)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.templates.builtin.anomaly_detect.template import AnomalyDetect
from data2flow_analytics.templates.guide import parse_guide
from data2flow_analytics.templates.registry import BUILTIN_DIR, TemplateRegistry, build_registry
from data2flow_analytics.templates.search import evaluate_runnable, keyword_search

GUIDE = (BUILTIN_DIR / "anomaly_detect" / "GUIDE.md").read_text(encoding="utf-8")


class V120(AnomalyDetect):
    version = "1.2.0"


class V130(AnomalyDetect):
    version = "1.3.0"


@pytest.mark.spec("ANA-01.02")
def test_ANA_01_02_TC_ANA_009_versions():
    """[ANA-01.02][TC-ANA-009] 1.2.0과 1.3.0이 함께 있으면 기본은 최신, 버전 지정 시 그 버전, 없는 key·버전은 TEMPLATE_NOT_FOUND"""
    reg = TemplateRegistry()
    assert reg.register(V120(), GUIDE) and reg.register(V130(), GUIDE)
    assert reg.get("anomaly-detect").version == "1.3.0"
    assert reg.get("anomaly-detect", "1.2.0").version == "1.2.0"
    assert reg.versions("anomaly-detect") == ["1.2.0", "1.3.0"]
    for key, version in (("no-such", None), ("anomaly-detect", "9.9.9")):
        with pytest.raises(BusinessError) as exc:
            reg.get(key, version)
        assert exc.value.code.name == "TEMPLATE_NOT_FOUND" and exc.value.code.status == 404


@pytest.mark.spec("ANA-01.06")
@pytest.mark.parametrize(("mutate", "missing"), [
    (lambda g: g.replace("- 이 센서 값이 갑자기 튄 적이 있나?\n", "").replace("- 전력 사용량에 평소와 다른 날이 있었나?\n", ""), "whenToUse>=3"),
    (lambda g: g.replace("## 결과 읽는 법", "## 다른 제목"), "howToRead"),
    (lambda g: g.replace("- `sensitivity`:", "- 민감도:"), "params:sensitivity"),
])
def test_ANA_01_06_TC_ANA_028_broken_guides_rejected(mutate, missing, caplog):
    """[ANA-01.06][AT-ANA-01.3] '언제 쓰나' 2개, '결과 읽는 법' 누락, 파라미터 설명 누락 → 등록 거부와 구조화 로그 한 줄"""
    reg = TemplateRegistry()
    with caplog.at_level(logging.ERROR, logger="data2flow_analytics.templates"):
        assert reg.register(AnomalyDetect(), mutate(GUIDE)) is False
    assert missing in reg.rejected["anomaly-detect@1.0.0"]
    assert any(r.getMessage().startswith("template=anomaly-detect missing=[") and missing in r.getMessage() for r in caplog.records)
    assert reg.keys() == []


@pytest.mark.spec("ANA-01.01")
def test_ANA_01_01_TC_ANA_002_discovery_of_new_folder(tmp_path: Path):
    """[ANA-01.01][AT-ANA-01.2] 템플릿 폴더(template.py + GUIDE.md)를 더하면 프론트 변경 없이 목록에 나타난다. 결함 폴더는 빠진다(TC-ANA-029)"""
    good = tmp_path / "my_heatmap"
    good.mkdir()
    (good / "template.py").write_text(
        "from data2flow_analytics.templates.builtin.pattern_heatmap.template import PatternHeatmap\n"
        "class Mine(PatternHeatmap):\n    key = 'my-heatmap'\n    name = '새 히트맵'\nTEMPLATE = Mine()\n", encoding="utf-8")
    (good / "GUIDE.md").write_text((BUILTIN_DIR / "pattern_heatmap" / "GUIDE.md").read_text(encoding="utf-8"), encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "template.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    noguide = tmp_path / "noguide"
    noguide.mkdir()
    (noguide / "template.py").write_text(
        "from data2flow_analytics.templates.builtin.correlation.template import Correlation\n"
        "class C(Correlation):\n    key = 'no-guide'\nTEMPLATE = C()\n", encoding="utf-8")
    reg = build_registry([tmp_path])
    assert "my-heatmap" in reg.keys() and len(reg.keys()) == 14
    assert "broken" in reg.rejected and "no-guide@1.0.0" in reg.rejected


@pytest.mark.spec("ANA-01.07")
def test_ANA_01_07_TC_ANA_031_keyword_search():
    """[ANA-01.07][AT-ANA-08.1] '붐비' → pattern-heatmap·occupancy-estimate 상위 2개, '센서 고장' → sensor-health 1위, 일치 구간 반환"""
    regs = build_registry().current()
    top = keyword_search(regs, "붐비")
    assert {h["key"] for h in top[:2]} == {"pattern-heatmap", "occupancy-estimate"}
    hit = keyword_search(regs, "센서 고장")[0]
    assert hit["key"] == "sensor-health" and hit["mode"] == "KEYWORD"
    start, end = hit["highlight"]["start"], hit["highlight"]["end"]
    assert hit["matchedQuestion"][start:end] in "센서고장"
    assert keyword_search(regs, "   ") == []


@pytest.mark.spec("ANA-01.04")
def test_ANA_01_04_TC_ANA_018_runnable():
    """[ANA-01.04][AT-ANA-08.2] 의미 태그 {noise}뿐이면 comfort-index는 '데이터 없음', anomaly-detect는 실행 가능, temperature만 있으면 humidity·co2 부족"""
    regs = build_registry().current()
    by_key = {r["key"]: r for r in evaluate_runnable(regs, {"noise"})}
    assert by_key["comfort-index"]["runnable"] is False and by_key["comfort-index"]["reason"] == "데이터 없음"
    assert by_key["anomaly-detect"]["runnable"] is True
    temp_only = {r["key"]: r for r in evaluate_runnable(regs, {"temperature"})}
    assert [m["semantic"] for m in temp_only["comfort-index"]["missingRoles"]] == ["humidity", "co2"]
    assert {r["key"]: r for r in evaluate_runnable(regs, set(), has_numeric=False)}["anomaly-detect"]["runnable"] is False


def test_guide_parser_sections_and_numbers():
    """설명서 구조화(API-ANA-02 guide 9항목)와 '필요한 데이터' 수치"""
    g = parse_guide(GUIDE)
    data = g.to_json()
    assert data["summary"] and len(data["whenToUse"]) >= 3 and {p["name"] for p in data["params"]} >= {"sensitivity", "method"}
    assert g.data_numbers() == {"minPeriodDays": 7.0, "minPoints": 100.0, "missingWarn": 0.10, "missingFail": 0.20}
    assert g.questions == data["whenToUse"][:3]
