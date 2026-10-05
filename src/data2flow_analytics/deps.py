"""서비스 의존성 묶음(API·워커가 같이 쓴다)."""

from __future__ import annotations

from dataclasses import dataclass, field

from psycopg_pool import ConnectionPool

from .clock import Clock
from .config import Settings
from .events.publisher import EventPublisher
from .loader.loader import TelemetryLoader
from .models.store import ObjectStore
from .templates.registry import TemplateRegistry


@dataclass
class Deps:
    settings: Settings
    pool: ConnectionPool
    registry: TemplateRegistry
    loader: TelemetryLoader
    publisher: EventPublisher | None
    clock: Clock
    store: ObjectStore
    extra_template_dirs: list = field(default_factory=list)
