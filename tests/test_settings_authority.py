from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.domain import SystemMessage, ToolSpec, UserMessage
from backend.providers import ChatCompletionsAdapter, LLMClient, MessagesAdapter, ModelConfig, ResponsesAdapter
from backend.runtime.core.context import AgentRuntime
from backend.storage.settings import LocalSettingsStore


def runtime_for(*messages, stream: bool = False) -> AgentRuntime:
    runtime = AgentRuntime.ephemeral(
        session_id="session-test",
        planner=object(),
        tools=object(),
        messages=list(messages),
    )
    runtime.state.model = "demo"
    runtime.exchange.messages = list(messages)
    runtime.exchange.stream = stream
    return runtime


def config(protocol: str) -> ModelConfig:
    return ModelConfig("secret", "https://example.test/v1", "demo", provider="openai", protocol=protocol)


def test_llm_client_selects_each_supported_protocol() -> None:
    assert isinstance(LLMClient(config("chat_completions")).llm, ChatCompletionsAdapter)
    assert isinstance(LLMClient(config("responses")).llm, ResponsesAdapter)
    assert isinstance(LLMClient(config("messages")).llm, MessagesAdapter)


def test_responses_adapter_converts_json_output() -> None:
    adapter = ResponsesAdapter(config("responses"))
    runtime = runtime_for(UserMessage(content="hello"))
    runtime.exchange.raw_response = {
        "id": "resp_1",
        "model": "demo",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Hi"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Think"}]},
            {"type": "function_call", "id": "fc_1", "name": "lookup", "arguments": '{"q":"x"}'},
        ],
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }

    prepared = adapter.prepare_response(runtime)

    assert prepared.message.content == "Hi"
    assert prepared.message.reasoning == "Think"
    assert prepared.message.tool_messages[0].arguments == {"q": "x"}
    assert prepared.usage == {"input_tokens": 2, "output_tokens": 3}


def test_messages_adapter_preserves_system_and_tool_blocks() -> None:
    adapter = MessagesAdapter(config("messages"))
    runtime = runtime_for(SystemMessage(content="rules"), UserMessage(content="hello"))
    runtime.exchange.allowed_tools = [ToolSpec("lookup", "Look up a value.", {"type": "object"})]

    payload = adapter.prepare_request(runtime)

    assert payload["system"] == [{"type": "text", "text": "rules"}]
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert payload["tools"][0]["input_schema"] == {"type": "object"}


def test_local_settings_encrypts_provider_key_and_reopens_without_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_AGENT_LOCAL_DEK_FALLBACK", "test-local-key-material-that-is-at-least-32-bytes")
    state_db = tmp_path / ".mini_agent" / "runtime" / "state.db"
    config_file = tmp_path / ".mini_agent" / "config.toml"
    store = LocalSettingsStore(state_db, config_file)

    saved = store.update_provider_config(
        {
            "provider_name": "work-openai",
            "protocol": "responses",
            "base_url": "https://example.test/v1",
            "model": "demo",
            "api_key": "secret-key",
        }
    )

    assert saved["api_key_configured"] is True
    assert "api_key" not in saved
    with sqlite3.connect(state_db) as connection:
        raw = connection.execute("SELECT provider_configs_json FROM provider_settings WHERE id = 1").fetchone()[0]
    assert raw.startswith("[")
    assert "secret-key" not in raw
    assert "v4:" in raw
    reopened = LocalSettingsStore(state_db, config_file)
    assert reopened.model_config().api_key == "secret-key"


def test_local_profile_and_agent_preferences_are_stored_in_toml(tmp_path: Path) -> None:
    store = LocalSettingsStore(tmp_path / "runtime" / "state.db", tmp_path / "config.toml")

    store.update_profile(display_name=" One ", agent_preferences=" concise ")
    store.update_agent_config({"tone": "direct", "custom_instructions": "Use bullets"})

    assert store.profile() == {"display_name": "One", "agent_preferences": "concise"}
    assert store.agent_preferences() == "Preferred tone: direct\nUse bullets\nconcise"


def test_sandbox_cannot_be_disabled(tmp_path: Path) -> None:
    store = LocalSettingsStore(tmp_path / "runtime" / "state.db", tmp_path / "config.toml")
    current = store.sandbox_config()
    store.config_store.update({"sandbox": {**current, "enabled": False}})

    assert store.sandbox_config()["enabled"] is True
    with pytest.raises(ValueError, match="cannot be disabled"):
        store.update_sandbox_config({"enabled": False})


def test_provider_names_are_case_insensitive_unique_and_renamable(tmp_path: Path) -> None:
    store = LocalSettingsStore(tmp_path / "runtime" / "state.db", tmp_path / "config.toml")
    first = store.update_provider_config(
        {"provider_name": "Work-OpenAI", "base_url": "https://example.test/v1", "model": "demo"}
    )
    second = store.add_provider_config(
        {"provider_name": "Anthropic-Work", "base_url": "https://anthropic.test/v1", "model": "claude"}
    )

    with pytest.raises(ValueError, match="already exists"):
        store.update_provider_config_by_id(second["id"], {"provider_name": "work-openai"})
    renamed = store.update_provider_config_by_id(first["id"], {"provider_name": "work-openai-v2"})
    assert renamed["provider_name"] == "work-openai-v2"
    assert store.model_config("WORK-OPENAI-V2").model == "demo"
