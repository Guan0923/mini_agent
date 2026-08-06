from __future__ import annotations

import sqlite3

from backend.api.auth_store import AuthStore
from backend.domain import SystemMessage, ToolSpec, UserMessage
from backend.providers import (
    ChatCompletionsAdapter,
    LLMClient,
    MessagesAdapter,
    ModelConfig,
    ResponsesAdapter,
)
from backend.runtime.core.context import AgentRuntime


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
    store = AuthStore(tmp_path / "auth.sqlite3")
    store.insert_challenge("one@example.com", "register", "123456", None)
    store.insert_challenge("two@example.com", "register", "123456", None)
    first = store.register_user("one@example.com", "123456", "password")
    second = store.register_user("two@example.com", "123456", "password")

    assert store.profile_for_user(first.id) == {"display_name": "", "agent_preferences": ""}
    store.update_profile(first.id, display_name=" One ", agent_preferences=" concise ")
    store.update_agent_config(first.id, {"tone": "direct", "custom_instructions": "Use bullets"})
    saved = store.update_provider_config(
        first.id,
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
    assert store.profile_for_user(first.id)["display_name"] == "One"
    assert store.agent_preferences_for_user(first.id) == "Preferred tone: direct\nUse bullets\nconcise"
    assert store.profile_for_user(second.id) == {"display_name": "", "agent_preferences": ""}
    assert store.provider_config_for_user(second.id)["api_key_configured"] is False
    with sqlite3.connect(store.path) as connection:
        raw = connection.execute(
            "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id = ?",
            (first.id,),
        ).fetchone()[0]
    assert raw and "secret-key" not in raw
    assert store.model_config_for_user(first.id).api_key == "secret-key"
