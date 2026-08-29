from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.api.routes.settings import SandboxConfigPayload
from backend.domain import SystemMessage, ToolSpec, UserMessage
from backend.providers import (
    ChatCompletionsAdapter,
    LLMClient,
    MessagesAdapter,
    ModelConfig,
    ModelConfigurationError,
    ResponsesAdapter,
)
from backend.runtime.core.context import AgentRuntime
from backend.storage.settings import LocalSettingsStore, normalize_sandbox_config
from backend.storage.settings.contract import normalize_provider_config


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


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "messages"])
def test_provider_requests_preserve_tool_parameter_descriptions(protocol: str) -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The value to look up.",
            }
        },
        "required": ["query"],
    }
    runtime = runtime_for(UserMessage(content="look it up"))
    runtime.exchange.allowed_tools = [ToolSpec("lookup", "Looks up a value.", schema)]

    payload = LLMClient(config(protocol)).llm.prepare_request(runtime)

    if protocol == "chat_completions":
        exposed_schema = payload["tools"][0]["function"]["parameters"]
    elif protocol == "responses":
        exposed_schema = payload["tools"][0]["parameters"]
    else:
        exposed_schema = payload["tools"][0]["input_schema"]
    assert exposed_schema == schema


@pytest.mark.parametrize(
    ("protocol", "token_field"),
    [
        ("chat_completions", "max_tokens"),
        ("responses", "max_output_tokens"),
        ("messages", "max_tokens"),
    ],
)
def test_provider_requests_use_configured_model_parameters(protocol: str, token_field: str) -> None:
    model_config = ModelConfig(
        "secret",
        "https://example.test/v1",
        "configured-model",
        max_tokens=1536,
        context_size=65536,
        temperature=0.7,
        protocol=protocol,
    )
    client = LLMClient(model_config)
    runtime = runtime_for(UserMessage(content="hello"))

    client.prepare_runtime(runtime)
    payload = client.llm.prepare_request(runtime)

    assert runtime.state.model_snapshot["context_length"] == 65536
    assert payload[token_field] == 1536
    assert payload["temperature"] == 0.7


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


def test_provider_model_parameters_persist_and_legacy_temperature_defaults(tmp_path: Path) -> None:
    state_db = tmp_path / ".mini_agent" / "runtime" / "state.db"
    config_file = tmp_path / ".mini_agent" / "config.toml"
    store = LocalSettingsStore(state_db, config_file)

    saved = store.update_provider_config(
        {
            "provider_name": "local",
            "base_url": "https://example.test/v1",
            "model": "demo",
            "max_tokens": 2048,
            "context_size": 65536,
            "temperature": 0.7,
        }
    )

    assert saved["max_tokens"] == 2048
    assert saved["context_size"] == 65536
    assert saved["temperature"] == 0.7
    reopened = LocalSettingsStore(state_db, config_file)
    assert reopened.provider_config()["temperature"] == 0.7
    assert reopened.model_config().temperature == 0.7

    with sqlite3.connect(state_db) as connection:
        raw = connection.execute("SELECT provider_configs_json FROM provider_settings WHERE id = 1").fetchone()[0]
        records = json.loads(raw)
        records[0].pop("temperature")
        connection.execute(
            "UPDATE provider_settings SET provider_configs_json = ? WHERE id = 1",
            (json.dumps(records),),
        )

    legacy = LocalSettingsStore(state_db, config_file)
    assert legacy.provider_config()["temperature"] == 0.0
    assert legacy.provider_configs()[0]["temperature"] == 0.0
    assert legacy.model_config().temperature == 0.0


