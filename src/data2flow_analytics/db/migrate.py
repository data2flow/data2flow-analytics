"""Flyway 형식 SQL 마이그레이션(design/erd/analytics.md: Python 서비스도 Flyway 형식 SQL을 쓴다).

`db/migration/V{yyyyMMddHHmm}__{설명}.sql`을 버전 순으로 적용하고 이력을 Flyway와 같은 모양의
`data2flow_analytics.flyway_schema_history`에 남긴다(체크섬도 Flyway와 같은 CRC32 방식). 그래서 나중에 Flyway CLI로 바꿔도
이력을 그대로 쓸 수 있다. migrate는 staging 배포 때만, prod·local은 validate만 한다(ADR-030). 여러 파드가 함께 떠도
pg_advisory_lock으로 한 번만 적용한다.
"""

from __future__ import annotations

import logging
import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import psycopg

log = logging.getLogger("data2flow_analytics.migrate")

SCHEMA = "data2flow_analytics"
HISTORY = f"{SCHEMA}.flyway_schema_history"
MIGRATION_DIR = Path(__file__).parent / "migration"
_FILE = re.compile(r"^V(?P<version>[0-9_.]+)__(?P<desc>.+)\.sql$")
_LOCK_KEY = 0x0DA7A2F10A  # 분석 스키마 마이그레이션 전용 advisory lock 키


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    script: str
    path: Path

    @property
    def sort_key(self) -> tuple[int, ...]:
        return tuple(int(p) for p in re.split(r"[._]", self.version) if p)

    def checksum(self) -> int:
        """Flyway와 같은 체크섬: 줄 끝을 뺀 각 줄(UTF-8)을 CRC32에 이어 넣고 부호 있는 32비트 정수로."""
        crc = 0
        text = self.path.read_text(encoding="utf-8")
        if text.startswith("﻿"):
            text = text[1:]
        for line in text.splitlines():
            crc = zlib.crc32(line.encode("utf-8"), crc)
        return crc - (1 << 32) if crc >= (1 << 31) else crc


def discover(directory: Path = MIGRATION_DIR) -> list[Migration]:
    out = []
    for path in directory.glob("V*.sql"):
        m = _FILE.match(path.name)
        if not m:
            raise MigrationError(f"마이그레이션 파일 이름이 규칙과 다릅니다: {path.name}")
        out.append(Migration(m.group("version").replace("_", "."), m.group("desc").replace("_", " "), path.name, path))
    return sorted(out, key=lambda mig: mig.sort_key)


def _ensure_history(conn: psycopg.Connection) -> None:
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {HISTORY} (
          installed_rank integer      NOT NULL,
          version        varchar(50),
          description    varchar(200) NOT NULL,
          type           varchar(20)  NOT NULL,
          script         varchar(1000) NOT NULL,
          checksum       integer,
          installed_by   varchar(100) NOT NULL,
          installed_on   timestamp    NOT NULL DEFAULT now(),
          execution_time integer      NOT NULL,
          success        boolean      NOT NULL,
          CONSTRAINT flyway_schema_history_pk PRIMARY KEY (installed_rank)
        )""")


def _applied(conn: psycopg.Connection) -> dict[str, tuple[int, bool]]:
    exists = conn.execute("SELECT to_regclass(%s)", (HISTORY,)).fetchone()[0]
    if not exists:
        return {}
    rows = conn.execute(f"SELECT version, checksum, success FROM {HISTORY} WHERE version IS NOT NULL").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def validate(conninfo: str, directory: Path = MIGRATION_DIR) -> None:
    """적용된 이력과 파일이 같은지 본다. 적용 안 된 파일이나 체크섬 불일치가 있으면 실패(prod 기동 차단)."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        applied = _applied(conn)
    problems = []
    for mig in discover(directory):
        state = applied.get(mig.version)
        if state is None:
            problems.append(f"미적용 {mig.script}")
        elif not state[1]:
            problems.append(f"실패한 적용 {mig.script}")
        elif state[0] != mig.checksum():
            problems.append(f"체크섬 불일치 {mig.script}")
    if problems:
        raise MigrationError("마이그레이션 검증 실패: " + ", ".join(problems))


def migrate(conninfo: str, directory: Path = MIGRATION_DIR, installed_by: str = "data2flow-analytics") -> list[str]:
    """적용하지 않은 마이그레이션을 버전 순으로 적용한다. 적용한 스크립트 이름을 돌려준다."""
    done: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            _ensure_history(conn)
            applied = _applied(conn)
            for mig in discover(directory):
                state = applied.get(mig.version)
                if state is not None:
                    if state[0] != mig.checksum():
                        raise MigrationError(f"이미 적용한 마이그레이션이 바뀌었습니다: {mig.script}")
                    continue
                started = time.monotonic()
                sql = mig.path.read_text(encoding="utf-8")
                with conn.transaction():
                    conn.execute(sql)
                    rank = conn.execute(f"SELECT coalesce(max(installed_rank), 0) + 1 FROM {HISTORY}").fetchone()[0]
                    conn.execute(
                        f"INSERT INTO {HISTORY} (installed_rank, version, description, type, script, checksum, installed_by, "
                        "execution_time, success) VALUES (%s, %s, %s, 'SQL', %s, %s, %s, %s, true)",
                        (rank, mig.version, mig.description, mig.script, mig.checksum(), installed_by,
                         int((time.monotonic() - started) * 1000)))
                log.info("migration applied script=%s", mig.script)
                done.append(mig.script)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return done


def run(conninfo: str, mode: str) -> None:
    if mode == "migrate":
        migrate(conninfo)
    elif mode == "validate":
        validate(conninfo)
    elif mode != "none":
        raise MigrationError(f"알 수 없는 마이그레이션 모드: {mode}")
