"""服务级端到端冒烟：真 MCP 协议（StreamableHTTP）+ 假 ComfyUI（无 /ws，
正好覆盖轮询兜底路径）。覆盖验收标准里可在无 ComfyUI 环境验证的条款：
401 三态、工具可见性、list 结构、run 出图（内联）、原值兜底、超时/running/
completed 状态机、参考图两种图源、错误信息明确性。

v0.2：图片签名 URL 端点（免 Bearer、HMAC 查询串签名、过期、403/404、
.ext 后缀容忍）与工具结果 urls 字段（PUBLIC_BASE_URL 设/未设）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import socket
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
from comfy_mcp_lite.signing import build_image_url
from fake_comfy import FakeComfy

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_TOKEN = "test-token"


async def _start_server(app: Any, port: int = 0) -> tuple[uvicorn.Server, asyncio.Task, int]:
    # lifespan 必须开：MCPServer 的会话管理器靠它启动
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


def _free_port() -> int:
    # 预占一个空闲端口：public 模式下 PUBLIC_BASE_URL 要在启动前指向服务自身
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def _env(simple_ui: dict, object_info: dict, public: bool = False):
    """起假 ComfyUI + 真服务；public=True 时配置指向自身的 PUBLIC_BASE_URL。"""
    fake = FakeComfy({"simple.json": simple_ui}, object_info)
    fake_server, fake_task, fake_port = await _start_server(fake.build())
    # public 模式需固定端口：urls 的公网前缀必须在 build_app 之前拼好
    mcp_port = _free_port() if public else 0
    config = Config(
        comfyui_url=f"http://127.0.0.1:{fake_port}",
        mcp_host="127.0.0.1",
        mcp_port=mcp_port,
        auth_token=_TOKEN,
        public_base_url=f"http://127.0.0.1:{mcp_port}" if public else "",
    )
    mcp_server, mcp_task, mcp_port = await _start_server(build_app(config), port=mcp_port)
    try:
        yield types.SimpleNamespace(
            fake=fake,
            fake_port=fake_port,
            mcp_port=mcp_port,
            mcp_url=f"http://127.0.0.1:{mcp_port}/mcp",
        )
    finally:
        for server, task in ((fake_server, fake_task), (mcp_server, mcp_task)):
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.fixture
async def env(simple_ui: dict, object_info: dict):
    async with _env(simple_ui, object_info) as e:
        yield e


@pytest.fixture
async def env_public(simple_ui: dict, object_info: dict):
    """配置了 PUBLIC_BASE_URL 的环境：工具结果带可公开抓取的签名 urls。"""
    async with _env(simple_ui, object_info, public=True) as e:
        yield e


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


# ---- 鉴权（验收条 6/7 的 HTTP 部分；v0.2 规格条 7：/mcp 仍要求 Bearer）----

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


# ---- 响应格式（JSON 模式，防大图断流）----

async def test_tool_call_response_is_json_not_sse(env) -> None:
    """POST /mcp 的 tool 调用响应必须是 application/json，不得走 SSE。

    回归：SSE 模式下 httpx2>=2.13 的解析器有单事件 1MiB 硬限制，
    base64 内联大图（原始 >约 768KB，如 4K 图）会触发
    "SSE stream ended without a response"。json_response=True 让
    响应整体 JSON 返回，绕过 SSE 解析器。
    """
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(30, read=300)) as http:
        resp = await http.post(
            env.mcp_url,
            headers=headers,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "raw", "version": "0"},
                },
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        session_id = resp.headers.get("mcp-session-id")
        assert session_id, "会话 ID 必须下发"
        headers["mcp-session-id"] = session_id

        await http.post(
            env.mcp_url, headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        resp = await http.post(
            env.mcp_url,
            headers=headers,
            json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "list_workflows", "arguments": {}},
            },
        )
        assert resp.status_code == 200
        # 核心：text/event-stream 会让大图在 SSE 解析器处断流
        assert not resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["id"] == 2
        assert body["result"]["content"], "工具结果必须整体在同一个 JSON 响应里"


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


# ---- 图片签名 URL（v0.2 规格 8 条）----


async def _completed_prompt_id(env) -> str:
    """跑一个即时完成的任务，返回 prompt_id。"""
    async with mcp_session(env) as session:
        result = await session.call_tool("run_workflow", {"workflow": "simple"})
        payload = _text_payload(result)
        assert payload["status"] == "completed"
        return payload["prompt_id"]


def _image_url(env, prompt_id: str, index: int, *, token: str = _TOKEN, ttl: int = 3600, ext: str = "") -> str:
    """测试侧拼签名 URL（与 signing.build_image_url 同一算法）；ext 模拟发图组件带后缀。"""
    url = build_image_url(f"http://127.0.0.1:{env.mcp_port}", token, prompt_id, index, ttl)
    if ext:
        url = url.replace(f"/{index}?", f"/{index}.{ext}?")
    return url


async def test_image_url_valid_signature_returns_bytes(env) -> None:
    """规格条 1/6：签名正确且未过期 → 不带 Authorization 也能 200 取回图片字节。"""
    prompt_id = await _completed_prompt_id(env)
    async with httpx2.AsyncClient() as http:
        resp = await http.get(_image_url(env, prompt_id, 0))  # 无鉴权头
        assert resp.status_code == 200
        assert resp.content == _TINY_PNG
        assert resp.headers["content-type"] == "image/png"


async def test_image_url_wrong_signature_403(env) -> None:
    """规格条 2：签名错误（按错误 token 签）→ 403。"""
    prompt_id = await _completed_prompt_id(env)
    async with httpx2.AsyncClient() as http:
        resp = await http.get(_image_url(env, prompt_id, 0, token="wrong-token"))
        assert resp.status_code == 403


async def test_image_url_expired_403(env) -> None:
    """规格条 3：e 已在过去（负 TTL）→ 403。"""
    prompt_id = await _completed_prompt_id(env)
    async with httpx2.AsyncClient() as http:
        resp = await http.get(_image_url(env, prompt_id, 0, ttl=-10))
        assert resp.status_code == 403


async def test_image_url_missing_or_bad_query_403(env) -> None:
    """规格条 4：缺 e / 缺 s / e 非数字 / 无查询串 → 403。"""
    prompt_id = await _completed_prompt_id(env)
    good = _image_url(env, prompt_id, 0)
    base, query = good.split("?", 1)
    e_part, s_part = query.split("&", 1)  # e=... / s=...
    cases = [base, f"{base}?{s_part}", f"{base}?{e_part}", f"{base}?e=abc&{s_part}"]
    async with httpx2.AsyncClient() as http:
        for url in cases:
            resp = await http.get(url)
            assert resp.status_code == 403, url


async def test_image_url_unknown_prompt_or_index_out_of_range_404(env) -> None:
    """规格条 5：prompt_id 不存在 → 404；index 越界 → 404。"""
    prompt_id = await _completed_prompt_id(env)  # 该任务只有 1 张图
    async with httpx2.AsyncClient() as http:
        for url in (_image_url(env, "no-such-id", 0), _image_url(env, prompt_id, 99)):
            resp = await http.get(url)
            assert resp.status_code == 404, url


async def test_image_url_tolerates_extension_suffix(env) -> None:
    """路由规格：{index} 容忍 .ext 后缀（如 0.png），取点前数字作 index。"""
    prompt_id = await _completed_prompt_id(env)
    async with httpx2.AsyncClient() as http:
        resp = await http.get(_image_url(env, prompt_id, 0, ext="png"))
        assert resp.status_code == 200
        assert resp.content == _TINY_PNG


async def test_tool_results_include_urls_when_public_base_url_set(env_public) -> None:
    """规格条 8（已设）：run_workflow / get_image 结果带 urls，且原样无鉴权可抓。"""
    async with mcp_session(env_public) as session:
        result = await session.call_tool("run_workflow", {"workflow": "simple"})
        payload = _text_payload(result)
        assert payload["status"] == "completed"
        assert len(payload["urls"]) == payload["images"] == 1
        url = payload["urls"][0]
        base = f"http://127.0.0.1:{env_public.mcp_port}"
        assert url.startswith(f"{base}/images/{payload['prompt_id']}/0?")
        assert "e=" in url and "s=" in url

        result = await session.call_tool("get_image", {"prompt_id": payload["prompt_id"]})
        assert len(_text_payload(result)["urls"]) == 1
    async with httpx2.AsyncClient() as http:
        resp = await http.get(url)  # 工具给的 URL 原样抓取，无 Authorization
        assert resp.status_code == 200
        assert resp.content == _TINY_PNG


async def test_tool_results_without_public_base_url_have_no_urls(env) -> None:
    """规格条 8（未设）：不出现 urls 字段，行为与 v0.1 一致。"""
    async with mcp_session(env) as session:
        result = await session.call_tool("run_workflow", {"workflow": "simple"})
        payload = _text_payload(result)
        assert "urls" not in payload
        result = await session.call_tool("get_image", {"prompt_id": payload["prompt_id"]})
        assert "urls" not in _text_payload(result)


async def test_multi_image_urls_follow_block_order(env_public) -> None:
    """多图逐张给 urls，顺序与内联图块一致（下标 0..n-1）。"""
    async with mcp_session(env_public) as session:
        result = await session.call_tool("run_workflow", {"workflow": "simple"})
        prompt_id = _text_payload(result)["prompt_id"]
        env_public.fake.complete(prompt_id, images=[
            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"},
            {"filename": "ComfyUI_00002_.png", "subfolder": "", "type": "output"},
        ])
        result = await session.call_tool("get_image", {"prompt_id": prompt_id, "index": 99})  # 越界 → 全量
        payload = _text_payload(result)
        assert payload["images"] == 2
        assert len(payload["urls"]) == 2
        base = f"http://127.0.0.1:{env_public.mcp_port}"
        assert payload["urls"][0].startswith(f"{base}/images/{prompt_id}/0?")
        assert payload["urls"][1].startswith(f"{base}/images/{prompt_id}/1?")
    async with httpx2.AsyncClient() as http:
        for url in payload["urls"]:
            resp = await http.get(url)
            assert resp.status_code == 200
            assert resp.content == _TINY_PNG


async def test_get_image_selected_index_keeps_original_index_in_url(env_public) -> None:
    """get_image 选定 index 时，url 的下标仍是产物全集中的原始位置。"""
    async with mcp_session(env_public) as session:
        result = await session.call_tool("run_workflow", {"workflow": "simple"})
        prompt_id = _text_payload(result)["prompt_id"]
        env_public.fake.complete(prompt_id, images=[
            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"},
            {"filename": "ComfyUI_00002_.png", "subfolder": "", "type": "output"},
        ])
        result = await session.call_tool("get_image", {"prompt_id": prompt_id, "index": 1})
        payload = _text_payload(result)
        assert payload["images"] == 1
        assert len(payload["urls"]) == 1
        url = payload["urls"][0]
        assert f"/images/{prompt_id}/1?" in url
    async with httpx2.AsyncClient() as http:
        resp = await http.get(url)
        assert resp.status_code == 200
        assert resp.content == _TINY_PNG
