"""응답 모양(ANA-api 부록 A). DB 컬럼 snake_case → camelCase, ID는 문자열, 시각은 ISO-8601 UTC."""

from __future__ import annotations

from datetime import datetime

from ..clock import iso
from ..common.jsonutil import to_jsonable
from ..templates.registry import Registered


def sid(value) -> str | None:
    return None if value is None else str(value)


def template_summary(reg: Registered, enabled: bool | None = None) -> dict:
    t = reg.template
    out = {
        "key": t.key, "version": t.version, "name": t.name, "kind": t.kind.value, "category": t.category.value,
        "summary": str(reg.guide.sections.get("summary", "")), "questions": reg.guide.questions,
        "roles": [r.to_json() for r in t.roles], "requirements": t.requirements.to_json(), "sampleImageUrl": t.sample_image_url,
        "fast": t.fast, "realtime": t.realtime, "trainable": t.trainable,
    }
    if enabled is not None:
        out["enabled"] = enabled
    return to_jsonable({k: v for k, v in out.items() if v is not None})


def template_detail(reg: Registered, versions: list[str], enabled: bool | None) -> dict:
    out = template_summary(reg, enabled)
    out.update({"paramsSchema": reg.template.params_schema(), "guide": reg.guide.to_json(), "guideMarkdown": reg.guide.markdown,
                "versions": versions, "outputs": list(reg.template.outputs)})
    return out


def analysis_view(row: dict, next_at: datetime | None = None, newer_dataset_version: int | None = None) -> dict:
    out = {
        "analysisId": sid(row["id"]), "name": row["name"], "templateKey": row["template_key"], "templateVersion": row["template_version"],
        "datasetId": sid(row.get("dataset_id")), "datasetVersion": row.get("dataset_version"), "bindings": row["bindings"],
        "period": row["period"], "resolution": row["resolution"], "qualityFilter": row["quality_filter"],
        "includeVirtual": row["include_virtual"], "params": row["params"], "schedule": row.get("schedule"),
        "scheduleState": row.get("schedule_state"), "consecutiveFailures": row.get("consecutive_failures"),
        "recipients": row.get("recipients"), "retrainPolicy": row.get("retrain_policy"), "realtime": row["realtime"],
        "owner": {"userId": sid(row["owner_user_id"])}, "spaceScopeIds": [str(s) for s in (row.get("space_scope_ids") or [])],
        "status": row["status"], "version": row["version"], "createdAt": iso(row["created_at"]), "updatedAt": iso(row["updated_at"]),
        "nextScheduledAt": iso(next_at), "newerDatasetVersion": newer_dataset_version,
    }
    return to_jsonable({k: v for k, v in out.items() if v is not None})


def analysis_list_item(row: dict, next_at: datetime | None) -> dict:
    target = []
    for b in row.get("bindings") or []:
        for s in b.get("sources") or []:
            target.append(s.get("label") or s.get("metricKey"))
    out = {
        "analysisId": sid(row["id"]), "name": row["name"], "templateKey": row["template_key"], "templateVersion": row["template_version"],
        "targetSummary": ", ".join(t for t in target[:3] if t) + (f" 외 {len(target) - 3}" if len(target) > 3 else ""),
        "lastRun": {"runId": sid(row.get("last_run_id")), "status": row.get("last_run_status"),
                    "finishedAt": iso(row.get("last_run_finished_at"))} if row.get("last_run_id") else None,
        "nextScheduledAt": iso(next_at), "scheduleState": row.get("schedule_state"), "realtime": row["realtime"],
        "owner": {"userId": sid(row["owner_user_id"])},
    }
    return to_jsonable({k: v for k, v in out.items() if v is not None})


def run_view(row: dict, estimated_start: datetime | None = None) -> dict:
    out = {
        "runId": sid(row["id"]), "analysisId": sid(row["analysis_id"]), "status": row["status"], "trigger": row["trigger"],
        "progress": row.get("progress"), "stage": row.get("stage"), "queuePosition": row.get("queue_position") if row["status"] == "QUEUED" else None,
        "estimatedStartAt": iso(estimated_start), "periodFrom": iso(row["period_from"]), "periodTo": iso(row["period_to"]),
        "startedAt": iso(row.get("started_at")), "finishedAt": iso(row.get("finished_at")), "errorCode": row.get("error_code"),
        "errorMessage": row.get("error_message"), "errorDetail": row.get("error_detail"), "templateVersion": row["template_version"],
        "requestedBy": sid(row.get("requested_by")),
    }
    return to_jsonable({k: v for k, v in out.items() if v is not None})


def result_view(row: dict, provenance: dict | None) -> dict:
    commentary = row.get("ai_commentary") or {}
    out = {
        "summary": row["summary"], "charts": row["charts"], "tables": row["tables"], "evidence": row.get("evidence"),
        "caveats": list(row.get("caveats") or []), "provenance": provenance or {},
        "aiCommentaryId": sid(commentary.get("commentaryId")), "expiresAt": iso(row["expires_at"]), "contentHash": row.get("content_hash"),
    }
    return to_jsonable({k: v for k, v in out.items() if v is not None})
