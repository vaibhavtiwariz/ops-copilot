"""Canonical repository-name normalization.

Both the API graph (which mints approval tokens that bind a repo target)
and the source-control MCP server (which verifies those tokens) must agree
on the exact "owner/name" string, including how the default repo is resolved.
This single function is the one source of truth.
"""

import os

DEFAULT_REPO = "psf/requests"


def normalize_repo(repo: str | None) -> str:
    """Resolve a repo name to canonical "owner/name" form.

    Falls back to OPS_GITHUB_DEFAULT_REPO (default psf/requests) when no
    repo is given, and rejects anything that is not owner/name shaped.
    """
    full = repo or os.environ.get("OPS_GITHUB_DEFAULT_REPO", DEFAULT_REPO)
    parts = full.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"repo must look like 'owner/name', got {full!r}")
    return full.strip("/")
