"""설정·기동(ADR-030): 프로필별 마이그레이션 모드, 기동 시 템플릿 동기화, 소비자 그룹 이름."""

from __future__ import annotations

from urllib.parse import urlparse

from data2flow_analytics.bootstrap import build_deps, make_services
from data2flow_analytics.config import Settings
from data2flow_analytics.events.publisher import RecordingPublisher
from tests.conftest import FakeCore


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DATA2FLOW_PROFILE", "staging")
    monkeypatch.setenv("DATA2FLOW_DB_HOST_INTERNAL", "10.0.0.1")
    monkeypatch.setenv("DATA2FLOW_DB_HOST", "localhost")
    s = Settings.from_env()
    assert s.flyway_mode == "migrate" and s.db_host == "10.0.0.1" and s.realtime_enabled is True and s.consumer_group == "analytics"
    monkeypatch.setenv("DATA2FLOW_PROFILE", "local")
    monkeypatch.setenv("DATA2FLOW_DEV_NAME", "nhn")
    s = Settings.from_env()
    assert s.flyway_mode == "validate" and s.db_host == "localhost" and s.realtime_enabled is False and s.consumer_group == "analytics-nhn"
    assert "application_name=data2flow-analytics" in s.conninfo


def test_build_deps_against_db(conninfo, tmp_path):
    """기동: validate 통과 → 풀 → 템플릿 13종 동기화(현재 버전 1개씩) → 서비스 묶음"""
    url = urlparse(conninfo)
    settings = Settings(db_host=url.hostname, db_port=url.port, db_name=url.path.lstrip("/"), db_user=url.username,
                        db_password=url.password, flyway_mode="validate", events_enabled=False, store_dir=str(tmp_path))
    deps = build_deps(settings, core=FakeCore(), publisher=RecordingPublisher())
    try:
        with deps.pool.connection() as conn:
            rows = conn.execute("SELECT key FROM data2flow_analytics.templates WHERE current ORDER BY key").fetchall()
        assert len(rows) == 13
        services = make_services(deps, subprocess_runs=True)
        assert services.models.executor.__class__.__name__ == "SubprocessExecutor"
    finally:
        from data2flow_analytics import management

        management.attach(None)
        deps.pool.close()
