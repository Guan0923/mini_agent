from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_agent.domain import (
    AssistantMessage,
    RunHandoff,
    RunState,
    SkillSelection,
    SkillSnapshot,
    ToolMessage,
    UserMessage,
)
from mini_agent.planning import LLMPlanner
from mini_agent.runtime import AgentRunner, PreparedResponse
from mini_agent.skills import (
    MAX_INSTRUCTION_LINES,
    MAX_SKILL_BYTES,
    SkillCatalog,
    SkillConfigurationError,
    SkillDefinition,
)
from mini_agent.tools import ToolRegistry
from mini_agent.tui.cli import TerminalApp


def write_skill(
    workspace: Path,
    name: str = "demo",
    *,
    description: str = "Use for demo tasks.",
    instructions: str = "Follow the demo workflow.",
) -> Path:
    directory = workspace / ".mini_agent" / "skills" / name
    directory.mkdir(parents=True)
    manifest = directory / "SKILL.md"
    manifest.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n",
        encoding="utf-8",
    )
    return manifest


def definition(name: str, description: str | None = None) -> SkillDefinition:
    return SkillDefinition(
        name,
        description or f"Use {name}.",
        f"Follow {name}.",
        f".mini_agent/skills/{name}",
        f"sha-{name}",
    )


def test_missing_skill_directory_produces_empty_catalog(tmp_path: Path) -> None:
    catalog = SkillCatalog.discover(tmp_path)

    assert not catalog
    assert catalog.names() == ()


def test_rejects_skill_directory_without_manifest(tmp_path: Path) -> None:
    (tmp_path / ".mini_agent" / "skills" / "demo").mkdir(parents=True)

    with pytest.raises(SkillConfigurationError, match="missing SKILL.md"):
        SkillCatalog.discover(tmp_path)


def test_discovers_valid_skill_and_explicit_reference(tmp_path: Path) -> None:
    write_skill(tmp_path, "demo-skill", instructions="# Workflow\nRead references/guide.md.")

    catalog = SkillCatalog.discover(tmp_path)
    skill = catalog.definitions()[0]

    assert catalog.names() == ("demo-skill",)
    assert catalog.explicit_names("Use $demo-skill but preserve $HOME.") == ("demo-skill",)
    assert skill.root == ".mini_agent/skills/demo-skill"
    assert skill.instructions == "# Workflow\nRead references/guide.md."
    assert len(skill.sha256) == 64


@pytest.mark.parametrize(
    ("manifest", "error"),
    [
        ("name: demo\ndescription: Demo\n", "start with YAML frontmatter"),
        ("---\nname: demo\n---\nBody\n", "only 'name' and 'description'"),
        ("---\nname: Demo\ndescription: Demo\n---\nBody\n", "lowercase letters"),
        ("---\nname: demo\ndescription: ''\n---\nBody\n", "description must be"),
        ("---\nname: demo\ndescription: Demo\nextra: true\n---\nBody\n", "only 'name' and 'description'"),
        ("---\nname: demo\ndescription: Demo\n---\n", "instructions must not be empty"),
    ],
)
def test_rejects_invalid_manifests(tmp_path: Path, manifest: str, error: str) -> None:
    directory = tmp_path / ".mini_agent" / "skills" / "demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(manifest, encoding="utf-8")

    with pytest.raises(SkillConfigurationError, match=error):
        SkillCatalog.discover(tmp_path)


