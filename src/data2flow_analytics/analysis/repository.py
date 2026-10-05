"""`data2flow_analytics` 스키마 SQL(쓰기는 이 스키마에만, conventions.md §6). 모든 조회는 조직 ID를 받는다(IAM-04.05)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

S = "data2flow_analytics"
ACTIVE_RUN = ("PENDING", "RUNNING")
TERMINAL = ("SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED")
_ADMIT_LOCK = 0x0A11A1  # 조직별 실행 입장(BR-ANA-08) advisory lock 네임스페이스


def j(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


# ---------------------------------------------------------------- templates
def sync_templates(conn: Connection, manifests: list[dict], now: datetime) -> None:
    """코드의 템플릿을 전역 테이블에 올린다(전역, organization_id 없음). key마다 최신 버전 하나만 current."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMIT_LOCK + 1,))
    latest: dict[str, str] = {}
    for m in manifests:
        conn.execute(
            f"""INSERT INTO {S}.templates (key, version, name, kind, category, roles, params_schema, requirements, guide_md, fast,
                   realtime, trainable, sample_image_url, current, created_at, updated_at)
                VALUES (%(key)s, %(version)s, %(name)s, %(kind)s, %(category)s, %(roles)s, %(params)s, %(req)s, %(guide)s, %(fast)s,
                   %(realtime)s, %(trainable)s, %(img)s, false, %(now)s, %(now)s)
                ON CONFLICT (key, version) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind, category = EXCLUDED.category,
                   roles = EXCLUDED.roles, params_schema = EXCLUDED.params_schema, requirements = EXCLUDED.requirements,
                   guide_md = EXCLUDED.guide_md, fast = EXCLUDED.fast, realtime = EXCLUDED.realtime, trainable = EXCLUDED.trainable,
                   sample_image_url = EXCLUDED.sample_image_url, updated_at = EXCLUDED.updated_at""",
            {"key": m["key"], "version": m["version"], "name": m["name"], "kind": m["kind"], "category": m["category"],
             "roles": j(m["roles"]), "params": j(m["paramsSchema"]), "req": j(m["requirements"]), "guide": m["guideMarkdown"],
             "fast": m["fast"], "realtime": m["realtime"], "trainable": m["trainable"], "img": m.get("sampleImageUrl"), "now": now})
        if m.get("current"):
            latest[m["key"]] = m["version"]
    for key, version in latest.items():
        conn.execute(f"UPDATE {S}.templates SET current = false WHERE key = %s AND version <> %s AND current", (key, version))
        conn.execute(f"UPDATE {S}.templates SET current = true WHERE key = %s AND version = %s AND NOT current", (key, version))


def template_settings(conn: Connection, org: int) -> dict[str, bool]:
    rows = conn.execute(f"SELECT template_key, enabled FROM {S}.template_settings WHERE organization_id = %s", (org,)).fetchall()
    return {r["template_key"]: r["enabled"] for r in rows}


def set_template_enabled(conn: Connection, org: int, key: str, enabled: bool, user: int | None, now: datetime) -> None:
    conn.execute(
        f"""INSERT INTO {S}.template_settings (organization_id, template_key, enabled, created_by, updated_by, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (organization_id, template_key) DO UPDATE SET enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by,
              updated_at = EXCLUDED.updated_at, version = {S}.template_settings.version + 1""",
        (org, key, enabled, user, user, now, now))


def template_usage(conn: Connection, org: int) -> dict[str, dict]:
    rows = conn.execute(
        f"""SELECT template_key, count(*) AS analyses, count(*) FILTER (WHERE schedule_state = 'ACTIVE') AS schedules,
                   count(*) FILTER (WHERE realtime) AS realtimes
            FROM {S}.analyses WHERE organization_id = %s AND status = 'ACTIVE' GROUP BY template_key""", (org,)).fetchall()
    return {r["template_key"]: r for r in rows}


