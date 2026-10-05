"""Super Stream data2flow.telemetry → 실시간 추론 → data2flow.events 발행(Testcontainers RabbitMQ 4 + stream, PostgreSQL 18).

TC-ANA-136·163: 스트림으로 들어온 측정값으로 analytics.anomaly.detected가 나가고, 발행한 라우팅 키는 analytics.*뿐이다.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import pandas as pd
import pika
import pytest

from data2flow_analytics.config import Settings
from data2flow_analytics.events.publisher import EXCHANGE, RabbitPublisher
from data2flow_analytics.realtime.consumer import TelemetryConsumer
from data2flow_analytics.realtime.engine import RealtimeEngine
from tests.conftest import HEADERS, ORG, T0, insert_telemetry
from tests.fixtures import synth


@pytest.fixture(scope="module")
def rabbit(tmp_path_factory):
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    conf = tmp_path_factory.mktemp("rmq")
    (conf / "enabled_plugins").write_text("[rabbitmq_management,rabbitmq_stream].", encoding="utf-8")
    container = (DockerContainer("rabbitmq:4.1-management").with_exposed_ports(5672, 5552)
                 .with_volume_mapping(str(conf / "enabled_plugins"), "/etc/rabbitmq/enabled_plugins", "ro"))
    with container:
        wait_for_logs(container, "Server startup complete", timeout=120)
        yield container.get_container_host_ip(), int(container.get_exposed_port(5672)), int(container.get_exposed_port(5552))


async def _route(message) -> str:
    return str(json.loads(message.body)["deviceId"])


async def _produce(host, port, messages):
    from rstream import AMQPMessage, SuperStreamCreationOption, SuperStreamProducer

    async with SuperStreamProducer(host=host, port=port, username="guest", password="guest", super_stream="data2flow.telemetry",
                                   load_balancer_mode=True, routing_extractor=_route,
                                   super_stream_creation_option=SuperStreamCreationOption(n_partitions=3)) as producer:
        for msg in messages:
            await producer.send(AMQPMessage(body=json.dumps(msg).encode()))


async def _scenario(consumer: TelemetryConsumer, host: str, port: int, messages: list, budget_sec: float = 60.0) -> None:
    """구독이 끝난 뒤 발행하고, 처리 수가 찰 때까지 기다린다(작업 진행 대기, 고정 sleep 없음)."""
    task = asyncio.create_task(consumer.run())
    await asyncio.wait_for(consumer.subscribed.wait(), timeout=budget_sec)
    await _produce(host, port, messages)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_sec
    while consumer.processed < len(messages) and loop.time() < deadline and not task.done():
        await asyncio.wait({task}, timeout=0.2)
    consumer.stop()
    await asyncio.wait_for(task, timeout=30)


@pytest.mark.spec("ANA-06.01")
def test_ANA_06_01_TC_ANA_136_stream_to_event(client, deps, rabbit):
    """[ANA-06.01][AT-ANA-05.1] 스트림 → 추론 → data2flow.events에 analytics.anomaly.detected, 다른 라우팅 키(command.*) 0건"""
    host, amqp_port, stream_port = rabbit
    hist = synth.series(5, days=7, step="5min").series
    hist.index = hist.index + (T0 - hist.index[-1] - timedelta(minutes=5))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 131, "co2", hist)
    body = {"name": "실시간", "templateKey": "anomaly-detect", "period": {"type": "RELATIVE", "days": 7},
            "bindings": [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "131", "metricKey": "co2"}]}]}
    a = client.post("/internal/analytics/analyses", json=body, headers=HEADERS).json()["response"]
    client.post(f"/internal/analytics/realtime/{a['analysisId']}", json={"enabled": True}, headers=HEADERS)
    deps.publisher = RabbitPublisher(host, amqp_port, "/", "guest", "guest", deps.clock)
    watch = pika.BlockingConnection(pika.ConnectionParameters(host=host, port=amqp_port))
    ch = watch.channel()
    ch.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
    q = ch.queue_declare("", exclusive=True).method.queue
    ch.queue_bind(q, EXCHANGE, routing_key="#")
    msgs = []
    for k in range(5):
        value = 3000.0 if k == 4 else float(hist.iloc[-288 + k])
        at = pd.Timestamp(T0) + timedelta(minutes=k)
        msgs.append({"v": 1, "messageId": str(uuid.uuid4()), "organizationId": ORG, "sourceId": 1, "externalId": "d131", "deviceId": 131,
                     "deviceStatus": "ACTIVE", "measuredAt": at.isoformat(), "receivedAt": at.isoformat(), "late": False, "virtual": False,
                     "metrics": [{"key": "co2", "value": value, "quality": 0}], "rawMessageId": k + 1})
    asyncio.run(_produce(host, stream_port, []))  # pipeline처럼 super stream을 먼저 만든다
    settings = Settings(rabbit_host=host, rabbit_vhost="/", rabbit_user="guest", rabbit_password="guest", instance_id="it", developer="it")
    engine = RealtimeEngine(deps)
    engine.reload()
    consumer = TelemetryConsumer(settings, engine, load_balancer_mode=True, port=stream_port, reload_sec=0.5)
    asyncio.run(_scenario(consumer, host, stream_port, msgs))
    assert consumer.processed == 5
    received = []
    for _ in range(20):
        method, props, payload = ch.basic_get(q, auto_ack=True)
        if method is None:
            break
        received.append((method.routing_key, json.loads(payload), props.headers))
    watch.close()
    deps.publisher.close()
    keys = [r[0] for r in received]
    assert "analytics.anomaly.detected" in keys and all(k.startswith("analytics.") for k in keys)
    event = next(r for r in received if r[0] == "analytics.anomaly.detected")
    assert event[1]["type"] == "analytics.anomaly.detected" and event[1]["payload"]["deviceId"] == "131"
    assert event[2]["messageId"] == event[1]["messageId"] and event[2]["organizationId"] == ORG
