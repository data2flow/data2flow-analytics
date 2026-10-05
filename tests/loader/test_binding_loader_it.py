"""바인딩 로더(Testcontainers PostgreSQL 18 + pipeline 스키마): SQL 해석, 집계 단위, 품질·가상 필터."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from data2flow_analytics.loader.bindings import parse_bindings
from data2flow_analytics.loader.loader import LoadRequest
from data2flow_analytics.loader.sufficiency import check
from data2flow_analytics.templates.registry import build_registry
from tests.conftest import ORG, T0, aggregate, insert_telemetry

REG = build_registry()
ANOMALY = REG.get("anomaly-detect").template


def _series(seed: int, days: int = 30, step: str = "10min", base: float = 600.0) -> pd.Series:
    idx = pd.date_range(T0 - timedelta(days=days), periods=int(days * 86400 / pd.Timedelta(step).total_seconds()), freq=step, tz="UTC")
    return pd.Series(base + np.random.default_rng(seed).normal(0, 5, len(idx)), index=idx)


def _req(bindings, resolution="RAW", quality="NORMAL_ONLY", virtual=False, days=30):
    return LoadRequest(ORG, parse_bindings(bindings), T0 - timedelta(days=days), T0, resolution, quality, virtual)


@pytest.mark.spec("ANA-01.05")
def test_ANA_01_05_TC_ANA_022_three_binding_kinds(deps):
    """[ANA-01.05][TC-ANA-022] 기기·공간 집계(avg)·파생 항목이 각각 올바른 SQL로 풀려 같은 시간축 DataFrame. 1h면 telemetry_1h를 읽는다"""
    deps.loader.core.spaces[50] = [201, 202, 203, 204]
    with deps.pool.connection() as conn:
        for i, dev in enumerate((201, 202, 203, 204)):
            insert_telemetry(conn, dev, "temperature", _series(i, base=20 + i))
        insert_telemetry(conn, 201, "comfort_score", _series(9, base=80))
        aggregate(conn, "1h")
    bindings = [{"role": "target", "sources": [
        {"kind": "DEVICE_METRIC", "deviceId": "201", "metricKey": "temperature"},
        {"kind": "SPACE_AGGREGATE", "spaceId": "50", "metricKey": "temperature", "agg": "avg"},
        {"kind": "DERIVED_METRIC", "deviceId": "201", "metricKey": "comfort_score"}]}]
    data = deps.loader.load(_req(bindings, "1h"), ANOMALY)
    frame = data.role("target")
    assert list(frame.columns) == ["dev:201:temperature", "space:50:temperature:avg", "dev:201:comfort_score"]
    assert len(frame) == 30 * 24 and frame.notna().all().all()
    assert abs(frame["space:50:temperature:avg"].mean() - 21.5) < 0.1  # 기기 4대(20~23) 평균
    raw = deps.loader.load(_req(bindings[:1]), ANOMALY).role("target")
    assert len(raw) == 30 * 144
    with deps.pool.connection() as conn:
        conn.execute("TRUNCATE data2flow_pipeline.telemetry")
    assert len(deps.loader.load(_req(bindings, "1h"), ANOMALY).role("target")) == 30 * 24  # 집계 테이블만으로 읽는다


@pytest.mark.spec("ANA-03.04")
def test_ANA_03_04_TC_ANA_090_quality_filter(deps):
    """[ANA-03.04][TC-ANA-090] NORMAL_ONLY면 품질 0만, INCLUDE_ALL이면 1(범위 초과)·3(의심)도. provenance에 필터 기록"""
    s = _series(1, days=7)
    q = pd.Series(0, index=s.index)
    q.iloc[::10] = 1
    q.iloc[5::20] = 3
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 301, "co2", s, quality=q)
    b = [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "301", "metricKey": "co2"}]}]
    normal = deps.loader.load(_req(b, days=7), ANOMALY).role("target")
    everything = deps.loader.load(_req(b, quality="INCLUDE_ALL", days=7), ANOMALY).role("target")
    assert len(normal) == int((q == 0).sum()) and len(everything) == len(s)
    stats = deps.loader.stats(_req(b, quality="INCLUDE_ALL", days=7))
    assert stats[0].quality[1] == int((q == 1).sum()) and stats[0].received == len(s)


@pytest.mark.spec("ANA-03.05")
def test_ANA_03_05_TC_ANA_092_virtual_excluded(deps):
    """[ANA-03.05][AT-ANA-02.6] 같은 공간 실제 2대 + 가상 1대, 공간 집계 바인딩, 가상 포함 끔: 충분성 통계 가상 0건, 결과 virtual=false"""
    deps.loader.core.spaces[60] = [401, 402, 403]
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 401, "co2", _series(1, days=7))
        insert_telemetry(conn, 402, "co2", _series(2, days=7))
        insert_telemetry(conn, 403, "co2", _series(3, days=7, base=5000), virtual=True)
    b = [{"role": "target", "sources": [{"kind": "SPACE_AGGREGATE", "spaceId": "60", "metricKey": "co2"}]}]
    req = _req(b, "1m", days=7)
    res = check(deps.loader.stats(req), ANOMALY.requirements, req.period_from, req.period_to, "RAW", False)
    assert res.stats["virtualPoints"] == 0
    data = deps.loader.load(req, ANOMALY)
    assert data.role("target").iloc[:, 0].max() < 1000  # 가상 5000ppm이 섞이지 않음
    with_virtual = deps.loader.load(_req(b, "1m", virtual=True, days=7), ANOMALY)
    assert with_virtual.role("target").iloc[:, 0].max() > 1000


@pytest.mark.spec("ANA-08.03")
def test_ANA_08_03_TC_ANA_161_virtual_count(deps):
    """[ANA-08.03][AT-ANA-16.8] 실측 7일 + 가상 7일 같은 기기: 가상 포함 없이 → 포인트 = 실측분만"""
    real = _series(1, days=7)
    virtual = real.copy()
    virtual.index = virtual.index + pd.Timedelta(minutes=5)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 501, "co2", real)
        insert_telemetry(conn, 501, "co2", virtual, virtual=True)
    b = [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "501", "metricKey": "co2"}]}]
    assert deps.loader.load(_req(b, days=7), ANOMALY).total_points() == len(real)
    assert deps.loader.load(_req(b, virtual=True, days=7), ANOMALY).total_points() == 2 * len(real)
    st = deps.loader.stats(_req(b, virtual=True, days=7))[0]
    assert st.virtual_points == len(real)


@pytest.mark.spec("ANA-03.03")
def test_ANA_03_03_TC_ANA_087_missing_rate_by_report_interval(deps):
    """[ANA-03.03][AT-ANA-02.3] 누락률은 기기 보고 주기 기준: 1분 주기 하루 1,440 기대, 1,224 수신 → 15%"""
    idx = pd.date_range(T0 - timedelta(days=1), periods=1440, freq="1min", tz="UTC")
    keep = np.ones(1440, dtype=bool)
    drop = np.random.default_rng(3).choice(np.arange(1, 1439), size=216, replace=False)
    keep[drop] = False
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 601, "co2", pd.Series(500.0, index=idx[keep]))
    b = [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "601", "metricKey": "co2"}]}]
    req = _req(b, days=1)
    res = check(deps.loader.stats(req), REG.get("threshold-eta").template.requirements, req.period_from, req.period_to, "RAW", False)
    assert res.stats["estimatedRawPoints"] == 1224
    assert res.stats["missingRate"] == pytest.approx(0.15, abs=0.001)


def test_sensor_health_quality_and_device_state(deps):
    """센서 건강은 품질 코드와 기기 상태(배터리·신호, data2flow_pipeline.device_state)를 함께 읽는다"""
    s = _series(1, days=7)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 701, "temperature", s, quality=pd.Series(1, index=s.index[:50]).reindex(s.index, fill_value=0))
        conn.execute("INSERT INTO data2flow_pipeline.device_state (device_id, organization_id, battery, rssi) VALUES (701, %s, 15, -115)", (ORG,))
    t = REG.get("sensor-health").template
    b = [{"role": "devices", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "701", "metricKey": "temperature"}]}]
    data = deps.loader.load(_req(b, days=7), t)
    assert int((data.quality["devices"].iloc[:, 0] == 1).sum()) == 50
    assert data.extras["deviceStatus"]["701"] == {"battery": 15.0, "rssi": -115.0}
    row = t.run(data, t.parse_params({}), __import__("tests.fixtures.inputs", fromlist=["ctx"]).ctx(1)).tables[0]["rows"][0]
    assert row["status"] == "WARN" and row["battery"] == 15.0


def test_comfort_space_targets_from_core(deps):
    """쾌적도의 공간 목표 환경은 core-api(API-DEV-126)에서 읽는다"""
    deps.loader.core.spaces[70] = [801]
    deps.loader.core.targets[70] = {"temperature": {"min": 22, "max": 24}}
    t = REG.get("comfort-index").template
    b = [{"role": r, "sources": [{"kind": "SPACE_AGGREGATE", "spaceId": "70", "metricKey": m}]}
         for r, m in (("temp", "temperature"), ("humidity", "humidity"), ("co2", "co2"))]
    data = deps.loader.load(_req(b, "1h", days=1), t)
    assert data.extras["spaceTargets"] == {"temperature": {"min": 22, "max": 24}}
    empty = deps.loader.load(_req([{"role": "target", "sources": [{"kind": "SPACE_AGGREGATE", "spaceId": "99", "metricKey": "co2"}]}],
                                  days=1), ANOMALY)
    assert empty.total_points() == 0
