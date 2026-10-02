"""执行等待：WebSocket 事件优先，history 轮询兜底。

规格 §6.2：先连 ws://…/ws 收事件；连不上（或事件不来）时轮询
GET /history/<prompt_id> 兜底，防工作流不触发 WS 事件。

实现上两者并发跑：WS 提供即时唤醒，轮询（1 秒）是权威完成信号
（history 出现该 prompt 的条目即为终态）。超时前再做一次收尾确认，
避免「恰好在 deadline 完成」被误报为超时。
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse

import websockets

from .comfy import ComfyClient, ComfyError, history_error

logger = logging.getLogger(__name__)

_WS_MAX_SIZE = 16 * 1024 * 1024  # 预览帧上限，超出按断连处理
_POLL_INTERVAL_SECONDS = 1.0


class _Outcome:
    """一次等待的终态信号。"""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.error: str | None = None

    def settle(self, error: str | None = None) -> None:
        self.error = error
        self.event.set()


def _ws_url(comfyui_url: str) -> str:
    parsed = urlparse(comfyui_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws"


async def wait_for_completion(
    client: ComfyClient,
    prompt_id: str,
    client_id: str,
    comfyui_url: str,
    timeout_seconds: float,
) -> str:
    """等待任务终态。返回 "completed"；失败抛 ComfyError；超时返回 "timeout"。

    注意：超时后任务仍在 ComfyUI 队列里执行，prompt_id 可供 get_image 补取。
    """
    outcome = _Outcome()
    ws_task = asyncio.create_task(
        _listen_ws(_ws_url(comfyui_url), client_id, prompt_id, outcome)
    )
    poll_task = asyncio.create_task(_poll_history(client, prompt_id, outcome))
    try:
        await asyncio.wait_for(outcome.event.wait(), timeout_seconds)
        if outcome.error:
            raise ComfyError(outcome.error)
        return "completed"
    except asyncio.TimeoutError:
        # 收尾确认：deadline 边界上可能刚好完成
        entry = await _safe_history(client, prompt_id)
        if entry is not None:
            error = history_error(entry)
            if error:
                raise ComfyError(error)
            return "completed"
        return "timeout"
    finally:
        for task in (ws_task, poll_task):
            task.cancel()
        await asyncio.gather(ws_task, poll_task, return_exceptions=True)


async def _listen_ws(ws_url: str, client_id: str, prompt_id: str, outcome: _Outcome) -> None:
    """连 WS 收事件；连不上静默退出（轮询兜底）。被取消即退出。"""
    try:
        async with websockets.connect(
            f"{ws_url}?clientId={client_id}",
            open_timeout=5,
            close_timeout=1,
            max_size=_WS_MAX_SIZE,
        ) as ws:
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    continue  # 二进制预览帧
                try:
                    data = json.loads(message)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "execution_error":
                    outcome.settle(_describe_error(data.get("data") or {}))
                    return
                if data.get("type") in ("execution_success", "execution_interrupted"):
                    outcome.settle(
                        "任务被中断" if data.get("type") == "execution_interrupted" else None
                    )
                    return
                if data.get("type") == "executing":
                    payload = data.get("data") or {}
                    if payload.get("node") is None and payload.get("prompt_id", prompt_id) == prompt_id:
                        outcome.settle()
                        return
    except (OSError, websockets.WebSocketException):
        # WS 连不上：规格允许，轮询兜底
        logger.debug("ComfyUI WS 连接失败，转为轮询：%s", ws_url)
        return


def _describe_error(payload: dict) -> str:
    node = f"节点 {payload.get('node_type', '?')}(#{payload.get('node_id', '?')})："
    return f"{node}{payload.get('exception_type', '')} {payload.get('exception_message', '')}".strip()


async def _poll_history(client: ComfyClient, prompt_id: str, outcome: _Outcome) -> None:
    """轮询 /history/<id>；条目出现即为终态（权威信号）。"""
    while not outcome.event.is_set():
        entry = await _safe_history(client, prompt_id)
        if entry is not None:
            outcome.settle(history_error(entry))
            return
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _safe_history(client: ComfyClient, prompt_id: str) -> dict | None:
    """history 查询；瞬时故障不中断等待（由总超时兜底）。"""
    try:
        return await client.get_history_entry(prompt_id)
    except ComfyError:
        return None
