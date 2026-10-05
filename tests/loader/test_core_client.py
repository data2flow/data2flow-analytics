"""core-api 내부 API 클라이언트(API-DEV-128·126): 응답 봉투 해석과 실패 시 기본 기준."""

from __future__ import annotations

import httpx

from data2flow_analytics.loader.core_client import CoreClient


def _transport(status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-CALLER-SERVICE"] == "data2flow-analytics"
        if request.url.path.endswith("/devices"):
            assert request.url.params["relation"] == "MEASURES" and request.url.params["includeDescendants"] == "true"
            return httpx.Response(status, json={"header": {"isSuccessful": True}, "response": [
                {"deviceId": "12"}, {"deviceId": "11"}, {"deviceId": "12"}, {"name": "no id"}]})
        if request.url.path.endswith("/targets"):
            return httpx.Response(status, json={"header": {"isSuccessful": True}, "response": {"effective": [
                {"metricKey": "temperature", "min": 22, "max": 24}, {"metricKey": "co2", "min": None, "max": 900},
                {"metricKey": "custom_x", "min": 1, "max": 2}]}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_space_devices_and_targets():
    core = CoreClient("http://core", transport=_transport())
    assert core.space_devices(5, "req-1") == [11, 12]
    assert core.space_targets(5) == {"temperature": {"min": 22, "max": 24}, "co2": {"min": None, "max": 900},
                                     "custom_x": {"min": 1, "max": 2}}


def test_targets_unavailable_returns_none():
    """목표 환경을 못 읽으면 None → 쾌적도는 기본 기준으로 계산하고 표시한다(BR-ANA-22)"""
    assert CoreClient("http://core", transport=_transport(503)).space_targets(5) is None
