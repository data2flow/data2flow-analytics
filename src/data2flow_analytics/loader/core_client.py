"""core-api 내부 API 클라이언트(ADR-021: 토큰 없음, X-CALLER-SERVICE 표시).

- API-DEV-128 `GET /internal/core/spaces/{space-id}/devices?relation=MEASURES&includeDescendants=true` — 공간 집계 바인딩을 기기 목록으로 푼다
- API-DEV-126 `GET /internal/core/spaces/{space-id}/targets` — 쾌적도의 공간 목표 환경(DEV-01.04)
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

log = logging.getLogger("data2flow_analytics.core")


class CoreLookup(Protocol):
    def space_devices(self, space_id: int, request_id: str | None = None) -> list[int]: ...

    def space_targets(self, space_id: int, request_id: str | None = None) -> dict | None: ...


class CoreClient:
    def __init__(self, base_url: str, timeout: float = 3.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                    headers={"X-CALLER-SERVICE": "data2flow-analytics"})

    def _get(self, path: str, params: dict | None = None, request_id: str | None = None):
        headers = {"X-REQUEST-ID": request_id} if request_id else None
        res = self._client.get(path, params=params, headers=headers)
        res.raise_for_status()
        body = res.json()
        return body.get("response")

    def space_devices(self, space_id: int, request_id: str | None = None) -> list[int]:
        items = self._get(f"/internal/core/spaces/{space_id}/devices",
                          {"relation": "MEASURES", "includeDescendants": "true"}, request_id) or []
        return sorted({int(i["deviceId"]) for i in items if i.get("deviceId") is not None})

    def space_targets(self, space_id: int, request_id: str | None = None) -> dict | None:
        """{의미(metricKey): {min, max}} 또는 목표가 없으면 None. 연결 실패도 None(기본 기준으로 계산하고 표시, BR-ANA-22)."""
        try:
            body = self._get(f"/internal/core/spaces/{space_id}/targets", None, request_id) or {}
        except (httpx.HTTPError, ValueError):
            log.warning("space targets unavailable space=%s", space_id)
            return None
        out = {}
        for item in body.get("effective") or []:
            key = METRIC_SEMANTIC.get(str(item.get("metricKey", "")).lower(), item.get("metricKey"))
            out[key] = {"min": item.get("min"), "max": item.get("max")}
        return out or None


#: 표준 측정 항목 키 → 의미 태그(DEV-04 semantic). 바인딩에 semantic이 없을 때 의미 조건 검사에 쓴다(BR-ANA-02)
METRIC_SEMANTIC = {
    "temperature": "temperature", "temp": "temperature", "humidity": "humidity", "rh": "humidity", "co2": "co2",
    "tvoc": "tvoc", "pm2_5": "pm25", "pm25": "pm25", "pm10": "pm10", "battery": "battery", "activity": "activity",
    "pir": "activity", "motion": "activity", "noise": "noise", "laeq": "noise", "sound_level": "noise",
    "illuminance": "illuminance", "light_level": "illuminance", "lux": "illuminance", "energy": "energy", "kwh": "energy",
    "energy_kwh": "energy", "power": "power",
}
