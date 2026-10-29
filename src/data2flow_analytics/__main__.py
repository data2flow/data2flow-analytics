"""실행: `python -m data2flow_analytics [api|worker|migrate]`.

- api(기본): API 8080 + 관리 8081
- worker: 작업 큐·정리·실시간 추론 + 관리 8081(같은 이미지, 명령만 다름 — analytics-service.md §2)
- migrate: 마이그레이션만 적용하고 끝(DATA2FLOW_FLYWAY_MODE)
SIGTERM을 받으면 readiness를 먼저 내리고 진행 중 작업을 마친 뒤 끝낸다.
"""

import asyncio
import logging
import os
import signal
import sys

import uvicorn

from . import management
from .api import app as api_app
from .management import app as management_app

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

# httptools 파서는 `Upgrade: h2c`가 붙은 요청의 본문을 버린다(ADR-060). 호출 쪽이 HTTP/1.1로 고정해도 서버에서 한 번 더 막는다
HTTP_IMPL = "h11"


def api_config(app, port: int, **kwargs) -> uvicorn.Config:
    return uvicorn.Config(app, host="0.0.0.0", port=port, http=HTTP_IMPL, **kwargs)


async def serve_api() -> None:
    api = uvicorn.Server(api_config(api_app, int(os.getenv("PORT", "8080")), timeout_graceful_shutdown=30))
    mgmt = uvicorn.Server(api_config(management_app, int(os.getenv("MANAGEMENT_PORT", "8081")), access_log=False))
    await asyncio.gather(api.serve(), mgmt.serve())


async def serve_worker() -> None:
    from .bootstrap import build_services
    from .realtime.consumer import TelemetryConsumer
    from .realtime.engine import RealtimeEngine
    from .worker.main import Worker

    services = await asyncio.to_thread(build_services, "worker")
    deps = services.deps
    worker = Worker(deps, services.models.executor, services.analytics)
    mgmt = uvicorn.Server(api_config(management_app, int(os.getenv("MANAGEMENT_PORT", "8081")), access_log=False))
    consumer = None
    loop = asyncio.get_running_loop()

    def shutdown() -> None:
        management.set_ready(False)
        worker.stop.set()
        if consumer:
            consumer.stop()
        mgmt.should_exit = True

    loop.add_signal_handler(signal.SIGTERM, shutdown)
    loop.add_signal_handler(signal.SIGINT, shutdown)
    tasks = [asyncio.to_thread(worker.serve, deps.settings.worker_threads), mgmt.serve()]
    if deps.settings.realtime_enabled:
        engine = RealtimeEngine(deps)
        await asyncio.to_thread(engine.reload)
        consumer = TelemetryConsumer(deps.settings, engine)
        tasks.append(consumer.run())
    await asyncio.gather(*tasks)
    services.close()


def main(argv: list[str]) -> None:
    role = argv[1] if len(argv) > 1 else "api"
    if role == "migrate":
        from .config import Settings
        from .db.migrate import run

        settings = Settings.from_env()
        run(settings.conninfo, os.getenv("DATA2FLOW_FLYWAY_MODE", "migrate"))
        return
    asyncio.run(serve_worker() if role == "worker" else serve_api())


if __name__ == "__main__":
    main(sys.argv)
