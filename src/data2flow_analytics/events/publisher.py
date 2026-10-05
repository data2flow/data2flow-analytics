"""도메인 이벤트 발행(ANA-api §3, architecture.md §4.5).

topic exchange `data2flow.events`, 라우팅 키 = type(`analytics.*`). 본문은 contracts의 도메인 이벤트 봉투
`{v, messageId, type, organizationId, occurredAt, requestId?, payload}`, 헤더는 messageId·v·organizationId·occurredAt·X-REQUEST-ID.
analytics는 `analytics.*`만 발행하고 제어 명령(`command.*`)은 보내지 않는다(BR-ANA-13, TC-ANA-163) — 발행 전에 막는다.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..clock import Clock, SystemClock, iso
from ..common.jsonutil import to_jsonable

log = logging.getLogger("data2flow_analytics.events")

EXCHANGE = "data2flow.events"
RUN_STATUS = "analytics.run.{status}"  # EVT-ANA-01
ANOMALY = "analytics.anomaly.detected"  # EVT-ANA-02
ETA = "analytics.eta.updated"  # EVT-ANA-03
DRIFT = "analytics.model.drift"  # EVT-ANA-04
SCHEDULE_STOPPED = "analytics.schedule.stopped"  # EVT-ANA-05
EXPORT_COMPLETED = "analytics.export.completed"  # EVT-ANA-06


@dataclass(frozen=True)
class DomainEvent:
    type: str
    organization_id: int
    payload: dict
    occurred_at: datetime
    message_id: str
    request_id: str | None = None
    v: int = 1

    def envelope(self) -> dict:
        return to_jsonable({"v": self.v, "messageId": self.message_id, "type": self.type, "organizationId": self.organization_id,
                            "occurredAt": iso(self.occurred_at), "requestId": self.request_id, "payload": self.payload})

    def headers(self) -> dict:
        out = {"messageId": self.message_id, "v": self.v, "organizationId": self.organization_id, "occurredAt": iso(self.occurred_at),
               "schema": "domain-event.v1"}
        if self.request_id:
            out["X-REQUEST-ID"] = self.request_id
        return out


class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> None: ...


def make_event(clock: Clock, event_type: str, organization_id: int, payload: dict, request_id: str | None = None) -> DomainEvent:
    if not event_type.startswith("analytics."):
        raise ValueError(f"analytics는 analytics.* 이벤트만 발행한다(BR-ANA-13): {event_type}")
    return DomainEvent(event_type, int(organization_id), payload, clock.now(), str(uuid.uuid4()), request_id)


class RecordingPublisher:
    """테스트·이벤트 비활성용: 발행 내용을 메모리에 남긴다."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []
        self._lock = threading.Lock()

    def publish(self, event: DomainEvent) -> None:
        with self._lock:
            self.events.append(event)

    def of_type(self, prefix: str) -> list[DomainEvent]:
        return [e for e in self.events if e.type.startswith(prefix)]


class RabbitPublisher:
    """pika(BSD) 발행 확인(confirm) 모드. 연결이 끊기면 다시 연결해 한 번 더 보낸다."""

    def __init__(self, host: str, port: int, vhost: str, user: str, password: str, clock: Clock | None = None):
        self._params = (host, port, vhost, user, password)
        self._lock = threading.Lock()
        self._conn = None
        self._channel = None
        self.clock = clock or SystemClock()

    def _connect(self):
        import pika

        host, port, vhost, user, password = self._params
        params = pika.ConnectionParameters(host=host, port=port, virtual_host=vhost, heartbeat=30, blocked_connection_timeout=10,
                                           credentials=pika.PlainCredentials(user, password),
                                           client_properties={"connection_name": "data2flow-analytics-events"})
        self._conn = pika.BlockingConnection(params)
        self._channel = self._conn.channel()
        self._channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
        self._channel.confirm_delivery()

    def publish(self, event: DomainEvent) -> None:
        import pika

        if not event.type.startswith("analytics."):
            raise ValueError("BR-ANA-13: analytics.* 이외 발행 금지")
        body = json.dumps(event.envelope(), ensure_ascii=False).encode("utf-8")
        props = pika.BasicProperties(content_type="application/json", delivery_mode=2, message_id=event.message_id,
                                     headers=event.headers(), app_id="data2flow-analytics", type=event.type)
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._channel is None or self._channel.is_closed:
                        self._connect()
                    self._channel.basic_publish(EXCHANGE, event.type, body, props, mandatory=False)
                    return
                except Exception:  # noqa: BLE001 — 다시 연결해 한 번 더
                    log.warning("event publish failed type=%s attempt=%s", event.type, attempt, exc_info=attempt == 2)
                    self.close()
                    if attempt == 2:
                        raise

    def close(self) -> None:
        try:
            if self._conn is not None and self._conn.is_open:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None
        self._channel = None


def safe_publish(publisher: EventPublisher | None, event: DomainEvent) -> bool:
    """DB 커밋 뒤 발행. 실패해도 업무 처리는 되돌리지 않는다(상태는 GET으로 다시 읽을 수 있음)."""
    if publisher is None:
        return False
    try:
        publisher.publish(event)
        return True
    except Exception:  # noqa: BLE001
        log.exception("event publish failed type=%s", event.type)
        return False
