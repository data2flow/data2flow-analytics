"""기동: 설정 → 마이그레이션(staging만 migrate, 그 밖 validate, ADR-030) → 연결 풀 → 템플릿 등록·동기화 → 서비스 묶음."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import management
from .analysis import repository as repo
from .analysis.datasets import DatasetService
from .analysis.service import AnalyticsService
from .clock import Clock, SystemClock
from .config import Settings
from .db import create_pool
from .db.migrate import run as run_migrations
from .deps import Deps
from .events.publisher import EventPublisher, RabbitPublisher, RecordingPublisher
from .export.service import ExportService
from .kpi import KpiService
from .loader.core_client import CoreClient, CoreLookup
from .loader.loader import TelemetryLoader
from .models.lifecycle import ModelService
from .models.store import ObjectStore, build_store
from .templates.registry import TemplateRegistry, build_registry, extra_dirs_from_env
from .worker.executor import InlineExecutor, SubprocessExecutor

log = logging.getLogger("data2flow_analytics.bootstrap")


@dataclass
class Services:
    deps: Deps
    analytics: AnalyticsService
    datasets: DatasetService
    models: ModelService
    exports: ExportService
    kpis: KpiService

    def close(self) -> None:
        closer = getattr(self.deps.publisher, "close", None)
        if closer:
            closer()
        self.deps.pool.close()


def sync_registry(deps: Deps) -> None:
    """코드의 템플릿(모든 버전)을 templates 테이블에 올린다(서비스 시작 시 자동 등록, ANA-01.01)."""
    manifests = []
    for reg in deps.registry.all_versions():
        m = reg.template.manifest()
        m["guideMarkdown"] = reg.guide.markdown
        m["current"] = reg.version == deps.registry.versions(reg.key)[-1]
        manifests.append(m)
    with deps.pool.connection() as conn:
        repo.sync_templates(conn, manifests, deps.clock.now())


def make_services(deps: Deps, subprocess_runs: bool = False) -> Services:
    analytics = AnalyticsService(deps)
    executor = SubprocessExecutor(deps.extra_template_dirs) if subprocess_runs else InlineExecutor(deps.registry)
    return Services(deps, analytics, DatasetService(deps), ModelService(deps, executor), ExportService(deps, analytics), KpiService(deps))


def build_deps(settings: Settings, *, clock: Clock | None = None, publisher: EventPublisher | None = None,
               core: CoreLookup | None = None, store: ObjectStore | None = None, registry: TemplateRegistry | None = None) -> Deps:
    run_migrations(settings.conninfo, settings.flyway_mode)
    pool = create_pool(settings.conninfo, settings.db_pool_max)
    pool.wait(timeout=30)
    extra = extra_dirs_from_env(settings.template_dirs)
    if publisher is None:
        publisher = (RabbitPublisher(settings.rabbit_host, settings.rabbit_port, settings.rabbit_vhost, settings.rabbit_user,
                                     settings.rabbit_password) if settings.events_enabled else RecordingPublisher())
    deps = Deps(settings=settings, pool=pool, registry=registry or build_registry(extra),
                loader=TelemetryLoader(pool, core if core is not None else CoreClient(settings.core_uri)), publisher=publisher,
                clock=clock or SystemClock(), store=store or build_store(settings), extra_template_dirs=extra)
    sync_registry(deps)
    management.attach(deps)
    return deps


def build_services(role: str = "api") -> Services:
    settings = Settings.from_env()
    deps = build_deps(settings)
    management.set_ready(True)
    return make_services(deps, subprocess_runs=role == "worker")
