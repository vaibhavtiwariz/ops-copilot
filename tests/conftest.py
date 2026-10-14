import os

# Must happen before ops_copilot.settings is first imported anywhere in the
# suite: provider validation refuses to construct Settings without a key.
#
# Integration tests get their OWN database so a pytest run can never wipe
# demo data or in-flight proposals of a running local stack.
os.environ.setdefault("OPS_LLM_PROVIDER", "groq")
os.environ.setdefault("OPS_GROQ_API_KEY", "test-key-not-real")
os.environ.setdefault(
    "OPS_DATABASE_URL", "postgresql://ops:test-password-not-real@localhost:5432/ops_test"
)
os.environ.setdefault(
    "OPS_CHECKPOINT_DB_URL",
    "postgresql://ops:test-password-not-real@localhost:5432/ops_test_checkpoints",
)
os.environ.setdefault("OPS_AUDIT_LOG_PATH", "/tmp/ops-copilot-test-audit.jsonl")
# The API refuses to boot without these (fail-closed auth, no weak DB
# credential); tests that build the app through lifespan get throwaway values.
os.environ.setdefault("OPS_API_KEY", "test-key-not-real")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password-not-real")
# The gate needs this before get_settings() is first cached (api.main builds
# the app at import time); tests/conftest.py loads before any test module.
os.environ.setdefault("OPS_APPROVAL_SECRET", "test-approval-secret-not-real")
