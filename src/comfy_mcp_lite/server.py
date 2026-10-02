"""服务器装配与入口。

流程：校验配置（token 未设即拒绝启动）→ MCPServer（StreamableHTTP，
端点 /mcp）→ 套 Bearer 鉴权中间件 → uvicorn 监听。
"""

from __future__ import annotations

import logging
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer

from . import __version__, tools
from .auth import BearerAuthMiddleware
from .config import Config, ConfigError


def build_app(config: Config) -> BearerAuthMiddleware:
    """组装完整的 ASGI 应用（MCPServer StreamableHTTP + Bearer 鉴权）。"""
    mcp = MCPServer("comfy-mcp-lite", version=__version__)
    tools.register(mcp, config)
    # host 传入实际绑定地址：绑 0.0.0.0（跨机共享）时关闭 SDK 的
    # DNS 重绑定自动防护（该防护默认按 127.0.0.1 触发，会拒绝远端 Host 头）
    # json_response=True：POST 响应整体 application/json 返回而非 SSE 流。
    # SSE 模式下 httpx2>=2.13 的解析器有「单事件 <= 1MiB」硬限制，内联
    # base64 大图（原始 >约 768KB，如 4K 图）会报 "SSE stream ended
    # without a response"；JSON 模式绕过 SSE 解析器，实测 20MB 稳定通过。
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", host=config.mcp_host, json_response=True
    )
    return BearerAuthMiddleware(app, config.auth_token)


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
