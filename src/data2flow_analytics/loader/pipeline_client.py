"""pipeline 내부 API 클라이언트(ADR-021: 토큰 없음, X-CALLER-SERVICE 표시).

- `POST /internal/pipeline/derived-points`(API-ING-25) — 가상 센서 계산값을 공간 가상 기기의 텔레메트리로 저장한다(ENE-02.02).
  요청 `{orgId, source: DERIVED, points[{deviceId, metricKey, ts, value, estimated, meta}]}`(최대 5,000점), 응답 `{accepted, rejected[]}`
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import httpx

MAX_POINTS = 5000


class DerivedSink(Protocol):
    def derived_points(self, org: int, points: list[dict]) -> dict: ...


def _utc(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PipelineClient:
    def __init__(self, base_url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                    headers={"X-CALLER-SERVICE": "data2flow-analytics"})

    def derived_points(self, org: int, points: list[dict]) -> dict:
        """points: [{deviceId, metricKey, ts(datetime), value, meta?}] → {accepted, rejected}. 5,000점씩 나눠 보낸다"""
        accepted, rejected = 0, []
        for i in range(0, len(points), MAX_POINTS):
            chunk = points[i:i + MAX_POINTS]
            body = {"orgId": org, "source": "DERIVED",
                    "points": [{"deviceId": int(p["deviceId"]), "metricKey": p["metricKey"], "ts": _utc(p["ts"]), "value": float(p["value"]),
                                "estimated": True, **({"meta": p["meta"]} if p.get("meta") else {})} for p in chunk]}
            res = self._client.post("/internal/pipeline/derived-points", json=body)
            res.raise_for_status()
            out = (res.json() or {}).get("response") or {}
            accepted += int(out.get("accepted") or 0)
            rejected.extend({"index": i + int(r["index"]), "reason": r.get("reason")} for r in out.get("rejected") or [])
        return {"accepted": accepted, "rejected": rejected}
