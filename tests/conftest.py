"""공통 픽스처. 통합 테스트(*_it.py)는 Testcontainers PostgreSQL 18을 쓴다(H2 금지, s3·s4 공용 서버에 닿지 않음).

data2flow_pipeline 스키마는 pipeline 저장소 마이그레이션 사본(tests/fixtures/pipeline_schema/, 원본
data2flow-pipeline/src/main/resources/db/migration)으로 만들고, data2flow_analytics는 이 서비스의 Flyway 형식 마이그레이션으로 만든다.
시간은 MutableClock으로 옮기고 sleep은 쓰지 않는다.
"""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import psycopg
import pytest

from data2flow_analytics.bootstrap import make_services, sync_registry
from data2flow_analytics.clock import MutableClock
from data2flow_analytics.config import Settings
from data2flow_analytics.db import create_pool
from data2flow_analytics.db.migrate import migrate
from data2flow_analytics.deps import Deps
from data2flow_analytics.events.publisher import RecordingPublisher
from data2flow_analytics.loader.loader import TelemetryLoader
from data2flow_analytics.models.store import FileStore
from data2flow_analytics.templates.registry import build_registry

# 수치 라이브러리 스레드 과다 사용을 막아 테스트 시간을 안정시킨다
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
# Docker Desktop(macOS)에서 Ryuk가 호스트 소켓 경로를 마운트하지 못하는 문제를 피한다(CI의 Linux에서는 같은 값)
os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")
PIPELINE_DIR = Path(__file__).parent / "fixtures" / "pipeline_schema"
T0 = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.fspath.basename.endswith("_it.py"):
            item.add_marker(pytest.mark.it)


@pytest.fixture(scope="session")
def pg_url():
    url = os.getenv("DATA2FLOW_TEST_PG_URL")
    if url:
        yield url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18", username="data2flow", password="data2flow", dbname="data2flow", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def conninfo(pg_url) -> str:
    url = pg_url.replace("postgresql+psycopg2://", "postgresql://").replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS data2flow_pipeline")
        exists = conn.execute("SELECT to_regclass('data2flow_pipeline.telemetry')").fetchone()[0]
        if not exists:
            for f in sorted(PIPELINE_DIR.glob("V*.sql")):
                conn.execute("SET search_path TO data2flow_pipeline")
                conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("SET search_path TO public")
    migrate(url)
    return url


class FakeCore:
    """core-api 내부 API 대역(API-DEV-128·126)."""

    def __init__(self):
        self.spaces: dict[int, list[int]] = {}
        self.targets: dict[int, dict] = {}

    def space_devices(self, space_id, request_id=None):
        return list(self.spaces.get(int(space_id), []))

    def space_targets(self, space_id, request_id=None):
        return self.targets.get(int(space_id))


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(T0)


@pytest.fixture
def deps(conninfo, clock, tmp_path):
    pool = create_pool(conninfo, max_size=8)
    pool.wait(timeout=30)
    with pool.connection() as conn:
        conn.execute("""TRUNCATE data2flow_analytics.anomaly_feedback, data2flow_analytics.realtime_events, data2flow_analytics.model_artifacts,
                        data2flow_analytics.results, data2flow_analytics.jobs, data2flow_analytics.runs, data2flow_analytics.analyses,
                        data2flow_analytics.dataset_versions, data2flow_analytics.datasets, data2flow_analytics.template_settings CASCADE""")
        conn.execute("""TRUNCATE data2flow_pipeline.telemetry, data2flow_pipeline.telemetry_long, data2flow_pipeline.telemetry_1m,
                        data2flow_pipeline.telemetry_1h, data2flow_pipeline.telemetry_1d, data2flow_pipeline.device_state,
                        data2flow_pipeline.data_quality_daily, data2flow_pipeline.agg_watermarks""")
    settings = Settings(profile="test", events_enabled=False, realtime_enabled=False, instance_id="test", sync_wait_sec=60.0,
                        store_dir=str(tmp_path / "store"))
    d = Deps(settings=settings, pool=pool, registry=build_registry(), loader=TelemetryLoader(pool, FakeCore()),
             publisher=RecordingPublisher(), clock=clock, store=FileStore(str(tmp_path / "store")))
    sync_registry(d)
    yield d
    pool.close()


