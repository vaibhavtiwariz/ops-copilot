"""Shared fixtures for the ops-copilot regression suite.

The repo's own tests/conftest.py is not loaded when pytest is pointed at this
directory (it lives outside that tree), so the environment the source modules
expect is set up here instead. All values are throwaway test values; nothing
here touches a live service.
"""

import os

os.environ.setdefault("OPS_LLM_PROVIDER", "groq")
os.environ.setdefault("OPS_GROQ_API_KEY", "test-key-not-real")
os.environ.setdefault("OPS_API_KEY", "test-api-key-not-real")
os.environ.setdefault("OPS_APPROVAL_SECRET", "test-approval-secret-not-real")
os.environ.setdefault("OPS_AUDIT_LOG_PATH", "/tmp/ops-copilot-regression-audit.jsonl")
os.environ.setdefault("OPS_GITHUB_DEFAULT_REPO", "psf/requests")
