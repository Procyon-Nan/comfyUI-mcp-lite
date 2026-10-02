"""Bearer 鉴权中间件。

共享服务：所有到达本服务的 HTTP 请求一律要求合法的
Authorization: Bearer <MCP_AUTH_TOKEN>，否则 401。
用 hmac.compare_digest 比较，防时序侧信道。
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[..., Awaitable[Any]]
Send = Callable[..., Awaitable[None]]


class BearerAuthMiddleware:
    """纯 ASGI 中间件，包住整个 MCP 应用。"""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        auth = b""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                auth = value
                break
        scheme, _, value = auth.decode("latin-1").partition(" ")
        supplied = value.strip().encode("utf-8")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, self._token):
            await self._send_unauthorized(send, "无 Bearer 凭据或凭据错误")
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _send_unauthorized(send: Send, detail: str) -> None:
        body = json.dumps({"error": detail}, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
