import pytest
from pydantic import ValidationError

from ops_copilot.llm import build_chat_model, model_string
from ops_copilot.security import mint, verify
from ops_copilot.settings import Settings


def make_settings(**overrides) -> Settings:
    base = dict(
        llm_provider="groq",
        groq_api_key="k-groq",
        gemini_api_key=None,
        openai_api_base=None,
        openai_api_key=None,
    )
    base.update(overrides)
    return Settings(**base)


class TestSettings:
    def test_missing_keys_do_not_block_boot(self):
        # Settings construct without credentials; the model factory is where
        # provider availability gets enforced.
        assert make_settings(groq_api_key=None).groq_api_key is None

    def test_default_models_per_provider(self):
        assert model_string("groq", make_settings()) == "groq/llama-3.3-70b-versatile"
        assert (
            model_string(
                "gemini",
                make_settings(llm_provider="gemini", groq_api_key=None, gemini_api_key="g"),
            )
            == "gemini/gemini-3.5-flash-lite"
        )

    def test_openai_compatible_needs_base_url(self):
        with pytest.raises(ValidationError):
            make_settings(llm_provider="openai_compatible", openai_api_key="k")

    def test_ollama_needs_nothing(self):
        s = make_settings(llm_provider="ollama", groq_api_key=None)
        assert model_string("ollama", s) == "ollama_chat/llama3.1:8b"


class TestModelFactory:
    def test_factory_rejects_provider_without_credentials(self):
        s = make_settings(llm_provider="groq", groq_api_key=None)
        with pytest.raises(ValueError, match="OPS_GROQ_API_KEY"):
            build_chat_model(s)

    def test_builds_chat_litellm(self):
        model = build_chat_model(make_settings(), temperature=0.1)
        # litellm prefixes provider on the bound model name
        assert "groq" in str(model.model_name or model.model)

    def test_explicit_provider_override(self):
        s = make_settings(llm_provider="groq", gemini_api_key="g")
        model = build_chat_model(s, provider="gemini")
        assert model is not None


class TestApprovalTokens:
    def test_roundtrip(self):
        token = mint(
            secret="s3cret", action="github_comment", target="acme/app#7", content="looks good"
        )
        ok, reason = verify(
            secret="s3cret",
            token=token,
            action="github_comment",
            target="acme/app#7",
            content="looks good",
        )
        assert ok, reason

    def test_tampered_content_rejected(self):
        token = mint(secret="s", action="a", target="t", content="approved text")
        ok, reason = verify(secret="s", token=token, action="a", target="t", content="evil text")
        assert not ok and "differs" in reason

    def test_wrong_target_rejected(self):
        token = mint(secret="s", action="a", target="repo#1", content="x")
        ok, _ = verify(secret="s", token=token, action="a", target="repo#2", content="x")
        assert not ok

    def test_garbage_token(self):
        ok, reason = verify(secret="s", token="not-a-token", action="a", target="t", content="c")
        assert not ok and "malformed" in reason
