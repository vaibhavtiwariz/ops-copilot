"""Model construction for the copilot's LLM providers.

One provider serves each call site, chosen from OPS_LLM_PROVIDER. When a
free-tier provider rate-limits, ainvoke_patient waits out the 429 window
instead of failing the turn.
"""

import asyncio
import re
from contextvars import ContextVar
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable
from langchain_litellm import ChatLiteLLM

from ops_copilot.logging import get_logger
from ops_copilot.settings import PROVIDER_ENV_KEYS, Provider, Settings

log = get_logger(__name__)

_LITELLM_PREFIX = {
    "groq": "groq",
    "gemini": "gemini",
    # ollama/ hits /generate; the chat endpoint lives under ollama_chat/
    "ollama": "ollama_chat",
    "openai_compatible": "openai",
}

_MAX_RATE_LIMIT_RETRIES = 3

# The runner sets this to (bus, thread_id) while a graph run is in flight,
# so rate-limit waits can surface as console events instead of silent
# "working..." stalls. Unset (None) outside a run, e.g. in evals and tests.
rate_limit_sink: ContextVar[tuple[Any, str] | None] = ContextVar("rate_limit_sink", default=None)


def ensure_user_message(messages: list) -> list:
    """Guarantee at least one user-role message in an outbound prompt.

    Some chat templates (Groq/qwen) reject a prompt with no user message
    ("No user query found in messages"). The synthesis prompt used to be
    system-only; worker prompts can also lose their user turn when the
    context tail slices past the last HumanMessage. Append-only and
    only-if-absent: it never splits an AIMessage(tool_calls)/ToolMessage
    pair, so tool transcripts are untouched.
    """
    for message in messages:
        if isinstance(message, HumanMessage):
            return messages
    return [*messages, HumanMessage(content="Continue.")]


def _attr_for(env_name: str) -> str:
    return env_name.lower().removeprefix("ops_")


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text or "quota" in text.lower() or "RateLimit" in type(exc).__name__


def _rate_limit_wait(exc: Exception, attempt: int) -> float:
    match = re.search(r"retry in\s+([\d.]+)s", str(exc), re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 1.0, 90.0)
    return min(20.0 * (2**attempt), 90.0)


async def ainvoke_patient(
    model: Runnable,
    messages: list,
    *,
    context: str = "",
):
    """Invoke a model, waiting out free-tier 429 windows instead of failing.

    Gemini's free tier allows ~5-15 requests/minute; one agent turn makes
    several. The API reports exactly how long to wait - we honor it up to
    three times before giving up and surfacing the error.
    """
    messages = ensure_user_message(messages)
    for attempt in range(_MAX_RATE_LIMIT_RETRIES):
        try:
            return await model.ainvoke(messages)
        except Exception as exc:
            if not _is_rate_limit(exc):
                raise
            wait = _rate_limit_wait(exc, attempt)
            log.warning(
                "provider_rate_limited",
                context=context,
                attempt=attempt + 1,
                waiting_seconds=round(wait),
            )
            # Surface the wait on the console: a silent "working..." reads
            # as a stall. Best-effort; a failed publish must not break the
            # retry.
            sink = rate_limit_sink.get()
            if sink is not None:
                try:
                    bus, thread_id = sink
                    await bus.publish(
                        thread_id,
                        {
                            "type": "rate_limited",
                            "wait_seconds": round(wait),
                            "context": context,
                        },
                    )
                except Exception:
                    pass
            await asyncio.sleep(wait)
    return await model.ainvoke(messages)


def _litellm_kwargs(provider: Provider, settings: Settings) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if provider == "groq":
        base["api_key"] = settings.groq_api_key
    elif provider == "gemini":
        base["api_key"] = settings.gemini_api_key
    elif provider == "ollama":
        base["api_base"] = settings.ollama_base_url
    elif provider == "openai_compatible":
        base["api_base"] = settings.openai_api_base
        base["api_key"] = settings.openai_api_key or "sk-no-key-required"
    return base


def model_string(provider: Provider, settings: Settings) -> str:
    prefix = _LITELLM_PREFIX[provider]
    if provider == "openai_compatible":
        return f"openai/{settings.llm_model}"
    return f"{prefix}/{settings.resolved_model}"


def build_chat_model(
    settings: Settings,
    *,
    temperature: float | None = None,
    provider: Provider | None = None,
) -> BaseChatModel:
    chosen = provider or settings.llm_provider
    missing = [
        var
        for var in PROVIDER_ENV_KEYS.get(chosen, ())
        if not getattr(settings, _attr_for(var), None)
    ]
    if missing:
        raise ValueError(
            f"provider {chosen!r} needs {', '.join(missing)}; set it or switch OPS_LLM_PROVIDER"
        )
    return ChatLiteLLM(
        model=model_string(chosen, settings),
        temperature=settings.llm_temperature if temperature is None else temperature,
        max_retries=2,
        request_timeout=60,
        **_litellm_kwargs(chosen, settings),
    )
