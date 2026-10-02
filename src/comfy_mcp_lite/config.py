"""环境变量配置。

规格：COMFYUI_URL / MCP_HOST / MCP_PORT / MCP_AUTH_TOKEN；
v0.2 新增 PUBLIC_BASE_URL / IMAGE_URL_TTL_SECONDS（图片签名 URL，见 signing.py）。
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
    # 公网前缀：未设（空）则工具结果不带 urls，行为与 v0.1 一致
    public_base_url: str = ""
    # 签名图片 URL 的有效秒数
    image_url_ttl_seconds: int = 3600

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
        # PUBLIC_BASE_URL 须以 http(s):// 开头，避免拼出不可抓取的相对地址
        public_base_url = os.environ.get("PUBLIC_BASE_URL", "").strip()
        if public_base_url and not public_base_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"PUBLIC_BASE_URL 必须以 http:// 或 https:// 开头：{public_base_url!r}"
            )
        ttl_raw = os.environ.get("IMAGE_URL_TTL_SECONDS", "3600").strip() or "3600"
        try:
            image_url_ttl_seconds = int(ttl_raw)
        except ValueError:
            raise ConfigError(f"IMAGE_URL_TTL_SECONDS 不是合法整数：{ttl_raw!r}") from None
        if image_url_ttl_seconds <= 0:
            raise ConfigError(f"IMAGE_URL_TTL_SECONDS 必须为正数：{image_url_ttl_seconds}")
        return cls(
            comfyui_url=os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").strip()
            or "http://127.0.0.1:8188",
            mcp_host=os.environ.get("MCP_HOST", "0.0.0.0").strip() or "0.0.0.0",
            mcp_port=port,
            auth_token=token,
            public_base_url=public_base_url,
            image_url_ttl_seconds=image_url_ttl_seconds,
        )
