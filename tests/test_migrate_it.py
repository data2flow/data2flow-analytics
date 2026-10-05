"""Flyway 형식 마이그레이션(ADR-030: staging migrate, prod·local validate)."""

from __future__ import annotations

import psycopg
import pytest

from data2flow_analytics.db.migrate import Migration, MigrationError, discover, migrate, run, validate


def test_migration_applied_and_validates(conninfo):
    """V1이 적용되어 17개 테이블과 Flyway 이력이 있고, validate가 통과한다. 다시 migrate해도 아무것도 하지 않는다"""
    validate(conninfo)
    assert migrate(conninfo) == []
    run(conninfo, "none")
    with psycopg.connect(conninfo) as conn:
        tables = conn.execute("""SELECT count(*) FROM pg_tables WHERE schemaname = 'data2flow_analytics'
                                 AND tablename NOT LIKE 'realtime_events_%' AND tablename <> 'flyway_schema_history'""").fetchone()[0]
        history = conn.execute("SELECT version, type, success, checksum FROM data2flow_analytics.flyway_schema_history").fetchall()
    assert tables == 17
    mig = discover()[0]
    assert history == [(mig.version, "SQL", True, mig.checksum())]
    with pytest.raises(MigrationError):
        run(conninfo, "weird")


def test_validate_detects_changes(conninfo, tmp_path):
    """미적용 파일·체크섬 불일치·이름 규칙 위반을 잡는다"""
    extra = tmp_path / "V202701010000__later.sql"
    extra.write_text("SELECT 1;", encoding="utf-8")
    from data2flow_analytics.db import migrate as m

    with pytest.raises(MigrationError, match="미적용"):
        validate(conninfo, tmp_path)
    (tmp_path / "bad.sql").write_text("", encoding="utf-8")
    (tmp_path / "Vx__.sql").write_text("", encoding="utf-8")
    with pytest.raises(MigrationError):
        discover(tmp_path)
    changed = tmp_path / "chg"
    changed.mkdir()
    original = discover()[0]
    (changed / original.script).write_text(original.path.read_text(encoding="utf-8") + "\n-- 바뀜\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="체크섬"):
        validate(conninfo, changed)
    with pytest.raises(MigrationError, match="바뀌었습니다"):
        migrate(conninfo, changed)
    assert isinstance(original, Migration) and m.SCHEMA == "data2flow_analytics"
