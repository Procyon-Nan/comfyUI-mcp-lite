"""config：环境变量解析与「未设 token 拒绝启动」。"""

from __future__ import annotations

import pytest

from comfy_mcp_lite.config import Config, ConfigError


def test_missing_token_refuses_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN"):
        Config.from_env()


def test_empty_token_refuses_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "   ")
    with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN"):
        Config.from_env()


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    for var in ("COMFYUI_URL", "MCP_HOST", "MCP_PORT"):
        monkeypatch.delenv(var, raising=False)
    config = Config.from_env()
    assert config.auth_token == "secret"
    assert config.comfyui_url == "http://127.0.0.1:8188"
    assert config.mcp_host == "0.0.0.0"
    assert config.mcp_port == 9101


def test_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:9999/")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "9102")
    config = Config.from_env()
    assert config.comfyui_url == "http://127.0.0.1:9999/"
    assert config.mcp_host == "127.0.0.1"
    assert config.mcp_port == 9102


@pytest.mark.parametrize("bad", ["not-a-port", "0", "70000"])
def test_bad_port(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("MCP_PORT", bad)
    with pytest.raises(ConfigError):
        Config.from_env()
