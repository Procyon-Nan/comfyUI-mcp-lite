"""UI 格式 → API 格式转换封装。

comfy-cli 在本项目的唯一 import 点：comfy_cli.workflow_to_api.convert_ui_to_api。
将来若更换转换实现，只需改本文件。

object_info 在进程内缓存（TTL 300 秒）：装节点/重启 ComfyUI 属低频运维事件，
不必为每次转换拉数 MB 的 schema。
"""

from __future__ import annotations

import time
from typing import Any

from comfy_cli.workflow_to_api import (
    WorkflowConversionError,
    convert_ui_to_api,
    is_api_format,
)

from .comfy import ComfyClient, ComfyError

_OBJECT_INFO_TTL_SECONDS = 300.0


class Converter:
    """工作流转换器：UI 格式转 API 格式；已是 API 格式则原样直通。"""

    def __init__(self, client: ComfyClient) -> None:
        self._client = client
        self._object_info: dict[str, Any] | None = None
        self._object_info_at = 0.0

    async def get_object_info(self) -> dict[str, Any]:
        """缓存的 /object_info（TTL 内复用）。

        转换与字段发现共用；字段发现用它区分用户标题与节点默认显示名。
        """
        if (
            self._object_info is None
            or time.monotonic() - self._object_info_at > _OBJECT_INFO_TTL_SECONDS
        ):
            self._object_info = await self._client.get_object_info()
            self._object_info_at = time.monotonic()
        return self._object_info

    async def to_api_prompt(self, workflow: Any) -> dict[str, Any]:
        """转出可直接 POST /prompt 的 API 格式 dict。

        转换失败抛 ComfyError（含原因）。
        """
        if is_api_format(workflow):
            return workflow
        if not isinstance(workflow, dict):
            raise ComfyError("工作流文件内容不是 JSON 对象")
        object_info = await self.get_object_info()
        try:
            prompt = convert_ui_to_api(workflow, object_info)
        except WorkflowConversionError as exc:
            raise ComfyError(f"工作流转换失败：{exc}") from exc
        if not isinstance(prompt, dict) or not prompt:
            raise ComfyError("工作流转换失败：结果为空（工作流里没有可执行节点？）")
        return prompt
