from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.domain import (
    AssistantMessage,
    RunHandoff,
    RunState,
    SkillSelection,
    SkillSnapshot,
    ToolMessage,
    UserMessage,
)
from backend.planning import LLMPlanner
from backend.runtime import AgentRunner, PreparedResponse
from backend.skills import (
    MAX_INSTRUCTION_LINES,
    MAX_SKILL_BYTES,
    SkillCatalog,
    SkillConfigurationError,
    SkillDefinition,
    discover_project_skills,
)
from backend.tools import ToolRegistry


def write_skill(
    root: Path,
    name: str = "demo",
    *,
    description: str = "Use for demo tasks.",
    instructions: str = "Follow the demo workflow.",
) -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True)
    manifest = directory / "SKILL.md"
    manifest.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n",
        encoding="utf-8",
    )
    return manifest


def definition(
    name: str,
    description: str | None = None,
    *,
    metadata: tuple[tuple[str, str], ...] = (),
    allowed_tools: tuple[str, ...] = (),
    manifest: Path | None = None,
) -> SkillDefinition:
    return SkillDefinition(
        name,
        description or f"Use {name}.",
        metadata,
        allowed_tools,
        f"skills/{name}",
        manifest or Path(f"skills/{name}/SKILL.md"),
    )


def test_missing_skill_directory_produces_empty_catalog(tmp_path: Path) -> None:
    catalog = SkillCatalog.discover(tmp_path / "skills")

    assert not catalog
    assert catalog.names() == ()


def test_rejects_skill_directory_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "skills" / "demo").mkdir(parents=True)

    with pytest.raises(SkillConfigurationError, match="missing SKILL.md"):
        SkillCatalog.discover(tmp_path / "skills")


def test_discovers_valid_skill_and_explicit_reference(tmp_path: Path) -> None:
    write_skill(tmp_path, "demo-skill", instructions="# Workflow\nRead references/guide.md.")

    catalog = SkillCatalog.discover(tmp_path / "skills")
    skill = catalog.definitions()[0]

    assert catalog.names() == ("demo-skill",)
    assert catalog.explicit_names("Use $demo-skill but preserve $HOME.") == ("demo-skill",)
    assert skill.root == (tmp_path / "skills" / "demo-skill").resolve().as_posix()
    snapshot = skill.snapshot()
    assert snapshot.instructions == "# Workflow\nRead references/guide.md."
    assert len(snapshot.sha256) == 64


def test_project_skills_are_ignored_when_global_root_is_given(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "home" / "mini_agent" / "skills"
    for name, instructions in (("shared", "Global instructions."), ("global-only", "Global only.")):
        directory = global_root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Global {name}.\n---\n{instructions}\n",
            encoding="utf-8",
        )
    write_skill(workspace, "shared", instructions="Project instructions.")

    catalog = SkillCatalog.discover(workspace, global_root=global_root)
    definitions = {item.name: item for item in catalog.definitions()}

    assert catalog.names() == ("global-only", "shared")
    assert definitions["shared"].snapshot().instructions == "Global instructions."
    assert definitions["shared"].root == (global_root / "shared").resolve().as_posix()
    assert definitions["global-only"].root == (global_root / "global-only").resolve().as_posix()


def test_malformed_global_manifest_is_rejected(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    malformed = global_root / "shared"
    malformed.mkdir(parents=True)
    (malformed / "SKILL.md").write_text("not frontmatter", encoding="utf-8")

    with pytest.raises(SkillConfigurationError, match="start with YAML frontmatter"):
        SkillCatalog.discover(global_root=global_root)


def test_unknown_explicit_skill_reference_is_rejected_before_model_call() -> None:
    planner = ReplyOnlyPlanner()
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        skill_catalog=SkillCatalog((definition("demo"),)),
    )

    state = runner.run(runner.new_runtime(task="Use $missing-skill."))

    assert state.status == "failed"
    assert "Unknown explicit Skill reference: missing-skill" in (state.final_answer or "")
    assert planner.decision_calls == 0


def test_unknown_explicit_skill_is_rejected_when_catalog_is_empty() -> None:
    planner = ReplyOnlyPlanner()
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        skill_catalog=SkillCatalog(()),
    )

    state = runner.run(runner.new_runtime(task="Use $missing-skill."))

    assert state.status == "failed"
    assert "Available Skills: none" in (state.final_answer or "")
    assert planner.decision_calls == 0


