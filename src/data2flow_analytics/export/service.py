"""결과 내보내기(ANA-05.07, ANA-10.03): CSV(UTF-8 BOM·ISO-8601), PNG(1600×900), PDF(차트·수치·출처·AI 해설, 하단 버전·기간·생성 시각).

CSV·PNG는 바로 만들어 200, PDF는 EXPORT 작업으로 만들고 끝나면 EVT-ANA-06을 보낸다. 다운로드 링크는 BFF 경로 `/bff/download/{exportId}`이고
24시간 뒤 만료된다. 파일은 S3 호환 저장소 `analytics/exports/{org}/{exportId}.{ext}`에 둔다(BFF가 요청자 확인 뒤 사전 서명 URL로 내려받음).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta

from ..analysis import repository as repo
from ..clock import iso
from ..common.errors import BusinessError, ErrorCode, FieldError
from ..deps import Deps
from ..events import publisher as ev
from . import render

MAX_CSV_ROWS = 1_000_000
CONTENT_TYPES = {"CSV": "text/csv; charset=utf-8", "PNG": "image/png", "PDF": "application/pdf"}


class ExportService:
    def __init__(self, deps: Deps, analytics):
        self.deps = deps
        self.analytics = analytics  # AnalyticsService(결과 조회)

    def export(self, org: int, user: int | None, run_id: int, body: dict) -> tuple[int, dict]:
        fmt = str(body.get("format", "")).upper()
        if fmt not in CONTENT_TYPES:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("format", "Enum", "CSV·PNG·PDF")])
        view = self.analytics.get_run(org, run_id)
        if "result" not in view:
            raise BusinessError(ErrorCode.ANALYSIS_RUN_STATE_CONFLICT)
        now = self.deps.clock.now()
        export_id = f"ana-{run_id}-{uuid.uuid4().hex[:12]}"
        if fmt == "PDF":
            with self.deps.pool.connection() as conn:
                job = repo.insert_job(conn, org, "EXPORT", run_id, now, {"format": "PDF", "exportId": export_id, "requestedBy": user})
            return 202, {"exportJobId": str(job["id"]), "exportId": export_id}
        if fmt == "CSV":
            data = to_csv(view["result"], body.get("tableId"), body.get("chartId"))
        else:
            data = render.chart_png(_pick_chart(view["result"], body.get("chartId")))
        return 200, self._store(org, export_id, fmt, data, now)

    def _store(self, org: int, export_id: str, fmt: str, data: bytes, now: datetime) -> dict:
        ext = fmt.lower()
        key = f"exports/{org}/{export_id}.{ext}"
        uri = self.deps.store.put(key, data, CONTENT_TYPES[fmt])
        return {"exportId": export_id, "downloadPath": f"/bff/download/{export_id}", "expiresAt": iso(now + timedelta(hours=24)),
                "contentType": CONTENT_TYPES[fmt], "objectUri": uri, "bytes": len(data)}

    def process(self, job: dict) -> str:
        """EXPORT 작업(PDF)."""
        now = self.deps.clock.now()
        payload = job.get("payload") or {}
        org = job["organization_id"]
        try:
            view = self.analytics.get_run(org, job["ref_id"])
            pdf = render.report_pdf(view, now)
            stored = self._store(org, payload.get("exportId") or f"ana-{job['ref_id']}-{job['id']}", "PDF", pdf, now)
        except Exception:  # noqa: BLE001
            with self.deps.pool.connection() as conn:
                if job["attempts"] >= 3:
                    repo.dead_job(conn, job["id"], "PDF 생성 실패", now)
                else:
                    repo.retry_job(conn, job["id"], "PDF 생성 실패", now + timedelta(seconds=30), now)
            return "RETRY"
        with self.deps.pool.connection() as conn:
            repo.finish_job(conn, job["id"], now)
        ev.safe_publish(self.deps.publisher, ev.make_event(self.deps.clock, ev.EXPORT_COMPLETED, org, {
            "exportJobId": str(job["id"]), "userId": str(payload.get("requestedBy")) if payload.get("requestedBy") is not None else None,
            "downloadUrl": stored["downloadPath"], "expiresAt": stored["expiresAt"], "exportId": stored["exportId"]}))
        return "DONE"


def _pick_chart(result: dict, chart_id: str | None) -> dict:
    charts = result.get("charts") or []
    if chart_id:
        for c in charts:
            if c["id"] == chart_id:
                return c
        raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("chartId", "NotFound", "차트를 찾을 수 없습니다")])
    if not charts:
        raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("chartId", "NotFound", "차트가 없습니다")])
    return charts[0]


def to_csv(result: dict, table_id: str | None = None, chart_id: str | None = None) -> bytes:
    """표나 차트 시계열을 CSV로. UTF-8 BOM, 시각은 ISO-8601 그대로. 100만 행 초과는 409."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    tables = result.get("tables") or []
    table = None
    if table_id:
        table = next((t for t in tables if t["id"] == table_id), None)
        if table is None:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("tableId", "NotFound", "표를 찾을 수 없습니다")])
    elif not chart_id and tables:
        table = tables[0]
    if table is not None:
        if len(table["rows"]) > MAX_CSV_ROWS:
            raise BusinessError(ErrorCode.ANALYSIS_EXPORT_TOO_LARGE)
        cols = table["columns"]
        writer.writerow([c["label"] for c in cols])
        for row in table["rows"]:
            writer.writerow(["" if row.get(c["key"]) is None else _cell(row.get(c["key"])) for c in cols])
    else:
        chart = _pick_chart(result, chart_id)
        total = sum(len(s.get("data") or []) for s in chart.get("series") or [])
        if total > MAX_CSV_ROWS:
            raise BusinessError(ErrorCode.ANALYSIS_EXPORT_TOO_LARGE)
        writer.writerow(["series", "x", "y"])
        for s in chart.get("series") or []:
            for x, y in s.get("data") or []:
                writer.writerow([s.get("label", s["key"]), x, "" if y is None else y])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _cell(value) -> str:
    if isinstance(value, (list, dict)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)
