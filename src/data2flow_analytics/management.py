"""관리 포트(8081): k8s 프로브와 Prometheus 지표. Java 서비스의 actuator와 같은 경로를 쓴다(design/conventions.md §4).

API-ANA-38 `GET /health`(RAW {status, checks}), `GET /metrics`(Prometheus 텍스트)도 같은 포트에 둔다.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, Info, generate_latest

from . import __version__

APPLICATION = "data2flow-analytics"
_ready = {"value": True}
_deps: dict[str, Any] = {"deps": None}

Info("data2flow_build", "빌드 정보").info({"application": APPLICATION, "version": __version__})
RUNS = Counter("data2flow_analytics_runs_total", "끝난 분석 실행 수", ["status", "application"])
RUN_SECONDS = Histogram("data2flow_analytics_run_seconds", "분석 실행 시간(초)", ["template", "application"],
                        buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600))
REALTIME_EVENTS = Counter("data2flow_analytics_realtime_events_total", "실시간 추론 이벤트 수", ["type", "application"])

app = FastAPI(title=f"{APPLICATION}-management", docs_url=None, redoc_url=None, openapi_url=None)


def set_ready(ready: bool) -> None:
    """종료 중이거나 의존성이 끊기면 readiness를 DOWN으로 바꾼다(무중단 배포)."""
    _ready["value"] = ready


def attach(deps) -> None:
    _deps["deps"] = deps


def _checks() -> dict:
    deps = _deps["deps"]
    if deps is None:
        return {}
    checks: dict = {"templates": {"status": "UP", "registered": len(deps.registry.keys()), "rejected": sorted(deps.registry.rejected)}}
    try:
        with deps.pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        checks["db"] = {"status": "UP"}
    except Exception:  # noqa: BLE001
        checks["db"] = {"status": "DOWN"}
    return checks


@app.get("/actuator/health/liveness")
def liveness() -> dict:
    return {"status": "UP"}


@app.get("/actuator/health/readiness")
def readiness(response: Response) -> dict:
    if not _ready["value"]:
        response.status_code = 503
        return {"status": "DOWN"}
    return {"status": "UP"}


@app.get("/health")
def health(response: Response) -> dict:
    checks = _checks()
    down = not _ready["value"] or any(c.get("status") == "DOWN" for c in checks.values())
    if down:
        response.status_code = 503
    return {"status": "DOWN" if down else "UP", "checks": checks}


@app.get("/actuator/prometheus")
def prometheus() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/metrics")
def metrics() -> Response:
    return prometheus()