def test_model_config_loaders_preserve_temperature_and_default_to_zero(tmp_path: Path) -> None:
    mapped = ModelConfig.from_mapping(
        {
            "api_key": "secret",
            "base_url": "https://example.test/v1",
            "model": "mapped",
            "max_tokens": 2048,
            "context_size": 65536,
            "temperature": 0.6,
        }
    )
    default_env = ModelConfig.from_env(
        tmp_path / ".env",
        environ={"API_KEY": "secret", "BASE_URL": "https://example.test/v1", "MODEL": "default"},
    )
    configured_env = ModelConfig.from_env(
        tmp_path / ".env",
        environ={
            "API_KEY": "secret",
            "BASE_URL": "https://example.test/v1",
            "MODEL": "environment",
            "MAX_TOKENS": "3072",
            "CONTEXT_SIZE": "131072",
            "TEMPERATURE": "0.8",
        },
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[model]
api_key = "secret"
base_url = "https://example.test/v1"
model = "toml"
max_tokens = 4096
context_size = 262144
temperature = 1.1
""",
        encoding="utf-8",
    )
    configured_toml = ModelConfig.from_toml(config_path)

    assert mapped.temperature == 0.6
    assert default_env.temperature == 0.0
    assert configured_env.temperature == 0.8
    assert configured_env.max_tokens == 3072
    assert configured_env.context_size == 131072
    assert configured_toml.temperature == 1.1


@pytest.mark.parametrize("temperature", [-0.1, 2.1, True, float("nan"), float("inf")])
def test_temperature_validation_rejects_invalid_values(temperature: object) -> None:
    values = {
        "provider_name": "local",
        "base_url": "https://example.test/v1",
        "model": "demo",
        "temperature": temperature,
    }

    with pytest.raises(ValueError, match="temperature"):
        normalize_provider_config({}, values)
    with pytest.raises(ModelConfigurationError):
        ModelConfig.from_mapping({**values, "api_key": "secret"})


def test_local_profile_and_agent_preferences_are_stored_in_toml(tmp_path: Path) -> None:
    store = LocalSettingsStore(tmp_path / "runtime" / "state.db", tmp_path / "config.toml")

    store.update_profile(display_name=" One ", agent_preferences=" concise ")
    store.update_agent_config({"tone": "direct", "custom_instructions": "Use bullets"})

    assert store.profile() == {"display_name": "One", "agent_preferences": "concise"}
    assert store.agent_preferences() == "Preferred tone: direct\nUse bullets\nconcise"


def test_sandbox_enabled_parameter_is_removed_from_every_settings_projection(tmp_path: Path) -> None:
    store = LocalSettingsStore(tmp_path / "runtime" / "state.db", tmp_path / "config.toml")
    current = store.sandbox_config()
    store.config_store.update({"sandbox": {**current, "enabled": False}})

    normalized = store.sandbox_config()
    updated = store.update_sandbox_config({"enabled": False})

    assert "enabled" not in normalized
    assert "enabled" not in updated
    assert "enabled" not in store.config_store.read()["sandbox"]
    assert "enabled" not in SandboxConfigPayload.model_fields
    assert "enabled" not in SandboxConfigPayload.model_json_schema()["properties"]
    assert "file_mode" not in normalized
    assert "file_mode" not in SandboxConfigPayload.model_fields
    assert normalized["policy_version"] == 3
    assert normalized["proxy_port"] == 17831


def test_v2_sandbox_config_migrates_only_command_network_and_limits(tmp_path: Path) -> None:
    del tmp_path
    normalized = normalize_sandbox_config(
        {
            "policy_version": 2,
            "file_mode": "full_access",
            "full_access_acknowledged": True,
            "network_mode": "restricted_network",
            "network_allowlist": [{"host": "127.0.0.1"}, {"host": "EXAMPLE.test.", "port": 443}],
            "limits": {"wall_seconds": 60},
        }
    )

    assert normalized["policy_version"] == 3
    assert normalized["network_allowlist"] == [
        {"host": "127.0.0.1"},
        {"host": "example.test"},
    ]
    assert normalized["limits"]["wall_seconds"] == 60
    assert "file_mode" not in normalized
    assert "full_access_acknowledged" not in normalized


def test_sandbox_network_rule_api_accepts_only_host_and_at_most_64_rules() -> None:
    rule_schema = SandboxConfigPayload.model_json_schema()["$defs"]["SandboxNetworkRulePayload"]
    assert set(rule_schema["properties"]) == {"host"}
    assert SandboxConfigPayload.model_fields["network_allowlist"].metadata[0].max_length == 64

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SandboxConfigPayload(
            network_mode="restricted_network",
            network_allowlist=[{"host": "example.test", "port": 443}],
        )

    with pytest.raises(ValidationError, match="at most 64 items"):
        SandboxConfigPayload(network_allowlist=[{"host": f"host-{index}.example"} for index in range(65)])


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