def pause_template_usage(conn: Connection, org: int, key: str, now: datetime) -> list[int]:
    """BR-ANA-17: 일정은 PAUSED, 실시간은 끈다(다시 켜도 자동 재개하지 않음)."""
    rows = conn.execute(
        f"""UPDATE {S}.analyses SET schedule_state = CASE WHEN schedule_state = 'ACTIVE' THEN 'PAUSED' ELSE schedule_state END,
               realtime = false, updated_at = %s
            WHERE organization_id = %s AND template_key = %s AND status = 'ACTIVE' AND (schedule_state = 'ACTIVE' OR realtime)
            RETURNING id""", (now, org, key)).fetchall()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- analyses
ANALYSIS_COLS = """id, organization_id, template_key, template_version, dataset_id, dataset_version, name, bindings, period, resolution,
  quality_filter, include_virtual, params, schedule, schedule_state, consecutive_failures, recipients, retrain_policy, realtime,
  owner_user_id, space_scope_ids, status, version, created_by, updated_by, created_at, updated_at"""


def insert_analysis(conn: Connection, row: dict) -> dict:
    return conn.execute(
        f"""INSERT INTO {S}.analyses (organization_id, template_key, template_version, dataset_id, dataset_version, name, bindings, period,
               resolution, quality_filter, include_virtual, params, schedule, schedule_state, recipients, retrain_policy, owner_user_id,
               space_scope_ids, created_by, updated_by, created_at, updated_at)
            VALUES (%(org)s, %(template_key)s, %(template_version)s, %(dataset_id)s, %(dataset_version)s, %(name)s, %(bindings)s,
               %(period)s, %(resolution)s, %(quality_filter)s, %(include_virtual)s, %(params)s, %(schedule)s, %(schedule_state)s,
               %(recipients)s, %(retrain_policy)s, %(owner)s, %(spaces)s, %(user)s, %(user)s, %(now)s, %(now)s)
            RETURNING {ANALYSIS_COLS}""", row).fetchone()


def update_analysis(conn: Connection, row: dict) -> dict | None:
    return conn.execute(
        f"""UPDATE {S}.analyses SET template_key = %(template_key)s, template_version = %(template_version)s, dataset_id = %(dataset_id)s,
               dataset_version = %(dataset_version)s, name = %(name)s, bindings = %(bindings)s, period = %(period)s,
               resolution = %(resolution)s, quality_filter = %(quality_filter)s, include_virtual = %(include_virtual)s,
               params = %(params)s, schedule = %(schedule)s, schedule_state = %(schedule_state)s, recipients = %(recipients)s,
               retrain_policy = %(retrain_policy)s, owner_user_id = %(owner)s, space_scope_ids = %(spaces)s, updated_by = %(user)s,
               updated_at = %(now)s, version = version + 1
            WHERE id = %(id)s AND organization_id = %(org)s AND status = 'ACTIVE' AND version = %(base_version)s
            RETURNING {ANALYSIS_COLS}""", row).fetchone()


def get_analysis(conn: Connection, org: int, analysis_id: int, for_update: bool = False, include_archived: bool = False) -> dict | None:
    status = "" if include_archived else " AND status = 'ACTIVE'"
    lock = " FOR UPDATE" if for_update else ""
    return conn.execute(f"SELECT {ANALYSIS_COLS} FROM {S}.analyses WHERE id = %s AND organization_id = %s{status}{lock}",
                        (analysis_id, org)).fetchone()


