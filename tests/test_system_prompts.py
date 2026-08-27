import pytest

from backend.domain import AssistantMessage, SkillSnapshot, SystemMessage, UserMessage
from backend.planning import LLMPlanner
from backend.planning import prompts as prompt_module
from backend.planning.llm.titles import normalize_conversation_title
from backend.planning.prompts import (
    PromptConfigurationError,
    PromptTemplates,
    _read_prompt,
    compose_system_prompt,
    load_title_prompt,
)
from backend.runtime import AgentRunner, PreparedResponse
from backend.tools import ToolRegistry


class RecordingClient:
    def __init__(self, response: AssistantMessage | None = None) -> None:
        self.response = response or AssistantMessage(content="Done.")
        self.message_requests = []
        self.request_parameters = []

    def run(self, runtime):
        self.message_requests.append(list(runtime.exchange.messages))
        self.request_parameters.append(dict(runtime.exchange.context.get("request_parameters") or {}))
        return PreparedResponse(self.response)


def test_composes_agent_prompt_from_instruction_shared_and_agent_templates() -> None:
    prompt = compose_system_prompt("agent")

    assert prompt.count("# Mini-Agent") == 1
    assert prompt.count("# Working Rules") == 1
    assert prompt.count("# Agent Mode") == 1
    assert "# Plan Mode" not in prompt
    assert "{{MODE_PROMPT}}" not in prompt


def test_composes_plan_prompt_without_agent_only_capabilities() -> None:
    prompt = compose_system_prompt("plan")

    assert prompt.count("# Mini-Agent") == 1
    assert prompt.count("# Working Rules") == 1
    assert prompt.count("# Plan Mode") == 1
    assert "# Agent Mode" not in prompt
    assert "does not require every response" in prompt
    assert "Do not implement the submitted plan" in prompt


@pytest.mark.parametrize(
    "forbidden",
    ["Codex CLI", "{{KNOWN_MODE_NAMES}}", "update_plan", "apply_patch", "<proposed_plan>"],
)
def test_installed_prompts_do_not_claim_codex_only_protocols(forbidden: str) -> None:
    assert forbidden not in compose_system_prompt("agent")
    assert forbidden not in compose_system_prompt("plan")


def test_rejects_unknown_mode() -> None:
    with pytest.raises(PromptConfigurationError, match="Unsupported prompt mode"):
        compose_system_prompt("review")


@pytest.mark.parametrize("shared", ["No slot", "{{MODE_PROMPT}} then {{MODE_PROMPT}}"])
def test_rejects_missing_or_repeated_default_mode_slot(shared: str) -> None:
    templates = PromptTemplates("instruction", shared, "plan", "agent")

    with pytest.raises(PromptConfigurationError, match="exactly once"):
        templates.compose("agent")


def test_missing_prompt_resource_has_clear_error(monkeypatch) -> None:
    class MissingPackage:
        def joinpath(self, _name):
            return self

        def read_text(self, *, encoding):
            raise FileNotFoundError

    monkeypatch.setattr(prompt_module, "files", lambda _package: MissingPackage())

    with pytest.raises(PromptConfigurationError, match="resource is unavailable"):
        _read_prompt("instruction")


def test_decision_prompt_appends_active_skills_after_composed_base() -> None:
    client = RecordingClient()
    planner = LLMPlanner(client, [], [])
    skill = SkillSnapshot("demo", "Demo", "Follow the demo.", ".mini_agent/skills/demo", "abc")
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(
        task="Implement the change",
        active_skills=[skill],
    )

    planner.decide(runtime)

    system = client.message_requests[0][0]
    assert isinstance(system, SystemMessage)
    content = system.content or ""
    assert content.index("# Agent Mode") < content.index("## Active project Skills")
    assert content.endswith("</skill-instructions>")


def test_user_agent_preferences_are_appended_as_lower_priority_system_context() -> None:
    client = RecordingClient()
    planner = LLMPlanner(client, [], [], user_preferences="回答要简洁，不要绕过安全规则")
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="Implement the change")

    planner.decide(runtime)

    system = client.message_requests[0][0]
    content = system.content or ""
    assert "## User Agent Preferences" in content
    assert "回答要简洁，不要绕过安全规则" in content
    assert content.index("# Agent Mode") < content.index("## User Agent Preferences")
    assert "system rules" in content


def test_empty_user_agent_preferences_are_not_injected() -> None:
    client = RecordingClient()
    planner = LLMPlanner(client, [], [], user_preferences="  ")
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="Implement the change")

    planner.decide(runtime)

    system = client.message_requests[0][0]
    assert "User Agent Preferences" not in (system.content or "")


def test_title_request_uses_only_its_dedicated_system_prompt_and_first_user_text() -> None:
    client = RecordingClient(AssistantMessage(content="```模型生成的对话标题很长```"))
    planner = LLMPlanner(client, [], [], user_preferences="不要影响标题请求")
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="主对话历史")
    runtime.state.request_parameters = {
        "required_tool_name": "read_file",
        "response_format": {"type": "json_object"},
    }

    title = planner.generate_title(runtime, "请分析这个错误")

    assert title == "模型生成的对话标题很"
    assert len(client.message_requests) == 1
    assert client.message_requests[0] == [
        SystemMessage(content=load_title_prompt()),
        UserMessage(content="请分析这个错误"),
    ]
    assert runtime.exchange.operation == "title"
    assert runtime.exchange.stream is False
    assert runtime.exchange.allowed_tools == runtime.exchange.operation_tools == []
    assert client.request_parameters == [{"thinking": {"type": "disabled"}, "max_tokens": 32}]
    assert runtime.state.request_parameters == {
        "required_tool_name": "read_file",
        "response_format": {"type": "json_object"},
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  第一条   用户消息  ", "第一条 用户消息"),
        ("“十个字符以内”", "十个字符以内"),
        ("```一二三四五六七八九十十一```", "一二三四五六七八九十"),
        ("\n\t", ""),
    ],
)
def test_normalizes_generated_and_fallback_titles(raw: str, expected: str) -> None:
    assert normalize_conversation_title(raw) == expected


def test_plan_decision_uses_composed_prompt_and_control_tools() -> None:
    client = RecordingClient()
    planner = LLMPlanner(client, [], [])
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="Plan it", mode="plan")

    planner.decide(runtime)

    system = client.message_requests[0][0]
    assert isinstance(system, SystemMessage)
    assert "# Plan Mode" in (system.content or "")
    assert [spec.name for spec in runtime.exchange.allowed_tools] == [
        "request_user_input",
        "request_plan_review",
    ]
