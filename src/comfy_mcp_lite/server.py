"""服务器装配与入口。

流程：校验配置（token 未设即拒绝启动）→ 建 ComfyClient（工具与图片
端点共用）→ MCPServer（StreamableHTTP，端点 /mcp）→ Starlette 顶层
应用：GET /images/{prompt_id}/{index} 走查询串签名、免 Bearer（v0.2，
发图组件不带 Authorization 头），其余路径（含 /mcp）套 Bearer 鉴权
中间件 → uvicorn 监听。
"""

from __future__ import annotations

import logging
import mimetypes
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import __version__, tools
from .auth import BearerAuthMiddleware
from .comfy import ComfyClient, ComfyError, history_output_images
from .config import Config, ConfigError
from .signing import verify_signature


def _error(status: int, detail: str) -> JSONResponse:
    """JSON 错误响应（与 auth.py 的 401 格式一致）。"""
    return JSONResponse({"error": detail}, status_code=status)


def build_app(config: Config) -> Starlette:
    """组装完整 ASGI 应用（/images 签名端点 + Bearer 保护的 /mcp）。"""
    mcp = MCPServer("comfy-mcp-lite", version=__version__)
    client = ComfyClient(config.comfyui_url)  # 工具与 /images 端点共用
    tools.register(mcp, config, client)
    # host 传入实际绑定地址：绑 0.0.0.0（跨机共享）时关闭 SDK 的
    # DNS 重绑定自动防护（该防护默认按 127.0.0.1 触发，会拒绝远端 Host 头）
    # json_response=True：POST 响应整体 application/json 返回而非 SSE 流。
    # SSE 模式下 httpx2>=2.13 的解析器有「单事件 <= 1MiB」硬限制，内联
    # base64 大图（原始 >约 768KB，如 4K 图）会报 "SSE stream ended
    # without a response"；JSON 模式绕过 SSE 解析器，实测 20MB 稳定通过。
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", host=config.mcp_host, json_response=True
    )

    async def image_endpoint(request: Request) -> Response:
        """GET /images/{prompt_id}/{index}[.ext]?e=<unix过期>&s=<hmac>。

        免 Bearer：鉴权即查询串签名校验（见 signing.py）。.ext 后缀
        （如 0.png）只是给发图组件认文件类型用的，取点前数字作 index。
        签名/参数不合法 403；任务无记录或下标越界 404。
        """
        prompt_id = request.path_params["prompt_id"]
        raw_index = str(request.path_params["index"])
        head, _, _ = raw_index.rpartition(".")
        try:
            index = int(head or raw_index)
            expires = int(request.query_params.get("e", ""))
        except ValueError:
            return _error(403, "index / e 缺失或不是合法数字")
        supplied = request.query_params.get("s", "")
        if not verify_signature(config.auth_token, prompt_id, index, expires, supplied):
            return _error(403, "签名校验失败或链接已过期")
        entry = await client.get_history_entry(prompt_id)
        metas = history_output_images(entry or {})
        if not 0 <= index < len(metas):
            return _error(
                404, f"无此产物：{prompt_id} 的第 {index} 张（任务不存在、未完成或越界）"
            )
        meta = metas[index]
        try:
            data = await client.fetch_image(meta["filename"], meta["subfolder"], meta["type"])
        except ComfyError as exc:
            return _error(502, str(exc))
        # content-type 按 ComfyUI 产物文件名推断；URL 里的 .ext 只是装饰
        media_type = mimetypes.guess_type(meta["filename"])[0] or "image/png"
        return Response(content=data, media_type=media_type)

    return Starlette(
        routes=[
            # /images 免 Bearer（挂在 /mcp 前面，其余路径才会落到 Bearer 挂载）
            Route("/images/{prompt_id}/{index}", image_endpoint, methods=["GET"]),
            Mount("/", app=BearerAuthMiddleware(mcp_app, config.auth_token)),
        ],
        # Starlette 的 Mount 不转发 lifespan；内层 mcp 应用的 lifespan
        # （即 SDK 的会话管理器）必须由顶层接管，否则 /mcp 会话无法建立
        lifespan=mcp_app.router.lifespan_context,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"拒绝启动：{exc}", file=sys.stderr)
        raise SystemExit(2)
    app = build_app(config)
    logging.info("comfy-mcp-lite 启动：%s:%s/mcp → %s", config.mcp_host, config.mcp_port, config.comfyui_url)
    uvicorn.run(app, host=config.mcp_host, port=config.mcp_port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
