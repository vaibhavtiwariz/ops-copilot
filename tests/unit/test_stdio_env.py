"""Regression tests: stdio MCP servers must receive their environment.

The MCP SDK's stdio spawn inherits only a small safe subset on Linux, so
without an explicit env the OPS_* settings never reached the servers and
every DB call failed with "OPS_DATABASE_URL is not set". The config builder
now passes a curated env. These tests prove the env carries OPS_* vars and
that malformed proxy values are dropped instead of forwarded (a NO_PROXY
containing "[::1]" makes httpx raise InvalidURL).
"""

import pytest

from ops_copilot.mcp import client as mcp_client
from ops_copilot.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stdio_configs(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OPS_MCP_TRANSPORT", "stdio")
    get_settings.cache_clear()
    return mcp_client._server_configs()


def test_stdio_config_carries_ops_env(monkeypatch):
    configs = _stdio_configs(
        monkeypatch,
        OPS_DATABASE_URL="postgresql://ops:test@localhost:5432/ops_ops",
        OPS_APPROVAL_SECRET="secret-for-test",
    )
    for name in ("ops_db", "source_control"):
        assert configs[name]["transport"] == "stdio"
        env = configs[name]["env"]
        assert env["OPS_DATABASE_URL"] == "postgresql://ops:test@localhost:5432/ops_ops"
        assert env["OPS_APPROVAL_SECRET"] == "secret-for-test"
        assert "PATH" in env


def test_stdio_config_drops_malformed_proxy(monkeypatch):
    configs = _stdio_configs(
        monkeypatch,
        OPS_DATABASE_URL="postgresql://ops:test@localhost:5432/ops_ops",
        NO_PROXY="localhost,127.0.0.1,[::1]",
        http_proxy="not-a-url",
        https_proxy="http://proxy.example.com:8080",
    )
    env = configs["ops_db"]["env"]
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    assert "http_proxy" not in env
    assert env["https_proxy"] == "http://proxy.example.com:8080"


def test_http_config_unchanged(monkeypatch):
    monkeypatch.setenv("OPS_MCP_TRANSPORT", "http")
    monkeypatch.setenv("OPS_OPS_DB_MCP_URL", "http://localhost:8001/mcp")
    get_settings.cache_clear()
    configs = mcp_client._server_configs()
    assert configs["ops_db"] == {"transport": "http", "url": "http://localhost:8001/mcp"}
    assert "env" not in configs["ops_db"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://proxy:8080", True),
        ("https://proxy.example.com:3128", True),
        ("http://host", True),
        ("http://host:8080", True),
        ("http://[::1]:8080", True),
        ("http://proxy:abc", False),
        ("http://proxy:80x", False),
        ("http://proxy:99999", False),
        ("not-a-url", False),
        ("ftp://proxy:21", False),
        ("http://", False),
    ],
)
def test_valid_proxy_url(value, expected):
    assert mcp_client._valid_proxy_url(value) is expected


@pytest.mark.parametrize(
    "token,expected",
    [
        ("localhost", True),
        ("localhost:8080", True),
        ("127.0.0.1", True),
        ("10.0.0.0/8", True),
        (".example.com", True),
        ("*.example.com", True),
        ("[::1]", False),
        ("::1", False),
        ("bad host", False),
    ],
)
def test_valid_no_proxy_token(token, expected):
    assert mcp_client._valid_no_proxy_token(token) is expected
