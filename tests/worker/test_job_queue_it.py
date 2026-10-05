"""작업 큐 수명 주기(SKIP LOCKED, DEAD 3회), 조직 동시 실행 한도, 시간 초과 슬롯 반환(Testcontainers PostgreSQL 18)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from data2flow_analytics.analysis import repository as repo
from data2flow_analytics.worker.executor import InlineExecutor, RunTimeoutError
from data2flow_analytics.worker.main import Worker
from tests.conftest import HEADERS, ORG, insert_telemetry, shift_weeks
from tests.fixtures import synth


def _analysis(client, deps, device=901, org_headers=HEADERS):
    s, period = shift_weeks(synth.series(1, days=8, step="30min").series)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, device, "co2", s, org=int(org_headers["X-ORG-ID"]))
    body = {"name": "큐 시험", "templateKey": "anomaly-detect", "period": period, "resolution": "RAW",
            "bindings": [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": str(device), "metricKey": "co2"}]}]}
    res = client.post("/internal/analytics/analyses", json=body, headers=org_headers)
    assert res.status_code == 201, res.text
    return res.json()["response"]["analysisId"]


def _run(client, analysis_id, headers=HEADERS):
    res = client.post("/internal/analytics/runs", json={"analysisId": analysis_id, "mode": "ASYNC"}, headers=headers)
    assert res.status_code == 202, res.text
    return res.json()["response"]


class Boom:
    """예상하지 못한 오류를 내는 실행기(재시도·DEAD 시험)."""

    def execute(self, job, timeout, cancel):
        raise RuntimeError("db hiccup")


class Slow:
    def execute(self, job, timeout, cancel):
        raise RunTimeoutError()


@pytest.mark.spec("ANA-04.02")
def test_ANA_04_02_TC_ANA_101_lifecycle(client, deps, clock):
    """[ANA-04.02][AT-ANA-02.1] READY→LOCKED(SKIP LOCKED, 두 워커가 같은 작업을 잡지 않음)→DONE, 단계마다 EVT-ANA-01.
    워커 중단 뒤 locked_until 경과 → READY → 다른 워커가 완료. 3번 실패 → DEAD, 실행 FAILED"""
    aid = _analysis(client, deps)
    first, second = _run(client, aid), _run(client, aid)
    with deps.pool.connection() as c1, deps.pool.connection() as c2:
        j1 = repo.claim_job(c1, "w1", clock.now(), 60)
        j2 = repo.claim_job(c2, "w2", clock.now(), 60)  # 첫 트랜잭션이 잠근 행은 건너뛴다
        assert j1 and j2 and j1["id"] != j2["id"]
        c1.rollback()
        c2.rollback()
    worker = Worker(deps, InlineExecutor(deps.registry), None)
    assert worker.drain() == ["SUCCEEDED", "SUCCEEDED"]
    events = [(e.type, e.payload.get("stage"), e.payload.get("progress")) for e in deps.publisher.of_type("analytics.run.")
              if e.payload["runId"] == first["runId"]]
    assert ("analytics.run.running", "LOAD", 5) in events and ("analytics.run.running", "COMPUTE", 30) in events
    assert ("analytics.run.running", "SAVE", 90) in events and events[-1][0] == "analytics.run.succeeded"
    # 워커가 잡고 죽음 → 잠금 만료 → 회수 → 다른 워커가 완료
    third = _run(client, aid)
    with deps.pool.connection() as conn:
        repo.claim_job(conn, "dead-worker", clock.now(), 60)
    assert worker.reap() == {"ready": 0, "dead": 0}
    clock.advance(timedelta(seconds=61))
    assert worker.reap() == {"ready": 1, "dead": 0}
    assert worker.drain() == ["SUCCEEDED"]
    # 3번 실패 → DEAD
    failing = Worker(deps, Boom(), None)
    fourth = _run(client, aid)
    assert failing.tick() == "RETRY"
    for _ in range(2):
        clock.advance(timedelta(minutes=5))
        result = failing.tick()
    assert result == "FAILED"
    with deps.pool.connection() as conn:
        job = repo.jobs_for(conn, "RUN", int(fourth["runId"]))[0]
        run = repo.get_run(conn, ORG, int(fourth["runId"]))
    assert job["status"] == "DEAD" and job["attempts"] == 3
    assert run["status"] == "FAILED" and run["error_code"] == "INTERNAL_ERROR" and "db hiccup" in run["error_detail"]
    got = client.get(f"/internal/analytics/runs/{third['runId']}", headers=HEADERS).json()["response"]
    assert got["run"]["status"] == "SUCCEEDED"
    assert second["status"] == "PENDING"


@pytest.mark.spec("ANA-11.02")
def test_ANA_11_02_TC_ANA_188_org_concurrency(client, deps, clock):
    """[ANA-11.02][AT-ANA-04.1] 조직 실행 5건이 있으면 6번째는 QUEUED·순번 1·예상 시작 시각. 앞이 끝나면 FIFO로 시작, 다른 조직은 영향 없음"""
    aid = _analysis(client, deps)
    runs = [_run(client, aid) for _ in range(7)]
    assert [r["status"] for r in runs] == ["PENDING"] * 5 + ["QUEUED", "QUEUED"]
    assert runs[5]["queuePosition"] == 1 and runs[6]["queuePosition"] == 2 and runs[5]["estimatedStartAt"]
    other = {**HEADERS, "X-ORG-ID": "2"}
    aid2 = _analysis(client, deps, device=902, org_headers=other)
    assert _run(client, aid2, other)["status"] == "PENDING"
    worker = Worker(deps, InlineExecutor(deps.registry), None)
    assert worker.tick() == "SUCCEEDED"
    sixth = client.get(f"/internal/analytics/runs/{runs[5]['runId']}", headers=HEADERS).json()["response"]["run"]
    seventh = client.get(f"/internal/analytics/runs/{runs[6]['runId']}", headers=HEADERS).json()["response"]["run"]
    assert sixth["status"] == "PENDING" and seventh["status"] == "QUEUED" and seventh["queuePosition"] == 1
    assert worker.drain() == ["SUCCEEDED"] * 7
    listing = client.get(f"/internal/analytics/analyses/{aid}/runs?size=3", headers=HEADERS).json()
    assert listing["totalCount"] == 7 and listing["totalPages"] == 3 and len(listing["responses"]) == 3


@pytest.mark.spec("ANA-04.03")
def test_ANA_04_03_TC_ANA_108_timeout_releases_slot(client, deps):
    """[ANA-04.03][AT-ANA-15.1] TIMEOUT 뒤 조직 슬롯이 돌아와 QUEUED 1번이 PENDING으로 오른다. error_code=ANALYSIS_TIMEOUT"""
    aid = _analysis(client, deps)
    runs = [_run(client, aid) for _ in range(6)]
    assert Worker(deps, Slow(), None).tick() == "TIMEOUT"
    with deps.pool.connection() as conn:
        first = repo.get_run(conn, ORG, int(runs[0]["runId"]))
        sixth = repo.get_run(conn, ORG, int(runs[5]["runId"]))
    assert first["status"] == "TIMEOUT" and first["error_code"] == "ANALYSIS_TIMEOUT"
    assert first["error_message"] == "분석 시간이 초과되었습니다. 기간을 줄이거나 집계 단위를 올리세요"
    assert sixth["status"] == "PENDING"