def test_rejects_manifest_without_frontmatter(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("name: demo\ndescription: Demo\n", encoding="utf-8")

    with pytest.raises(SkillConfigurationError, match="start with YAML frontmatter"):
        SkillCatalog.discover(tmp_path / "skills")


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: demo",
        "name: Demo\ndescription: Demo",
        "name: demo\ndescription: ''",
        "name: other\ndescription: Demo",
    ],
)
def test_skips_skill_with_invalid_required_frontmatter(tmp_path: Path, frontmatter: str) -> None:
    directory = tmp_path / "skills" / "demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nBody\n", encoding="utf-8")
    write_skill(tmp_path, "valid")

    catalog = SkillCatalog.discover(tmp_path / "skills")

    assert catalog.names() == ("valid",)


def test_ignores_unknown_frontmatter_fields(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: Demo\n"
        "license: Complete terms in LICENSE.txt\n"
        "origin: ECC\n"
        "keywords:\n"
        "  - workflow\n"
        "keywards: legacy spelling\n"
        "future-field:\n"
        "  nested: true\n"
        "---\nBody\n",
        encoding="utf-8",
    )

    skill = SkillCatalog.discover(tmp_path / "skills").definitions()[0]

    assert skill.name == "demo"
    assert skill.metadata == ()
    assert skill.allowed_tools == ()
    assert skill.snapshot().instructions == "Body"


def test_rejects_non_utf8_and_size_limits(tmp_path: Path) -> None:
    manifest = write_skill(tmp_path)
    manifest.write_bytes(b"\xff\xfe")

    with pytest.raises(SkillConfigurationError, match="UTF-8"):
        SkillCatalog.discover(tmp_path / "skills")

    manifest.write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    with pytest.raises(SkillConfigurationError, match="exceeds"):
        SkillCatalog.discover(tmp_path / "skills")


def test_rejects_instruction_line_limit_at_snapshot(tmp_path: Path) -> None:
    body = "\n".join("line" for _ in range(MAX_INSTRUCTION_LINES + 1))
    write_skill(tmp_path, instructions=body)

    catalog = SkillCatalog.discover(tmp_path / "skills")
    assert catalog.names() == ("demo",)

    with pytest.raises(SkillConfigurationError, match="instructions exceed"):
        catalog.snapshots({"demo"})


def test_empty_instructions_fail_only_at_snapshot(tmp_path: Path) -> None:
    write_skill(tmp_path, instructions="")

    catalog = SkillCatalog.discover(tmp_path / "skills")

    with pytest.raises(SkillConfigurationError, match="instructions must not be empty"):
        catalog.snapshots({"demo"})


def test_rejects_manifest_symlink_outside_skill_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: demo\ndescription: Demo\n---\nBody\n", encoding="utf-8")
    directory = tmp_path / "skills" / "demo"
    directory.mkdir(parents=True)
    try:
        os.symlink(outside, directory / "SKILL.md")
    except OSError:
        pytest.skip("Symbolic links are unavailable on this platform.")

    with pytest.raises(SkillConfigurationError, match="escapes"):
        SkillCatalog.discover(tmp_path / "skills")


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


def test_explicit_skill_bypasses_automatic_selection(tmp_path: Path) -> None:
    planner = SelectingPlanner(("beta", "beta"))
    catalog = SkillCatalog(
        (
            definition("alpha", manifest=write_skill(tmp_path, "alpha")),
            definition("beta", manifest=write_skill(tmp_path, "beta")),
        )
    )
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        skill_catalog=catalog,
    )

    events = []
    state = runner.run(runner.new_runtime(task="Use $alpha for this task.", on_event=events.append))

    assert state.status == "completed"
    assert [skill.name for skill in state.active_skills] == ["alpha"]
    assert planner.selection_calls == 0
    assert state.model_turns == 1
    event = next(event for event in events if event.kind == "skills_selected")
    assert event.data["explicit"] == ["alpha"]
    assert event.data["automatic"] == []


def test_runner_without_skills_does_not_call_selector() -> None:
    planner = SelectingPlanner(("unused",))
    runner = AgentRunner(planner, ToolRegistry(), skill_catalog=SkillCatalog())

    state = runner.run(runner.new_runtime(task="hello"))

    assert state.status == "completed"
    assert planner.selection_calls == 0
    assert state.model_turns == 1


def test_automatic_skill_selection_has_a_separate_budget_counter(tmp_path: Path) -> None:
    planner = SelectingPlanner(("demo",))
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        skill_catalog=SkillCatalog((definition("demo", manifest=write_skill(tmp_path, "demo")),)),
        skill_auto_select=True,
    )

    state = runner.run(runner.new_runtime(task="demo"))

    assert state.status == "completed"
    assert state.model_turns == 1
    assert state.skill_selection_calls == 1
    assert planner.selection_calls == 1
    assert planner.decision_calls == 1


