"""analytics-worker: 작업 큐 처리(SELECT … FOR UPDATE SKIP LOCKED), 회수·정리 스케줄, 실시간 추론.

- 작업자 스레드 N개가 READY 작업을 하나씩 잡는다. 같은 작업을 두 워커가 잡지 않는다(SKIP LOCKED).
- 회수: locked_until이 지난 LOCKED 작업은 READY로 돌리고(실행은 PENDING), attempts가 3이면 DEAD(실행 FAILED, 모델 FAILED).
- 정리: 보관 기간이 지난 결과 삭제(BR-ANA-15), realtime_events 월 파티션 생성·30일 지난 파티션 삭제(pg_partman 없음, ADR-019),
  하루 한 번 드리프트·재학습 점검(ANA-07.03·07.04). 정리는 advisory lock으로 한 워커만 한다.
- SIGTERM: 새 작업을 잡지 않고 진행 중 작업을 마친 뒤 끝낸다(무중단 배포).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from ..analysis import repository as repo
from ..analysis import runs as runs_mod
from ..common.errors import ErrorCode
from ..deps import Deps
from ..export.service import ExportService
from ..models.lifecycle import ModelService
from .runner import RunRunner

log = logging.getLogger("data2flow_analytics.worker")
_MAINT_LOCK = 0x0A11A9
REALTIME_RETENTION_DAYS = 30


class Worker:
    def __init__(self, deps: Deps, executor, analytics_service, lease_sec: int = 900):
        self.deps = deps
        self.executor = executor
        self.lease_sec = lease_sec
        self.runner = RunRunner(deps, executor)
        self.models = ModelService(deps, executor)
        self.exports = ExportService(deps, analytics_service)
        self.stop = threading.Event()
        self.name = f"{deps.settings.instance_id}"
        self._last_daily: str | None = None

    # ---- 작업 하나 ----
    def tick(self, worker_name: str | None = None) -> str | None:
        """READY 작업 하나를 잡아 처리한다. 없으면 None."""
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            job = repo.claim_job(conn, worker_name or self.name, now, self.lease_sec)
        if job is None:
            return None
        if job["type"] == "RUN":
            return self.runner.process(job)
        if job["type"] == "TRAIN":
            return self.models.process(job)
        return self.exports.process(job)

    def drain(self, limit: int = 100) -> list[str]:
        out = []
        for _ in range(limit):
            res = self.tick()
            if res is None:
                break
            out.append(res)
        return out

    # ---- 회수(erd/README §11.3) ----
    def reap(self) -> dict:
        now = self.deps.clock.now()
        out = {"ready": 0, "dead": 0}
        events = []
        with self.deps.pool.connection() as conn:
            for job in repo.expired_locks(conn, now):
                if job["attempts"] >= 3:
                    repo.dead_job(conn, job["id"], job.get("last_error") or "작업 제한 시간 초과(워커 중단)", now)
                    out["dead"] += 1
                    if job["type"] == "RUN":
                        run = repo.update_run(conn, job["ref_id"], now, expect=("PENDING", "RUNNING"), status="FAILED", finished_at=now,
                                              error_code=ErrorCode.INTERNAL_ERROR.name,
                                              error_message="분석 중 오류가 발생했습니다. 3번 시도했지만 실패했습니다",
                                              error_detail="worker lease expired 3 times")
                        if run:
                            events.append(run)
                            events.extend(runs_mod.promote(conn, run["organization_id"], self.deps.settings.org_run_limit, now))
                    elif job["type"] == "TRAIN":
                        conn.execute(f"UPDATE {repo.S}.model_artifacts SET status = 'FAILED', updated_at = %s WHERE id = %s "
                                     "AND status = 'TRAINING'", (now, job["ref_id"]))
                else:
                    repo.retry_job(conn, job["id"], job.get("last_error") or "lease expired", now, now)
                    out["ready"] += 1
                    if job["type"] == "RUN":
                        run = repo.update_run(conn, job["ref_id"], now, expect=("RUNNING",), status="PENDING", stage=None, progress=0)
                        if run:
                            events.append(run)
        for run in events:
            self.runner._publish_run(run)
        return out

    # ---- 정리 ----
    def maintenance(self, force_daily: bool = False) -> dict:
        now = self.deps.clock.now()
        out: dict = {"reaped": self.reap()}
        with self.deps.pool.connection() as conn:
            got = conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (_MAINT_LOCK,)).fetchone()["ok"]
            if not got:
                return out
            out["purgedResults"] = repo.purge_results(conn, now)
            out["partitions"] = ensure_partitions(conn, now)
            out["droppedPartitions"] = drop_old_partitions(conn, now)
        today = now.date().isoformat()
        if force_daily or (self._last_daily != today and now.hour >= 1):
            self._last_daily = today
            out["daily"] = self.models.daily_check()
        return out

    # ---- 실행 루프 ----
    def serve(self, threads: int, poll_sec: float = 1.0, maintenance_sec: float = 60.0) -> None:
        def loop(i: int) -> None:
            name = f"{self.name}-{i}"
            while not self.stop.is_set():
                try:
                    if self.tick(name) is None:
                        self.stop.wait(poll_sec)
                except Exception:  # noqa: BLE001
                    log.exception("worker loop error")
                    self.stop.wait(poll_sec)

        def maint() -> None:
            while not self.stop.is_set():
                try:
                    self.maintenance()
                except Exception:  # noqa: BLE001
                    log.exception("maintenance error")
                self.stop.wait(maintenance_sec)

        workers = [threading.Thread(target=loop, args=(i,), name=f"job-{i}", daemon=True) for i in range(threads)]
        workers.append(threading.Thread(target=maint, name="maintenance", daemon=True))
        for t in workers:
            t.start()
        for t in workers:
            t.join()


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(dt: datetime) -> datetime:
    return (_month_start(dt) + timedelta(days=32)).replace(day=1)


def ensure_partitions(conn, now: datetime, months_ahead: int = 2) -> list[str]:
    """이번 달과 다음 두 달의 realtime_events 파티션을 만든다(이미 있으면 그대로)."""
    created = []
    start = _month_start(now)
    for _ in range(months_ahead + 1):
        end = _next_month(start)
        name = f"realtime_events_y{start.year}m{start.month:02d}"
        exists = conn.execute("SELECT to_regclass(%s) AS r", (f"{repo.S}.{name}",)).fetchone()["r"]
        if not exists:
            try:
                with conn.transaction():
                    conn.execute(f"CREATE TABLE {repo.S}.{name} PARTITION OF {repo.S}.realtime_events "
                                 f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')")
                created.append(name)
            except Exception:  # noqa: BLE001 — 기본 파티션에 같은 달 행이 있으면 만들 수 없다(다음 달부터)
                log.warning("partition create skipped name=%s", name)
        start = end
    return created


def drop_old_partitions(conn, now: datetime) -> list[str]:
    """보관 30일(README §14)이 지난 달 파티션은 통째로 지우고, 기본 파티션의 오래된 행도 지운다."""
    cutoff = now - timedelta(days=REALTIME_RETENTION_DAYS)
    dropped = []
    rows = conn.execute(
        """SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
           JOIN pg_namespace n ON n.oid = p.relnamespace WHERE n.nspname = %s AND p.relname = 'realtime_events'""", (repo.S,)).fetchall()
    for r in rows:
        name = r["relname"]
        if not name.startswith("realtime_events_y"):
            continue
        year, month = int(name[17:21]), int(name[22:24])
        end = _next_month(datetime(year, month, 1, tzinfo=now.tzinfo))
        if end <= cutoff:
            conn.execute(f"DROP TABLE {repo.S}.{name}")
            dropped.append(name)
    conn.execute(f"DELETE FROM {repo.S}.realtime_events_default WHERE occurred_at < %s", (cutoff,))
    return dropped
