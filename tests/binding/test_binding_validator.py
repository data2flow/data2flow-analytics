"""바인딩 검사(ANA-01.05, BR-ANA-02)와 기간 해석."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from data2flow_analytics.common.errors import BusinessError
from data2flow_analytics.loader.bindings import binding_spaces, parse_bindings, resolve_period, validate_roles
from data2flow_analytics.templates.registry import build_registry

REG = build_registry()


def _src(metric="co2", device="1", **kw):
    return {"kind": "DEVICE_METRIC", "deviceId": device, "metricKey": metric, **kw}


CASES = [
    ("comfort-index", [{"role": "temp", "sources": [_src("temperature")]}, {"role": "humidity", "sources": [_src("humidity")]},
                       {"role": "co2", "sources": [_src("temperature")]}], "co2"),
    ("comfort-index", [{"role": "temp", "sources": [_src("t", semantic="temperature")]}, {"role": "humidity", "sources": [_src("humidity")]},
                       {"role": "co2", "sources": [_src("x", semantic="humidity")]}], "co2"),
    ("comfort-index", [{"role": "temp", "sources": [_src("temperature")]}, {"role": "humidity", "sources": [_src("humidity")]}], "co2"),
    ("anomaly-detect", [{"role": "target", "sources": []}], "target"),
    ("anomaly-detect", [{"role": "target", "sources": [_src(device=str(i)) for i in range(51)]}], "target"),
    ("anomaly-detect", [{"role": "nope", "sources": [_src()]}], "nope"),
    ("correlation", [{"role": "target", "sources": [_src()]}], "target"),
    ("forecast", [{"role": "target", "sources": [_src(), _src(device="2")]}], "target"),
    ("forecast", [{"role": "target", "sources": [_src()]}, {"role": "covariates", "sources": [_src(device=str(i)) for i in range(6)]}],
     "covariates"),
    ("battery-life", [{"role": "battery", "sources": [_src("co2")]}], "battery"),
    ("sensor-health", [{"role": "devices", "sources": []}], "devices"),
    ("pattern-heatmap", [{"role": "target", "sources": [_src(), _src(device="3")]}], "target"),
]


@pytest.mark.spec("ANA-01.05")
@pytest.mark.parametrize(("key", "bindings", "role"), CASES)
def test_ANA_01_05_TC_ANA_021_binding_validator(key, bindings, role):
    """[ANA-01.05][TC-ANA-021] 개수(min~max)·의미 조건 위반 → ANALYSIS_BINDING_INVALID(role 포함), 12케이스"""
    t = REG.get(key).template
    with pytest.raises(BusinessError) as exc:
        validate_roles(parse_bindings(bindings), t.roles_for(t.parse_params({})))
    assert exc.value.code.name == "ANALYSIS_BINDING_INVALID" and exc.value.args_["role"] == role
    msg = exc.value.code.message("ko", **exc.value.args_)
    assert role in msg and "데이터를 연결하세요" in msg


def test_valid_bindings_and_spaces():
    """정상 바인딩: 3종(기기·공간 집계·파생) 해석, 공간 범위 계산(BR-ANA-03)"""
    sources = parse_bindings([{"role": "target", "sources": [_src(), {"kind": "SPACE_AGGREGATE", "spaceId": "5", "metricKey": "co2", "agg": "max"},
                                                             {"kind": "DERIVED_METRIC", "spaceId": "6", "metricKey": "occupancy_ratio"}]}])
    assert [s.key for s in sources] == ["dev:1:co2", "space:5:co2:max", "space:6:occupancy_ratio:avg"]
    assert binding_spaces(sources) == [5, 6]
    validate_roles(sources, REG.get("anomaly-detect").template.roles)


@pytest.mark.parametrize("bad", [
    [{"sources": [_src()]}], [{"role": "target", "sources": [{"kind": "WHAT", "metricKey": "x"}]}],
    [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "metricKey": "x"}]}],
    [{"role": "target", "sources": [{"kind": "SPACE_AGGREGATE", "metricKey": "x"}]}],
    [{"role": "target", "sources": [{"kind": "DERIVED_METRIC", "metricKey": "x"}]}],
    [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "x1", "metricKey": "x"}]}],
    [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "1"}]}], "not-a-list",
])
def test_malformed_bindings(bad):
    with pytest.raises(BusinessError) as exc:
        parse_bindings(bad)
    assert exc.value.code.status == 400


def test_resolve_period():
    now = datetime(2026, 10, 5, 3, 4, 59, tzinfo=UTC)
    start, end = resolve_period({"type": "RELATIVE", "days": 7}, now)
    assert end == datetime(2026, 10, 5, 3, 4, tzinfo=UTC) and (end - start).days == 7
    start, end = resolve_period({"type": "FIXED", "from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"}, now)
    assert (end - start).days == 1
    for bad in (None, {"type": "RELATIVE"}, {"type": "RELATIVE", "days": 0}, {"type": "FIXED", "from": "2026-10-02T00:00:00Z",
                                                                                "to": "2026-10-01T00:00:00Z"},
                {"type": "FIXED"}, {"type": "WEEKLY"}):
        with pytest.raises(BusinessError):
            resolve_period(bad, now)