def test_explicit_skill_does_not_require_a_selector(tmp_path: Path) -> None:
    planner = ReplyOnlyPlanner()
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        skill_catalog=SkillCatalog((definition("demo", manifest=write_skill(tmp_path, "demo")),)),
    )

    state = runner.run(runner.new_runtime(task="Use $demo."))

    assert state.status == "completed"
    assert [skill.name for skill in state.active_skills] == ["demo"]
    assert planner.decision_calls == 1


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
        skill_catalog=catalog,
    ).new_runtime(task="Use demo now.", messages=history)

    selection = planner.select_skills(runtime)

    assert selection.names == ("demo",)
    assert [message.content for message in client.requests[0][1:]] == ["Use demo now."]


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_llm_selection_sees_metadata_then_active_body_is_injected(tmp_path: Path, mode: str) -> None:
    client = RecordingClient(
        [
            PreparedResponse(AssistantMessage(content='{"skills":["demo"]}')),
            PreparedResponse(AssistantMessage(content="done")),
        ]
    )
    catalog = SkillCatalog(
        (definition("demo", "Use for reports.", manifest=write_skill(tmp_path, "demo", instructions="Follow demo.")),)
    )
    planner = LLMPlanner(client, [], [])
    runner = AgentRunner(planner, ToolRegistry(), skill_catalog=catalog)
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
    assert "skills/demo" in decision_system


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


def test_handoff_skills_skip_reselection(tmp_path: Path) -> None:
    planner = SelectingPlanner(("other",))
    demo_manifest = write_skill(tmp_path, "demo")
    inherited = definition("demo", manifest=demo_manifest).snapshot()
    catalog = SkillCatalog(
        (
            definition("demo", manifest=demo_manifest),
            definition("other", manifest=write_skill(tmp_path, "other")),
        )
    )
    runner = AgentRunner(planner, ToolRegistry(), skill_catalog=catalog)
    events = []
    runtime = runner.new_runtime(task="Implement", active_skills=[inherited], on_event=events.append)

    state = runner.run(runtime)

    assert state.active_skills == [inherited]
    assert planner.selection_calls == 0
    event = next(event for event in events if event.kind == "skills_selected")
    assert event.data["source"] == "handoff"


def test_discovers_skill_with_optional_frontmatter(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: Demo tasks.\n"
        "metadata:\n"
        "  owner: platform\n"
        "  risk: low\n"
        "allowed-tools:\n"
        "  - read\n"
        "  - write\n"
        "---\nBody\n",
        encoding="utf-8",
    )

    skill = SkillCatalog.discover(tmp_path / "skills").definitions()[0]

    assert skill.metadata == (("owner", "platform"), ("risk", "low"))
    assert skill.allowed_tools == ("read", "write")
    assert skill.snapshot().instructions == "Body"


def test_ignores_invalid_optional_frontmatter(tmp_path: Path) -> None:
    directory = tmp_path / "skills" / "demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo\nmetadata:\n  owner: 42\nallowed-tools:\n  - 42\n---\nBody\n",
        encoding="utf-8",
    )

    skill = SkillCatalog.discover(tmp_path / "skills").definitions()[0]

    assert skill.metadata == ()
    assert skill.allowed_tools == ()
    assert skill.snapshot().instructions == "Body"


def test_llm_skill_selection_sees_optional_frontmatter() -> None:
    client = RecordingClient([PreparedResponse(AssistantMessage(content='{"skills":[]}'))])
    catalog = SkillCatalog((definition("demo", metadata=(("owner", "platform"),), allowed_tools=("read", "write")),))
    planner = LLMPlanner(client, [], [])
    runtime = AgentRunner(planner, ToolRegistry(), skill_catalog=catalog).new_runtime(task="demo")

    planner.select_skills(runtime)

    selection_system = client.requests[0][0].content or ""
    assert '"metadata": {"owner": "platform"}' in selection_system
    assert '"allowed-tools": ["read", "write"]' in selection_system


def test_explicit_skill_with_empty_instructions_fails_run(tmp_path: Path) -> None:
    planner = ReplyOnlyPlanner()
    catalog = SkillCatalog((definition("demo", manifest=write_skill(tmp_path, "demo", instructions="")),))
    runner = AgentRunner(planner, ToolRegistry(), skill_catalog=catalog)

    state = runner.run(runner.new_runtime(task="Use $demo."))

    assert state.status == "failed"
    assert "Skill activation failed" in (state.final_answer or "")
    assert "instructions must not be empty" in (state.final_answer or "")
    assert planner.decision_calls == 0


