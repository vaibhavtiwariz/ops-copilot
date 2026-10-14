"""Connection between the graph and the MCP tool servers.

Each worker only ever sees the tools its role allows; that mapping is the
agent-level RBAC story. In stdio mode servers are spawned as subprocesses, in
http mode we point at the compose services.
"""

from functools import lru_cache
from typing import Any

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


def _server_configs() -> dict[str, dict]:
    settings = get_settings()
    if settings.mcp_transport == "stdio":
        return {
            "ops_db": {
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "ops_copilot.mcp.servers.ops_db"],
            },
            "source_control": {
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "ops_copilot.mcp.servers.source_control"],
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
