"""모델 생애주기 통합 테스트(ANA-07): 학습·버전·지표·자동 적용·강제 적용·드리프트·재학습(Testcontainers PostgreSQL 18)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from data2flow_analytics.worker.executor import InlineExecutor
from data2flow_analytics.worker.main import Worker
from tests.conftest import HEADERS, T0, insert_telemetry, shift_weeks
from tests.fixtures import synth

P = "/internal/analytics"


def _forecast_analysis(client, deps, retrain="MANUAL", include_virtual=False):
    s, period = shift_weeks(synth.forecastable(3, days=21))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 111, "co2", s)
        if include_virtual:
            v = (s * 3).iloc[::2]
            v.index = v.index + timedelta(minutes=5)
            insert_telemetry(conn, 111, "co2", v, virtual=True)
    body = {"name": "CO2 예측", "templateKey": "forecast", "period": period, "resolution": "1h", "retrainPolicy": retrain,
            "includeVirtual": include_virtual,
            "bindings": [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "111", "metricKey": "co2"}]}],
            "params": {"horizonHours": 24}}
    return client.post(f"{P}/analyses", json=body, headers=HEADERS).json()["response"], period


@pytest.mark.spec("ANA-07.01")
def test_ANA_07_01_TC_ANA_145_training(client, deps):
    """[ANA-07.01][AT-ANA-06.1] 학습 요청 → 202 {modelId, version 1}, 학습 기간·파라미터·지표·uri 저장, 첫 모델은 자동 ACTIVE, 가상 제외 경고"""
    a, period = _forecast_analysis(client, deps, include_virtual=True)
    res = client.post(f"{P}/analyses/{a['analysisId']}/models/train", json={}, headers=HEADERS)
    assert res.status_code == 202 and res.json()["response"]["version"] == 1
    dup = client.post(f"{P}/analyses/{a['analysisId']}/models/train", json={}, headers=HEADERS).json()["response"]
    assert dup["modelId"] == res.json()["response"]["modelId"]  # 학습 중이면 같은 모델(중복 방지)
    worker = Worker(deps, InlineExecutor(deps.registry), None)
    assert worker.drain() == ["CANDIDATE"]
    models = client.get(f"{P}/models", headers=HEADERS).json()
    m = models["responses"][0]
    assert m["status"] == "ACTIVE" and m["version"] == 1 and m["analysisName"] == "CO2 예측"
    assert m["metrics"]["mape"] < 0.1 and m["metrics"]["warnings"] == ["VIRTUAL_EXCLUDED_FROM_TRAINING"]
    assert m["trainFrom"] == period["from"].replace("+00:00", "Z") and m["trainedAt"]
    with deps.pool.connection() as conn:
        uri = conn.execute("SELECT uri FROM data2flow_analytics.model_artifacts").fetchone()["uri"]
    assert uri.startswith("file://") and uri.endswith("/v1.pkl")
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]
    assert run["run"]["status"] == "SUCCEEDED"
    not_trainable = client.post(f"{P}/analyses", headers=HEADERS, json={"name": "x", "templateKey": "pattern-heatmap", "period": period,
                                                                       "bindings": a["bindings"]}).json()["response"]
    assert client.post(f"{P}/analyses/{not_trainable['analysisId']}/models/train", json={}, headers=HEADERS).status_code == 400
    assert client.post(f"{P}/analyses/999999/models/train", json={}, headers=HEADERS).status_code == 404


@pytest.mark.spec("ANA-07.03")
def test_ANA_07_03_TC_ANA_149_150_worse_model_stays_candidate(client, deps):
    """[ANA-07.03][AT-ANA-06.1] 새 모델이 더 나쁘면 CANDIDATE로 남고 '기존보다 성능이 낮음', force=false 적용은 409, force=true면 교체"""
    a, period = _forecast_analysis(client, deps)
    worker = Worker(deps, InlineExecutor(deps.registry), None)
    client.post(f"{P}/analyses/{a['analysisId']}/models/train", json={}, headers=HEADERS)
    worker.drain()
    with deps.pool.connection() as conn:
        conn.execute("UPDATE data2flow_analytics.model_artifacts SET metrics = jsonb_set(metrics, '{mae}', '0.0001') WHERE status = 'ACTIVE'")
    second = client.post(f"{P}/analyses/{a['analysisId']}/models/train", json={"trainFrom": period["from"], "trainTo": period["to"]},
                         headers=HEADERS).json()["response"]
    assert second["version"] == 2
    worker.drain()
    models = {m["version"]: m for m in client.get(f"{P}/models?analysisId={a['analysisId']}", headers=HEADERS).json()["responses"]}
    assert models[1]["status"] == "ACTIVE" and models[2]["status"] == "CANDIDATE" and models[2]["metrics"]["note"] == "기존보다 성능이 낮음"
    res = client.post(f"{P}/models/{second['modelId']}/activate", json={"force": False}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["header"]["resultCode"] == "MODEL_WORSE_THAN_ACTIVE"
    ok = client.post(f"{P}/models/{second['modelId']}/activate", json={"force": True}, headers=HEADERS).json()["response"]
    assert ok["status"] == "ACTIVE" and ok["previousModelId"] == models[1]["modelId"]
    models = {m["version"]: m["status"] for m in client.get(f"{P}/models", headers=HEADERS).json()["responses"]}
    assert models == {1: "RETIRED", 2: "ACTIVE"}
    assert client.post(f"{P}/models/999999/activate", json={}, headers=HEADERS).json()["header"]["resultCode"] == "ML_MODEL_NOT_FOUND"
    assert client.post(f"{P}/models/{second['modelId']}/activate", json={}, headers={**HEADERS, "X-ORG-ID": "2"}).status_code == 404


@pytest.mark.spec("ANA-07.04")
def test_ANA_07_04_TC_ANA_151_153_drift_and_retrain(client, deps, clock):
    """[ANA-07.04][AT-ANA-14.1] 매일 점검: 최근 7일 분포가 바뀌면 PSI > 0.2 → EVT-ANA-04 1건(같은 날 중복 없음), ON_DRIFT면 재학습 작업 1건"""
    a, period = _forecast_analysis(client, deps, retrain="ON_DRIFT")
    worker = Worker(deps, InlineExecutor(deps.registry), None)
    client.post(f"{P}/analyses/{a['analysisId']}/models/train", json={}, headers=HEADERS)
    worker.drain()
    # 학습 뒤 7일 동안 값이 2.5배로 바뀐 데이터가 들어오고 시계가 그만큼 흐른다
    s = synth.forecastable(9, days=7) * 2.5
    s.index = s.index + (T0 + timedelta(days=7) - s.index[-1] - timedelta(hours=1))
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 111, "co2", s)
    clock.advance(timedelta(days=7))
    out = worker.maintenance(force_daily=True)
    assert out["daily"] == {"drift": 1, "retrain": 1}
    drift = deps.publisher.of_type("analytics.model.drift")
    assert len(drift) == 1 and drift[0].payload["psi"] > 0.2
    assert worker.maintenance(force_daily=True)["daily"] == {"drift": 0, "retrain": 0}
    assert worker.drain() == ["CANDIDATE"]
    models = client.get(f"{P}/models", headers=HEADERS).json()
    assert models["totalCount"] == 2
    clock.advance(timedelta(days=1, hours=2))
    assert "daily" in worker.maintenance()
