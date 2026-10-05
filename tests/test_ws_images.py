"""WS 取图单元测试：帧解析、收集器上限、wait_for_completion 集成、内存缓存。

帧格式依据 ComfyUI server.py / protocol.py（实机核实）：
帧 = struct.pack(">I", event_type) + 数据；PREVIEW_IMAGE(=1) 与
UNENCODED_PREVIEW_IMAGE(=2) 的数据 = struct.pack(">I", image_type) +
图片字节（1=JPEG，2=PNG）。SaveImageWebsocket 的成品图恒为 PNG；
服务级双源优先级与缓存命中的测试见 test_service.py 的 WS 段。
"""

from __future__ import annotations

import struct

import websockets
from fake_comfy import make_png

from comfy_mcp_lite.comfy import ComfyError
from comfy_mcp_lite.image_cache import ImageCache
from comfy_mcp_lite.progress import (
    WsImageCollector,
    extract_png_frame,
    wait_for_completion,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RED = make_png(b"\xff\x00\x00")
_BLUE = make_png(b"\x00\x00\xff")


def _frame(event_type: int, image_type: int, payload: bytes) -> bytes:
    """按 ComfyUI 二进制帧格式拼一条消息。"""
    return struct.pack(">II", event_type, image_type) + payload


# ---- 帧解析 ----

def test_extract_png_frame_accepts_png() -> None:
    assert extract_png_frame(_frame(1, 2, _RED)) == _RED  # PREVIEW_IMAGE
    assert extract_png_frame(_frame(2, 2, _BLUE)) == _BLUE  # UNENCODED_PREVIEW_IMAGE


def test_extract_png_frame_rejects_non_final_frames() -> None:
    jpeg = b"\xff\xd8\xff\xe0jpegdata"
    assert extract_png_frame(_frame(1, 1, jpeg)) is None  # JPEG 预览（image_type=1）
    assert extract_png_frame(_frame(2, 1, jpeg)) is None
    assert extract_png_frame(struct.pack(">I", 3) + b"some text") is None  # TEXT 事件
    assert extract_png_frame(_frame(4, 2, _RED)) is None  # PREVIEW_IMAGE_WITH_METADATA 不在白名单
    assert extract_png_frame(_frame(1, 2, b"\x89PNXgarbage")) is None  # 魔数不符


def test_extract_png_frame_rejects_truncated_frames() -> None:
    assert extract_png_frame(b"") is None
    assert extract_png_frame(struct.pack(">I", 1)) is None
    assert extract_png_frame(_frame(1, 2, b"")) is None
    assert extract_png_frame(_frame(1, 2, b"\x89PNG")) is None  # 魔数不完整


# ---- 收集器 ----

def test_collector_keeps_png_frames_in_order() -> None:
    collector = WsImageCollector()
    collector.add_frame(_frame(1, 2, _RED))
    collector.add_frame(_frame(1, 1, b"\xff\xd8\xffpreview"))  # JPEG 预览不收
    collector.add_frame(struct.pack(">I", 3) + b"text")
    collector.add_frame(_frame(2, 2, _BLUE))
    assert collector.images == [_RED, _BLUE]


def test_collector_caps_frame_count() -> None:
    frames = [_frame(1, 2, make_png(bytes([i, 0, 0]))) for i in range(20)]
    collector = WsImageCollector()
    for frame in frames:
        collector.add_frame(frame)
    assert collector.images == [f[8:] for f in frames[:16]]  # 最多 16 帧


def test_collector_rejects_oversized_frame() -> None:
    collector = WsImageCollector()
    huge = _frame(1, 2, _PNG_MAGIC + b"\x00" * (32 * 1024 * 1024 - len(_PNG_MAGIC) + 1))
    collector.add_frame(huge)
    assert collector.images == []


# ---- wait_for_completion 对真 WS 服务端 ----

class _NoHistoryClient:
    """history 永远查询失败：轮询兜底不生效，逼出 WS 事件路径。"""

    async def get_history_entry(self, prompt_id: str) -> None:
        raise ComfyError("no history")


async def test_wait_for_completion_collects_ws_pngs() -> None:
    async def handler(connection) -> None:
        await connection.send("not json at all")  # 非 JSON 文本
        await connection.send(_frame(1, 1, b"\xff\xd8\xffpreview"))  # JPEG 预览帧
        await connection.send(_frame(1, 2, _RED))
        await connection.send(_frame(1, 2, _BLUE))
        await connection.send('{"type": "execution_success", "data": {}}')

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        completion = await wait_for_completion(
            _NoHistoryClient(), "pid-1", "cid-1", f"http://127.0.0.1:{port}", 10.0
        )
    assert completion.status == "completed"
    assert completion.ws_images == [_RED, _BLUE]


# ---- 内存缓存（供 get_image 用） ----

def test_image_cache_roundtrip_and_miss() -> None:
    cache = ImageCache()
    cache.store("p1", [_RED])
    assert cache.get("p1") == [_RED]
    assert cache.get("nope") is None


def test_image_cache_entry_limit_is_lru() -> None:
    cache = ImageCache(max_entries=2)
    cache.store("a", [_RED])
    cache.store("b", [_BLUE])
    assert cache.get("a") == [_RED]  # a 置为最近使用
    cache.store("c", [_RED])  # 超条数上限 → 淘汰最久未用的 b
    assert cache.get("b") is None
    assert cache.get("a") == [_RED]
    assert cache.get("c") == [_RED]


def test_image_cache_byte_limit_evicts_oldest_first() -> None:
    cache = ImageCache(max_entries=10, max_total_bytes=2 * len(_RED))
    cache.store("a", [_RED])
    cache.store("b", [_BLUE])
    cache.store("c", [_RED])  # 总字节超限 → 淘汰最旧的 a
    assert cache.get("a") is None
    assert cache.get("b") == [_BLUE]
    assert cache.get("c") == [_RED]


def test_image_cache_overwrite_replaces_without_leak() -> None:
    cache = ImageCache(max_entries=10, max_total_bytes=len(_RED))
    cache.store("p", [_RED])
    cache.store("p", [_BLUE])  # 覆盖写：旧字节不再计入
    assert cache.get("p") == [_BLUE]


def test_image_cache_single_entry_over_limit_is_dropped() -> None:
    cache = ImageCache(max_total_bytes=8)
    cache.store("p", [_RED])  # 单任务即超总上限 → 不驻留（宁缺毋超）
    assert cache.get("p") is None
