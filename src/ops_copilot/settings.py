from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["groq", "gemini", "ollama", "openai_compatible"]

# Sensible per-provider defaults so OPS_LLM_MODEL can stay empty.
DEFAULT_MODELS: dict[str, str] = {
    "groq": "llama-3.3-70b-versatile",
    # flash-lite over flash: same tool-calling quality for routing/SQL, and a
    # much friendlier free-tier requests-per-minute limit.
    "gemini": "gemini-3.5-flash-lite",
    "ollama": "llama3.1:8b",
    "openai_compatible": "",
}

PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "groq": ("OPS_GROQ_API_KEY",),
    "gemini": ("OPS_GEMINI_API_KEY",),
    "ollama": (),
    "openai_compatible": ("OPS_OPENAI_API_KEY",),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OPS_", extra="ignore")

    llm_provider: Provider = "groq"
    llm_model: str = ""
    llm_temperature: float = 0.2
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    openai_api_base: str | None = None
    openai_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"

    database_url: str = "postgresql://ops@localhost:5432/ops_ops"
    checkpoint_db_url: str = "postgresql://ops@localhost:5432/ops_checkpoints"
    mcp_transport: Literal["http", "stdio"] = "http"
    ops_db_mcp_url: str = "http://localhost:8001/mcp"
    source_control_mcp_url: str = "http://localhost:8002/mcp"
    github_token: str | None = None
    github_default_repo: str = "psf/requests"

    sql_max_rows: int = Field(default=200, ge=1)
    sql_statement_timeout_ms: int = Field(default=8000, ge=100)
    run_token_budget: int = Field(default=80_000, ge=1000)
    graph_recursion_limit: int = Field(default=48, ge=4)

    # S16: loopback by default for local runs. docker compose passes
    # --host 0.0.0.0 explicitly for the api service, so containers are
    # unaffected; set OPS_API_HOST=0.0.0.0 only for LAN access.
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000"
    api_key: str | None = None
    audit_log_path: str = "./data/audit.jsonl"
    approval_secret: str | None = None

    phoenix_endpoint: str | None = None

    @model_validator(mode="after")
    def _check_provider_shape(self) -> "Settings":
        # Structural requirements only - secrets are checked lazily by the
        # model factory so the service can boot (health, MCP, audit) without
        # any provider configured.
        if self.llm_provider == "openai_compatible" and not self.openai_api_base:
            raise ValueError("llm_provider='openai_compatible' requires OPS_OPENAI_API_BASE")
        return self

    @property
    def resolved_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


def _env_to_attr(env_name: str) -> str:
    attr = env_name.lower()
    return attr.removeprefix("ops_")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