def list_analyses(conn: Connection, org: int, filters: dict, page: int, size: int) -> tuple[list[dict], int]:
    where = ["a.organization_id = %(org)s", "a.status = 'ACTIVE'"]
    if filters.get("templateKey"):
        where.append("a.template_key = %(templateKey)s")
    if filters.get("ownerUserId") is not None:
        where.append("a.owner_user_id = %(ownerUserId)s")
    if filters.get("scheduleState"):
        where.append("a.schedule_state = %(scheduleState)s")
    if filters.get("realtime") is not None:
        where.append("a.realtime = %(realtime)s")
    if filters.get("keyword"):
        where.append("a.name ILIKE %(kw)s")
        filters["kw"] = f"%{filters['keyword']}%"
    if filters.get("spaceIds") is not None:
        where.append("a.space_scope_ids <@ %(spaceIds)s::bigint[]")  # BR-ANA-03: 권한 밖 공간이 섞인 분석은 숨긴다
    params = {"org": org, **filters, "limit": size, "offset": (page - 1) * size}
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT count(*) AS n FROM {S}.analyses a WHERE {sql_where}", params).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT a.*, r.id AS last_run_id, r.status AS last_run_status, r.finished_at AS last_run_finished_at
            FROM {S}.analyses a
            LEFT JOIN LATERAL (SELECT id, status, finished_at FROM {S}.runs WHERE analysis_id = a.id ORDER BY created_at DESC, id DESC LIMIT 1) r
              ON true
            WHERE {sql_where} ORDER BY a.updated_at DESC, a.id DESC LIMIT %(limit)s OFFSET %(offset)s""", params).fetchall()
    return rows, total


def archive_analysis(conn: Connection, org: int, analysis_id: int, user: int | None, now: datetime) -> bool:
    row = conn.execute(
        f"""UPDATE {S}.analyses SET status = 'ARCHIVED', realtime = false,
               schedule_state = CASE WHEN schedule IS NULL THEN NULL ELSE 'PAUSED' END, updated_by = %s, updated_at = %s,
               version = version + 1
            WHERE id = %s AND organization_id = %s AND status = 'ACTIVE' RETURNING id""", (user, now, analysis_id, org)).fetchone()
    return row is not None


def set_realtime(conn: Connection, org: int, analysis_id: int, enabled: bool, now: datetime) -> None:
    conn.execute(f"UPDATE {S}.analyses SET realtime = %s, updated_at = %s WHERE id = %s AND organization_id = %s",
                 (enabled, now, analysis_id, org))


def realtime_analyses(conn: Connection) -> list[dict]:
    """실시간이 켜진 분석(모든 조직, 워커 등록용)."""
    return conn.execute(f"SELECT {ANALYSIS_COLS} FROM {S}.analyses WHERE realtime AND status = 'ACTIVE' ORDER BY id").fetchall()


# ---------------------------------------------------------------- runs
RUN_COLS = """id, organization_id, analysis_id, template_version, trigger, status, queue_position, progress, stage, period_from, period_to,
  provenance, error_code, error_message, error_detail, requested_by, started_at, finished_at, created_at, updated_at"""


def lock_org_admission(conn: Connection, org: int) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_ADMIT_LOCK, int(org) % 2_147_483_647))


def count_active_runs(conn: Connection, org: int) -> int:
    return conn.execute(f"SELECT count(*) AS n FROM {S}.runs WHERE organization_id = %s AND status IN ('PENDING', 'RUNNING')",
                        (org,)).fetchone()["n"]


def insert_run(conn: Connection, row: dict) -> dict:
    return conn.execute(
        f"""INSERT INTO {S}.runs (organization_id, analysis_id, template_version, trigger, status, queue_position, progress, period_from,
               period_to, provenance, requested_by, created_at, updated_at)
            VALUES (%(org)s, %(analysis_id)s, %(template_version)s, %(trigger)s, %(status)s, %(queue_position)s, 0, %(period_from)s,
               %(period_to)s, %(provenance)s, %(requested_by)s, %(now)s, %(now)s)
            RETURNING {RUN_COLS}""", row).fetchone()


def get_run(conn: Connection, org: int, run_id: int, for_update: bool = False) -> dict | None:
    lock = " FOR UPDATE" if for_update else ""
    return conn.execute(f"SELECT {RUN_COLS} FROM {S}.runs WHERE id = %s AND organization_id = %s{lock}", (run_id, org)).fetchone()


def get_run_any_org(conn: Connection, run_id: int) -> dict | None:
    return conn.execute(f"SELECT {RUN_COLS} FROM {S}.runs WHERE id = %s", (run_id,)).fetchone()


def run_status(conn: Connection, run_id: int) -> str | None:
    row = conn.execute(f"SELECT status FROM {S}.runs WHERE id = %s", (run_id,)).fetchone()
    return row["status"] if row else None


def list_runs(conn: Connection, org: int, analysis_id: int, page: int, size: int) -> tuple[list[dict], int]:
    total = conn.execute(f"SELECT count(*) AS n FROM {S}.runs WHERE organization_id = %s AND analysis_id = %s", (org, analysis_id)).fetchone()["n"]
    rows = conn.execute(f"SELECT {RUN_COLS} FROM {S}.runs WHERE organization_id = %s AND analysis_id = %s ORDER BY created_at DESC, id DESC "
                        "LIMIT %s OFFSET %s", (org, analysis_id, size, (page - 1) * size)).fetchall()
    return rows, total


def update_run(conn: Connection, run_id: int, now: datetime, expect: tuple[str, ...] | None = None, **fields) -> dict | None:
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    params = {**{k: (j(v) if k == "provenance" else v) for k, v in fields.items()}, "id": run_id, "now": now}
    cond = ""
    if expect:
        cond = " AND status = ANY(%(expect)s)"
        params["expect"] = list(expect)
    return conn.execute(f"UPDATE {S}.runs SET {sets}, updated_at = %(now)s WHERE id = %(id)s{cond} RETURNING {RUN_COLS}", params).fetchone()


def renumber_queue(conn: Connection, org: int, now: datetime) -> None:
    conn.execute(
        f"""UPDATE {S}.runs r SET queue_position = q.pos, updated_at = %s
            FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS pos FROM {S}.runs
                  WHERE organization_id = %s AND status = 'QUEUED') q
            WHERE r.id = q.id AND r.queue_position IS DISTINCT FROM q.pos""", (now, org))


def oldest_queued(conn: Connection, org: int, limit: int) -> list[dict]:
    return conn.execute(f"SELECT {RUN_COLS} FROM {S}.runs WHERE organization_id = %s AND status = 'QUEUED' ORDER BY created_at, id "
                        "LIMIT %s FOR UPDATE SKIP LOCKED", (org, limit)).fetchall()


def recent_durations(conn: Connection, org: int, limit: int = 20) -> list[float]:
    rows = conn.execute(f"""SELECT extract(epoch FROM finished_at - started_at) AS sec FROM {S}.runs
                            WHERE organization_id = %s AND status = 'SUCCEEDED' AND started_at IS NOT NULL
                            ORDER BY finished_at DESC LIMIT %s""", (org, limit)).fetchall()
    return [float(r["sec"]) for r in rows if r["sec"] is not None]


def bump_schedule_failures(conn: Connection, analysis_id: int, failed: bool, now: datetime) -> dict | None:
    """BR-ANA-11: 일정 실행이 실패하면 +1, 성공하면 0. 3번째 연속 실패면 STOPPED_BY_FAILURE."""
    if failed:
        return conn.execute(
            f"""UPDATE {S}.analyses SET consecutive_failures = consecutive_failures + 1,
                   schedule_state = CASE WHEN consecutive_failures + 1 >= 3 AND schedule_state = 'ACTIVE' THEN 'STOPPED_BY_FAILURE'
                                         ELSE schedule_state END, updated_at = %s
                WHERE id = %s RETURNING id, organization_id, owner_user_id, consecutive_failures, schedule_state""",
            (now, analysis_id)).fetchone()
    conn.execute(f"UPDATE {S}.analyses SET consecutive_failures = 0 WHERE id = %s AND consecutive_failures <> 0", (analysis_id,))
    return None


# ---------------------------------------------------------------- results
def insert_result(conn: Connection, run_id: int, org: int, result: dict, content_hash: str, expires_at: datetime, now: datetime) -> None:
    conn.execute(
        f"""INSERT INTO {S}.results (run_id, organization_id, summary, charts, tables, evidence, caveats, content_hash, expires_at, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (run_id, org, j(result["summary"]), j(result["charts"]), j(result["tables"]), j(result.get("evidence")),
         result.get("caveats") or [], content_hash, expires_at, now))


