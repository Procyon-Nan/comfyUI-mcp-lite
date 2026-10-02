"""ComfyUI REST 客户端（httpx）。

与 ComfyUI 的全部 HTTP 交互集中于此。接口事实依据 ComfyUI 0.38 源码：
- GET /userdata?dir=workflows&recurse=true → 工作流相对路径字符串列表（如 "simple.json"、"sub/x.json"）
- GET /userdata/<%2F 编码的路径> → 文件内容（路由按单段匹配，斜杠必须编码为 %2F）
- GET /object_info → {节点类: schema}
- POST /upload/image（multipart: image=<文件>, overwrite）→ {name, subfolder, type}
- POST /prompt {prompt, client_id} → {prompt_id, number, node_errors}；校验失败 400
- GET /history/<prompt_id> → {prompt_id: {outputs, status}} 或 {}（运行中无条目）
- GET /queue → {queue_running: [[编号, prompt_id, ...]], queue_pending: [...]}
- GET /view?filename=&subfolder=&type= → 图片字节
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver.exceptions import ToolError


class ComfyError(ToolError):
    """与 ComfyUI 交互失败（连接失败、HTTP 错误、业务错误）。

    继承 SDK 的 ToolError（故意型工具错误）：经 @mcp.tool 包装后
    以 is_error=True 的工具结果返回给模型，且错误文本完整保留
    （普通异常会被替换成 "Error executing tool ..."）。
    """


class ComfyClient:
    """长期存活的 ComfyUI 客户端；随服务进程存活。"""

    def __init__(self, base_url: str, request_timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=request_timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- 底层 ----

    async def _get_json(self, path: str, *, params: dict[str, str] | None = None, action: str) -> Any:
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）：{exc}") from exc
        self._raise_for_status(resp, action)
        try:
            return resp.json()
        except ValueError:
            raise ComfyError(f"ComfyUI {action}返回的不是合法 JSON") from None

    def _raise_for_status(self, resp: httpx.Response, action: str) -> None:
        if resp.is_success:
            return
        detail = resp.text[:400].strip()
        raise ComfyError(f"ComfyUI {action}失败：HTTP {resp.status_code} {detail}")

    # ---- 工作流（userdata）----

    async def list_workflow_paths(self) -> list[str]:
        """列出 workflows 目录（含子目录）下的 .json 相对路径。"""
        entries = await self._get_json(
            "/userdata",
            params={"dir": "workflows", "recurse": "true"},
            action="列出工作流",
        )
        if not isinstance(entries, list):
            raise ComfyError("ComfyUI 列出工作流返回结构异常")
        return sorted(e for e in entries if isinstance(e, str) and e.endswith(".json"))

    async def get_workflow(self, rel_path: str) -> dict[str, Any]:
        """按相对路径（如 "simple.json"）读取工作流 JSON。"""
        quoted = quote(f"workflows/{rel_path}", safe="")
        return await self._get_json(f"/userdata/{quoted}", action=f"读取工作流 {rel_path}")

    async def get_object_info(self) -> dict[str, Any]:
        """全量节点 schema（数 MB，由调用方缓存）。"""
        info = await self._get_json("/object_info", action="获取节点信息")
        if not isinstance(info, dict):
            raise ComfyError("ComfyUI /object_info 返回结构异常")
        return info

    # ---- 参考图上传 ----

    async def upload_image(self, filename: str, data: bytes, content_type: str) -> str:
        """上传参考图到 input/，返回 LoadImage 可用的文件名（含子目录前缀）。"""
        try:
            resp = await self._http.post(
                "/upload/image",
                data={"overwrite": "true"},
                files={"image": (filename, data, content_type)},
            )
        except httpx.HTTPError as exc:
            raise ComfyError(f"上传参考图失败（{self.base_url}）：{exc}") from exc
        self._raise_for_status(resp, "上传参考图")
        payload = resp.json()
        name = payload.get("name")
        if not name:
            raise ComfyError(f"上传参考图返回结构异常：{payload}")
        subfolder = payload.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    # ---- 执行 ----

    async def queue_prompt(self, prompt: dict[str, Any], client_id: str) -> str:
        """提交 API 格式 prompt，返回 prompt_id。校验失败抛 ComfyError（含原因）。"""
        try:
            resp = await self._http.post(
                "/prompt", json={"prompt": prompt, "client_id": client_id}
            )
        except httpx.HTTPError as exc:
            raise ComfyError(f"提交任务失败（{self.base_url}）：{exc}") from exc
        if resp.is_success:
            return resp.json()["prompt_id"]
        # 400：节点校验失败，尽量给出可读的原因
        try:
            body = resp.json()
        except ValueError:
            body = {}
        reason = body.get("error")
        if isinstance(reason, dict):
            reason = reason.get("message") or reason.get("details") or str(reason)
        node_errors = body.get("node_errors") or {}
        details = []
        for node_id, errs in node_errors.items():
            for err in (errs.get("errors") or []):
                details.append(
                    f"节点 {errs.get('class_type', '?')}(#{node_id})："
                    f"{err.get('message', '')} {err.get('details', '')}".strip()
                )
        msg = f"ComfyUI 拒绝该任务：HTTP {resp.status_code} {reason or resp.text[:400]}"
        if details:
            msg += "；" + "；".join(details)
        raise ComfyError(msg)

    async def get_history_entry(self, prompt_id: str) -> dict[str, Any] | None:
        """取单个任务的 history 条目；运行中/不存在返回 None。"""
        data = await self._get_json(f"/history/{prompt_id}", action="查询任务历史")
        if not isinstance(data, dict):
            return None
        entry = data.get(prompt_id)
        return entry if isinstance(entry, dict) else None

    async def is_in_queue(self, prompt_id: str) -> bool:
        """任务是否还在执行队列（运行或排队）。"""
        data = await self._get_json("/queue", action="查询执行队列")
        for key in ("queue_running", "queue_pending"):
            for entry in data.get(key) or []:
                if isinstance(entry, list) and len(entry) > 1 and entry[1] == prompt_id:
                    return True
        return False

    # ---- 产物 ----

    async def fetch_image(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        """按 /view 参数取回图片字节。"""
        params = {"filename": filename, "subfolder": subfolder, "type": type_}
        try:
            resp = await self._http.get("/view", params=params)
        except httpx.HTTPError as exc:
            raise ComfyError(f"取图失败（{self.base_url}/view）：{exc}") from exc
        self._raise_for_status(resp, f"取图 {filename}")
        return resp.content


def history_output_images(entry: dict[str, Any]) -> list[dict[str, str]]:
    """从 history 条目提取全部产物图片的 /view 参数。

    返回 [{filename, subfolder, type}]，顺序按节点 id 稳定排序。
    """
    images: list[dict[str, str]] = []
    outputs = entry.get("outputs") or {}
    for node_id in sorted(outputs, key=str):
        for image in (outputs[node_id] or {}).get("images") or []:
            if image.get("filename"):
                images.append(
                    {
                        "filename": image["filename"],
                        "subfolder": image.get("subfolder") or "",
                        "type": image.get("type") or "output",
                    }
                )
    return images


def history_error(entry: dict[str, Any]) -> str | None:
    """任务执行失败时返回可读错误信息，否则 None。"""
    status = entry.get("status") or {}
    if status.get("status_str") != "error":
        return None
    for message in status.get("messages") or []:
        # message = [类型, 负载]；执行错误负载含节点与异常信息
        if isinstance(message, list) and message and message[0] == "execution_error":
            payload = message[1] or {}
            node = f"节点 {payload.get('node_type', '?')}(#{payload.get('node_id', '?')})："
            return f"{node}{payload.get('exception_type', '')} {payload.get('exception_message', '')}".strip()
    return "任务执行失败（ComfyUI 未提供详细原因）"


