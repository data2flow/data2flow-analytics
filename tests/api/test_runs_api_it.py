"""실행 API 통합 테스트(API-ANA-32·33·34, Testcontainers PostgreSQL 18)."""

from __future__ import annotations

import pytest

from data2flow_analytics.worker.executor import InlineExecutor
from data2flow_analytics.worker.main import Worker
from tests.conftest import HEADERS, ORG, binding, insert_telemetry, shift_weeks
from tests.fixtures import synth


def _seed_co2(deps, device=101, days=10, step="10min", seed=42):
    lab = synth.series(seed, days=days, step=step).spike(2, 600)
    s, period = shift_weeks(lab.series)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, device, "co2", s)
    return lab, period


def _create(client, template="anomaly-detect", bindings=None, period=None, params=None, **kw):
    body = {"name": "실습실 CO2 이상", "templateKey": template, "bindings": bindings or [binding("target", 101)],
            "period": period or {"type": "RELATIVE", "days": 10}, "resolution": "RAW", "params": params or {"sensitivity": 3}, **kw}
    res = client.post("/internal/analytics/analyses", json=body, headers=HEADERS)
    assert res.status_code == 201, res.text
    return res.json()["response"]


@pytest.mark.spec("ANA-04.01")
def test_ANA_04_01_TC_ANA_098_sync_vs_async(client, deps):
    """[ANA-04.01][TC-ANA-098] fast 템플릿 + 5만 포인트 이하 → 200 + 결과, 넘으면 202 {runId} 후 비동기 완료"""
    _, period = _seed_co2(deps)
    analysis = _create(client, period=period)
    res = client.post("/internal/analytics/runs", json={"analysisId": analysis["analysisId"], "trigger": "MANUAL", "mode": "SYNC"},
                      headers=HEADERS)
    assert res.status_code == 200, res.text
    body = res.json()["response"]
    assert body["run"]["status"] == "SUCCEEDED"
    assert body["result"]["provenance"]["template"] == "anomaly-detect@1.0.0"
    # 주입한 급등 2건을 모두 찾고 오탐은 1건 이하(M6 완료 기준)
    assert 2 <= body["result"]["summary"]["metrics"][0]["value"] <= 3
    # 한도를 낮춰 5만 포인트 초과를 흉내 낸다 → 202 후 워커가 완료
    deps.settings.sync_points_limit = 100
    res = client.post("/internal/analytics/runs", json={"analysisId": analysis["analysisId"], "mode": "SYNC"}, headers=HEADERS)
    assert res.status_code == 202
    run_id = res.json()["response"]["runId"]
    assert res.headers["Location"].endswith(f"/runs/{run_id}")
    worker = Worker(deps, InlineExecutor(deps.registry), None)
    assert worker.drain() == ["SUCCEEDED"]
    got = client.get(f"/internal/analytics/runs/{run_id}", headers=HEADERS).json()["response"]
    assert got["run"]["status"] == "SUCCEEDED" and got["result"]["summary"]["level"] == "WARN"
    statuses = [e.type for e in deps.publisher.of_type("analytics.run.")]
    assert "analytics.run.pending" in statuses and "analytics.run.succeeded" in statuses
    assert ORG == 1


def _template_v2(deps):
    """템플릿 새 버전(1.1.0)을 등록한다(템플릿 교체 픽스처)."""
    from data2flow_analytics.bootstrap import sync_registry
    from data2flow_analytics.templates.builtin.anomaly_detect.template import AnomalyDetect

    class V110(AnomalyDetect):
        version = "1.1.0"

    reg = deps.registry.get("anomaly-detect")
    deps.registry.register(V110(), reg.guide.markdown)
    sync_registry(deps)


