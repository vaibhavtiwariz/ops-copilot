"""Connection between the graph and the MCP tool servers.

Each worker only ever sees the tools its role allows; that mapping is the
agent-level RBAC story. In stdio mode servers are spawned as subprocesses, in
http mode we point at the compose services.
"""

import ipaddress
import os
import re
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from ops_copilot.logging import get_logger
from ops_copilot.settings import get_settings

log = get_logger(__name__)

WorkerRole = str  # "analyst" | "devops" | "triage"

ROLE_SERVERS: dict[WorkerRole, tuple[str, ...]] = {
    "analyst": ("ops_db",),
    "devops": ("source_control", "ops_db"),
    "triage": ("source_control", "ops_db"),
}

# Hard exclusions on top of server membership. Even inside an allowed server,
# a worker never gets tools outside its mandate. apply_approved_write is
# deliberately hidden from every worker: execution goes through the graph's
# executor node after human review, never straight from a model decision.
ROLE_TOOL_DENYLIST: dict[WorkerRole, frozenset[str]] = {
    "analyst": frozenset({"draft_pr_comment", "apply_approved_write"}),
    "devops": frozenset({"apply_approved_write"}),
    "triage": frozenset({"apply_approved_write"}),
}

SERVER_NAMES = ("ops_db", "source_control")

# Proxy vars that take a full URL (validated with _valid_proxy_url).
_PROXY_URL_VARS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
)
# Proxy vars that take a comma-separated host list (validated per entry).
_NO_PROXY_VARS = ("no_proxy", "NO_PROXY")
# Basic process vars the subprocess needs besides OPS_*.
_BASIC_VARS = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER", "PYTHONPATH", "VIRTUAL_ENV")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_.\-]*[A-Za-z0-9_])?(?::\d{1,5})?$")


def _valid_proxy_url(value: str) -> bool:
    """True when a proxy URL parses cleanly enough for httpx to use."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    try:
        host = parsed.hostname
    except ValueError:
        return False
    if not host:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return True


def _valid_no_proxy_token(token: str) -> bool:
    """One comma-separated NO_PROXY entry: host, host:port, IPv4, CIDR.

    IPv6 literals (bracketed or bare) are rejected: httpx raises InvalidURL
    on entries like [::1], so they must never reach a subprocess.
    """
    token = token.strip()
    if not token or token == "*":
        return True
    if _HOSTNAME_RE.match(token):
        return True
    if token.startswith("*.") and _HOSTNAME_RE.match(token[2:]):
        return True
    if token.startswith(".") and _HOSTNAME_RE.match(token[1:]):
        return True
    if ":" in token:
        return False
    try:
        ipaddress.ip_network(token, strict=False)
        return True
    except ValueError:
        return False


def _stdio_env() -> dict[str, str]:
    """Environment for stdio MCP subprocesses.

    The SDK spawn inherits only a small safe subset on Linux, so without an
    explicit env the OPS_* settings never reached the servers and every DB
    call failed with "OPS_DATABASE_URL is not set". Build the env here:
    OPS_* (same trust boundary as the parent), basic process vars, and only
    proxy vars that parse cleanly. Malformed proxy values (for example a
    NO_PROXY containing "[::1]") make httpx raise InvalidURL, so they are
    dropped instead of forwarded.
    """
    env: dict[str, str] = {}
    for key in _BASIC_VARS:
        if key in os.environ:
            env[key] = os.environ[key]
    for key, value in os.environ.items():
        if key.startswith("OPS_"):
            env[key] = value
    for key in _PROXY_URL_VARS:
        value = os.environ.get(key)
        if value and _valid_proxy_url(value):
            env[key] = value
    for key in _NO_PROXY_VARS:
        value = os.environ.get(key)
        if not value:
            continue
        kept = [t.strip() for t in value.split(",") if t.strip() and _valid_no_proxy_token(t)]
        if kept:
            env[key] = ",".join(kept)
    return env


def _server_configs() -> dict[str, dict]:
    settings = get_settings()
    if settings.mcp_transport == "stdio":
        env = _stdio_env()
        return {
            "ops_db": {
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "ops_copilot.mcp.servers.ops_db"],
                "env": env,
            },
            "source_control": {
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "ops_copilot.mcp.servers.source_control"],
                "env": env,
            },
        }
    return {
        "ops_db": {"transport": "http", "url": settings.ops_db_mcp_url},
        "source_control": {
            "transport": "http",
            "url": settings.source_control_mcp_url,
        },
    }


@lru_cache(maxsize=1)
def _client() -> MultiServerMCPClient:
    configs: dict[str, Any] = _server_configs()
    return MultiServerMCPClient(configs)


async def tools_for_role(role: WorkerRole) -> list[BaseTool]:
    """Load and filter tools for one worker. Empty list on total failure so a
    degraded worker returns a clear message instead of crashing the run."""
    denylist = ROLE_TOOL_DENYLIST.get(role, frozenset())
    collected: list[BaseTool] = []
    for server in ROLE_SERVERS.get(role, ()):
        try:
            server_tools = await _client().get_tools(server_name=server)
        except Exception as exc:
            log.error("mcp_load_failed", server=server, role=role, error=str(exc))
            continue
        collected.extend(t for t in server_tools if t.name not in denylist)
    return collected


async def all_tools() -> dict[str, list[BaseTool]]:
    out: dict[str, list[BaseTool]] = {}
    for role in ROLE_SERVERS:
        out[role] = await tools_for_role(role)
    return out


async def tool_by_name(server: str, name: str) -> BaseTool | None:
    try:
        server_tools = await _client().get_tools(server_name=server)
    except Exception as exc:
        log.error("mcp_load_failed", server=server, error=str(exc))
        return None
    return next((t for t in server_tools if t.name == name), None)


def reset_client_cache() -> None:
    """Test hook: force reconnect after config changes."""
    _client.cache_clear()
