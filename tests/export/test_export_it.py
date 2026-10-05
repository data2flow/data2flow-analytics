"""결과 내보내기(ANA-05.07)와 PDF 보고서(ANA-10.03)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from data2flow_analytics.export.service import MAX_CSV_ROWS, to_csv
from data2flow_analytics.worker.executor import InlineExecutor
from data2flow_analytics.worker.main import Worker
from tests.conftest import HEADERS, insert_telemetry, shift_weeks
from tests.fixtures import synth

P = "/internal/analytics"


def _succeeded_run(client, deps):
    s, period = shift_weeks(synth.series(42, days=8, step="10min").spike(2, 600).series)
    with deps.pool.connection() as conn:
        insert_telemetry(conn, 141, "co2", s)
    body = {"name": "내보내기", "templateKey": "anomaly-detect", "period": period, "resolution": "RAW",
            "bindings": [{"role": "target", "sources": [{"kind": "DEVICE_METRIC", "deviceId": "141", "metricKey": "co2"}]}]}
    a = client.post(f"{P}/analyses", json=body, headers=HEADERS).json()["response"]
    run = client.post(f"{P}/runs", json={"analysisId": a["analysisId"], "mode": "SYNC"}, headers=HEADERS).json()["response"]
    return a, run


def _read(deps, uri: str) -> bytes:
    return deps.store.get(uri)


@pytest.mark.spec("ANA-05.07")
def test_ANA_05_07_TC_ANA_130_export_csv_png_pdf(client, deps):
    """[ANA-05.07][AT-ANA-09.3] CSV(UTF-8 BOM·ISO-8601), PNG(1600×900), PDF(202 → 작업 완료 뒤 EVT-ANA-06), 다운로드 경로 24시간"""
    _, run = _succeeded_run(client, deps)
    rid = run["run"]["runId"]
    res = client.post(f"{P}/runs/{rid}/export", json={"format": "CSV"}, headers=HEADERS)
    assert res.status_code == 200
    csv = res.json()["response"]
    assert csv["downloadPath"] == f"/bff/download/{csv['exportId']}" and csv["expiresAt"] == "2026-10-06T00:00:00Z"
    data = _read(deps, csv["objectUri"])
    assert data.startswith("﻿".encode()) and "시각" in data.decode("utf-8-sig").splitlines()[0]
    assert "T" in data.decode("utf-8-sig").splitlines()[1].split(",")[0] and data.decode("utf-8-sig").splitlines()[1].split(",")[0].endswith("Z")
    chart_csv = client.post(f"{P}/runs/{rid}/export", json={"format": "CSV", "chartId": "raw"}, headers=HEADERS).json()["response"]
    assert _read(deps, chart_csv["objectUri"]).decode("utf-8-sig").splitlines()[0] == "series,x,y"
    png = client.post(f"{P}/runs/{rid}/export", json={"format": "PNG", "chartId": "raw"}, headers=HEADERS).json()["response"]
    assert Image.open(io.BytesIO(_read(deps, png["objectUri"]))).size == (1600, 900)
    pdf = client.post(f"{P}/runs/{rid}/export", json={"format": "PDF"}, headers={**HEADERS, "X-USER-ID": "8"})
    assert pdf.status_code == 202
    job_id = pdf.json()["response"]["exportJobId"]
    assert Worker(deps, InlineExecutor(deps.registry), client.app.state.services.analytics).drain() == ["DONE"]
    done = deps.publisher.of_type("analytics.export.completed")
    assert len(done) == 1 and done[0].payload["exportJobId"] == job_id and done[0].payload["userId"] == "8"
    assert done[0].payload["downloadUrl"].startswith("/bff/download/ana-")
    assert client.post(f"{P}/runs/{rid}/export", json={"format": "DOCX"}, headers=HEADERS).status_code == 400
    assert client.post(f"{P}/runs/{rid}/export", json={"format": "CSV", "tableId": "nope"}, headers=HEADERS).status_code == 400
    assert client.post(f"{P}/runs/{rid}/export", json={"format": "PNG", "chartId": "nope"}, headers=HEADERS).status_code == 400


@pytest.mark.spec("ANA-05.07")
def test_ANA_05_07_TC_ANA_131_too_large():
    """[ANA-05.07][TC-ANA-131] 100만 행 초과 CSV → 409 ANALYSIS_EXPORT_TOO_LARGE"""
    from data2flow_analytics.common.errors import BusinessError

    result = {"tables": [{"id": "t", "title": "t", "columns": [{"key": "a", "label": "a", "type": "number"}],
                          "rows": [{"a": 1}] * (MAX_CSV_ROWS + 1)}], "charts": []}
    with pytest.raises(BusinessError) as exc:
        to_csv(result)
    assert exc.value.code.name == "ANALYSIS_EXPORT_TOO_LARGE" and exc.value.code.status == 409


@pytest.mark.spec("ANA-10.03")
def test_ANA_10_03_TC_ANA_183_pdf_report(client, deps):
    """[ANA-10.03][AT-ANA-09.3] PDF에 차트 이미지·핵심 수치·데이터 출처·AI 해설, 하단에 템플릿 버전·기간·생성 시각(한글 CID 글꼴)"""
    from pypdf import PdfReader

    from data2flow_analytics.export.render import report_pdf

    _, run = _succeeded_run(client, deps)
    client.put(f"{P}/runs/{run['run']['runId']}/ai-commentary", json={"commentaryId": "77"}, headers=HEADERS)
    view = client.get(f"{P}/runs/{run['run']['runId']}", headers=HEADERS).json()["response"]
    pdf = report_pdf(view, deps.clock.now())
    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "anomaly-detect@1.0.0" in text and "2026-10-05T00:00:00Z" in text and "77" in text
    assert any(page.images for page in reader.pages)
    assert b"HYGothic-Medium" in pdf
