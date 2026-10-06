"""按请求头 X-Comfy-Workflows 过滤 list_workflows（per-client 工作流作用域）。

语义（硬性）：
1. 头缺失 / 空 / 纯空白 → 不过滤，返回全部工作流（完全向后兼容）；
2. 头存在 → 逗号分隔逐项 unquote 解码后与服务端真名精确匹配，只返回命中项；
3. 点名了服务端不存在的工作流（改名/删除）→ 静默忽略，不报错；
4. 重复项去重；忽略空项（连续逗号、首尾逗号、纯空白项）；
5. 先按解码名精确匹配，不中再按原样精确匹配（容忍已编码/未编码两种写法）；
6. 只作用于 list_workflows 的输出，run_workflow / get_image 行为不变。

分两层测：select_workflow_names 纯函数各分支；httpx2 ASGITransport 直打
build_app（手动驱动 lifespan —— MCPServer 的会话管理器靠 lifespan 启动，
而 ASGITransport 只发 http scope 不会跑它；BearerAuthMiddleware 把非 http
scope 原样透传给内层 Starlette 应用，故向最外层发 lifespan scope 即可）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import types
from typing import Any
from urllib.parse import quote

import httpx2
import pytest
import uvicorn

from fake_comfy import FakeComfy

from comfy_mcp_lite.config import Config
from comfy_mcp_lite.discover import select_workflow_names
from comfy_mcp_lite.server import build_app

_TOKEN = "test-token"
# 三个工作流：ASCII 名 + 两个中文名（其一带空格与括号，覆盖 URL 编码）
_ALL_NAMES = ["simple", "文生图", "文生图 (Copy)"]
# 规格给出的头值示例：两个中文名逐项 percent-encoding（UTF-8）
_SPEC_HEADER = "%E6%96%87%E7%94%9F%E5%9B%BE,%E6%96%87%E7%94%9F%E5%9B%BE%20%28Copy%29"


# ---- select_workflow_names：纯函数各分支 ----

def test_select_missing_header_returns_all() -> None:
    assert select_workflow_names(_ALL_NAMES, None) == _ALL_NAMES


def test_select_empty_or_whitespace_header_returns_all() -> None:
    # 空 / 纯空白 → 不过滤（向后兼容）
    assert select_workflow_names(_ALL_NAMES, "") == _ALL_NAMES
    assert select_workflow_names(_ALL_NAMES, "   ") == _ALL_NAMES


def test_select_spec_example_two_encoded_names() -> None:
    # 规格示例头值：解码后即「文生图」与「文生图 (Copy)」
    assert select_workflow_names(_ALL_NAMES, _SPEC_HEADER) == ["文生图", "文生图 (Copy)"]


def test_select_ascii_name_matches_directly() -> None:
    # ASCII 名编码后就是自身，直接写原名即命中
    assert select_workflow_names(_ALL_NAMES, "simple") == ["simple"]


def test_select_unknown_names_are_ignored() -> None:
    # 服务端不存在的工作流（改名/删除）静默忽略，其余正常
    assert select_workflow_names(_ALL_NAMES, f"{quote('文生图')},ghost") == ["文生图"]
    assert select_workflow_names(_ALL_NAMES, "ghost,deleted%20name") == []


def test_select_accepts_raw_unencoded_names() -> None:
    # 未编码的原样写法（含空格括号）也能命中：解码不中再按原样匹配
    assert select_workflow_names(_ALL_NAMES, "文生图 (Copy)") == ["文生图 (Copy)"]


def test_select_dedups_and_skips_empty_items() -> None:
    # 重复项去重；连续逗号 / 首尾逗号 / 纯空白项 / 项两侧空白都容忍
    assert select_workflow_names(_ALL_NAMES, "simple,simple,,") == ["simple"]
    assert select_workflow_names(_ALL_NAMES, " , simple , ") == ["simple"]


def test_select_preserves_server_order() -> None:
    # 头里逆序点名 → 输出仍按服务端名字列表的顺序
    header = f"{quote('文生图 (Copy)')},{quote('文生图')},simple"
    assert select_workflow_names(_ALL_NAMES, header) == _ALL_NAMES


def test_select_literal_percent_name_matches_raw() -> None:
    # 工作流名本身含 %：解码写法（a b）不中，回退原样精确匹配
    assert select_workflow_names(["a%20b", "simple"], "a%20b") == ["a%20b"]


# ---- HTTP 级：ASGI transport 直打 build_app ----

async def _start_server(app: Any, port: int = 0) -> tuple[uvicorn.Server, asyncio.Task, int]:
    # 假 ComfyUI 需要真 HTTP（ComfyClient 按真实地址访问），起 uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.02)
    else:
        raise RuntimeError("server failed to start")
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


@contextlib.asynccontextmanager
async def _drive_lifespan(app: Any):
    """手动向最外层应用发 ASGI lifespan scope（见模块 docstring）。"""
    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    events: asyncio.Queue[dict] = asyncio.Queue()
    started = asyncio.Event()
    failure: list[str] = []

    async def receive() -> dict:
        return await events.get()

    async def send(message: dict) -> None:
        if message["type"] == "lifespan.startup.complete":
            started.set()
        elif message["type"] == "lifespan.startup.failed":
            failure.append(message.get("message", "unknown"))
            started.set()

    task = asyncio.create_task(app(scope, receive, send))
    await events.put({"type": "lifespan.startup"})
    await asyncio.wait_for(started.wait(), 10)
    if failure:
        await asyncio.wait_for(task, 10)
        raise RuntimeError(f"lifespan 启动失败：{failure[0]}")
    try:
        yield
    finally:
        await events.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(task, 10)


@pytest.fixture
async def app_env(simple_ui: dict, object_info: dict):
    """假 ComfyUI（三个工作流同用 simple 内容）+ 进程内 build_app 应用。"""
    fake = FakeComfy({f"{name}.json": simple_ui for name in _ALL_NAMES}, object_info)
    fake_server, fake_task, fake_port = await _start_server(fake.build())
    config = Config(
        comfyui_url=f"http://127.0.0.1:{fake_port}",
        mcp_host="127.0.0.1",
        mcp_port=0,
        auth_token=_TOKEN,
    )
    app = build_app(config)
    try:
        async with _drive_lifespan(app):
            yield types.SimpleNamespace(app=app, fake=fake)
    finally:
        fake_server.should_exit = True
        await asyncio.wait_for(fake_task, 5)


async def _rpc(app: Any, method: str, params: dict, extra_headers: dict[str, str] | None = None) -> Any:
    """直打 /mcp 的一条 JSON-RPC：建会话（initialize → initialized）后发请求。"""
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    # base_url 带 127.0.0.1 与端口：Host 头需匹配 SDK 防重绑定的
    # allowed_hosts 通配（127.0.0.1:*）；ASGI transport 并不真正连网
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:9101",
        timeout=httpx2.Timeout(30, read=300),
    ) as http:
        resp = await http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw", "version": "0"},
            },
        })
        assert resp.status_code == 200, resp.text
        headers["mcp-session-id"] = resp.headers["mcp-session-id"]
        if extra_headers:
            headers.update(extra_headers)
        await http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        resp = await http.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": method, "params": params,
        })
        assert resp.status_code == 200, resp.text
        return resp.json()


def _tool_text(body: dict) -> str:
    return body["result"]["content"][0]["text"]


async def _listed_names(app: Any, extra_headers: dict[str, str] | None = None) -> list[str]:
    body = await _rpc(
        app, "tools/call",
        {"name": "list_workflows", "arguments": {}},
        extra_headers=extra_headers,
    )
    assert not body["result"].get("isError"), body
    return sorted(json.loads(_tool_text(body)))


async def test_http_no_header_lists_all(app_env) -> None:
    # 无头 → 全部工作流（回归：现有行为不变）
    assert await _listed_names(app_env.app) == _ALL_NAMES


async def test_http_header_two_encoded_names(app_env) -> None:
    # 规格示例头值 → 仅返回这两个工作流
    names = await _listed_names(
        app_env.app, {"X-Comfy-Workflows": _SPEC_HEADER}
    )
    assert names == ["文生图", "文生图 (Copy)"]


async def test_http_header_with_unknown_name_ignored(app_env) -> None:
    # 头点名不存在的工作流 → 该项被忽略，其余正常
    names = await _listed_names(
        app_env.app, {"X-Comfy-Workflows": f"{quote('文生图')},ghost"}
    )
    assert names == ["文生图"]


async def test_http_header_empty_or_whitespace_lists_all(app_env) -> None:
    # 头存在但值为空 / 纯空白 → 全部
    for value in ("", "   "):
        names = await _listed_names(app_env.app, {"X-Comfy-Workflows": value})
        assert names == _ALL_NAMES, value


async def test_http_header_ascii_name(app_env) -> None:
    # ASCII 名工作流直接匹配
    assert await _listed_names(app_env.app, {"X-Comfy-Workflows": "simple"}) == ["simple"]


async def test_http_header_duplicates_and_empty_items(app_env) -> None:
    # 重复项 / 空项去重后仍只返回该工作流
    names = await _listed_names(
        app_env.app, {"X-Comfy-Workflows": "simple,simple,,"}
    )
    assert names == ["simple"]


async def test_http_header_case_insensitive(app_env) -> None:
    # 头名大小写不敏感（Starlette Headers）
    names = await _listed_names(app_env.app, {"x-comfy-workflows": "simple"})
    assert names == ["simple"]


async def test_http_list_workflows_schema_exposes_no_parameters(app_env) -> None:
    # ctx: Context 由 SDK 自动注入，不得作为工具入参暴露
    body = await _rpc(app_env.app, "tools/list", {})
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert tools["list_workflows"]["inputSchema"]["properties"] == {}


async def test_http_run_workflow_not_blocked_by_header(app_env) -> None:
    # 只作用于 list_workflows：头收窄列表后，不在头里的工作流仍可直接运行
    body = await _rpc(
        app_env.app, "tools/call",
        {"name": "run_workflow", "arguments": {"workflow": "simple"}},
        extra_headers={"X-Comfy-Workflows": quote("文生图")},
    )
    assert not body["result"].get("isError"), body
    payload = json.loads(_tool_text(body))
    assert payload["status"] == "completed"
    assert payload["images"] == 1
