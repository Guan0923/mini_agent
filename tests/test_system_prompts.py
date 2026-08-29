import pytest

from backend.configuration import ClientPaths
from backend.domain import AssistantMessage, SkillSnapshot, SystemMessage
from backend.planning import LLMPlanner
from backend.planning import prompts as prompt_module
from backend.planning.prompts import (
    PromptConfigurationError,
    PromptTemplates,
    _read_prompt,
    compose_system_prompt,
)
from backend.runtime import AgentRunner, PreparedResponse
from backend.runtime.application import factory as application_factory
from backend.runtime.capability_settings import SkillSettings
from backend.runtime.core.config import RunnerSettings
from backend.tools import ToolRegistry


class RecordingClient:
    def __init__(self, response: AssistantMessage | None = None) -> None:
        self.response = response or AssistantMessage(content="Done.")
        self.message_requests = []

    def run(self, runtime):
        self.message_requests.append(list(runtime.exchange.messages))
        return PreparedResponse(self.response)


def test_composes_agent_prompt_from_instruction_shared_and_agent_templates() -> None:
    prompt = compose_system_prompt("agent")

    assert prompt.count("# Mini-Agent") == 1
    assert prompt.count("# Shared Working Rules") == 1
    assert prompt.count("# Agent Mode") == 1
    assert "# Plan Mode" not in prompt
    assert "{{MODE_PROMPT}}" not in prompt


def test_composes_plan_prompt_without_agent_only_capabilities() -> None:
    prompt = compose_system_prompt("plan")

    assert prompt.count("# Mini-Agent") == 1
    assert prompt.count("# Shared Working Rules") == 1
    assert prompt.count("# Plan Mode") == 1
    assert "# Agent Mode" not in prompt
    assert "does not require every response" in prompt
    assert "Do not attempt commands, tests, builds" in prompt


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


def test_agent_instruction_is_injected_before_active_skills() -> None:
    client = RecordingClient()
    instructions = "### Project instructions: AGENTS.md\n<agents-md>Run focused tests.</agents-md>"
    planner = LLMPlanner(
        client,
        [],
        [],
        user_preferences="回答简洁",
        agent_instructions=instructions,
    )
    skill = SkillSnapshot("demo", "Demo", "Follow the demo.", ".mini_agent/skills/demo", "abc")
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(
        task="Implement the change",
        active_skills=[skill],
    )

    planner.decide(runtime)

    system = client.message_requests[0][0]
    content = system.content or ""
    assert "## Applicable AGENTS.md Instructions" in content
    assert "### Project instructions: AGENTS.md" in content
    assert content.index("## User Agent Preferences") < content.index("## Applicable AGENTS.md Instructions")
    assert content.index("## Applicable AGENTS.md Instructions") < content.index("## Active project Skills")
    assert "project-root AGENTS.md replaces the global AGENTS.md" in content
    assert "cannot expand permissions" in content


def test_empty_agent_instructions_are_not_injected() -> None:
    client = RecordingClient()
    planner = LLMPlanner(client, [], [], agent_instructions="  ")
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="Implement the change")

    planner.decide(runtime)

    system = client.message_requests[0][0]
    assert "Applicable AGENTS.md Instructions" not in (system.content or "")


def test_application_factory_prefers_project_agents_over_global_agents(tmp_path, monkeypatch) -> None:
    agents_home = tmp_path / ".mini_agent"
    workspace = tmp_path / "project"
    agents_home.mkdir()
    workspace.mkdir()
    (agents_home / "AGENTS.md").write_text("global factory guidance", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("project factory guidance", encoding="utf-8")
    client = RecordingClient()
    monkeypatch.setattr(application_factory, "LLMClient", lambda _config: client)

    runner = application_factory._build_runner(
        workspace,
        "llm",
        RunnerSettings(),
        ToolRegistry(),
        None,
        ClientPaths(tmp_path / "user"),
        SkillSettings(),
        agents_home=agents_home,
        model_config=object(),  # type: ignore[arg-type]
    )
    try:
        planner = runner.planner
        assert isinstance(planner, LLMPlanner)
        assert "project factory guidance" in planner.agent_instructions
        assert "global factory guidance" not in planner.agent_instructions
        assert str(tmp_path) not in planner.agent_instructions
    finally:
        runner.close()


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
