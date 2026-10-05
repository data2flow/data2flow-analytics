"""실시간 추론(ANA-06.01·06.04): 실시간이 켜진 분석의 바인딩만 골라 들어오는 측정값마다 추론한다.

- 상태(기준선·최근 구간)는 메모리에 두고, 재시작하면 최근 데이터로 다시 만든다(analytics-service.md §6, Redis 스냅샷 없이 재계산).
- 결과는 realtime_events에 남기고(DB 커밋 뒤) `analytics.anomaly.detected`·`analytics.eta.updated`로 발행한다(EVT-ANA-02·03).
- 이벤트만 발행하고 제어 명령은 보내지 않는다(BR-ANA-13).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd

from ..analysis import repository as repo
from ..clock import iso, parse_iso
from ..common.jsonutil import to_jsonable
from ..deps import Deps
from ..events import publisher as ev
from ..loader.bindings import Source, parse_bindings
from ..loader.loader import LoadRequest

log = logging.getLogger("data2flow_analytics.realtime")
HISTORY = {"anomaly-detect": timedelta(days=7), "threshold-eta": timedelta(hours=2)}


@dataclass
class Registration:
    analysis_id: int
    organization_id: int
    template: Any
    params: Any
    device_id: int
    metric_key: str
    include_virtual: bool
    version: int
    state: Any = None


@dataclass
class RealtimeEngine:
    deps: Deps
    regs: dict[tuple[int, str], list[Registration]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reload(self) -> int:
        """실시간이 켜진 분석을 다시 읽어 등록을 맞춘다. 바뀌지 않은 등록은 상태를 그대로 둔다."""
        with self.deps.pool.connection() as conn:
            analyses = repo.realtime_analyses(conn)
        keep: dict[tuple[int, str, int], Registration] = {}
        for regs in self.regs.values():
            for r in regs:
                keep[(r.analysis_id, r.metric_key, r.device_id)] = r
        fresh: dict[tuple[int, str], list[Registration]] = {}
        for a in analyses:
            try:
                reg = self.deps.registry.get(a["template_key"], a["template_version"])
                params = reg.template.parse_params(a["params"])
                model = None
                if reg.template.realtime_requires_model(params):
                    model = self._active_model(a["id"])
                    if model is None:
                        continue
                for src in parse_bindings(a["bindings"]):
                    for device in self._devices(src):
                        old = keep.get((a["id"], src.metric_key, device))
                        if old is not None and old.version == a["version"]:
                            r = old
                        else:
                            r = Registration(a["id"], a["organization_id"], reg.template, params, device, src.metric_key,
                                             a["include_virtual"], a["version"])
                            r.state = self._init_state(r, model)
                        if r.state is not None:
                            fresh.setdefault((device, src.metric_key), []).append(r)
            except Exception:  # noqa: BLE001 — 분석 하나의 문제가 나머지를 막지 않는다
                log.exception("realtime registration failed analysis=%s", a["id"])
        with self._lock:
            self.regs = fresh
        return sum(len(v) for v in fresh.values())

    def _devices(self, src: Source) -> list[int]:
        if src.device_id is not None and src.kind != "SPACE_AGGREGATE":
            return [src.device_id]
        return self.deps.loader.devices_of(src)

    def _active_model(self, analysis_id: int):
        import pickle

        with self.deps.pool.connection() as conn:
            row = conn.execute(f"SELECT uri FROM {repo.S}.model_artifacts WHERE analysis_id = %s AND status = 'ACTIVE'",
                               (analysis_id,)).fetchone()
        if not row or not row["uri"]:
            return None
        return pickle.loads(self.deps.store.get(row["uri"]))  # noqa: S301

    def _init_state(self, r: Registration, model):
        now = self.deps.clock.now()
        span = HISTORY.get(r.template.key, timedelta(days=7))
        src = Source("target", "DEVICE_METRIC", r.metric_key, r.device_id)
        data = self.deps.loader.load(LoadRequest(r.organization_id, [src], now - span, now, "RAW", "NORMAL_ONLY", r.include_virtual),
                                     r.template)
        history = data.role("target").iloc[:, 0] if data.role("target").shape[1] else pd.Series(dtype=float)
        try:
            return r.template.realtime_init(history, r.params, model, 0)
        except Exception:  # noqa: BLE001
            log.warning("realtime state not ready analysis=%s device=%s metric=%s", r.analysis_id, r.device_id, r.metric_key)
            return None

    # ---- 측정값 처리 ----
    def handle(self, message: dict) -> list[ev.DomainEvent]:
        """CanonicalTelemetry v1 하나를 처리하고 발행한 이벤트를 돌려준다."""
        try:
            device = int(message["deviceId"])
            org = int(message["organizationId"])
            at = pd.Timestamp(parse_iso(message["measuredAt"]))
        except (KeyError, ValueError, TypeError):
            log.warning("bad telemetry message skipped")
            return []
        virtual = bool(message.get("virtual"))
        published = []
        for metric in message.get("metrics") or []:
            if int(metric.get("quality", 0)) != 0:
                continue
            with self._lock:
                regs = list(self.regs.get((device, str(metric.get("key"))), []))
            for r in regs:
                if r.organization_id != org or (virtual and not r.include_virtual):
                    continue
                out = r.template.infer(r.state, at, float(metric["value"]), r.params)
                if out:
                    published.append(self._record(r, at, out, message.get("requestId")))
        return published

    def _record(self, r: Registration, at: pd.Timestamp, out: dict, request_id: str | None) -> ev.DomainEvent:
        now = self.deps.clock.now()
        if out["type"] == "ANOMALY":
            payload = {"analysisId": str(r.analysis_id), "deviceId": str(r.device_id), "metricKey": r.metric_key, "occurredAt": iso(at),
                       "value": out["value"], "score": out["score"], "kind": out["kind"], "evidence": out["evidence"]}
            event_type = ev.ANOMALY
        else:
            payload = {"analysisId": str(r.analysis_id), "deviceId": str(r.device_id), "metricKey": r.metric_key,
                       "threshold": out["threshold"], "direction": out["direction"], "etaAt": iso(out["etaAt"]),
                       "minutesLeft": out["minutesLeft"], "confidence": out["confidence"], "occurredAt": iso(at), "value": out["value"]}
            event_type = ev.ETA
        payload = to_jsonable(payload)
        with self.deps.pool.connection() as conn:
            conn.execute(f"""INSERT INTO {repo.S}.realtime_events (organization_id, analysis_id, type, device_id, metric_key, occurred_at,
                                payload, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                         (r.organization_id, r.analysis_id, out["type"], r.device_id, r.metric_key, at.to_pydatetime(), repo.j(payload), now))
        event = ev.make_event(self.deps.clock, event_type, r.organization_id, payload, request_id)
        ev.safe_publish(self.deps.publisher, event)
        return event
