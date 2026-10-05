"""템플릿 계산 실행기. 시간 제한(BR-ANA-09)과 협조적 취소(ANA-04.04)를 맡는다.

- InlineExecutor: 같은 프로세스에서 실행. 체크포인트마다 취소·시간 초과를 본다(동기 실행·테스트).
- SubprocessExecutor: 별도 프로세스(spawn)에서 실행하고, 시간이 지나거나 취소되면 프로세스를 끝낸다(워커 기본, TC-ANA-107).
"""

from __future__ import annotations

import multiprocessing
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.errors import BusinessError, ErrorCode
from ..templates.base import CancelledError, InputData, RunContext


class RunTimeoutError(Exception):
    pass


class TemplateFailure(Exception):
    """템플릿 계산 중 예상하지 못한 오류(기술 로그는 error_detail로)."""

    def __init__(self, message: str, detail: str):
        super().__init__(message)
        self.detail = detail


@dataclass
class Job:
    template_key: str
    template_version: str
    data: InputData
    params: dict
    seed: int
    now: Any
    timezone: str
    model: Any = None
    labels: list | None = None
    mode: str = "run"  # run | fit


def _execute(job: Job, registry, cancel_check: Callable[[], bool]):
    reg = registry.get(job.template_key, job.template_version)
    template = reg.template
    params = template.parse_params(job.params)
    ctx = RunContext(seed=job.seed, now=job.now, timezone=job.timezone, model=job.model, labels=job.labels or [],
                     cancel_check=cancel_check)
    if job.mode == "fit":
        return template.fit(job.data, params, ctx)
    return template.run(job.data, params, ctx).to_json()


class InlineExecutor:
    def __init__(self, registry):
        self.registry = registry

    def execute(self, job: Job, timeout_sec: float, cancel_check: Callable[[], bool]):
        deadline = time.monotonic() + timeout_sec

        def check() -> bool:
            if time.monotonic() > deadline:
                raise RunTimeoutError()
            return cancel_check()

        try:
            return _execute(job, self.registry, check)
        except (BusinessError, CancelledError, RunTimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise TemplateFailure(str(exc) or type(exc).__name__, traceback.format_exc()) from exc


def _child(conn, job: Job, extra_dirs: list[str]) -> None:  # pragma: no cover — 별도 프로세스에서 돈다
    from ..templates.registry import build_registry

    try:
        registry = build_registry([Path(d) for d in extra_dirs])
        conn.send(("ok", _execute(job, registry, lambda: False)))
    except BusinessError as exc:
        conn.send(("business", exc.code.name, exc.args_, exc.detail))
    except Exception as exc:  # noqa: BLE001
        conn.send(("error", str(exc) or type(exc).__name__, traceback.format_exc()))
    finally:
        conn.close()


class SubprocessExecutor:
    def __init__(self, extra_dirs: list[Path] | None = None, poll_sec: float = 0.2):
        self.extra_dirs = [str(d) for d in (extra_dirs or [])]
        self.poll_sec = poll_sec
        self._ctx = multiprocessing.get_context("spawn")

    def execute(self, job: Job, timeout_sec: float, cancel_check: Callable[[], bool]):
        parent, child = self._ctx.Pipe(duplex=False)
        proc = self._ctx.Process(target=_child, args=(child, job, self.extra_dirs), daemon=True)
        proc.start()
        child.close()
        deadline = time.monotonic() + timeout_sec
        try:
            while True:
                # 결과를 기다리는 동안 poll_sec마다 취소·시간 초과를 본다(취소는 1초 안에 반영, ANA-04.04)
                if parent.poll(self.poll_sec):
                    try:
                        msg = parent.recv()
                    except EOFError as exc:
                        raise TemplateFailure("계산 프로세스가 결과 없이 끝났습니다", f"exitcode={proc.exitcode}") from exc
                    break
                if not proc.is_alive():
                    raise TemplateFailure("계산 프로세스가 비정상 종료했습니다", f"exitcode={proc.exitcode}")
                if cancel_check():
                    raise CancelledError()
                if time.monotonic() > deadline:
                    raise RunTimeoutError()
        finally:
            if proc.is_alive():
                proc.kill()
            proc.join(timeout=5)
            parent.close()
        if msg[0] == "ok":
            return msg[1]
        if msg[0] == "business":
            raise BusinessError(ErrorCode[msg[1]], msg[2] or {}, detail=msg[3])
        raise TemplateFailure(msg[1], msg[2])