def get_result(conn: Connection, org: int, run_id: int) -> dict | None:
    return conn.execute(f"SELECT * FROM {S}.results WHERE run_id = %s AND organization_id = %s", (run_id, org)).fetchone()


def set_ai_commentary(conn: Connection, org: int, run_id: int, value: dict) -> bool:
    row = conn.execute(f"UPDATE {S}.results SET ai_commentary = %s WHERE run_id = %s AND organization_id = %s RETURNING run_id",
                       (j(value), run_id, org)).fetchone()
    return row is not None


def purge_results(conn: Connection, now: datetime) -> int:
    """BR-ANA-15: 보관 기간이 지난 결과를 지운다. 실행 메타데이터(runs)는 남긴다."""
    return conn.execute(f"DELETE FROM {S}.results WHERE expires_at < %s", (now,)).rowcount


# ---------------------------------------------------------------- jobs (SKIP LOCKED 큐, erd/README §11.3)
JOB_COLS = "id, organization_id, type, ref_id, status, locked_by, locked_until, attempts, available_at, last_error, payload"


def insert_job(conn: Connection, org: int, job_type: str, ref_id: int, now: datetime, payload: dict | None = None,
               status: str = "READY", locked_by: str | None = None, locked_until: datetime | None = None) -> dict:
    return conn.execute(
        f"""INSERT INTO {S}.jobs (organization_id, type, ref_id, status, locked_by, locked_until, attempts, available_at, payload,
               created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {JOB_COLS}""",
        (org, job_type, ref_id, status, locked_by, locked_until, 1 if status == "LOCKED" else 0, now, j(payload or {}), now, now)).fetchone()


