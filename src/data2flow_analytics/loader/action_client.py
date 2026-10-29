"""action 내부 API 클라이언트(ADR-021: 토큰 없음, X-CALLER-SERVICE 표시).

- `GET /internal/action/runtime/daily?orgId=&date=&deviceIds=` — 기기 하루 가동 시간(ACT-08.02). 에너지 추정 원천(BR-ENE-04)이 쓴다
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

import httpx


class RuntimeLookup(Protocol):
    def on_seconds(self, org: int, day: date, device_ids: list[int]) -> dict[int, int]: ...


class ActionClient:
    def __init__(self, base_url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                    headers={"X-CALLER-SERVICE": "data2flow-analytics"})

    def on_seconds(self, org: int, day: date, device_ids: list[int]) -> dict[int, int]:
        """기기 ID → 그날 가동 초. 집계가 없는 기기는 빠진다"""
        out: dict[int, int] = {}
        for i in range(0, len(device_ids), 500):
            res = self._client.get("/internal/action/runtime/daily", params={
                "orgId": org, "date": day.isoformat(), "deviceIds": ",".join(str(d) for d in device_ids[i:i + 500])})
            res.raise_for_status()
            for item in (res.json().get("response") or {}).get("items") or []:
                out[int(item["deviceId"])] = int(item["onSeconds"])
        return out
