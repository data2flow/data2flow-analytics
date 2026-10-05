"""ANA-02.06 배터리 교체 예측(BR-ANA-23)."""

from __future__ import annotations

import pytest

from data2flow_analytics.templates.builtin.battery_life.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


@pytest.mark.spec("ANA-02.06")
def test_ANA_02_06_TC_ANA_054_linear_drain():
    """[ANA-02.06][AT-ANA-16.6] 60일 하루 0.2%씩(80→68%) + σ0.3: 20%까지 240일 ± 5%, 신뢰도 HIGH, 20% 이하 기기는 교체 권장"""
    low = synth.battery(5, days=60, slope=-0.2, start=31)
    res = T.run(make_input({"battery": {"b1": synth.battery(4, days=60, slope=-0.2), "b2": low}}), T.parse_params({}), ctx(4))
    rows = {r["label"]: r for r in res.tables[0]["rows"]}
    assert abs(rows["b1"]["daysLeft"] - 240) <= 240 * 0.05 and rows["b1"]["confidence"] == "HIGH"
    assert rows["b2"]["recommend"] is True and [r["label"] for r in res.tables[1]["rows"]] == ["b2"]


@pytest.mark.spec("ANA-02.06")
def test_ANA_02_06_TC_ANA_055_flat_unpredictable():
    """[ANA-02.06][AT-ANA-16.7] 30일 같은 값 → '예측 불가(변화 없음)', 20일 → '예측 불가(30일 미만)', 나머지 정상 계산"""
    flat = synth.battery(1, days=30, slope=0.0, noise=0.0)
    short = synth.battery(2, days=20)
    ok = synth.battery(3, days=40, slope=-0.3)
    res = T.run(make_input({"battery": {"flat": flat, "short": short, "ok": ok}}), T.parse_params({}), ctx(1))
    rows = {r["label"]: r for r in res.tables[0]["rows"]}
    assert rows["flat"]["predictable"] is False and rows["flat"]["reason"] == "변화 없음"
    assert rows["short"]["predictable"] is False and rows["short"]["reason"] == "30일 미만"
    assert rows["ok"]["predictable"] is True and rows["ok"]["replaceDate"]
    none = T.run(make_input({"battery": {"flat": flat}}), T.parse_params({}), ctx(1))
    assert none.headline.startswith("예측 불가") and "UNPREDICTABLE" in none.flags
