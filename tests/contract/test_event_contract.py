"""이벤트 계약(TC-ANA-140): EVT-ANA-01~06 페이로드가 스키마를 통과하고 공통 헤더(messageId UUID, v, X-REQUEST-ID, organizationId)를 가진다."""

from __future__ import annotations

import uuid

import pytest

from data2flow_analytics.clock import MutableClock
from data2flow_analytics.events import publisher as ev
from data2flow_analytics.schemas import event_validator
from tests.conftest import T0

SAMPLES = {
    "analytics.run.succeeded": {"runId": "1", "analysisId": "2", "status": "SUCCEEDED", "progress": 100, "trigger": "MANUAL",
                                "finishedAt": "2026-10-05T00:00:00Z"},
    "analytics.anomaly.detected": {"analysisId": "2", "deviceId": "101", "metricKey": "co2", "occurredAt": "2026-10-05T00:00:00Z",
                                   "value": 1500.0, "score": 6.2, "kind": "SPIKE",
                                   "evidence": {"baseline": 600.0, "threshold": 4.5, "contributors": []}},
    "analytics.eta.updated": {"analysisId": "2", "deviceId": "101", "metricKey": "co2", "threshold": 1000, "direction": "UP",
                              "etaAt": "2026-10-05T00:10:00Z", "minutesLeft": 10.0, "confidence": "HIGH"},
    "analytics.model.drift": {"analysisId": "2", "modelId": "3", "psi": 0.31, "detectedAt": "2026-10-05T00:00:00Z"},
    "analytics.schedule.stopped": {"analysisId": "2", "ownerUserId": "7", "consecutiveFailures": 3},
    "analytics.export.completed": {"exportJobId": "9", "userId": "7", "downloadUrl": "/bff/download/x", "expiresAt": "2026-10-06T00:00:00Z"},
}


@pytest.mark.spec("ANA-06.02")
@pytest.mark.parametrize("event_type", sorted(SAMPLES))
def test_contract_ANA_06_02_TC_ANA_140_event_payloads(event_type):
    """[ANA-06.02][TC-ANA-140] 봉투(v, messageId, type, organizationId, occurredAt)와 헤더, 페이로드 스키마"""
    event = ev.make_event(MutableClock(T0), event_type, 1, SAMPLES[event_type], "req-1")
    env = event.envelope()
    assert env["v"] == 1 and env["type"] == event_type and env["organizationId"] == 1 and env["occurredAt"] == "2026-10-05T00:00:00Z"
    uuid.UUID(env["messageId"])
    headers = event.headers()
    assert headers["messageId"] == env["messageId"] and headers["X-REQUEST-ID"] == "req-1" and headers["organizationId"] == 1
    assert list(event_validator(event_type).iter_errors(env["payload"])) == []


@pytest.mark.spec("ANA-08.04")
def test_ANA_08_04_TC_ANA_163_only_analytics_events():
    """[ANA-08.04][TC-ANA-163] analytics는 analytics.* 이벤트만 만들 수 있다(command.* 발행 금지, BR-ANA-13)"""
    with pytest.raises(ValueError):
        ev.make_event(MutableClock(T0), "command.status.requested", 1, {})
    recorder = ev.RecordingPublisher()
    assert ev.safe_publish(recorder, ev.make_event(MutableClock(T0), ev.ANOMALY, 1, SAMPLES[ev.ANOMALY])) is True
    assert recorder.of_type("analytics.") and not recorder.of_type("command.")
    assert ev.safe_publish(None, ev.make_event(MutableClock(T0), ev.ANOMALY, 1, {})) is False
