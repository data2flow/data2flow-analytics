"""API(8080)와 관리 포트(8081)를 한 프로세스에서 띄운다. SIGTERM을 받으면 readiness를 먼저 내리고 종료한다."""

import asyncio
import os

import uvicorn

from .api import app as api_app
from .management import app as management_app


async def main() -> None:
    api = uvicorn.Server(uvicorn.Config(api_app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), timeout_graceful_shutdown=30))
    mgmt = uvicorn.Server(uvicorn.Config(management_app, host="0.0.0.0", port=int(os.getenv("MANAGEMENT_PORT", "8081")), access_log=False))
    await asyncio.gather(api.serve(), mgmt.serve())


if __name__ == "__main__":
    asyncio.run(main())
