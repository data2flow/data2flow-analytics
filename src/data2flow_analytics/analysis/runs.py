"""실행 입장·대기열(BR-ANA-08)과 상태 전이(domain-model Run.status).

조직당 PENDING·RUNNING은 5건까지이고 나머지는 QUEUED(들어온 순서). 앞 실행이 끝나면 가장 오래된 QUEUED를 PENDING으로 올리고
RUN 작업을 만든다. 입장과 승격은 조직별 advisory lock 안에서 해서 서버가 여러 대여도 한도를 넘지 않는다.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from psycopg import Connection

from ..common.errors import BusinessError, ErrorCode
from . import repository as repo

TRANSITIONS = {
    "QUEUED": {"PENDING", "CANCELLED"},
    "PENDING": {"RUNNING", "CANCELLED", "QUEUED"},
    "RUNNING": {"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "PENDING"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "TIMEOUT": set(),
    "CANCELLED": set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def ensure_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise BusinessError(ErrorCode.ANALYSIS_RUN_STATE_CONFLICT)


def admit(conn: Connection, org: int, limit: int, row: dict, now: datetime, job_payload: dict | None = None,
          sync_worker: str | None = None, lease_sec: int = 600) -> tuple[dict, dict | None]:
    """실행을 만들고 자리가 있으면 PENDING + RUN 작업, 없으면 QUEUED. sync_worker를 주면 작업을 그 인스턴스가 잡은 채로 만든다."""
    repo.lock_org_admission(conn, org)
    active = repo.count_active_runs(conn, org)
    queued = active >= limit
    row = {**row, "status": "QUEUED" if queued else "PENDING", "queue_position": None, "now": now}
    run = repo.insert_run(conn, row)
    job = None
    if queued:
        repo.renumber_queue(conn, org, now)
        run = repo.get_run(conn, org, run["id"])
    elif sync_worker:
        job = repo.insert_job(conn, org, "RUN", run["id"], now, job_payload, status="LOCKED", locked_by=sync_worker,
                              locked_until=now + timedelta(seconds=lease_sec))
    else:
        job = repo.insert_job(conn, org, "RUN", run["id"], now, job_payload)
    return run, job


def promote(conn: Connection, org: int, limit: int, now: datetime) -> list[dict]:
    """자리가 난 만큼 QUEUED를 PENDING으로 올린다(FIFO, 다른 조직과 무관)."""
    repo.lock_org_admission(conn, org)
    free = limit - repo.count_active_runs(conn, org)
    promoted = []
    if free > 0:
        for run in repo.oldest_queued(conn, org, free):
            updated = repo.update_run(conn, run["id"], now, expect=("QUEUED",), status="PENDING", queue_position=None)
            if updated:
                repo.insert_job(conn, org, "RUN", run["id"], now)
                promoted.append(updated)
    repo.renumber_queue(conn, org, now)
    return promoted


def estimated_start(conn: Connection, org: int, run: dict, limit: int, now: datetime) -> datetime | None:
    """대기 순번과 최근 실행 시간의 중앙값으로 예상 시작 시각을 낸다(ANA-11.02)."""
    if run["status"] != "QUEUED" or not run.get("queue_position"):
        return None
    durations = sorted(repo.recent_durations(conn, org))
    typical = durations[len(durations) // 2] if durations else 60.0
    waves = math.ceil(run["queue_position"] / max(1, limit))
    return now + timedelta(seconds=typical * waves)