def _write_project_skill(
    workspace: Path, name: str, *, body: str = "Project body.", extra: dict[str, str] | None = None
) -> Path:
    directory = workspace / ".mini_agent" / "skills" / name
    directory.mkdir(parents=True)
    manifest = directory / "SKILL.md"
    manifest.write_text(f"---\nname: {name}\ndescription: Project {name}.\n---\n{body}\n", encoding="utf-8")
    for relative, content in (extra or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return manifest


def test_project_skill_candidates_are_sorted_and_scanned(tmp_path: Path) -> None:
    _write_project_skill(tmp_path, "zeta", body="Zeta body.")
    _write_project_skill(tmp_path, "alpha", extra={"references/guide.md": "guide"})

    candidates = discover_project_skills(tmp_path, "project_1")

    assert [skill.name for skill in candidates] == ["alpha", "zeta"]
    assert all(skill.source == "project" for skill in candidates)
    assert all(skill.project_id == "project_1" for skill in candidates)
    assert all(len(skill.tree_sha256) == 64 for skill in candidates)
    assert candidates[0].snapshot().instructions == "Project body."


def test_project_skill_ignores_unknown_and_invalid_optional_frontmatter(tmp_path: Path) -> None:
    manifest = _write_project_skill(tmp_path, "demo")
    manifest.write_text(
        "---\n"
        "name: demo\n"
        "description: Project demo.\n"
        "license: Complete terms in LICENSE.txt\n"
        "origin: ECC\n"
        "metadata: 42\n"
        "allowed-tools: read\n"
        "---\nProject body.\n",
        encoding="utf-8",
    )

    candidate = discover_project_skills(tmp_path, "project_1")[0]

    assert candidate.name == "demo"
    assert candidate.metadata == ()
    assert candidate.allowed_tools == ()
    assert candidate.snapshot().instructions == "Project body."


def test_project_skill_skips_invalid_required_frontmatter(tmp_path: Path) -> None:
    manifest = _write_project_skill(tmp_path, "bad")
    manifest.write_text("---\nname: bad\ndescription: null\n---\nBody\n", encoding="utf-8")
    _write_project_skill(tmp_path, "good")

    candidates = discover_project_skills(tmp_path, "project_1")

    assert [skill.name for skill in candidates] == ["good"]


def test_project_skill_tree_hash_changes_when_any_file_changes(tmp_path: Path) -> None:
    manifest = _write_project_skill(tmp_path, "demo", body="Version one.")
    before = discover_project_skills(tmp_path, "project_1")[0].tree_sha256

    manifest.write_text("---\nname: demo\ndescription: Project demo.\n---\nVersion two.\n", encoding="utf-8")
    after_manifest = discover_project_skills(tmp_path, "project_1")[0].tree_sha256
    assert after_manifest != before

    script = tmp_path / ".mini_agent" / "skills" / "demo" / "scripts" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('malicious')\n", encoding="utf-8")
    after_script = discover_project_skills(tmp_path, "project_1")[0].tree_sha256
    assert after_script != after_manifest


def test_project_skill_rejects_symlink_escape(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows sandbox cannot create symlinks (WinError 1314).")
    _write_project_skill(tmp_path, "demo")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / ".mini_agent" / "skills" / "demo" / "links" / "leak"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    with pytest.raises(SkillConfigurationError, match="symlink|escape|not a regular file"):
        discover_project_skills(tmp_path, "project_1")


def test_project_skill_rejects_path_escape(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows sandbox cannot create symlinks (WinError 1314).")
    _write_project_skill(tmp_path, "demo")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / ".mini_agent" / "skills" / "demo" / "SKILL.md").unlink()
    (tmp_path / ".mini_agent" / "skills" / "demo" / "SKILL.md").symlink_to(outside)

    with pytest.raises(SkillConfigurationError, match="symlink|escape|not a regular file"):
        discover_project_skills(tmp_path, "project_1")


def test_project_skill_bad_manifest_does_not_block_other_skills(tmp_path: Path) -> None:
    bad = tmp_path / ".mini_agent" / "skills" / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    _write_project_skill(tmp_path, "good")

    candidates = discover_project_skills(tmp_path, "project_1")

    assert [skill.name for skill in candidates] == ["good"]
    assert candidates[0].snapshot().instructions == "Project body."
