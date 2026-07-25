import os
from pathlib import Path
from typing import Any

import pytest

from backend.domain import AssistantMessage, ToolMessage
from backend.providers.config import ModelConfig, load_env_file
from backend.runtime import AgentRunner
from backend.tools import Tool, ToolError, ToolRegistry, WorkspaceCommand


def test_tool_registry_validates_schema_without_applying_defaults() -> None:
    calls: list[tuple[str, int | None]] = []

    def handler(label: str, count: int | None = None) -> str:
        calls.append((label, count))
        return label

    registry = ToolRegistry(
        [
            Tool(
                "label",
                "Returns a label.",
                handler,
                {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "count": {"type": "integer", "default": 3},
                    },
                    "required": ["label"],
                    "additionalProperties": False,
                },
            )
        ]
    )

    assert registry.invoke("label", {"label": "ready"}) == "ready"
    assert calls == [("ready", None)]

    for arguments in ({}, {"label": 1}, {"label": "ready", "count": True}, {"label": "ready", "extra": 1}):
        with pytest.raises(ToolError, match="Invalid arguments"):
            registry.invoke("label", arguments)
    assert calls == [("ready", None)]


def test_tool_registry_rejects_invalid_schemas_and_non_text_results() -> None:
    with pytest.raises(ToolError, match="Invalid schema"):
        ToolRegistry([Tool("broken", "Broken schema.", lambda: "ok", {"type": "invalid"})])

    registry = ToolRegistry(
        [
            Tool(
                "number",
                "Returns the wrong type.",
                lambda: 1,  # type: ignore[return-value]
                {"type": "object", "additionalProperties": False},
            )
        ]
    )
    with pytest.raises(ToolError, match="must return text"):
        registry.invoke("number", {})


class MessageArgumentPlanner:
    name = "message-argument"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, runtime) -> AssistantMessage:
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(
                tool_messages=[ToolMessage(name="echo", call_id="call_1", arguments={"message": "hello"})]
            )
        return AssistantMessage(content="done")


def test_tool_call_trace_nests_arguments_that_match_event_field_names() -> None:
    tool = Tool(
        "echo",
        "Returns a message.",
        lambda message: message,
        {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    )
    runner = AgentRunner(MessageArgumentPlanner(), ToolRegistry([tool]), strategy="reactive")

    state = runner.run(runner.new_runtime(task="echo a message"))

    event = next(event for event in state.events if event.kind == "tool_call")
    assert state.status == "completed"
    assert event.data == {"call_id": "call_1", "arguments": {"message": "hello"}}


def test_env_file_loading_is_pure_and_process_values_take_precedence(tmp_path: Path, monkeypatch) -> None:
    for name in ("API_KEY", "BASE_URL", "MODEL", "MAX_TOKENS", "PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "API_KEY=file-key\nBASE_URL=https://file.test/v1\nMODEL=file-model\nMAX_TOKENS=1024\n",
        encoding="utf-8",
    )

    assert load_env_file(env_path)["API_KEY"] == "file-key"
    assert "API_KEY" not in os.environ

    config = ModelConfig.from_env(
        env_path,
        environ={"API_KEY": "process-key", "MODEL": "process-model", "PROVIDER": "DEEPSEEK"},
    )

    assert config.api_key == "process-key"
    assert config.base_url == "https://file.test/v1"
    assert config.model == "process-model"
    assert config.max_tokens == 1024
    assert config.provider == "deepseek"
    assert "API_KEY" not in os.environ


def test_workspace_command_filters_sensitive_environment_variables(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    environment = {
        "PATH": "tools",
        "VISIBLE_SETTING": "visible",
        "API_KEY": "generic-key",
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "DEEPSEEK_API_KEY": "provider-key",
        "GITHUB_PAT": "personal-access-token",
        "GITHUB_TOKEN": "token",
        "DATABASE_PASSWORD": "password",
        "CLIENT_SECRET": "secret",
        "SSH_PRIVATE_KEY": "private-key",
        "SERVICE_AUTH": "authorization",
        "SESSION_COOKIE": "cookie",
        "lower_secret": "case-insensitive",
    }

    class FakeProcess:
        pid = 1234
        returncode = 0

        @staticmethod
        def communicate(timeout: int | None = None) -> tuple[str, str]:
            return "", ""

        def poll(self) -> int:
            return self.returncode

    def popen_factory(_args: list[str], **kwargs: Any) -> FakeProcess:
        calls.append(kwargs)
        return FakeProcess()

    output = WorkspaceCommand(
        tmp_path,
        is_windows=False,
        popen_factory=popen_factory,
        environment=environment,
    ).run("true")

    assert output == "Command completed successfully."
    assert calls[0]["env"] == {"PATH": "tools", "VISIBLE_SETTING": "visible"}
    assert environment["API_KEY"] == "generic-key"