def claim_job(conn: Connection, worker: str, now: datetime, lease_sec: int, types: tuple[str, ...] = ("RUN", "TRAIN", "EXPORT")) -> dict | None:
    return conn.execute(
        f"""UPDATE {S}.jobs SET status = 'LOCKED', locked_by = %(worker)s, locked_until = %(until)s, attempts = attempts + 1,
               updated_at = %(now)s
            WHERE id = (SELECT id FROM {S}.jobs WHERE status = 'READY' AND available_at <= %(now)s AND type = ANY(%(types)s)
                        ORDER BY available_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING {JOB_COLS}""",
        {"worker": worker, "until": now + _seconds(lease_sec), "now": now, "types": list(types)}).fetchone()


def extend_job(conn: Connection, job_id: int, until: datetime, now: datetime) -> None:
    conn.execute(f"UPDATE {S}.jobs SET locked_until = %s, updated_at = %s WHERE id = %s AND status = 'LOCKED'", (until, now, job_id))


def finish_job(conn: Connection, job_id: int, now: datetime) -> None:
    conn.execute(f"UPDATE {S}.jobs SET status = 'DONE', locked_until = NULL, updated_at = %s WHERE id = %s", (now, job_id))


def retry_job(conn: Connection, job_id: int, error: str, available_at: datetime, now: datetime) -> None:
    conn.execute(f"""UPDATE {S}.jobs SET status = 'READY', locked_by = NULL, locked_until = NULL, last_error = %s, available_at = %s,
                         updated_at = %s WHERE id = %s""", (error[:4000], available_at, now, job_id))


def dead_job(conn: Connection, job_id: int, error: str, now: datetime) -> None:
    conn.execute(f"UPDATE {S}.jobs SET status = 'DEAD', locked_until = NULL, last_error = %s, updated_at = %s WHERE id = %s",
                 (error[:4000], now, job_id))


def expired_locks(conn: Connection, now: datetime) -> list[dict]:
    return conn.execute(f"SELECT {JOB_COLS} FROM {S}.jobs WHERE status = 'LOCKED' AND locked_until < %s ORDER BY id FOR UPDATE SKIP LOCKED",
                        (now,)).fetchall()


def jobs_for(conn: Connection, job_type: str, ref_id: int) -> list[dict]:
    return conn.execute(f"SELECT {JOB_COLS} FROM {S}.jobs WHERE type = %s AND ref_id = %s ORDER BY id", (job_type, ref_id)).fetchall()


def get_job(conn: Connection, org: int, job_id: int) -> dict | None:
    return conn.execute(f"SELECT {JOB_COLS} FROM {S}.jobs WHERE id = %s AND organization_id = %s", (job_id, org)).fetchone()


def _seconds(sec: float):
    from datetime import timedelta

    return timedelta(seconds=sec)
