"""실시간 추론 엔진(ANA-06.01·06.04, BR-ANA-13): 등록·추론·이력 저장·이벤트 발행(Testcontainers PostgreSQL 18)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pandas as pd
import pytest

from data2flow_analytics.realtime.engine import RealtimeEngine
from data2flow_analytics.schemas import event_validator
from tests.conftest import HEADERS, ORG, T0, insert_telemetry
from tests.fixtures import synth

P = "/internal/analytics"


def _telemetry(device: int, value: float, at: pd.Timestamp, metric: str = "co2", virtual: bool = False, quality: int = 0, org: int = ORG):
    return {"v": 1, "messageId": str(uuid.uuid4()), "organizationId": org, "sourceId": 1, "externalId": f"dev-{device}", "deviceId": device,
            "deviceStatus": "ACTIVE", "measuredAt": at.isoformat(), "receivedAt": at.isoformat(), "late": False, "virtual": virtual,
            "metrics": [{"key": metric, "value": value, "quality": quality}], "rawMessageId": 1}


def _realtime_analysis(client, template, device, params=None):
    body = {"name": "실시간", "templateKey": template, "period": {"type": "RELATIVE", "days": 7}, "params": params or {},
            "bindings": [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": str(device), "metricKey": "co2"}]}]}
    a = client.post(f"{P}/analyses", json=body, headers=HEADERS).json()["response"]
    res = client.post(f"{P}/realtime/{a['analysisId']}", json={"enabled": True}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["response"]["realtime"] is True
    return a


@pytest.mark.spec("ANA-06.01")
def test_ANA_06_01_TC_ANA_136_anomaly_event(client, deps):
    """[ANA-06.01][AT-ANA-05.1] 실시간 이상 탐지: 평소보다 5σ 이상 높은 값 → analytics.anomaly.detected(kind=SPIKE·score·evidence), 이력 1행"""
    hist = synth.series(1, days=7, step="5min").series
    hist.index = hist.index + (T0 - hist.index[-1] - timedelta(minutes=5))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 121, "co2", hist)
    a = _realtime_analysis(client, "anomaly-detect", 121)
    engine = RealtimeEngine(deps)
    assert engine.reload() == 1
    assert engine.handle(_telemetry(121, float(hist.iloc[-288]), pd.Timestamp(T0))) == []
    events = engine.handle(_telemetry(121, 2000.0, pd.Timestamp(T0) + timedelta(minutes=5)))
    assert len(events) == 1 and events[0].type == "analytics.anomaly.detected"
    payload = events[0].payload
    assert payload["kind"] == "SPIKE" and payload["score"] > 5 and payload["deviceId"] == "121" and payload["analysisId"] == a["analysisId"]
    assert list(event_validator("analytics.anomaly.detected").iter_errors(payload)) == []
    listing = client.get(f"{P}/analyses/{a['analysisId']}/realtime-events?from={(T0 - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}", headers=HEADERS).json()
    assert listing["totalCount"] == 1 and listing["size"] == 50 and listing["responses"][0]["type"] == "ANOMALY"
    assert client.get(f"{P}/analyses/{a['analysisId']}/realtime-events?deviceIds=999", headers=HEADERS).json()["totalCount"] == 0
    assert client.get(f"{P}/analyses/{a['analysisId']}/realtime-events?to=yesterday", headers=HEADERS).status_code == 400
    ev_id = listing["responses"][0]["realtimeEventId"]
    fb = client.post(f"{P}/feedback", headers=HEADERS, json={"realtimeEventId": ev_id, "occurredAt": payload["occurredAt"],
                                                             "seriesKey": "dev:121:co2", "verdict": "TRUE_POSITIVE"})
    assert fb.status_code == 201
    # 가상·품질 이상·다른 조직 메시지는 건너뛴다
    assert engine.handle(_telemetry(121, 3000.0, pd.Timestamp(T0) + timedelta(minutes=30), virtual=True)) == []
    assert engine.handle(_telemetry(121, 3000.0, pd.Timestamp(T0) + timedelta(minutes=31), quality=1)) == []
    assert engine.handle(_telemetry(121, 3000.0, pd.Timestamp(T0) + timedelta(minutes=32), org=2)) == []
    assert engine.handle({"bad": True}) == []
    client.post(f"{P}/realtime/{a['analysisId']}", json={"enabled": False}, headers=HEADERS)
    assert engine.reload() == 0


@pytest.mark.spec("ANA-02.09")
def test_ANA_02_09_TC_ANA_066_eta_event(client, deps):
    """[ANA-02.09][AT-ANA-17.7] 실시간: 분당 +15ppm 값 투입 → analytics.eta.updated, threshold=1000, minutesLeft 10±1, 스키마 통과"""
    ramp = synth.ramp(2, start=835, slope=15, minutes=20, at=pd.Timestamp(T0) - timedelta(minutes=20))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 122, "co2", ramp)
    _realtime_analysis(client, "threshold-eta", 122, {"threshold": 1000})
    engine = RealtimeEngine(deps)
    engine.reload()
    events = engine.handle(_telemetry(122, 850.0, pd.Timestamp(T0) + timedelta(minutes=1)))
    assert len(events) == 1 and events[0].type == "analytics.eta.updated"
    p = events[0].payload
    assert p["threshold"] == 1000 and p["direction"] == "UP" and abs(p["minutesLeft"] - 10) <= 1 and p["confidence"] == "HIGH"
    assert list(event_validator("analytics.eta.updated").iter_errors(p)) == []


@pytest.mark.spec("ANA-08.04")
def test_ANA_08_04_TC_ANA_163_no_control_commands(client, deps):
    """[ANA-08.04][AT-ANA-05.2] 실시간 이상 100건을 내도 발행한 라우팅 키는 analytics.*뿐, command.* 0건(BR-ANA-13)"""
    hist = synth.series(3, days=7, step="5min").series
    hist.index = hist.index + (T0 - hist.index[-1] - timedelta(minutes=5))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 123, "co2", hist)
    _realtime_analysis(client, "anomaly-detect", 123, {"cooldownMinutes": 0})
    engine = RealtimeEngine(deps)
    engine.reload()
    total = 0
    for k in range(100):
        total += len(engine.handle(_telemetry(123, 5000.0 + 100 * k, pd.Timestamp(T0) + timedelta(minutes=k))))
    assert total == 100
    types = {e.type for e in deps.publisher.events}
    assert types == {"analytics.anomaly.detected"} or all(t.startswith("analytics.") for t in types)
    assert not [e for e in deps.publisher.events if e.type.startswith("command.")]
