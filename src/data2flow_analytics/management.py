"""관리 포트(8081): k8s 프로브와 Prometheus 지표. Java 서비스의 actuator와 같은 경로를 쓴다(design/conventions.md §4)."""

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Info, generate_latest

from . import __version__

APPLICATION = "data2flow-analytics"
_ready = {"value": True}

Info("data2flow_build", "빌드 정보").info({"application": APPLICATION, "version": __version__})

app = FastAPI(title=f"{APPLICATION}-management", docs_url=None, redoc_url=None, openapi_url=None)


def set_ready(ready: bool) -> None:
    """종료 중이거나 의존성이 끊기면 readiness를 DOWN으로 바꾼다(무중단 배포)."""
    _ready["value"] = ready


@app.get("/actuator/health/liveness")
def liveness() -> dict:
    return {"status": "UP"}


@app.get("/actuator/health/readiness")
def readiness(response: Response) -> dict:
    if not _ready["value"]:
        response.status_code = 503
        return {"status": "DOWN"}
    return {"status": "UP"}


@app.get("/actuator/prometheus")
def prometheus() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
