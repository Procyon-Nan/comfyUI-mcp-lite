"""三个 MCP 工具：list_workflows / run_workflow / get_image。

地址语义（规格 §6.5）：prompts / numbers / images 里的键必须是
该工作流转换产物中真实存在的 node_id.field（与 list_workflows 报出的
一致），未知地址与跨类地址都报错并回列可填字段，绝不静默忽略。

图片以 MCP ImageContent（base64 字节）内联返回。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import uuid
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent
from pydantic import Field

from .comfy import (
    ComfyClient,
    ComfyError,
    history_error,
    history_output_images,
)
from .config import Config
from .converter import Converter
from .discover import discover_fields
from .images import resolve_image_source
from .progress import wait_for_completion


def _text(payload: dict[str, Any]) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))


def _split_address(address: str) -> tuple[str, str]:
    node_id, sep, field = address.rpartition(".")
    if not node_id or not sep or not field:
        raise ComfyError(f"字段地址格式应为 node_id.field：{address!r}")
    return node_id, field


def _validate_addresses(
    fields: dict[str, dict[str, Any]],
    prompts: dict[str, str],
    numbers: dict[str, int | float],
    images: dict[str, str],
) -> None:
    """三组地址逐一与字段发现结果对账；未知/跨类地址报错。"""
    category_of = {addr: cat for cat, entries in fields.items() for addr in entries}
    for category, provided in (("prompts", prompts), ("numbers", numbers), ("images", images)):
        valid = set(fields.get(category, {}))
        for address in provided:
            if address in valid:
                continue
            if address in category_of:
                raise ComfyError(
                    f"{address} 属于 {category_of[address]} 类字段，请放入 {category_of[address]} 参数"
                )
            readable = {
                cat: sorted(entries) for cat, entries in sorted(fields.items())
            } or "（无可填字段）"
            raise ComfyError(f"{address} 不是该工作流的可填字段。可填字段：{readable}")


def _image_content(data: bytes, filename: str) -> ImageContent:
    mime = mimetypes.guess_type(filename)[0] or "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    return ImageContent(type="image", data=encoded, mimeType=mime)

def _completed_payload(prompt_id: str, image_count: int) -> dict[str, Any]:
    """完成态结果 JSON：状态 + prompt_id + 图片数。"""
    return {"status": "completed", "prompt_id": prompt_id, "images": image_count}

def register(mcp: MCPServer, config: Config, client: ComfyClient) -> None:
    """在 MCPServer 实例上注册三个工具。

    client 由 build_app 创建并传入（闭包持转换器与 client_id）。
    """
    converter = Converter(client)
    client_id = uuid.uuid4().hex  # WS 订阅与任务提交共用的身份

    @mcp.tool()
    async def list_workflows() -> dict[str, Any]:
        """List available workflows and the fields each accepts (prompts/numbers/images; empty categories omit keys). (中文：列出可用工作流，以及每个工作流能填什么（prompts/numbers/images，空类省略键）。)

        Returns e.g. {"simple": {"prompts": {"78.text": {"label": "Positive prompt (中文：正面提示词)"}}, ...}}. A null label means no semantic was inferred, but the address is still callable. Omitted fields keep the workflow's original value. (中文：返回形如上例；label 为 null 表示未识别出语义，地址仍可直接调用；不填的字段运行时用工作流原值。)
        """
        paths = await client.list_workflow_paths()
        result: dict[str, Any] = {}
        for rel_path in paths:
            name = rel_path[: -len(".json")]
            try:
                workflow = await client.get_workflow(rel_path)
                prompt = await converter.to_api_prompt(workflow)
            except ComfyError as exc:
                result[name] = {"error": str(exc)}
                continue
            result[name] = discover_fields(prompt, await converter.get_object_info())
        return result

    @mcp.tool()
    async def run_workflow(
        workflow: Annotated[str, Field(description='Name of the workflow to run (see list_workflows). (中文：要运行的工作流名称，见 list_workflows。)')],
        prompts: Annotated[dict[str, str] | None, Field(description='Text prompts; keys are field addresses from list_workflows (e.g. "78.text"). (中文：文本提示词，键为 list_workflows 报出的字段地址。)')] = None,
        numbers: Annotated[dict[str, int | float] | None, Field(description='Numeric fields such as width/height; keys are field addresses (e.g. "80.width"). (中文：数值字段如宽高，键为字段地址。)')] = None,
        images: Annotated[dict[str, str] | None, Field(description='Reference images; keys are field addresses (e.g. "5.image"), values are a data URL or an http(s) URL. (中文：参考图，键为字段地址，值为 data URL 或 http(s) URL。)')] = None,
        timeout_seconds: Annotated[float, Field(description='Max seconds to wait for completion; default 60. (中文：等待完成的最大秒数，默认 60。)')] = 60.0,
    ) -> list[TextContent | ImageContent]:
        """Run a named workflow and wait for the output images; on success the images are inlined. (中文：按名称运行工作流并等待出图；成功时图片直接内联返回。)

        prompts/numbers/images keys are the addresses reported by list_workflows (e.g. "78.text", "80.width", "5.image"). images values accept only an inline data URL (data:image/png;base64,...) or an http(s) URL (the server fetches and uploads it). Omitted fields keep the workflow's original value. (中文：prompts/numbers/images 的键用 list_workflows 报出的地址（如 "78.text"、"80.width"、"5.image"）。images 的值仅支持内联 data URL（data:image/png;base64,...）或 http(s) URL（服务器代下载后自动上传）。不填的字段保持工作流原值。)

        If waiting exceeds timeout_seconds, returns {"status":"timeout","prompt_id":...} without images; fetch later with get_image. (中文：等待超过 timeout_seconds 时返回不含图的该 JSON，之后可用 get_image 补取。)
        """
        if timeout_seconds <= 0:
            raise ComfyError("timeout_seconds 必须为正数")
        prompts = prompts or {}
        numbers = numbers or {}
        images = images or {}

        rel_path = f"{workflow}.json"
        paths = await client.list_workflow_paths()
        if rel_path not in set(paths):
            names = ", ".join(p[: -len(".json")] for p in paths) or "（无）"
            raise ComfyError(f"无此工作流：{workflow}。可用工作流：{names}")

        prompt = await converter.to_api_prompt(await client.get_workflow(rel_path))
        fields = discover_fields(prompt, await converter.get_object_info())
        _validate_addresses(fields, prompts, numbers, images)

        # 参考图先解析上传（网络往返），失败在提交前暴露
        for address, source in images.items():
            node_id, field = _split_address(address)
            prompt[node_id]["inputs"][field] = await resolve_image_source(client, source)
        for address, value in prompts.items():
            node_id, field = _split_address(address)
            prompt[node_id]["inputs"][field] = value
        for address, value in numbers.items():
            node_id, field = _split_address(address)
            # 768.0 这类整数值写回 INT 字段，避免 ComfyUI 校验拒绝
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            prompt[node_id]["inputs"][field] = value

        prompt_id = await client.queue_prompt(prompt, client_id)
        status = await wait_for_completion(
            client, prompt_id, client_id, config.comfyui_url, timeout_seconds
        )
        if status == "timeout":
            return [
                _text(
                    {
                        "status": "timeout",
                        "prompt_id": prompt_id,
                        "hint": "任务仍在执行，稍后可用 get_image(prompt_id=...) 补取",
                    }
                )
            ]

        entry = await client.get_history_entry(prompt_id)
        metas = history_output_images(entry or {})
        if not metas:
            raise ComfyError("任务已完成但未产出图片（工作流里需要 SaveImage 类输出节点）")
        blocks = [
            _image_content(await client.fetch_image(m["filename"], m["subfolder"], m["type"]), m["filename"])
            for m in metas
        ]
        return [
            _text(_completed_payload(prompt_id, len(blocks))),
            *blocks,
        ]

    @mcp.tool()
    async def get_image(
        prompt_id: Annotated[str, Field(description='Task ID returned by run_workflow. (中文：run_workflow 返回的任务 ID。)')],
        index: Annotated[int, Field(description='Which image to return, 0-based; out of range returns all. (中文：返回第几张图，从 0 起；越界返回全部。)')] = 0,
    ) -> list[TextContent | ImageContent]:
        """Fetch images for a task ID immediately; never waits. (中文：按任务 ID 立即取图，绝不等待。)

        Returns {"status":"completed"} with inlined images if finished, or {"status":"running"} if still executing. index picks the n-th image (0-based); out of range returns all. (中文：已完成返回该 JSON 并内联附图；仍在执行返回 running；index 选第几张（从 0 起），越界返回全部。)
        """
        entry = await client.get_history_entry(prompt_id)
        if entry is None:
            if await client.is_in_queue(prompt_id):
                return [_text({"status": "running", "prompt_id": prompt_id})]
            raise ComfyError(
                f"无此任务：{prompt_id}（既不在执行队列也无历史记录，可能已被清理或从未提交）"
            )
        error = history_error(entry)
        if error:
            raise ComfyError(f"该任务执行失败：{error}")
        metas = history_output_images(entry)
        if not metas:
            raise ComfyError("任务已完成但未产出图片（工作流里需要 SaveImage 类输出节点）")
        selected = metas if not 0 <= index < len(metas) else [metas[index]]
        blocks = [
            _image_content(await client.fetch_image(m["filename"], m["subfolder"], m["type"]), m["filename"])
            for m in selected
        ]
        return [
            _text(_completed_payload(prompt_id, len(blocks))),
            *blocks,
        ]