@pytest.fixture
def services(deps):
    return make_services(deps)


@pytest.fixture
def client(services):
    from fastapi.testclient import TestClient

    from data2flow_analytics.api import create_app

    return TestClient(create_app(services), raise_server_exceptions=False)


ORG = 1
HEADERS = {"X-ORG-ID": str(ORG), "X-USER-ID": "7", "X-CALLER-SERVICE": "data2flow-core-api", "X-REQUEST-ID": "req-1"}


def insert_telemetry(conn, device_id: int, metric: str, series: pd.Series, *, org: int = ORG, quality=0, virtual: bool = False) -> int:
    """원본 telemetry에 COPY로 넣는다. quality는 정수 또는 같은 길이의 Series."""
    s = series.dropna()
    q = quality if isinstance(quality, pd.Series) else pd.Series(quality, index=s.index)
    buf = io.StringIO()
    for t, v in s.items():
        buf.write(f"{device_id}\t{metric}\t{pd.Timestamp(t).isoformat()}\t{org}\t{float(v)!r}\t{int(q.get(t, 0))}\t0\t{'t' if virtual else 'f'}\t"
                  f"{pd.Timestamp(t).isoformat()}\n")
    buf.seek(0)
    with conn.cursor() as cur, cur.copy("COPY data2flow_pipeline.telemetry (device_id, metric_key, time, organization_id, value, quality, "
                                        "flags, is_virtual, received_at) FROM STDIN") as cp:
        cp.write(buf.read())
    return len(s)


def aggregate(conn, level: str = "1h") -> None:
    """pipeline 연속 집계 대역: 원본에서 telemetry_{level}을 채운다(품질 정상만 avg, count_all은 전체)."""
    interval = {"1m": "1 minute", "1h": "1 hour", "1d": "1 day"}[level]
    origin = "2000-01-01 00:00:00+09" if level == "1d" else "2000-01-01 00:00:00+00"
    conn.execute(f"""INSERT INTO data2flow_pipeline.telemetry_{level} (device_id, metric_key, bucket, organization_id, count, count_all, avg, min,
                        max, sum, first, last, is_virtual)
                     SELECT device_id, metric_key, date_bin(interval '{interval}', time, timestamptz '{origin}') AS b, organization_id,
                            count(*) FILTER (WHERE quality = 0), count(*), avg(value) FILTER (WHERE quality = 0),
                            min(value) FILTER (WHERE quality = 0), max(value) FILTER (WHERE quality = 0), sum(value) FILTER (WHERE quality = 0),
                            min(value), max(value), bool_or(is_virtual)
                     FROM data2flow_pipeline.telemetry GROUP BY device_id, metric_key, b, organization_id
                     ON CONFLICT DO NOTHING""")
    conn.execute("""INSERT INTO data2flow_pipeline.agg_watermarks (level, processed_until) VALUES (%s, %s)
                    ON CONFLICT (level) DO UPDATE SET processed_until = EXCLUDED.processed_until""", (level, T0))


def binding(role: str, device_id: int | None = None, metric: str = "co2", space_id: int | None = None, **kw) -> dict:
    src = {"kind": "SPACE_AGGREGATE" if device_id is None else "DEVICE_METRIC", "metricKey": metric, **kw}
    if device_id is not None:
        src["deviceId"] = str(device_id)
    if space_id is not None:
        src["spaceId"] = str(space_id)
    return {"role": role, "sources": [src]}


def shift_weeks(series_or_frame, end=T0):
    """합성 데이터를 요일이 맞게(정수 주) 옮겨 end 직전에 끝나게 한다. (옮긴 데이터, FIXED 기간)"""
    obj = series_or_frame.copy()
    step = obj.index[1] - obj.index[0]
    weeks = int((end - (obj.index[-1] + step)) // timedelta(days=7))
    obj.index = obj.index + timedelta(days=7 * weeks)
    period = {"type": "FIXED", "from": obj.index[0].isoformat(), "to": (obj.index[-1] + step).isoformat()}
    return obj, period
