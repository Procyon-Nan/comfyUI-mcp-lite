"""服务级端到端冒烟：真 MCP 协议（StreamableHTTP）+ 假 ComfyUI（无 /ws，
正好覆盖轮询兜底路径）。覆盖验收标准里可在无 ComfyUI 环境验证的条款：
401 三态、工具可见性、list 结构、run 出图（内联）、原值兜底、超时/running/
completed 状态机、参考图两种图源、错误信息明确性。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import types
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from comfy_mcp_lite.comfy import ComfyClient
from comfy_mcp_lite.config import Config
from comfy_mcp_lite.images import resolve_image_source
from comfy_mcp_lite.server import build_app
from fake_comfy import FakeComfy

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_TOKEN = "test-token"


async def _start_server(app: Any) -> tuple[uvicorn.Server, asyncio.Task, int]:
    # lifespan 必须开：MCPServer 的会话管理器靠它启动
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
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


@pytest.fixture
async def env(simple_ui: dict, object_info: dict):
    fake = FakeComfy({"simple.json": simple_ui}, object_info)
    fake_server, fake_task, fake_port = await _start_server(fake.build())
    config = Config(
        comfyui_url=f"http://127.0.0.1:{fake_port}",
        mcp_host="127.0.0.1",
        mcp_port=0,
        auth_token=_TOKEN,
    )
    mcp_server, mcp_task, mcp_port = await _start_server(build_app(config))
    yield types.SimpleNamespace(
        fake=fake,
        fake_port=fake_port,
        mcp_url=f"http://127.0.0.1:{mcp_port}/mcp",
    )
    for server, task in ((fake_server, fake_task), (mcp_server, mcp_task)):
        server.should_exit = True
        await asyncio.wait_for(task, 5)


@contextlib.asynccontextmanager
async def mcp_session(env):
    """带正确 Bearer 的已初始化 MCP 客户端会话。"""
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {_TOKEN}"},
        timeout=httpx2.Timeout(30, read=300),
    )
    async with http_client:
        async with streamable_http_client(env.mcp_url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _json_payload(result: Any) -> dict:
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in result.content:
        if block.type == "text":
            return json.loads(block.text)
    raise AssertionError(f"no parseable payload in {result}")


def _text_payload(result: Any) -> dict:
    blocks = [b for b in result.content if b.type == "text"]
    assert blocks, "no text block"
    return json.loads(blocks[0].text)


def _error_text(result: Any) -> str:
    return result.content[0].text


# ---- 鉴权（验收条 6/7 的 HTTP 部分）----

async def test_http_401_without_token(env) -> None:
    async with httpx2.AsyncClient() as http:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": "Basic abc"},
        ):
            resp = await http.post(
                env.mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers=headers,
            )
            assert resp.status_code == 401, headers


# ---- 工具可见性 ----

async def test_tools_exposed(env) -> None:
    async with mcp_session(env) as session:
        tools = await session.list_tools()
        assert {t.name for t in tools.tools} == {
            "list_workflows",
            "run_workflow",
            "get_image",
        }


# ---- list_workflows（验收条 1）----

async def test_list_workflows_shape(env) -> None:
    async with mcp_session(env) as session:
        result = await session.call_tool("list_workflows", {})
        assert not result.is_error
        payload = _json_payload(result)
        assert set(payload) == {"simple"}
        entry = payload["simple"]
        assert set(entry) == {"prompts", "numbers"}  # 无 images 键（空类省略）
        assert set(entry["prompts"]) == {"78.text", "79.text"}
        assert set(entry["numbers"]) == {"80.width", "80.height"}


# ---- run_workflow ----

async def test_run_workflow_completed_with_inline_image(env) -> None:
    async with mcp_session(env) as session:
        result = await session.call_tool(
            "run_workflow",
            {"workflow": "simple", "prompts": {"78.text": "a cat"},
             "numbers": {"80.width": 768, "80.height": 1024}},
        )
        assert not result.is_error
        payload = _text_payload(result)
        assert payload["status"] == "completed"
        assert payload["images"] >= 1
        images = [b for b in result.content if b.type == "image"]
        assert images, "图片必须内联返回（ImageContent）"
        assert images[0].mime_type == "image/png"
        assert base64.b64decode(images[0].data) == _TINY_PNG

    # 提交到 ComfyUI 的 prompt 带上了用户覆盖值
    job = list(env.fake.jobs.values())[-1]
    assert job["prompt"]["78"]["inputs"]["text"] == "a cat"
    assert job["prompt"]["80"]["inputs"]["width"] == 768
    assert job["prompt"]["80"]["inputs"]["height"] == 1024


async def test_run_workflow_uses_original_dimensions(env) -> None:
    """验收条 3：不传 numbers 时用工作流原值（1080×1920）。"""
    async with mcp_session(env) as session:
        result = await session.call_tool(
            "run_workflow", {"workflow": "simple", "prompts": {"78.text": "no size"}}
        )
        assert not result.is_error
    job = list(env.fake.jobs.values())[-1]
    assert job["prompt"]["80"]["inputs"]["width"] == 1080
    assert job["prompt"]["80"]["inputs"]["height"] == 1920


async def test_run_workflow_timeout_then_get_image(env) -> None:
    """超时返回 timeout（无图）→ get_image running → 完成后 completed 带图。"""
    env.fake.job_policy = lambda prompt: "hold"  # 所有任务先挂起
    async with mcp_session(env) as session:
        result = await session.call_tool(
            "run_workflow", {"workflow": "simple", "timeout_seconds": 2}
        )
        assert not result.is_error
        payload = _text_payload(result)
        assert payload["status"] == "timeout"
        assert "prompt_id" in payload
        prompt_id = payload["prompt_id"]
        assert not [b for b in result.content if b.type == "image"]  # 超时不含图

        result = await session.call_tool("get_image", {"prompt_id": prompt_id})
        assert not result.is_error
        assert _text_payload(result)["status"] == "running"

        env.fake.complete(prompt_id)
        result = await session.call_tool("get_image", {"prompt_id": prompt_id})
        assert not result.is_error
        payload = _text_payload(result)
        assert payload["status"] == "completed"
        assert [b for b in result.content if b.type == "image"]


async def test_run_workflow_unknown_name(env) -> None:
    async with mcp_session(env) as session:
        result = await session.call_tool("run_workflow", {"workflow": "nope"})
        assert result.is_error
        assert "无此工作流" in _error_text(result)
        assert "simple" in _error_text(result)  # 回列可用工作流


async def test_run_workflow_rejects_unknown_address(env) -> None:
    async with mcp_session(env) as session:
        result = await session.call_tool(
            "run_workflow", {"workflow": "simple", "prompts": {"999.text": "x"}}
        )
        assert result.is_error
        assert "999.text" in _error_text(result)
        assert "78.text" in _error_text(result)  # 回列可填字段


async def test_run_workflow_rejects_cross_category(env) -> None:
    async with mcp_session(env) as session:
        result = await session.call_tool(
            "run_workflow", {"workflow": "simple", "numbers": {"78.text": 5}}
        )
        assert result.is_error
        assert "prompts" in _error_text(result)


# ---- get_image ----

async def test_get_image_unknown_id(env) -> None:
    async with mcp_session(env) as session:
        result = await session.call_tool("get_image", {"prompt_id": "no-such-id"})
        assert result.is_error
        assert "无此任务" in _error_text(result)


async def test_get_image_index_out_of_range_returns_all(env) -> None:
    async with mcp_session(env) as session:
        result = await session.call_tool(
            "run_workflow", {"workflow": "simple", "prompts": {"78.text": "multi"}}
        )
        assert not result.is_error
        prompt_id = _text_payload(result)["prompt_id"]
        env.fake.complete(prompt_id, images=[
            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"},
            {"filename": "ComfyUI_00002_.png", "subfolder": "", "type": "output"},
        ])
        result = await session.call_tool("get_image", {"prompt_id": prompt_id, "index": 99})
        assert not result.is_error
        assert _text_payload(result)["images"] == 2  # 越界 → 全部


# ---- 参考图（验收条 5 的两种图源解析；img2img 真机链路后补）----

async def test_image_source_data_url(env) -> None:
    data_url = "data:image/png;base64," + base64.b64encode(_TINY_PNG).decode()
    client = ComfyClient(f"http://127.0.0.1:{env.fake_port}")
    name = await resolve_image_source(client, data_url)
    assert name.startswith("mcp_") and name.endswith(".png")
    assert env.fake.uploads[name] == _TINY_PNG


async def test_image_source_http_url(env) -> None:
    client = ComfyClient(f"http://127.0.0.1:{env.fake_port}")
    name = await resolve_image_source(client, f"http://127.0.0.1:{env.fake_port}/static/ref.png")
    assert name.startswith("mcp_") and name.endswith(".png")
    assert env.fake.uploads[name] == _TINY_PNG


async def test_image_source_rejects_garbage(env) -> None:
    from comfy_mcp_lite.comfy import ComfyError

    client = ComfyClient(f"http://127.0.0.1:{env.fake_port}")
    with pytest.raises(ComfyError, match="不支持的参考图源"):
        await resolve_image_source(client, "/etc/passwd")
