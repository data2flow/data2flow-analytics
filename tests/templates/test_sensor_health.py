"""ANA-02.04 센서 건강 진단, ANA-02.05 같은 공간 기기 간 비교."""

from __future__ import annotations

import pandas as pd
import pytest

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.templates.builtin.sensor_health.template import TEMPLATE as T
from tests.fixtures import synth
from tests.fixtures.inputs import ctx, make_input


def _by_label(res):
    return {r["label"]: r for r in res.tables[0]["rows"]}


@pytest.mark.spec("ANA-02.04")
def test_ANA_02_04_TC_ANA_049_stuck_and_missing():
    """[ANA-02.04][AT-ANA-16.4] 5대 중 1대 6시간 같은 값 → '값 멈춤 6시간'(시작·끝·값), 1대 누락 30% → '누락률 30%', 둘 다 SUSPECT, 나머지 OK"""
    f = synth.fleet(9, devices=5)
    f = synth.stuck(f, 2, 6)
    f = synth.drop_rate(f, 4, 0.3, seed=9)
    res = T.run(make_input({"devices": f}), T.parse_params({}), ctx(9))
    rows = _by_label(res)
    assert rows["dev2"]["status"] == "SUSPECT" and any(r.startswith("값 멈춤 6시간") for r in rows["dev2"]["reasons"])
    assert rows["dev2"]["stuckFrom"] is not None and rows["dev2"]["stuckValue"] is not None
    assert rows["dev4"]["status"] == "SUSPECT" and abs(rows["dev4"]["missingRate"] - 0.3) < 0.03
    assert {rows[k]["status"] for k in ("dev0", "dev1", "dev3")} == {"OK"}


@pytest.mark.spec("ANA-02.04")
@pytest.mark.parametrize(("battery", "rssi", "out_rate", "expected"), [
    (19.0, -100.0, 0.0, "WARN"), (21.0, -111.0, 0.0, "WARN"), (50.0, -90.0, 0.0, "OK"), (50.0, -90.0, 0.15, "SUSPECT"),
])
def test_ANA_02_04_TC_ANA_050_range_battery_rssi(battery, rssi, out_rate, expected):
    """[ANA-02.04][TC-ANA-050] 범위 초과율 = 품질 코드 1 비율, 배터리 < 20% 주의, RSSI < −110 주의, 모든 판정에 근거 수치"""
    f = synth.fleet(1, devices=1)
    s = f["dev0"]
    q = pd.Series(0, index=s.index)
    q.iloc[: int(len(q) * out_rate)] = 1
    data = make_input({"devices": {"dev0": s}}, quality={"devices": pd.DataFrame({"dev0": q})})
    dev_id = str(data.series["dev0"].device_id)
    data.extras["deviceStatus"] = {dev_id: {"battery": battery, "rssi": rssi}}
    row = T.run(data, T.parse_params({}), ctx(1)).tables[0]["rows"][0]
    assert row["status"] == expected
    assert row["battery"] == battery and row["rssi"] == rssi and row["outOfRangeRate"] == pytest.approx(out_rate, abs=0.01)


@pytest.mark.spec("ANA-02.05")
def test_ANA_02_05_TC_ANA_052_drift_detected():
    """[ANA-02.05][AT-ANA-16.5] 온습도 4대 중 1대 +0.7℃ 편차 7일: 그 기기만 '드리프트 의심', ±0.2℃ 잡음 3대는 OK"""
    f = synth.fleet(21, devices=4)
    f = synth.offset(f, 1, 0.7)
    res = T.run(make_input({"devices": f}), T.parse_params({"peerCompare": True}), ctx(21))
    rows = _by_label(res)
    assert rows["dev1"]["status"] == "SUSPECT" and any("드리프트 의심" in r for r in rows["dev1"]["reasons"])
    assert {rows[k]["status"] for k in ("dev0", "dev2", "dev3")} == {"OK"}
    assert any(c["id"] == "peer" for c in res.charts)


@pytest.mark.spec("ANA-02.05")
def test_ANA_02_05_TC_ANA_053_needs_two_devices():
    """[ANA-02.05][TC-ANA-053] 비교 모드에 기기 1대 → ANALYSIS_BINDING_INVALID"""
    with pytest.raises(BusinessError) as exc:
        T.validate_inputs([{"role": "devices", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "1", "metricKey": "temperature"}]}],
                          T.parse_params({"peerCompare": True}))
    assert exc.value.code.name == "ANALYSIS_BINDING_INVALID"