def test_rejects_directory_name_mismatch(tmp_path: Path) -> None:
    manifest = write_skill(tmp_path, "folder")
    manifest.write_text(
        "---\nname: other\ndescription: Demo\n---\nBody\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillConfigurationError, match="must match directory name"):
        SkillCatalog.discover(tmp_path)


def test_rejects_non_utf8_and_size_limits(tmp_path: Path) -> None:
    manifest = write_skill(tmp_path)
    manifest.write_bytes(b"\xff\xfe")

    with pytest.raises(SkillConfigurationError, match="UTF-8"):
        SkillCatalog.discover(tmp_path)

    manifest.write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    with pytest.raises(SkillConfigurationError, match="exceeds"):
        SkillCatalog.discover(tmp_path)


def test_rejects_instruction_line_limit(tmp_path: Path) -> None:
    body = "\n".join("line" for _ in range(MAX_INSTRUCTION_LINES + 1))
    write_skill(tmp_path, instructions=body)

    with pytest.raises(SkillConfigurationError, match="instructions exceed"):
        SkillCatalog.discover(tmp_path)


def test_rejects_manifest_symlink_outside_skill_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: demo\ndescription: Demo\n---\nBody\n", encoding="utf-8")
    directory = tmp_path / ".mini_agent" / "skills" / "demo"
    directory.mkdir(parents=True)
    try:
        os.symlink(outside, directory / "SKILL.md")
    except OSError:
        pytest.skip("Symbolic links are unavailable on this platform.")

    with pytest.raises(SkillConfigurationError, match="escapes"):
        SkillCatalog.discover(tmp_path)


def test_rejects_duplicate_catalog_names() -> None:
    with pytest.raises(SkillConfigurationError, match="unique"):
        SkillCatalog((definition("demo"), definition("demo")))


def test_skill_snapshots_round_trip_and_old_state_defaults_empty() -> None:
    snapshot = SkillSnapshot("demo", "Demo", "Instructions", ".mini_agent/skills/demo", "abc")
    run = RunState(
        task="demo",
        mode="agent",
        active_skills=[snapshot],
        handoff=RunHandoff("agent", "Implement", active_skills=(snapshot,)),
    )

    restored = RunState.from_dict(run.to_dict())
    legacy = RunState.from_dict(
        {
            "task": "legacy",
            "mode": "agent",
            "run_id": "run_legacy",
        }
    )

    assert restored.active_skills == [snapshot]
    assert restored.handoff is not None and restored.handoff.active_skills == (snapshot,)
    assert legacy.active_skills == []


class SelectingPlanner:
    name = "selecting"

    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self.names = names
        self.selection_calls = 0
        self.decision_calls = 0

    def select_skills(self, runtime) -> SkillSelection:
        self.selection_calls += 1
        return SkillSelection(self.names)

    def decide(self, runtime) -> AssistantMessage:
        self.decision_calls += 1
        return AssistantMessage(content="done")


class ReplyOnlyPlanner:
    name = "reply-only"

    def __init__(self) -> None:
        self.decision_calls = 0

    def decide(self, runtime) -> AssistantMessage:
        self.decision_calls += 1
        return AssistantMessage(content="done")


def test_runner_merges_explicit_and_automatic_skills_in_catalog_order() -> None:
    planner = SelectingPlanner(("beta", "beta"))
    catalog = SkillCatalog((definition("alpha"), definition("beta")))
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        strategy="reactive",
        skill_catalog=catalog,
    )

    state = runner.run(runner.new_runtime(task="Use $alpha for this task."))

    assert state.status == "completed"
    assert [skill.name for skill in state.active_skills] == ["alpha", "beta"]
    assert planner.selection_calls == 1
    assert state.model_turns == 2
    event = next(event for event in state.events if event.kind == "skills_selected")
    assert event.data["explicit"] == ["alpha"]
    assert event.data["automatic"] == ["beta"]


def test_runner_without_skills_does_not_call_selector() -> None:
    planner = SelectingPlanner(("unused",))
    runner = AgentRunner(planner, ToolRegistry(), strategy="reactive", skill_catalog=SkillCatalog())

    state = runner.run(runner.new_runtime(task="hello"))

    assert state.status == "completed"
    assert planner.selection_calls == 0
    assert state.model_turns == 1


def test_skill_selection_consumes_model_turn_budget() -> None:
    planner = SelectingPlanner(("demo",))
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        strategy="reactive",
        max_model_turns=1,
        skill_catalog=SkillCatalog((definition("demo"),)),
    )

    state = runner.run(runner.new_runtime(task="demo"))

    assert state.status == "failed"
    assert state.model_turns == 1
    assert planner.selection_calls == 1
    assert planner.decision_calls == 0


def test_explicit_skill_requires_llm_planner() -> None:
    planner = ReplyOnlyPlanner()
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        strategy="reactive",
        skill_catalog=SkillCatalog((definition("demo"),)),
    )

    state = runner.run(runner.new_runtime(task="Use $demo."))

    assert state.status == "failed"
    assert state.final_answer == "Skill execution requires the LLM planner."
    assert planner.decision_calls == 0


