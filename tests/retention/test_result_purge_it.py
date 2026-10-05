"""결과 보관(ANA-05.08, BR-ANA-15): 보관 기간이 지난 결과 삭제, 실행 메타데이터는 남는다."""

from __future__ import annotations

from datetime import timedelta

import pytest

from data2flow_analytics.worker.executor import InlineExecutor
from data2flow_analytics.worker.main import Worker
from tests.conftest import HEADERS, insert_telemetry, shift_weeks
from tests.fixtures import synth

P = "/internal/analytics"


@pytest.mark.spec("ANA-05.08")
def test_ANA_05_08_TC_ANA_135_result_purge(client, deps, clock):
    """[ANA-05.08][AT-ANA-09.4] 보관 90일 조직: 91일 뒤 정리 → 결과 삭제, 실행은 목록에 남고 조회는 404(resultExpired=true)"""
    s, period = shift_weeks(synth.series(1, days=8, step="30min").series)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 151, "co2", s)
    a = client.post(f"{P}/analyses", headers=HEADERS, json={"name": "보관", "templateKey": "anomaly-detect", "period": period,
                                                            "bindings": [{"role": "target", "sources": [
                                                                {"kind": "DEVICE_METRIC", "deviceId": "151", "metricKey": "co2"}]}]}).json()["response"]
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "mode": "SYNC", "resultRetentionDays": 90}, headers=HEADERS)
    view = run.json()["response"]
    assert view["result"]["expiresAt"] == "2027-01-03T00:00:00Z"
    worker = Worker(deps, InlineExecutor(deps.registry), None)
    clock.advance(timedelta(days=89))
    assert worker.maintenance()["purgedResults"] == 0
    clock.advance(timedelta(days=2))
    assert worker.maintenance()["purgedResults"] == 1
    gone = client.get(f"{P}/runs/{view['run']['runId']}", headers=HEADERS)
    assert gone.status_code == 404 and gone.json()["resultExpired"] is True and gone.json()["run"]["status"] == "SUCCEEDED"
    listing = client.get(f"{P}/analyses/{a['analysisId']}/runs", headers=HEADERS).json()
    assert listing["totalCount"] == 1 and listing["responses"][0]["status"] == "SUCCEEDED"


def test_realtime_partitions_maintenance(deps, clock):
    """realtime_events 월 파티션을 미리 만들고 30일 지난 파티션을 지운다(pg_partman 없음)"""
    from data2flow_analytics.worker.main import drop_old_partitions, ensure_partitions

    def exists(conn, name):
        return conn.execute("SELECT to_regclass(%s) AS r", (f"data2flow_analytics.{name}",)).fetchone()["r"] is not None

    with deps.pool.connection() as conn:
        conn.execute("DELETE FROM data2flow_analytics.realtime_events")
        ensure_partitions(conn, clock.now())
        assert all(exists(conn, f"realtime_events_y2026m{m}") for m in ("10", "11", "12"))
        assert ensure_partitions(conn, clock.now()) == []
        drop_old_partitions(conn, clock.now() + timedelta(days=100))
        assert not exists(conn, "realtime_events_y2026m10") and not exists(conn, "realtime_events_y2026m11")
        assert "realtime_events_y2026m10" in ensure_partitions(conn, clock.now())
