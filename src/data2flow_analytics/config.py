"""설정(conventions.md §4). 비밀값은 환경변수로만 받는다(로컬 .env, 운영 k8s Secret).

프로필 DATA2FLOW_PROFILE: local(기본) | staging | prod. Flyway 형식 마이그레이션은 staging만 migrate, 나머지는 validate(ADR-030).
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    profile: str = "local"
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "data2flow"
    db_user: str = "data2flow"
    db_password: str = ""
    db_pool_max: int = 10
    flyway_mode: str = "validate"  # migrate | validate | none
    rabbit_host: str = "localhost"
    rabbit_port: int = 5672
    rabbit_stream_port: int = 5552
    rabbit_vhost: str = "data2flow-dev"
    rabbit_user: str = "guest"
    rabbit_password: str = "guest"
    events_enabled: bool = True
    realtime_enabled: bool = True
    developer: str = ""
    core_uri: str = "http://data2flow-core-api"
    instance_id: str = field(default_factory=socket.gethostname)
    worker_threads: int = 2
    org_run_limit: int = 5  # BR-ANA-08
    sync_points_limit: int = 50_000  # 동기 실행 기준(analytics-service.md §3)
    sync_wait_sec: float = 20.0  # 동기 실행이 이보다 오래 걸리면 비동기로 바꾼다
    store_endpoint: str | None = None
    store_access_key: str | None = None
    store_secret_key: str | None = None
    store_bucket: str | None = None
    store_dir: str = "/tmp/data2flow-analytics"
    template_dirs: str | None = None
    timezone: str = "Asia/Seoul"

    @property
    def consumer_group(self) -> str:
        """스트림 소비자 그룹. 로컬은 개발자 이름을 붙인다(ADR-030, contracts ConsumerGroups)."""
        return f"analytics-{self.developer}" if self.developer else "analytics"

    @property
    def conninfo(self) -> str:
        return (f"host={self.db_host} port={self.db_port} dbname={self.db_name} user={self.db_user} "
                f"password={self.db_password} application_name=data2flow-analytics connect_timeout=5 options='-c TimeZone=UTC'")

    @staticmethod
    def from_env() -> Settings:
        profile = _env("DATA2FLOW_PROFILE", "local")
        host = _env("DATA2FLOW_DB_HOST_INTERNAL") if profile in ("staging", "prod") else None
        return Settings(
            profile=profile,
            db_host=host or _env("DATA2FLOW_DB_HOST", "localhost"),
            db_port=int(_env("DATA2FLOW_DB_PORT", "5432")),
            db_name=_env("DATA2FLOW_DB_NAME", "data2flow"),
            db_user=_env("DATA2FLOW_DB_USERNAME", "data2flow"),
            db_password=_env("DATA2FLOW_DB_PASSWORD", ""),
            db_pool_max=int(_env("DATA2FLOW_ANALYTICS_DB_POOL_MAX", "10")),
            flyway_mode=_env("DATA2FLOW_FLYWAY_MODE", "migrate" if profile == "staging" else "validate"),
            rabbit_host=_env("DATA2FLOW_RABBITMQ_HOST", "localhost"),
            rabbit_port=int(_env("DATA2FLOW_RABBITMQ_PORT", "5672")),
            rabbit_stream_port=int(_env("DATA2FLOW_RABBITMQ_STREAM_PORT", "5552")),
            rabbit_vhost=_env("DATA2FLOW_RABBITMQ_VHOST", "data2flow-dev"),
            rabbit_user=_env("DATA2FLOW_RABBITMQ_USERNAME", "guest"),
            rabbit_password=_env("DATA2FLOW_RABBITMQ_PASSWORD", "guest"),
            events_enabled=_env("DATA2FLOW_ANALYTICS_EVENTS_ENABLED", "true") == "true",
            # 로컬은 운영과 같은 장비 데이터를 두 번 처리하지 않도록 실시간을 기본으로 끈다(deployment.md §8.2)
            realtime_enabled=_env("DATA2FLOW_ANALYTICS_REALTIME_ENABLED", "false" if profile == "local" else "true") == "true",
            developer=_env("DATA2FLOW_DEV_NAME", ""),
            core_uri=_env("DATA2FLOW_CORE_URI", "http://data2flow-core-api"),
            instance_id=_env("HOSTNAME", socket.gethostname()),
            worker_threads=int(_env("DATA2FLOW_ANALYTICS_WORKER_THREADS", "2")),
            store_endpoint=_env("DATA2FLOW_ARCHIVE_ENDPOINT"),
            store_access_key=_env("DATA2FLOW_ARCHIVE_ACCESS_KEY"),
            store_secret_key=_env("DATA2FLOW_ARCHIVE_SECRET_KEY"),
            store_bucket=_env("DATA2FLOW_ARCHIVE_BUCKET"),
            store_dir=_env("DATA2FLOW_ANALYTICS_STORE_DIR", "/tmp/data2flow-analytics"),
            template_dirs=_env("DATA2FLOW_ANALYTICS_TEMPLATE_DIRS"),
            timezone=_env("DATA2FLOW_ORG_TIMEZONE", "Asia/Seoul"),
        )
