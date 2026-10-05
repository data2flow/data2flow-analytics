"""시계 주입(테스트에서 sleep 대신 시간을 옮긴다). 저장·API 시각은 모두 UTC(conventions.md §4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class MutableClock:
    """테스트용 시계. advance()로 시간을 옮긴다."""

    def __init__(self, at: datetime):
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at = self._at + delta

    def set(self, at: datetime) -> None:
        self._at = at.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 UTC 문자열(초 단위, Z 접미사)."""
    if dt is None:
        return None
    dt = dt.astimezone(UTC)
    if dt.microsecond:
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
