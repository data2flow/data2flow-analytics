"""Super Stream `data2flow.telemetry` 소비(rstream, MIT). 소비자 그룹 `analytics`(로컬은 `analytics-<개발자>`), 단일 활성 소비자.

파티션마다 활성 소비자 하나가 순서대로 처리하고, 처리(DB 커밋·발행)가 끝난 뒤 오프셋을 서버에 저장한다(conventions.md §13.1).
"""

from __future__ import annotations

import asyncio
import json
import logging

from rstream import (
    AMQPMessage,
    ConsumerOffsetSpecification,
    MessageContext,
    OffsetSpecification,
    OffsetType,
    SuperStreamConsumer,
    amqp_decoder,
)

from ..config import Settings
from .engine import RealtimeEngine

log = logging.getLogger("data2flow_analytics.stream")
STREAM = "data2flow.telemetry"


class TelemetryConsumer:
    def __init__(self, settings: Settings, engine: RealtimeEngine, load_balancer_mode: bool = False, stream: str = STREAM,
                 port: int | None = None, reload_sec: float = 30.0):
        self.settings = settings
        self.engine = engine
        self.stream = stream
        self.port = port or settings.rabbit_stream_port
        self.load_balancer_mode = load_balancer_mode
        self.reload_sec = reload_sec
        self.consumer: SuperStreamConsumer | None = None
        self.processed = 0
        self.subscribed = asyncio.Event()
        self._stop = asyncio.Event()

    async def _on_message(self, msg: AMQPMessage, ctx: MessageContext) -> None:
        body = msg.body if isinstance(msg.body, (bytes, bytearray)) else bytes(msg.body or b"")
        try:
            data = json.loads(body.decode("utf-8"))
            await asyncio.to_thread(self.engine.handle, data)
        except (ValueError, UnicodeDecodeError):
            log.warning("undecodable telemetry skipped offset=%s", ctx.offset)
        self.processed += 1
        # 처리 뒤에 오프셋 저장(재시작하면 다음 메시지부터)
        await ctx.consumer.store_offset(stream=ctx.stream, offset=ctx.offset, subscriber_name=ctx.subscriber_name)

    async def _on_update(self, is_active: bool, event_context) -> OffsetSpecification:
        """단일 활성 소비자로 승격되면 저장된 오프셋 다음부터, 없으면 지금부터 읽는다."""
        try:
            stored = await event_context.consumer.query_offset(stream=event_context.stream,
                                                               subscriber_name=event_context.subscriber_name)
            return OffsetSpecification(OffsetType.OFFSET, stored + 1)
        except Exception:  # noqa: BLE001 — 저장된 오프셋이 없음
            return OffsetSpecification(OffsetType.NEXT, None)

    async def run(self) -> None:
        s = self.settings
        group = s.consumer_group
        self.consumer = SuperStreamConsumer(host=s.rabbit_host, port=self.port, vhost=s.rabbit_vhost, username=s.rabbit_user,
                                            password=s.rabbit_password, super_stream=self.stream, load_balancer_mode=self.load_balancer_mode,
                                            connection_name=f"data2flow-analytics-{s.instance_id}")
        await self.consumer.start()
        await self.consumer.subscribe(callback=self._on_message, decoder=amqp_decoder,
                                      offset_specification=ConsumerOffsetSpecification(OffsetType.NEXT, None),
                                      properties={"single-active-consumer": "true", "name": group},
                                      subscriber_name=group, consumer_update_listener=self._on_update)
        self.subscribed.set()
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.reload_sec)
                except TimeoutError:
                    await asyncio.to_thread(self.engine.reload)
        finally:
            await self.consumer.close()

    def stop(self) -> None:
        self._stop.set()
