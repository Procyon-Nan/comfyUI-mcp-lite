"""环境变量配置。

规格：COMFYUI_URL / MCP_HOST / MCP_PORT / MCP_AUTH_TOKEN。
本服务是共享端点，MCP_AUTH_TOKEN 未设置或为空时拒绝启动。
"""

from __future__ import annotations

import os

from pydantic import BaseModel


class ConfigError(RuntimeError):
    """配置非法，服务器应拒绝启动。"""


class Config(BaseModel):
    """进程级配置，启动时从环境读取一次。"""

    comfyui_url: str
    mcp_host: str
    mcp_port: int
    auth_token: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "未设置 MCP_AUTH_TOKEN：本服务为共享端点，禁止裸跑。"
                "请在环境中提供客户端 Bearer 鉴权令牌。"
            )
        port_raw = os.environ.get("MCP_PORT", "9101").strip() or "9101"
        try:
            port = int(port_raw)
        except ValueError:
            raise ConfigError(f"MCP_PORT 不是合法端口：{port_raw!r}") from None
        if not 1 <= port <= 65535:
            raise ConfigError(f"MCP_PORT 超出端口范围：{port}")
        return cls(
            comfyui_url=os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").strip()
            or "http://127.0.0.1:8188",
            mcp_host=os.environ.get("MCP_HOST", "0.0.0.0").strip() or "0.0.0.0",
            mcp_port=port,
            auth_token=token,
        )
