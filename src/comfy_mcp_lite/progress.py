"""执行等待：WebSocket 事件优先，history 轮询兜底。

规格 §6.2：先连 ws://…/ws 收事件；连不上（或事件不来）时轮询
GET /history/<prompt_id> 兜底，防工作流不触发 WS 事件。

实现上两者并发跑：WS 提供即时唤醒，轮询（1 秒）是权威完成信号
（history 出现该 prompt 的条目即为终态）。超时前再做一次收尾确认，
避免「恰好在 deadline 完成」被误报为超时。

WS 二进制帧（ComfyUI server.py / protocol.py）：帧 = struct.pack(">I",
event_type) + 数据；PREVIEW_IMAGE(=1) 与 UNENCODED_PREVIEW_IMAGE(=2) 的
数据 = struct.pack(">I", image_type) + 图片字节（1=JPEG，2=PNG）。
SaveImageWebsocket（custom_nodes/websocket_image_save.py）不落盘，把
整张成品 PNG 从进度 hook 推过来（image_type=2）；常规采样预览不是
PNG。据此把成品 PNG 帧收集起来随终态一并返回，预览/文本帧照旧丢弃。
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from typing import NamedTuple
from urllib.parse import urlparse

import websockets

from .comfy import ComfyClient, ComfyError, history_error

logger = logging.getLogger(__name__)

# 成品 PNG 收集上限（防异常数据撑爆内存）：最多 16 帧 / 单帧 ≤ 32MB
_MAX_WS_IMAGES = 16
_MAX_WS_IMAGE_BYTES = 32 * 1024 * 1024
# 单条 WS 消息的接收上限：帧头 8 字节 + 单帧图片上限，超出按断连处理。
# SaveImageWebsocket 推的是完整 PNG，须盖过只按 JPEG 预览设的旧值 16MB
_WS_MAX_SIZE = _MAX_WS_IMAGE_BYTES + 8
_POLL_INTERVAL_SECONDS = 1.0
# 完成后的收尾宽限：轮询抢先判定终态时，成品帧可能还在接收队列里，
# 等监听自然退出（收到终态 JSON 事件）可确保帧收全
_WS_DRAIN_SECONDS = 0.5

# BinaryEventTypes 里带图片的两种事件
_IMAGE_EVENTS = frozenset({1, 2})  # PREVIEW_IMAGE / UNENCODED_PREVIEW_IMAGE
_IMAGE_TYPE_PNG = 2
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class Completion(NamedTuple):
    """一次等待的结果：终态 + WS 捕获的成品 PNG（按帧序，可为空）。"""

    status: str  # "completed" | "timeout"
    ws_images: list[bytes]


def extract_png_frame(message: bytes) -> bytes | None:
    """从 WS 二进制帧提取成品 PNG 字节；非成品帧返回 None。

    只认 PREVIEW_IMAGE / UNENCODED_PREVIEW_IMAGE 且 image_type=PNG 的帧，
    并校验 PNG 魔数：JPEG 预览、TEXT、截断帧、坏数据一律不收。
    """
    if len(message) < 8 + len(_PNG_MAGIC):
        return None
    event_type, image_type = struct.unpack_from(">II", message)
    if event_type not in _IMAGE_EVENTS or image_type != _IMAGE_TYPE_PNG:
        return None
    image = message[8:]
    if not image.startswith(_PNG_MAGIC):
        return None
    return image


class WsImageCollector:
    """WS 成品 PNG 收集器（带上限：最多 16 帧 / 单帧 ≤ 32MB）。"""

    def __init__(self) -> None:
        self.images: list[bytes] = []

    def add_frame(self, message: bytes) -> None:
        # 先按数量与长度剪枝，超限帧连解析带拷贝都省掉
        if len(self.images) >= _MAX_WS_IMAGES or len(message) > _MAX_WS_IMAGE_BYTES + 8:
            return
        image = extract_png_frame(message)
        if image is not None:
            self.images.append(image)


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
) -> Completion:
    """等待任务终态。失败抛 ComfyError；超时 status="timeout"（任务仍可补取）。

    Completion.ws_images 是 WS 捕获的成品 PNG（SaveImageWebsocket 类
    工作流的唯一图源）；失败/超时时捕获到的部分帧也一并带回，由调用方取舍。
    """
    outcome = _Outcome()
    collector = WsImageCollector()
    ws_task = asyncio.create_task(
        _listen_ws(_ws_url(comfyui_url), client_id, prompt_id, outcome, collector)
    )
    poll_task = asyncio.create_task(_poll_history(client, prompt_id, outcome))
    completed = False
    try:
        await asyncio.wait_for(outcome.event.wait(), timeout_seconds)
        if outcome.error:
            raise ComfyError(outcome.error)
        completed = True
    except asyncio.TimeoutError:
        # 收尾确认：deadline 边界上可能刚好完成
        entry = await _safe_history(client, prompt_id)
        if entry is not None:
            error = history_error(entry)
            if error:
                raise ComfyError(error)
            completed = True
    finally:
        if completed:
            await _drain_ws_listener(ws_task)
        for task in (ws_task, poll_task):
            task.cancel()
        await asyncio.gather(ws_task, poll_task, return_exceptions=True)
    return Completion("completed" if completed else "timeout", collector.images)


async def _drain_ws_listener(ws_task: asyncio.Task) -> None:
    """给 WS 监听一个短宽限，让它自然退出（收到终态 JSON 事件即返回）。

    轮询先于 WS 判定终态时，成品帧可能仍在接收队列里；帧在流上先于
    终态事件，监听一旦自然退出即说明帧已收全。宽限耗尽则由外层取消。
    """
    try:
        await asyncio.wait_for(asyncio.shield(ws_task), _WS_DRAIN_SECONDS)
    except asyncio.TimeoutError:
        pass


async def _listen_ws(
    ws_url: str,
    client_id: str,
    prompt_id: str,
    outcome: _Outcome,
    collector: WsImageCollector,
) -> None:
    """连 WS 收事件；连不上静默退出（轮询兜底）。被取消即退出。

    二进制帧交给 collector：成品 PNG 收集，预览/文本帧在解析时丢弃。
    """
    try:
        async with websockets.connect(
            f"{ws_url}?clientId={client_id}",
            open_timeout=5,
            close_timeout=1,
            max_size=_WS_MAX_SIZE,
        ) as ws:
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    collector.add_frame(bytes(message))
                    continue
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
