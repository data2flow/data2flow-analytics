"""ANA-04.01·ADR-059: `Upgrade: h2c`가 붙은 POST도 본문이 앱에 도착한다(실제 uvicorn 프로세스).

JDK HttpClient 기본값(HTTP/2)은 평문 http 요청에 `Connection: Upgrade, HTTP2-Settings`·`Upgrade: h2c`를 붙인다.
uvicorn httptools 파서는 이 요청의 본문을 버려 core→analytics POST가 모두 400이었다. API·관리 서버는 h11 파서를 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest
import uvicorn

from data2flow_analytics import __main__ as entry

pytestmark = pytest.mark.spec("ANA-04.01")


async def _echo(scope, receive, send):
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    out = json.dumps({"bodyLength": len(body)}).encode()
    await send({"type": "http.response.start", "status": 200 if body else 400,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(out)).encode())]})
    await send({"type": "http.response.body", "body": out})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _h2c_post(port: int) -> tuple[int, dict]:
    body = b'{"analysisId":"1"}'
    request = (
        b"POST /internal/analytics/runs HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{port}\r\n".encode()
        + b"Connection: Upgrade, HTTP2-Settings\r\nUpgrade: h2c\r\nHTTP2-Settings: AAEAAEAAAAIAAAABAAMAAABkAAQBAAAAAAUAAEAA\r\n"
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(request)
        data = b""
        while b"\r\n\r\n" not in data or not data.rstrip().endswith(b"}"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(payload)


def _serve(config: uvicorn.Config):
    server = uvicorn.Server(config)
    started = threading.Event()

    def run():
        loop = asyncio.new_event_loop()
        task = loop.create_task(server.serve())

        async def wait_started():
            while not server.started:
                await asyncio.sleep(0.01)
            started.set()

        loop.run_until_complete(asyncio.gather(task, wait_started()))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(10)
    return server, thread


def test_ana_04_01_api_config_uses_h11_parser():
    """ANA-04.01: API·관리 서버 설정은 기본 h11 파서"""
    assert entry.HTTP_IMPL == "h11"
    assert entry.api_config(_echo, 8080).http == "h11"


@pytest.mark.parametrize(("http", "expected_status"), [("h11", 200), ("httptools", 400)])
def test_ana_04_01_h2c_upgrade_post_body_arrives_with_h11(http: str, expected_status: int):
    """ANA-04.01: h11은 `Upgrade: h2c` POST 본문을 앱에 넘긴다. httptools는 본문을 버린다(원인 재현)"""
    port = _free_port()
    config = uvicorn.Config(_echo, host="127.0.0.1", port=port, http=http, log_level="warning")
    server, thread = _serve(config)
    try:
        status, payload = _h2c_post(port)
    finally:
        server.should_exit = True
        thread.join(10)
    assert status == expected_status
    assert (payload["bodyLength"] > 0) == (expected_status == 200)
