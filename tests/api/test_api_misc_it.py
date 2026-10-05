"""내부 API 통합 테스트: 템플릿·설정·충분성·분석 정의·실행 오류·취소·비교·해설·실시간·피드백·KPI·응답 형식."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from data2flow_analytics.worker.executor import InlineExecutor
from data2flow_analytics.worker.main import Worker
from tests.conftest import HEADERS, ORG, T0, binding, insert_telemetry, shift_weeks
from tests.fixtures import synth

P = "/internal/analytics"


def _seed(deps, device=101, days=10, step="10min", metric="co2"):
    s, period = shift_weeks(synth.series(42, days=days, step=step).spike(2, 600).series)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, device, metric, s)
    return period


def _analysis(client, period, template="anomaly-detect", bindings=None, **kw):
    body = {"name": "분석", "templateKey": template, "bindings": bindings or [binding("target", 101)], "period": period,
            "resolution": "RAW", **kw}
    res = client.post(f"{P}/analyses", json=body, headers=HEADERS)
    assert res.status_code == 201, res.text
    return res.json()["response"]


@pytest.mark.spec("ANA-01.01")
def test_ANA_01_01_templates_list_detail_search_runnable(client):
    """[ANA-01.01][API-ANA-30] 목록(공통 목록 형식)·상세(설명서 9항목)·질문 검색·실행 가능성·필터"""
    res = client.get(f"{P}/templates", headers=HEADERS)
    body = res.json()
    assert res.status_code == 200 and body["header"] == {"isSuccessful": True, "resultCode": "SUCCESS", "resultMessage": "SUCCESS"}
    assert body["totalCount"] == 13 and body["page"] == 1 and {"key", "requirements", "questions", "enabled"} <= set(body["responses"][0])
    detail = client.get(f"{P}/templates/comfort-index", headers=HEADERS).json()["response"]
    assert detail["kind"] == "DOMAIN" and detail["paramsSchema"]["$schema"].endswith("2020-12/schema") and len(detail["guide"]["whenToUse"]) >= 3
    assert client.get(f"{P}/templates?category=PREDICTION", headers=HEADERS).json()["totalCount"] == 2
    assert client.get(f"{P}/templates?kind=DOMAIN", headers=HEADERS).json()["totalCount"] == 5
    search = client.get(f"{P}/templates?keyword=센서 고장", headers=HEADERS).json()["responses"]
    assert search[0]["key"] == "sensor-health" and search[0]["mode"] == "KEYWORD"
    runnable = {r["key"]: r for r in client.get(f"{P}/templates?view=runnable&semantics=noise", headers=HEADERS).json()["responses"]}
    assert runnable["comfort-index"]["runnable"] is False
    nf = client.get(f"{P}/templates/no-such?version=9.9.9", headers={**HEADERS, "Accept-Language": "en"})
    assert nf.status_code == 404 and nf.json()["header"] == {"isSuccessful": False, "resultCode": "TEMPLATE_NOT_FOUND",
                                                             "resultMessage": "Template not found"}
    assert client.get(f"{P}/templates").status_code == 401
    assert client.get(f"{P}/nothing", headers=HEADERS).json()["header"]["resultCode"] == "RESOURCE_NOT_FOUND"
    paged = client.get(f"{P}/templates?page=0&size=1000", headers=HEADERS).json()
    assert paged["page"] == 1 and paged["size"] == 100


@pytest.mark.spec("ANA-01.03")
def test_ANA_01_03_TC_ANA_012_013_disable_template(client, deps):
    """[ANA-01.03][AT-ANA-07.1~3] 비활성화 → 일정 PAUSED·실시간 OFF·갤러리 제외·실행/저장 409 TEMPLATE_DISABLED, 다시 켜도 PAUSED 유지"""
    period = _seed(deps)
    a = _analysis(client, period, schedule={"preset": "DAILY", "at": "06:00"})
    assert a["scheduleState"] == "ACTIVE" and a["nextScheduledAt"]
    assert client.post(f"{P}/realtime/{a['analysisId']}", json={"enabled": True}, headers=HEADERS).status_code == 200
    off = client.put(f"{P}/template-settings/anomaly-detect", json={"enabled": False}, headers=HEADERS).json()["response"]
    assert off["enabled"] is False and off["pausedAnalysisIds"] == [a["analysisId"]]
    detail = client.get(f"{P}/analyses/{a['analysisId']}", headers=HEADERS).json()["response"]
    assert detail["scheduleState"] == "PAUSED" and detail["realtime"] is False
    keys = [t["key"] for t in client.get(f"{P}/templates", headers=HEADERS).json()["responses"]]
    assert "anomaly-detect" not in keys and len(keys) == 12
    assert client.get(f"{P}/templates?includeDisabled=true", headers=HEADERS).json()["totalCount"] == 13
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"]}, headers=HEADERS)
    assert run.status_code == 409 and run.json()["header"]["resultCode"] == "TEMPLATE_DISABLED"
    save = client.post(f"{P}/analyses", json={"name": "x", "templateKey": "anomaly-detect", "bindings": [binding("target", 101)],
                                             "period": period}, headers=HEADERS)
    assert save.json()["header"]["resultCode"] == "TEMPLATE_DISABLED"
    client.put(f"{P}/template-settings/anomaly-detect", json={"enabled": True}, headers=HEADERS)
    assert client.get(f"{P}/analyses/{a['analysisId']}", headers=HEADERS).json()["response"]["scheduleState"] == "PAUSED"
    settings = {s["key"]: s for s in client.get(f"{P}/template-settings", headers=HEADERS).json()["responses"]}
    assert settings["anomaly-detect"]["analysisCount"] == 1 and settings["anomaly-detect"]["scheduleCount"] == 0
    other = client.get(f"{P}/templates", headers={**HEADERS, "X-ORG-ID": "2"}).json()["totalCount"]
    assert other == 13
    assert client.put(f"{P}/template-settings/anomaly-detect", json={"enabled": "no"}, headers=HEADERS).status_code == 400


@pytest.mark.spec("ANA-03.03")
def test_ANA_03_03_TC_ANA_088_check_and_run_errors(client, deps):
    """[ANA-03.03][AT-ANA-02.2·02.3] 충분성 FAIL → 실행 409 ANALYSIS_INSUFFICIENT_DATA{reason}, WARN 미확인 → 400, 한도 초과 → 409"""
    period = _seed(deps, days=10)
    short = {"type": "FIXED", "from": period["from"], "to": (pd.Timestamp(period["from"]) + timedelta(days=3)).isoformat()}
    chk = client.post(f"{P}/templates/anomaly-detect/check", headers=HEADERS,
                      json={"bindings": [binding("target", 101)], "period": short, "resolution": "RAW"}).json()["response"]
    assert chk["level"] == "FAIL" and chk["issues"][0]["message"] == "최소 7일 데이터가 필요합니다(현재 3일)"
    assert chk["issues"][0]["fix"] == {"type": "SET_PERIOD_DAYS", "value": 7}
    a = _analysis(client, short)
    res = client.post(f"{P}/runs", json={"analysisId": a["analysisId"]}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["header"]["resultCode"] == "ANALYSIS_INSUFFICIENT_DATA"
    assert "최소 7일" in res.json()["header"]["resultMessage"] and res.json()["check"]["level"] == "FAIL"
    # 누락 15% → WARN
    s = synth.series(1, days=8, step="10min").series
    s = s.drop(s.index[1::7][:int(len(s) * 0.15)])
    s, p2 = shift_weeks(s)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 202, "co2", s)
    w = _analysis(client, p2, bindings=[binding("target", 202)])
    warn = client.post(f"{P}/runs", json={"analysisId": w["analysisId"], "acknowledgeWarnings": False}, headers=HEADERS)
    assert warn.status_code == 400 and warn.json()["header"]["resultCode"] == "ANALYSIS_WARNING_NOT_ACKNOWLEDGED"
    assert client.post(f"{P}/runs", json={"analysisId": w["analysisId"], "acknowledgeWarnings": True}, headers=HEADERS).status_code == 202
    # 한도: 최대 기간 180일 초과 → 409 ANALYSIS_LIMIT_EXCEEDED
    long = _analysis(client, {"type": "RELATIVE", "days": 200})
    lim = client.post(f"{P}/runs", json={"analysisId": long["analysisId"]}, headers=HEADERS)
    assert lim.status_code == 409 and lim.json()["header"]["resultCode"] == "ANALYSIS_LIMIT_EXCEEDED"
    assert client.post(f"{P}/runs", json={"analysisId": "999999"}, headers=HEADERS).json()["header"]["resultCode"] == "ANALYSIS_NOT_FOUND"
    assert client.post(f"{P}/runs", json={"analysisId": "abc"}, headers=HEADERS).status_code == 400
    assert client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "trigger": "NOPE"}, headers=HEADERS).status_code == 400


@pytest.mark.spec("ANA-01.05")
def test_ANA_01_05_TC_ANA_024_analysis_validation(client, deps):
    """[ANA-01.05][AT-ANA-02.4·10.3] 저장 검증: 역할 누락 → ANALYSIS_BINDING_INVALID(문구 치환), 민감도 9 → ANALYSIS_PARAMS_INVALID,
    cron 오류 → ANALYSIS_SCHEDULE_INVALID, baseVersion 불일치 → VERSION_CONFLICT, 소프트 삭제 → 목록·조회에서 사라짐"""
    period = _seed(deps)
    bad = client.post(f"{P}/analyses", headers=HEADERS, json={"name": "x", "templateKey": "comfort-index", "period": period,
                                                              "bindings": [binding("temp", 101, "temperature")]})
    assert bad.status_code == 400 and bad.json()["header"]["resultCode"] == "ANALYSIS_BINDING_INVALID"
    assert bad.json()["header"]["resultMessage"] == "humidity 역할에 1~1개의 humidity 데이터를 연결하세요"
    params = client.post(f"{P}/analyses", headers=HEADERS, json={"name": "x", "templateKey": "anomaly-detect", "period": period,
                                                                 "bindings": [binding("target", 101)], "params": {"sensitivity": 9}})
    assert params.json()["header"]["resultCode"] == "ANALYSIS_PARAMS_INVALID" and params.json()["errors"][0]["field"] == "params.sensitivity"
    cron = client.post(f"{P}/analyses", headers=HEADERS, json={"name": "x", "templateKey": "anomaly-detect", "period": period,
                                                               "bindings": [binding("target", 101)], "schedule": {"cron": "0 61 * * *"}})
    assert cron.json()["header"]["resultCode"] == "ANALYSIS_SCHEDULE_INVALID"
    for body, field in (({"templateKey": "anomaly-detect"}, "name"), ({"name": "x"}, "templateKey")):
        res = client.post(f"{P}/analyses", headers=HEADERS, json={**body, "bindings": [binding("target", 101)], "period": period})
        assert res.status_code == 400 and res.json()["errors"][0]["field"] == field
    a = _analysis(client, period, retrainPolicy="MANUAL", recipients={"userIds": ["7"], "channelIds": []})
    assert a["version"] == 0 and a["owner"] == {"userId": "7"} and a["spaceScopeIds"] == []
    upd = client.put(f"{P}/analyses/{a['analysisId']}", headers=HEADERS, json={"name": "새 이름", "templateKey": "anomaly-detect",
                                                                               "bindings": [binding("target", 101)], "period": period,
                                                                               "baseVersion": 0})
    assert upd.status_code == 200 and upd.json()["response"]["version"] == 1 and upd.json()["response"]["name"] == "새 이름"
    stale = client.put(f"{P}/analyses/{a['analysisId']}", headers=HEADERS, json={"name": "x", "templateKey": "anomaly-detect",
                                                                                 "bindings": [binding("target", 101)], "period": period,
                                                                                 "baseVersion": 0})
    assert stale.status_code == 409 and stale.json()["header"]["resultCode"] == "VERSION_CONFLICT"
    assert client.put(f"{P}/analyses/{a['analysisId']}", headers=HEADERS, json={"name": "x"}).status_code == 400
    lst = client.get(f"{P}/analyses?owner=me&keyword=새", headers=HEADERS).json()
    assert lst["totalCount"] == 1 and lst["responses"][0]["targetSummary"]
    assert client.get(f"{P}/analyses?spaceIds=5", headers=HEADERS).json()["totalCount"] == 1  # 공간 바인딩 없음 → 빈 집합은 부분집합
    assert client.delete(f"{P}/analyses/{a['analysisId']}", headers=HEADERS).status_code == 204
    assert client.get(f"{P}/analyses/{a['analysisId']}", headers=HEADERS).json()["header"]["resultCode"] == "ANALYSIS_NOT_FOUND"
    assert client.delete(f"{P}/analyses/{a['analysisId']}", headers=HEADERS).status_code == 404
    assert client.put(f"{P}/analyses/999999", headers=HEADERS, json={"baseVersion": 0}).status_code == 404


def test_BR_ANA_03_space_scope_filter(client, deps):
    """BR-ANA-03: 권한 밖 공간이 섞인 분석은 목록에서 빠진다(spaceIds = 사용자 공간 범위)"""
    deps.loader.core.spaces[5] = [101]
    period = _seed(deps)
    _analysis(client, period, bindings=[{"role": "target", "sources": [{"kind": "SPACE_AGGREGATE", "spaceId": "5", "metricKey": "co2"}]}])
    assert client.get(f"{P}/analyses?spaceIds=5,6", headers=HEADERS).json()["totalCount"] == 1
    assert client.get(f"{P}/analyses?spaceIds=6", headers=HEADERS).json()["totalCount"] == 0


@pytest.mark.spec("ANA-04.04")
def test_ANA_04_04_TC_ANA_111_cancel(client, deps):
    """[ANA-04.04][AT-ANA-04.1] QUEUED·PENDING 취소는 즉시 CANCELLED, 끝난 실행 취소는 409, 없는 run은 404"""
    period = _seed(deps)
    a = _analysis(client, period)
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"]}, headers=HEADERS).json()["response"]
    cancelled = client.post(f"{P}/runs/{run['runId']}/cancel", headers=HEADERS).json()["response"]
    assert cancelled["status"] == "CANCELLED"
    assert Worker(deps, InlineExecutor(deps.registry), None).drain() == []  # 작업도 정리됨
    again = client.post(f"{P}/runs/{run['runId']}/cancel", headers=HEADERS)
    assert again.status_code == 409 and again.json()["header"]["resultCode"] == "ANALYSIS_RUN_STATE_CONFLICT"
    assert client.post(f"{P}/runs/999999/cancel", headers=HEADERS).json()["header"]["resultCode"] == "ANALYSIS_RUN_NOT_FOUND"
    assert client.get(f"{P}/runs/999999", headers=HEADERS).status_code == 404


@pytest.mark.spec("ANA-04.04")
def test_ANA_04_04_running_cancel_discards_partial_result(client, deps):
    """[ANA-04.04] RUNNING 중 취소되면 워커는 결과를 저장하지 않는다"""
    period = _seed(deps)
    a = _analysis(client, period)
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"]}, headers=HEADERS).json()["response"]

    class CancelDuring:
        def execute(self, job, timeout, cancel):
            client.post(f"{P}/runs/{run['runId']}/cancel", headers=HEADERS)
            return InlineExecutor(deps.registry).execute(job, timeout, lambda: False)

    assert Worker(deps, CancelDuring(), None).tick() == "CANCELLED"
    got = client.get(f"{P}/runs/{run['runId']}", headers=HEADERS).json()["response"]
    assert got["run"]["status"] == "CANCELLED" and "result" not in got


@pytest.mark.spec("ANA-05.05")
def test_ANA_05_05_compare_and_ai_commentary(client, deps):
    """[ANA-05.05][API-ANA-13] 같은 분석 실행끼리 핵심 수치 차이, 다른 분석이면 400. [ANA-05.04] 해설 ID 연결(TC-ANA-125)"""
    period = _seed(deps)
    a = _analysis(client, period)
    b = _analysis(client, period, params={"sensitivity": 5})
    r1 = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]["run"]["runId"]
    r2 = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "mode": "SYNC", "seed": 3}, headers=HEADERS).json()["response"]["run"]["runId"]
    r3 = client.post(f"{P}/runs", json={"analysisId": b["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]["run"]["runId"]
    cmp = client.get(f"{P}/analyses/{a['analysisId']}/runs/compare?base={r1}&target={r2}", headers=HEADERS).json()["response"]
    assert cmp["metrics"][0]["key"] == "anomalies" and cmp["metrics"][0]["direction"] == "SAME" and cmp["versionMismatch"] is False
    assert len(cmp["charts"]) == 2
    bad = client.get(f"{P}/analyses/{a['analysisId']}/runs/compare?base={r1}&target={r3}", headers=HEADERS)
    assert bad.status_code == 400
    assert client.put(f"{P}/runs/{r1}/ai-commentary", json={"commentaryId": "55"}, headers=HEADERS).status_code == 204
    assert client.get(f"{P}/runs/{r1}", headers=HEADERS).json()["response"]["result"]["aiCommentaryId"] == "55"
    assert client.put(f"{P}/runs/{r1}/ai-commentary", json={}, headers=HEADERS).status_code == 400
    assert client.put(f"{P}/runs/999999/ai-commentary", json={"commentaryId": "1"}, headers=HEADERS).status_code == 404


@pytest.mark.spec("ANA-06.01")
def test_ANA_06_01_TC_ANA_137_realtime_toggle_errors(client, deps):
    """[ANA-06.01][API-ANA-35] 실시간 미지원(correlation) → 400, ML 방식인데 ACTIVE 모델 없음 → 409 ANALYSIS_MODEL_REQUIRED"""
    period = _seed(deps)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 102, "co2", synth.series(2, days=10, step="10min").series)
    corr = _analysis(client, period, template="correlation", bindings=[{"role": "target", "sources": [
        binding("target", 101)["sources"][0], binding("target", 102)["sources"][0]]}])
    res = client.post(f"{P}/realtime/{corr['analysisId']}", json={"enabled": True}, headers=HEADERS)
    assert res.status_code == 400 and res.json()["header"]["resultCode"] == "ANALYSIS_REALTIME_NOT_SUPPORTED"
    ml = _analysis(client, period, bindings=corr["bindings"], params={"method": "ISOLATION_FOREST"})
    res = client.post(f"{P}/realtime/{ml['analysisId']}", json={"enabled": True}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["header"]["resultCode"] == "ANALYSIS_MODEL_REQUIRED"
    assert client.post(f"{P}/realtime/{corr['analysisId']}", json={"enabled": False}, headers=HEADERS).json()["response"]["realtime"] is False
    assert client.post(f"{P}/realtime/999999", json={"enabled": True}, headers=HEADERS).status_code == 404
    assert client.post(f"{P}/realtime/{corr['analysisId']}", json={}, headers=HEADERS).status_code == 400


@pytest.mark.spec("ANA-07.05")
def test_ANA_07_05_TC_ANA_155_feedback(client, deps):
    """[ANA-07.05][AT-ANA-06.1] 피드백 저장 201, runId·realtimeEventId 둘 다 없으면 400, 없는 run 404"""
    period = _seed(deps)
    a = _analysis(client, period)
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]
    row = run["result"]["tables"][0]["rows"][0]
    res = client.post(f"{P}/feedback", headers=HEADERS, json={"runId": run["run"]["runId"], "occurredAt": row["time"],
                                                              "seriesKey": row["seriesKey"], "verdict": "FALSE_POSITIVE"})
    assert res.status_code == 201 and res.json()["response"]["verdict"] == "FALSE_POSITIVE"
    assert client.post(f"{P}/feedback", headers=HEADERS, json={"occurredAt": row["time"], "seriesKey": "x",
                                                              "verdict": "TRUE_POSITIVE"}).status_code == 400
    assert client.post(f"{P}/feedback", headers=HEADERS, json={"runId": "999999", "occurredAt": row["time"], "seriesKey": "x",
                                                              "verdict": "TRUE_POSITIVE"}).json()["header"]["resultCode"] == "ANALYSIS_RUN_NOT_FOUND"
    assert client.post(f"{P}/feedback", headers=HEADERS, json={"realtimeEventId": "999999", "occurredAt": row["time"], "seriesKey": "x",
                                                              "verdict": "TRUE_POSITIVE"}).status_code == 404
    assert client.post(f"{P}/feedback", headers=HEADERS, json={"runId": run["run"]["runId"], "occurredAt": row["time"], "seriesKey": "x",
                                                              "verdict": "MAYBE"}).status_code == 400


@pytest.mark.spec("ANA-11.03")
def test_ANA_11_03_BR_ANA_11_schedule_stops_after_three_failures(client, deps):
    """[ANA-11.03][BR-ANA-11] 일정 실행 3번 연속 실패 → STOPPED_BY_FAILURE + EVT-ANA-05 1건, 이후 일정 실행 거부. 중간 성공은 0으로 초기화"""
    period = _seed(deps)
    a = _analysis(client, period, schedule={"preset": "DAILY", "at": "06:00"})

    class Fail:
        def execute(self, job, timeout, cancel):
            from data2flow_analytics.common.errors import BusinessError, ErrorCode

            raise BusinessError(ErrorCode.ANALYSIS_INSUFFICIENT_DATA, {"reason": "테스트"})

    worker_fail = Worker(deps, Fail(), None)
    worker_ok = Worker(deps, InlineExecutor(deps.registry), None)
    seq = [worker_fail, worker_fail, worker_ok, worker_fail, worker_fail, worker_fail]
    for w in seq:
        assert client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "trigger": "SCHEDULE"}, headers=HEADERS).status_code == 202
        w.tick()
    detail = client.get(f"{P}/analyses/{a['analysisId']}", headers=HEADERS).json()["response"]
    assert detail["scheduleState"] == "STOPPED_BY_FAILURE" and detail["consecutiveFailures"] == 3
    stopped = deps.publisher.of_type("analytics.schedule.stopped")
    assert len(stopped) == 1 and stopped[0].payload == {"analysisId": a["analysisId"], "ownerUserId": "7", "consecutiveFailures": 3}
    res = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "trigger": "SCHEDULE"}, headers=HEADERS)
    assert res.status_code == 409
    assert "nextScheduledAt" not in detail


@pytest.mark.spec("ANA-09.03")
def test_API_ANA_37_kpis_compute(client, deps):
    """[API-ANA-37] KPI: 센서 가용률·목표 준수율·활용률을 계산하고, 알람·장비 KPI는 M7 사유로 available=false"""
    deps.loader.core.spaces[9] = [101, 102]
    deps.loader.core.targets[9] = {"co2": {"min": None, "max": 1000}}
    df, _ = synth.occupancy(3, days=7)
    df, period = shift_weeks(df)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 101, "co2", df["co2"])
        insert_telemetry(conn, 102, "activity", df["activity"])
        conn.execute("""INSERT INTO data2flow_pipeline.data_quality_daily (device_id, day, organization_id, score, completeness, timeliness,
                        validity, stability) VALUES (101, %s, %s, 90, 80, 90, 90, 90), (102, %s, %s, 90, 100, 90, 90, 90)""",
                     (pd.Timestamp(period["from"]).date(), ORG, pd.Timestamp(period["from"]).date(), ORG))
        from tests.conftest import aggregate

        aggregate(conn, "1h")
    res = client.post(f"{P}/kpis/compute", headers=HEADERS, json={"spaceId": "9", "from": period["from"], "to": period["to"]}).json()
    kpis = {k["key"]: k for k in res["response"]["kpis"]}
    assert kpis["SENSOR_AVAILABILITY"]["value"] == 90.0 and kpis["COMPLIANCE_RATE"]["available"] is True
    assert 0 < kpis["COMPLIANCE_RATE"]["value"] <= 100 and kpis["UTILIZATION_RATE"]["available"] is True
    assert kpis["ALARM_RATE"]["available"] is False and kpis["EQUIPMENT_RUNTIME"]["available"] is False
    bad = client.post(f"{P}/kpis/compute", headers=HEADERS, json={"spaceId": "9", "from": period["from"], "to": period["to"], "keys": ["X"]})
    assert bad.status_code == 400
    empty = client.post(f"{P}/kpis/compute", headers=HEADERS, json={"spaceId": "77", "from": period["from"], "to": period["to"]}).json()
    assert all(k["available"] is False for k in empty["response"]["kpis"])
    assert T0


def test_ANA_api_2_1_core_paths(client, deps):
    """ANA-api §2.1(core M6): 분석 아래 내보내기 경로, 피드백 본문 userId, 데이터셋 본문 createdBy"""
    period = _seed(deps)
    a = _analysis(client, period)
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]
    rid = run["run"]["runId"]
    res = client.post(f"{P}/analyses/{a['analysisId']}/runs/{rid}/export", json={"format": "CSV"}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["response"]["downloadPath"].startswith("/bff/download/")
    other = _analysis(client, period)
    assert client.post(f"{P}/analyses/{other['analysisId']}/runs/{rid}/export", json={"format": "CSV"}, headers=HEADERS).status_code == 404
    no_user = {k: v for k, v in HEADERS.items() if k != "X-USER-ID"}
    row = run["result"]["tables"][0]["rows"][0]
    fb = client.post(f"{P}/feedback", headers=no_user, json={"runId": rid, "occurredAt": row["time"], "seriesKey": row["seriesKey"],
                                                             "verdict": "TRUE_POSITIVE", "userId": "9"})
    assert fb.status_code == 201
    ds = client.post(f"{P}/datasets", headers=no_user, json={"name": "d", "bindings": [binding("target", 101)], "period": period,
                                                             "createdBy": {"userId": "9"}}).json()["response"]
    assert ds["createdBy"] == {"userId": "9"}
