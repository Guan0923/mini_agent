"""Per-Skill project trust persistence tests (user config.toml)."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from backend.configuration import LocalConfigStore
from backend.domain import SkillSelection
from backend.runtime import AgentRunner
from backend.skills import ProjectSkillGate, ProjectSkillTrustStore, SkillCatalog, SkillDefinition
from backend.tools import ToolRegistry


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _make_root() -> Path:
    storage = Path(tempfile.gettempdir()) / f"trust-{uuid4().hex}"
    storage.mkdir(parents=True, exist_ok=True)
    return storage


def _write_project_skill(workspace: Path, name: str, *, body: str = "Project body.") -> None:
    directory = workspace / ".mini_agent" / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Project {name}.\n---\n{body}\n",
        encoding="utf-8",
    )


def _definition(name: str, description: str) -> SkillDefinition:
    return SkillDefinition(name, description, (), (), f"skills/{name}", Path(f"skills/{name}/SKILL.md"))


class RecordingInterrupt:
    def __init__(self, decisions: list[str]) -> None:
        self.decisions = list(decisions)
        self.requests: list = []

    def __call__(self, request):
        self.requests.append(request)
        choice = self.decisions.pop(0) if self.decisions else "skip"
        from backend.runtime.core.contracts import InterruptDecision

        return InterruptDecision(choice)


class SelectingPlanner:
    name = "selecting"

    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self.names = names
        self.selection_calls = 0
        self.decision_calls = 0

    def select_skills(self, runtime) -> SkillSelection:
        self.selection_calls += 1
        return SkillSelection(self.names)

    def decide(self, runtime):
        self.decision_calls += 1
        from backend.domain import AssistantMessage

        return AssistantMessage(content="done")


class ReplyOnlyPlanner:
    name = "reply-only"

    def __init__(self) -> None:
        self.decision_calls = 0

    def decide(self, runtime):
        self.decision_calls += 1
        from backend.domain import AssistantMessage

        return AssistantMessage(content="done")


@pytest.fixture
def user_root() -> Path:
    root = _make_root()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def trust_store(user_root: Path) -> ProjectSkillTrustStore:
    config = LocalConfigStore(user_root / "config.toml")
    config.update({"runtime": {"max_tool_calls": 32}})
    return ProjectSkillTrustStore(config)


def test_trust_requires_exact_tree_hash(user_root: Path) -> None:
    project_id = "project_1"
    workspace_sha = _h("workspace-a")
    config = LocalConfigStore(user_root / "config.toml")
    config.update({"runtime": {"max_tool_calls": 32}})
    store = ProjectSkillTrustStore(config)
    t1 = _h("tree-v1")
    t2 = _h("tree-v2")
    w2 = _h("workspace-b")

    assert store.is_trusted(project_id, workspace_sha, "demo", t1) is False

    store.record_trust(project_id, workspace_sha, "demo", t1)
    assert store.is_trusted(project_id, workspace_sha, "demo", t1) is True
    assert store.is_trusted(project_id, workspace_sha, "demo", t2) is False
    assert store.is_trusted(project_id, w2, "demo", t1) is False
    assert store.is_trusted(project_id, workspace_sha, "other", t1) is False


def test_skills_are_tracked_independently(user_root: Path) -> None:
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    store.record_trust("p", _h("w"), "alpha", _h("ta"))
    store.record_trust("p", _h("w"), "beta", _h("tb"))

    assert store.is_trusted("p", _h("w"), "alpha", _h("ta")) is True
    assert store.is_trusted("p", _h("w"), "beta", _h("tb")) is True
    assert store.is_trusted("p", _h("w"), "alpha", _h("tb")) is False

    store.revoke_skill("p", "alpha")
    assert store.is_trusted("p", _h("w"), "alpha", _h("ta")) is False
    assert store.is_trusted("p", _h("w"), "beta", _h("tb")) is True


def test_revoke_project_removes_all_skills(user_root: Path) -> None:
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    store.record_trust("p", _h("w"), "alpha", _h("ta"))
    store.record_trust("p", _h("w"), "beta", _h("tb"))

    store.revoke_project("p")
    assert store.is_trusted("p", _h("w"), "alpha", _h("ta")) is False
    assert store.is_trusted("p", _h("w"), "beta", _h("tb")) is False


def test_project_path_change_invalidates_trust(user_root: Path) -> None:
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    store.record_trust("p", _h("path-v1"), "demo", _h("t"))

    assert store.is_trusted("p", _h("path-v1"), "demo", _h("t")) is True
    assert store.is_trusted("p", _h("path-v2"), "demo", _h("t")) is False


def test_trust_preserves_other_config_sections(user_root: Path) -> None:
    config = LocalConfigStore(user_root / "config.toml")
    config.update({"runtime": {"max_tool_calls": 32}})
    store = ProjectSkillTrustStore(config)
    store.record_trust("p", _h("w"), "alpha", _h("ta"))

    reader = LocalConfigStore(user_root / "config.toml").read()
    assert reader["project_skill_trust"]
    assert reader["runtime"] == {"max_tool_calls": 32}


def test_trusted_skills_report(user_root: Path) -> None:
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    store.record_trust("p", _h("w"), "alpha", _h("ta"))
    store.record_trust("p", _h("w"), "beta", _h("tb"))

    assert store.trusted_skills("p", _h("w")) == {"alpha": _h("ta"), "beta": _h("tb")}
    assert store.trusted_skills("p", _h("w2")) == {}


def test_agent_preferences_section_is_not_required(user_root: Path) -> None:
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    assert store.trusted_skills("missing", _h("w")) == {}
    assert store.is_trusted("missing", _h("w"), "demo", _h("t")) is False


def test_gate_skips_untrusted_skills_without_interrupt(tmp_path: Path, user_root: Path) -> None:
    _write_project_skill(tmp_path, "alpha")
    _write_project_skill(tmp_path, "beta")
    gate = ProjectSkillGate(tmp_path, "project_1", ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml")))
    planner = SelectingPlanner()
    runtime = AgentRunner(planner, ToolRegistry(), skill_catalog=SkillCatalog()).new_runtime(task="hello")

    result = gate.prepare(runtime)

    assert [skill.name for skill in result.usable] == []
    assert sorted(result.untrusted_names) == ["alpha", "beta"]


def test_gate_approves_one_skill_at_a_time(tmp_path: Path, user_root: Path) -> None:
    _write_project_skill(tmp_path, "alpha")
    _write_project_skill(tmp_path, "beta")
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    gate = ProjectSkillGate(tmp_path, "project_1", store)
    runtime = AgentRunner(SelectingPlanner(), ToolRegistry(), skill_catalog=SkillCatalog()).new_runtime(task="task")
    runtime.services.interrupt = RecordingInterrupt(["trust", "skip"])

    result = gate.prepare(runtime)

    assert sorted(skill.name for skill in result.usable) == ["alpha"]
    assert ["beta"] == result.untrusted_names
    assert store.is_trusted("project_1", gate._workspace_sha, "alpha", result.usable[0].tree_sha256) is True
    assert store.is_trusted("project_1", gate._workspace_sha, "beta", result.usable[0].tree_sha256) is False


def test_gate_with_output_repair_keeps_untrusted_out_of_run(tmp_path: Path, user_root: Path) -> None:
    _write_project_skill(tmp_path, "alpha")
    _write_project_skill(tmp_path, "beta")
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    gate = ProjectSkillGate(tmp_path, "project_1", store)
    planner = SelectingPlanner(("alpha", "beta"))
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        skill_catalog=SkillCatalog(),
        skill_auto_select=True,
        project_skill_gate=gate,
    )
    runtime = runner.new_runtime(task="run")
    runtime.services.interrupt = RecordingInterrupt(["skip", "skip"])

    state = runner.run(runtime)

    # The auto-selection call sees neither project Skill (they were skipped),
    # so selecting them fails closed instead of activating untrusted content.
    assert state.status == "failed"
    rendered = state.final_answer or ""
    assert "Unknown Skill selection" in rendered


def test_runner_uses_only_trusted_project_skills_for_selection(tmp_path: Path, user_root: Path) -> None:
    from backend.skills import discover_project_skills, workspace_sha256

    _write_project_skill(tmp_path, "alpha")
    _write_project_skill(tmp_path, "beta")
    store = ProjectSkillTrustStore(LocalConfigStore(user_root / "config.toml"))
    alpha = next(c for c in discover_project_skills(tmp_path, "project_1") if c.name == "alpha")
    store.record_trust("project_1", workspace_sha256(tmp_path), "alpha", alpha.tree_sha256)
    gate = ProjectSkillGate(tmp_path, "project_1", store)

    planner = SelectingPlanner(("alpha",))
    runner = AgentRunner(
        planner,
        ToolRegistry(),
        skill_catalog=SkillCatalog(),
        skill_auto_select=True,
        project_skill_gate=gate,
    )
    runtime = runner.new_runtime(task="select")
    runtime.services.interrupt = RecordingInterrupt(["skip"])

    state = runner.run(runtime)

    assert state.status == "completed"
    assert [skill.name for skill in state.active_skills] == ["alpha"]
    assert planner.selection_calls == 1
