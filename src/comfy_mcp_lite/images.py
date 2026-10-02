"""参考图输入预处理（规格 §5）。

ComfyUI 的 LoadImage 只认服务器 input/ 里已有的文件名，
由本模块代劳：识别图源 → 取字节 → POST /upload/image → 返回文件名。

图源仅支持两种：
- 内联 base64："data:image/png;base64,iVBOR..."
- HTTP(S) URL："https://example.com/ref.png"（服务器代下载）
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import re
import uuid
from urllib.parse import urlparse

import httpx

from .comfy import ComfyClient, ComfyError

_MAX_IMAGE_BYTES = 64 * 1024 * 1024  # 参考图大小上限
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

_MIME_DEFAULT_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def _ext_for_mime(mime_type: str) -> str:
    ext = mimetypes.guess_extension(mime_type)
    if not ext or ext == ".jpe":  # guess_extension 对 jpeg 可能给出冷门后缀
        ext = _MIME_DEFAULT_EXT.get(mime_type, ".png")
    return ext


async def resolve_image_source(client: ComfyClient, source: str) -> str:
    """识别图源、取字节、上传，返回 LoadImage 可用的文件名。"""
    if not isinstance(source, str) or not source:
        raise ComfyError("参考图源必须是字符串（data URL 或 http(s) URL）")
    if source.startswith("data:"):
        filename, data, mime = _decode_data_url(source)
    elif source.startswith(("http://", "https://")):
        filename, data, mime = await _download(source)
    else:
        raise ComfyError(
            f"不支持的参考图源（仅支持 data URL 与 http(s) URL）：{source[:64]}..."
        )
    if len(data) > _MAX_IMAGE_BYTES:
        raise ComfyError(f"参考图过大（{len(data) // 1024 // 1024} MB，上限 64 MB）")
    return await client.upload_image(filename, data, mime)


def _decode_data_url(source: str) -> tuple[str, bytes, str]:
    """解析 data URL → (文件名, 字节, MIME)。"""
    header, sep, payload = source.partition(",")
    if not sep or ";base64" not in header:
        raise ComfyError("data URL 必须是 base64 形式（data:image/png;base64,...）")
    mime = header[5:].split(";", 1)[0].strip().lower() or "image/png"
    if not mime.startswith("image/"):
        raise ComfyError(f"data URL 不是图片类型：{mime}")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ComfyError(f"data URL base64 解码失败：{exc}") from exc
    if not data:
        raise ComfyError("data URL 内容为空")
    # 无原生文件名，造一个稳的
    return f"mcp_{uuid.uuid4().hex[:8]}{_ext_for_mime(mime)}", data, mime


async def _download(url: str) -> tuple[str, bytes, str]:
    """代下载 HTTP(S) 图源 → (文件名, 字节, MIME)。"""
    try:
        async with httpx.AsyncClient(
            timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
        ) as http:
            resp = await http.get(url)
    except httpx.HTTPError as exc:
        raise ComfyError(f"参考图下载失败（{url}）：{exc}") from exc
    if not resp.is_success:
        raise ComfyError(f"参考图下载失败：HTTP {resp.status_code}（{url}）")
    mime = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if mime and not mime.startswith("image/") and mime != "application/octet-stream":
        raise ComfyError(f"参考图 URL 返回的不是图片（Content-Type: {mime}）")
    data = resp.content
    if not data:
        raise ComfyError("参考图 URL 返回内容为空")
    name = _filename_from_url(url, mime or "image/png")
    return name, data, mime or "image/png"


def _filename_from_url(url: str, mime: str) -> str:
    """取 URL 里的文件名（清洗到安全字符集）；取不到就造一个。"""
    path = urlparse(url).path
    base = path.rsplit("/", 1)[-1] if path else ""
    base = _SAFE_FILENAME_RE.sub("_", base)
    if base and "." in base:
        return f"mcp_{uuid.uuid4().hex[:6]}_{base}"
    return f"mcp_{uuid.uuid4().hex[:8]}{_ext_for_mime(mime)}"