class RecordingClient:
    def __init__(self, responses: list[PreparedResponse]) -> None:
        self.responses = responses
        self.requests: list[list] = []

    def run(self, runtime) -> PreparedResponse:
        self.requests.append(list(runtime.exchange.messages))
        return self.responses.pop(0)


def test_llm_skill_selection_uses_only_current_turn() -> None:
    client = RecordingClient([PreparedResponse(AssistantMessage(content='{"skills":["demo"]}'))])
    catalog = SkillCatalog((definition("demo"),))
    planner = LLMPlanner(client, [], [])
    history = [
        UserMessage(content="Old task."),
        AssistantMessage(
            content="Old work.",
            tool_messages=[
                ToolMessage(
                    name="old_tool",
                    call_id="call_old",
                    content="old result",
                    status="succeeded",
                )
            ],
        ),
    ]
    runtime = AgentRunner(
        planner,
        ToolRegistry(),
        strategy="reactive",
        skill_catalog=catalog,
    ).new_runtime(task="Use demo now.", messages=history)

    selection = planner.select_skills(runtime)

    assert selection.names == ("demo",)
    assert [message.content for message in client.requests[0][1:]] == ["Use demo now."]


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_llm_selection_sees_metadata_then_active_body_is_injected(mode: str) -> None:
    client = RecordingClient(
        [
            PreparedResponse(AssistantMessage(content='{"skills":["demo"]}')),
            PreparedResponse(AssistantMessage(content="done")),
        ]
    )
    catalog = SkillCatalog((definition("demo", "Use for reports."),))
    planner = LLMPlanner(client, [], [])
    runner = AgentRunner(planner, ToolRegistry(), strategy="reactive", skill_catalog=catalog)
    runtime = runner.new_runtime(task="Prepare a report.", mode=mode)

    selection = planner.select_skills(runtime)
    runtime.run.active_skills = catalog.snapshots(set(selection.names))
    planner.decide(runtime)

    selection_system = client.requests[0][0].content or ""
    decision_system = client.requests[1][0].content or ""
    assert "Use for reports." in selection_system
    assert "Follow demo." not in selection_system
    assert "Follow demo." in decision_system
    assert "lower priority than every preceding system rule" in decision_system
    assert ".mini_agent/skills/demo" in decision_system


def test_llm_skill_selection_repairs_unknown_name() -> None:
    client = RecordingClient(
        [
            PreparedResponse(AssistantMessage(content='{"skills":["missing"]}')),
            PreparedResponse(AssistantMessage(content='{"skills":["demo"]}')),
        ]
    )
    catalog = SkillCatalog((definition("demo"),))
    planner = LLMPlanner(client, [], [])
    runtime = AgentRunner(planner, ToolRegistry(), skill_catalog=catalog).new_runtime(task="demo")

    selection = planner.select_skills(runtime)

    assert selection.names == ("demo",)
    repairs = planner.consume_output_repairs()
    assert len(repairs) == 1
    assert repairs[0]["outcome"] == "repaired"


def test_handoff_skills_skip_reselection() -> None:
    planner = SelectingPlanner(("other",))
    inherited = definition("demo").snapshot()
    catalog = SkillCatalog((definition("demo"), definition("other")))
    runner = AgentRunner(planner, ToolRegistry(), strategy="reactive", skill_catalog=catalog)
    runtime = runner.new_runtime(task="Implement", active_skills=[inherited])

    state = runner.run(runtime)

    assert state.active_skills == [inherited]
    assert planner.selection_calls == 0
    event = next(event for event in state.events if event.kind == "skills_selected")
    assert event.data["source"] == "handoff"


def test_skills_command_lists_catalog_and_empty_state() -> None:
    outputs: list[str] = []
    app = object.__new__(TerminalApp)
    app.runner = SimpleNamespace(skill_catalog=SkillCatalog((definition("demo", "Demo tasks."),)))
    app._write = outputs.append

    assert app._handle_command("skills", "") is True
    assert outputs == ["demo — Demo tasks."]

    outputs.clear()
    app.runner = SimpleNamespace(skill_catalog=SkillCatalog())
    assert app._handle_command("skills", "") is True
    assert outputs == ["No project Skills found in .mini_agent/skills."]
