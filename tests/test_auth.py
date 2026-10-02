"""Bearer 鉴权中间件：无/错凭据 401，正确凭据放行，非 HTTP 作用域直通。"""

from __future__ import annotations

import json

import pytest

from comfy_mcp_lite.auth import BearerAuthMiddleware


def _scope(headers: list[tuple[bytes, bytes]] | None) -> dict:
    return {"type": "http", "headers": headers or [], "method": "POST", "path": "/mcp"}


async def _run(middleware: BearerAuthMiddleware, scope: dict) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"]


async def _ok_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.asyncio
async def test_missing_credentials_401() -> None:
    sent = await _run(BearerAuthMiddleware(_ok_app, "tok"), _scope(None))
    assert _status(sent) == 401


@pytest.mark.asyncio
async def test_wrong_token_401() -> None:
    sent = await _run(
        BearerAuthMiddleware(_ok_app, "tok"),
        _scope([(b"authorization", b"Bearer wrong")]),
    )
    assert _status(sent) == 401
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert "error" in json.loads(body)


@pytest.mark.asyncio
async def test_non_bearer_scheme_401() -> None:
    sent = await _run(
        BearerAuthMiddleware(_ok_app, "tok"),
        _scope([(b"authorization", b"Basic dG9r")]),
    )
    assert _status(sent) == 401


@pytest.mark.asyncio
async def test_correct_token_passes() -> None:
    sent = await _run(
        BearerAuthMiddleware(_ok_app, "tok"),
        _scope([(b"authorization", b"Bearer tok")]),
    )
    assert _status(sent) == 200


@pytest.mark.asyncio
async def test_non_http_scope_passthrough() -> None:
    seen: list[dict] = []

    async def app(scope, receive, send):
        seen.append(scope)

    await BearerAuthMiddleware(app, "tok")({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen  # 非 HTTP 作用域不经鉴权直接透传（如 lifespan）
