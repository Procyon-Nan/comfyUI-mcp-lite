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


def test_public_base_url_and_ttl_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    for var in ("PUBLIC_BASE_URL", "IMAGE_URL_TTL_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    config = Config.from_env()
    assert config.public_base_url == ""  # 未设 → 不返回 urls（v0.1 行为）
    assert config.image_url_ttl_seconds == 3600


def test_public_base_url_and_ttl_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com/")
    monkeypatch.setenv("IMAGE_URL_TTL_SECONDS", "7200")
    config = Config.from_env()
    assert config.public_base_url == "https://example.com/"
    assert config.image_url_ttl_seconds == 7200


@pytest.mark.parametrize("bad", ["ftp://example.com", "example.com:9101", "/images"])
def test_public_base_url_bad_scheme_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", bad)
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        Config.from_env()


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_image_url_ttl_bad_value_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    monkeypatch.setenv("IMAGE_URL_TTL_SECONDS", bad)
    with pytest.raises(ConfigError, match="IMAGE_URL_TTL_SECONDS"):
        Config.from_env()
