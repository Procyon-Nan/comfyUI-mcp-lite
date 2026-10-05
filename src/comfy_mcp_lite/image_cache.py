"""WS 成品图的进程内缓存（供 get_image 补取）。

SaveImageWebsocket 类节点不落盘，成品 PNG 只经 WS 推一次；run_workflow
抓到的这些字节按 prompt_id 缓存在本进程，get_image 命中即直接内联返回，
不必依赖磁盘。上限：最多 32 个任务 / 总量 256MB，LRU 淘汰。

进程重启即失效——已知可接受的权衡：磁盘产物路（SaveImage →
history → /view）不受影响，未命中时 get_image 照旧回退磁盘。
"""

from __future__ import annotations

from collections import OrderedDict

_DEFAULT_MAX_ENTRIES = 32
_DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024


class ImageCache:
    """prompt_id → [PNG bytes, ...] 的 LRU 缓存（带上限；asyncio 单线程用，无锁）。"""

    def __init__(
        self,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._entries: OrderedDict[str, list[bytes]] = OrderedDict()
        self._bytes = 0

    def store(self, prompt_id: str, images: list[bytes]) -> None:
        """保存（或覆盖）一个任务的图片列表；随后淘汰到限额内。"""
        self._discard(prompt_id)
        self._entries[prompt_id] = list(images)
        self._bytes += _size_of(images)
        self._evict()

    def get(self, prompt_id: str) -> list[bytes] | None:
        """命中返回图片列表并把该任务置为最近使用；未命中返回 None。"""
        images = self._entries.get(prompt_id)
        if images is None:
            return None
        self._entries.move_to_end(prompt_id)
        return images

    def _discard(self, prompt_id: str) -> None:
        images = self._entries.pop(prompt_id, None)
        if images is not None:
            self._bytes -= _size_of(images)

    def _evict(self) -> None:
        # 单任务即超总上限时也会被淘汰：宁可不缓存也不超内存预算
        while self._entries and (
            len(self._entries) > self._max_entries or self._bytes > self._max_total_bytes
        ):
            self._bytes -= _size_of(self._entries.popitem(last=False)[1])


def _size_of(images: list[bytes]) -> int:
    return sum(len(image) for image in images)
