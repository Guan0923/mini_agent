from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from backend.configuration import UserConfigStore
from backend.domain import SystemMessage, ToolSpec, UserMessage
from backend.providers import (
    ChatCompletionsAdapter,
    LLMClient,
    MessagesAdapter,
    ModelConfig,
    ResponsesAdapter,
)
from backend.runtime.core.context import AgentRuntime
from backend.storage.user_settings import UserSettingsStore


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
    return ModelConfig(
        "secret",
        "https://example.test/v1",
        "demo",
        provider="openai",
        protocol=protocol,
    )


def test_llm_client_selects_each_supported_protocol() -> None:
    assert isinstance(LLMClient(config("chat_completions")).llm, ChatCompletionsAdapter)
    assert isinstance(LLMClient(config("responses")).llm, ResponsesAdapter)
    assert isinstance(LLMClient(config("messages")).llm, MessagesAdapter)


def test_responses_adapter_converts_json_and_streamed_output() -> None:
    adapter = ResponsesAdapter(config("responses"))
    runtime = runtime_for(UserMessage(content="hello"))
    runtime.exchange.raw_response = {
        "id": "resp_1",
        "model": "demo",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hi"}],
            },
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Think"}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "name": "lookup",
                "arguments": '{"q":"x"}',
            },
        ],
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }

    prepared = adapter.prepare_response(runtime)

    assert prepared.message.content == "Hi"
    assert prepared.message.reasoning == "Think"
    assert prepared.message.tool_messages[0].call_id == "fc_1"
    assert prepared.message.tool_messages[0].arguments == {"q": "x"}
    assert prepared.usage == {"input_tokens": 2, "output_tokens": 3}

    streamed = runtime_for(UserMessage(content="hello"), stream=True)
    streamed.exchange.raw_response = [
        {"__sse_event": "response.output_text.delta", "delta": "A"},
        {"__sse_event": "response.reasoning_summary_text.delta", "delta": "B"},
        {
            "__sse_event": "response.completed",
            "response": {
                "id": "resp_2",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "A"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        },
    ]
    chunks: list[str] = []
    streamed.exchange.on_content = chunks.append
    streamed_prepared = adapter.prepare_response(streamed)

    assert streamed_prepared.message.content == "A"
    assert chunks == ["A"]


def test_messages_adapter_preserves_system_and_tool_blocks() -> None:
    adapter = MessagesAdapter(config("messages"))
    tool = ToolSpec("lookup", "Look up a value.", {"type": "object"})
    runtime = runtime_for(SystemMessage(content="rules"), UserMessage(content="hello"))
    runtime.exchange.allowed_tools = [tool]
    payload = adapter.prepare_request(runtime)

    assert payload["system"] == [{"type": "text", "text": "rules"}]
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert payload["tools"][0]["input_schema"] == {"type": "object"}

    runtime.exchange.raw_response = {
        "id": "msg_1",
        "model": "demo",
        "content": [
            {"type": "thinking", "thinking": "Think"},
            {"type": "text", "text": "Done"},
            {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {"q": "x"}},
        ],
        "usage": {"input_tokens": 4, "output_tokens": 5},
        "stop_reason": "tool_use",
    }
    prepared = adapter.prepare_response(runtime)

    assert prepared.message.content == "Done"
    assert prepared.message.reasoning == "Think"
    assert prepared.message.tool_messages[0].arguments == {"q": "x"}
    assert prepared.finish_reason == "tool_use"


def test_user_settings_are_isolated_and_api_keys_are_not_returned(tmp_path) -> None:
    first_id = str(uuid4())
    second_id = str(uuid4())
    first = UserSettingsStore(tmp_path / first_id / "user.db")
    second = UserSettingsStore(tmp_path / second_id / "user.db")

    assert first.profile_for_user(first_id) == {"display_name": "", "agent_preferences": ""}
    first.update_profile(first_id, display_name=" One ", agent_preferences=" concise ")
    first.update_agent_config(first_id, {"tone": "direct", "custom_instructions": "Use bullets"})
    saved = first.update_provider_config(
        first_id,
        {
            "provider": "openai",
            "protocol": "responses",
            "base_url": "https://example.test/v1",
            "model": "demo",
            "api_key": "secret-key",
        },
    )

    assert saved["api_key_configured"] is True
    assert "api_key" not in saved
    assert first.profile_for_user(first_id)["display_name"] == "One"
    assert first.agent_preferences_for_user(first_id) == "Preferred tone: direct\nUse bullets\nconcise"
    assert second.profile_for_user(second_id) == {"display_name": "", "agent_preferences": ""}
    assert second.provider_config_for_user(second_id)["api_key_configured"] is False
    with sqlite3.connect(first.path) as connection:
        raw = connection.execute(
            "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id = ?",
            (first_id,),
        ).fetchone()[0]
    assert raw and "secret-key" not in raw
    assert first.model_config_for_user(first_id).api_key == "secret-key"


def test_legacy_profile_is_migrated_to_user_db_once(tmp_path: Path) -> None:
    user_id = str(uuid4())
    root = tmp_path / user_id
    config = UserConfigStore(root / "config.toml")
    config.update({"profile": {"display_name": "旧名字", "agent_preferences": "旧偏好"}})

    store = UserSettingsStore(root / "user.db")
    assert store.profile_for_user(user_id) == {"display_name": "旧名字", "agent_preferences": "旧偏好"}

    config.update({"profile": {"display_name": "后来修改", "agent_preferences": "后来偏好"}})
    assert UserSettingsStore(root / "user.db").profile_for_user(user_id) == {
        "display_name": "旧名字",
        "agent_preferences": "旧偏好",
    }


def test_legacy_preferences_fill_an_empty_database_field_without_overwriting_name(tmp_path: Path) -> None:
    user_id = str(uuid4())
    root = tmp_path / user_id
    config = UserConfigStore(root / "config.toml")
    config.update({"profile": {"display_name": "旧名字", "agent_preferences": "旧偏好"}})

    UserSettingsStore(root / "user.db")
    with sqlite3.connect(root / "user.db") as connection:
        connection.execute("UPDATE user_profiles SET display_name = ?, agent_preferences = ?", ("自定义名", ""))
        connection.execute("DELETE FROM app_metadata WHERE key = 'profile_migrated_v1'")
        connection.commit()

    reopened = UserSettingsStore(root / "user.db")
    assert reopened.profile_for_user(user_id) == {"display_name": "自定义名", "agent_preferences": "旧偏好"}

def test_provider_names_are_case_insensitive_unique_and_renamable(tmp_path) -> None:
    user_id = str(uuid4())
    store = UserSettingsStore(tmp_path / user_id / "user.db")
    first = store.update_provider_config(
        user_id,
        {
            "provider_name": "Work-OpenAI",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model": "demo",
            "api_key": "secret-key",
        },
    )
    second = store.add_provider_config(
        user_id,
        {
            "provider_name": "Anthropic-Work",
            "provider": "anthropic",
            "base_url": "https://anthropic.test/v1",
            "model": "claude",
        },
    )

    with pytest.raises(ValueError, match="already exists"):
        store.update_provider_config_by_id(user_id, second["id"], {"provider_name": "work-openai"})

    renamed = store.update_provider_config_by_id(user_id, first["id"], {"provider_name": "work-openai-v2"})
    assert renamed["provider_name"] == "work-openai-v2"
    assert store.model_config_for_provider_name(user_id, "WORK-OPENAI-V2").model == "demo"
