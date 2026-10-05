"""M0 완료 기준: 프로브와 지표(OPS-01, NFR-06)"""

from fastapi.testclient import TestClient

from data2flow_analytics import management
from data2flow_analytics.api import app as api_app

client = TestClient(management.app)


def test_probes_up():
    assert client.get("/actuator/health/liveness").json() == {"status": "UP"}
    assert client.get("/actuator/health/readiness").json() == {"status": "UP"}


def test_readiness_down_while_shutting_down():
    management.set_ready(False)
    try:
        res = client.get("/actuator/health/readiness")
        assert res.status_code == 503
        assert res.json() == {"status": "DOWN"}
    finally:
        management.set_ready(True)


def test_prometheus_has_application_label():
    res = client.get("/actuator/prometheus")
    assert res.status_code == 200
    assert 'application="data2flow-analytics"' in res.text


def test_api_has_no_public_docs():
    api = TestClient(api_app)
    assert api.get("/docs").status_code == 404


def test_health_and_metrics_aliases():
    """API-ANA-38: /health는 {status, checks}, /metrics는 Prometheus 텍스트"""
    res = client.get("/health")
    assert res.status_code == 200 and res.json()["status"] == "UP"
    assert "data2flow_build_info" in client.get("/metrics").text
    management.set_ready(False)
    try:
        assert client.get("/health").status_code == 503
    finally:
        management.set_ready(True)


def test_health_checks_with_deps(deps):
    management.attach(deps)
    try:
        body = client.get("/health").json()
        assert body["checks"]["db"]["status"] == "UP" and body["checks"]["templates"]["registered"] == 13
    finally:
        management.attach(None)