@pytest.mark.spec("ANA-01.02")
def test_ANA_01_02_TC_ANA_011_result_keeps_template_version(client, deps):
    """[ANA-01.02][AT-ANA-07.1] 1.0.0으로 실행한 결과를 남기고 템플릿을 1.1.0으로 올린 뒤 과거 run → template_version=1.0.0 그대로"""
    _, period = _seed_co2(deps)
    analysis = _create(client, period=period)
    run = client.post("/internal/analytics/runs", json={"analysisId": analysis["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]
    _template_v2(deps)
    old = client.get(f"/internal/analytics/runs/{run['run']['runId']}", headers=HEADERS).json()["response"]
    assert old["run"]["templateVersion"] == "1.0.0" and old["result"]["provenance"]["template"] == "anomaly-detect@1.0.0"
    assert old["result"]["charts"]
    detail = client.get("/internal/analytics/templates/anomaly-detect", headers=HEADERS).json()["response"]
    assert detail["version"] == "1.1.0" and detail["versions"] == ["1.0.0", "1.1.0"]
    pinned = client.get(f"/internal/analytics/analyses/{analysis['analysisId']}", headers=HEADERS).json()["response"]
    assert pinned["templateVersion"] == "1.0.0"


@pytest.mark.spec("ANA-10.02")
def test_ANA_10_02_TC_ANA_181_rerun_same_result(client, deps):
    """[ANA-10.02][AT-ANA-03.2·11.2] 실제 DB 로드를 거쳐 2회 실행해도 summary·이상 목록·내용 해시가 같다(시드는 provenance에 고정)"""
    _, period = _seed_co2(deps)
    analysis = _create(client, period=period)
    results = []
    for _ in range(2):
        body = client.post("/internal/analytics/runs", json={"analysisId": analysis["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()
        results.append(body["response"]["result"])
    assert results[0]["summary"] == results[1]["summary"] and results[0]["tables"] == results[1]["tables"]
    assert results[0]["contentHash"] == results[1]["contentHash"]
    assert results[0]["provenance"]["seed"] == results[1]["provenance"]["seed"]
    seeded = client.post("/internal/analytics/runs", json={"analysisId": analysis["analysisId"], "mode": "SYNC", "seed": 7},
                         headers=HEADERS).json()["response"]["result"]
    assert seeded["provenance"]["seed"] == 7


@pytest.mark.spec("ANA-10.01")
def test_ANA_10_01_TC_ANA_177_dataset_version_pinned(client, deps):
    """[ANA-10.01][AT-ANA-11.1] v1 데이터셋을 쓰는 분석, 데이터셋에 기기 1대 추가(v2) → 실행 datasetVersion=1, 상세 newerDatasetVersion=2"""
    _, period = _seed_co2(deps)
    ds = client.post("/internal/analytics/datasets", json={"name": "실습실 CO2", "bindings": [binding("target", 101)], "period": period},
                     headers=HEADERS)
    assert ds.status_code == 201
    ds = ds.json()["response"]
    analysis = _create(client, period=period, datasetId=ds["datasetId"])
    assert analysis["datasetVersion"] == 1
    upd = client.put(f"/internal/analytics/datasets/{ds['datasetId']}", headers=HEADERS, json={
        "name": "실습실 CO2(+1)", "bindings": [{"role": "target", "sources": [binding("target", 101)["sources"][0], binding("target", 102)["sources"][0]]}],
        "period": period, "baseVersion": 1})
    assert upd.status_code == 200 and upd.json()["response"]["version"] == 2
    stale = client.put(f"/internal/analytics/datasets/{ds['datasetId']}", headers=HEADERS,
                       json={"name": "x", "bindings": [binding("target", 101)], "period": period, "baseVersion": 1})
    assert stale.status_code == 409 and stale.json()["header"]["resultCode"] == "VERSION_CONFLICT"
    run = client.post("/internal/analytics/runs", json={"analysisId": analysis["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]
    assert run["result"]["provenance"]["datasetVersion"] == 1 and len(run["result"]["provenance"]["bindings"][0]["sources"]) == 1
    detail = client.get(f"/internal/analytics/analyses/{analysis['analysisId']}", headers=HEADERS).json()["response"]
    assert detail["newerDatasetVersion"] == 2
    up = client.post(f"/internal/analytics/datasets/{ds['datasetId']}/analyses/{analysis['analysisId']}/upgrade-dataset", headers=HEADERS)
    assert up.json()["response"]["datasetVersion"] == 2
    versions = client.get(f"/internal/analytics/datasets/{ds['datasetId']}/versions", headers=HEADERS).json()
    assert [v["version"] for v in versions["responses"]] == [2, 1] and versions["responses"][0]["bindingCount"] == 1
    listing = client.get("/internal/analytics/datasets", headers=HEADERS).json()["responses"][0]
    assert listing["analysisCount"] == 1 and listing["version"] == 2
    assert client.delete(f"/internal/analytics/datasets/{ds['datasetId']}", headers=HEADERS).json()["header"]["resultCode"] == "DATASET_IN_USE"
    assert client.get("/internal/analytics/datasets/999999", headers=HEADERS).json()["header"]["resultCode"] == "DATASET_NOT_FOUND"
    assert client.get(f"/internal/analytics/datasets/{ds['datasetId']}", headers={**HEADERS, "X-ORG-ID": "2"}).status_code == 404
