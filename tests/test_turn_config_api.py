from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.chat.interrupts import registry
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain.runtime_state import NodeWriter, RuntimeState
from backend.planning import RuleBasedPlanner
from backend.runtime import AgentRunner
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.tools import ToolRegistry


def test_running_turn_config_patch_merges_partial_model_and_rejects_completed_turn(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state)
        writer = NodeWriter(store)
        root = store.ensure_root_node(sidebar["session_id"])
        node = writer.create(
            RuntimeState.create(
                session_id=sidebar["session_id"],
                thread_id=sidebar["thread_id"],
                id="turn_config",
                parent=root,
                user_content=[{"type": "text", "text": "configure me"}],
                provider_name="local",
            )
        )

        reasoning = client.patch(f"/api/turns/{node.id}/config", json={"model": {"reasoning_effort": "high"}})
        assert reasoning.status_code == 200
        assert reasoning.json()["model"] == {**node.model, "reasoning_effort": "high"}

        mode = client.patch(f"/api/turns/{node.id}/config", json={"running_mode": "plan"})
        permission = client.patch(f"/api/turns/{node.id}/config", json={"permission_mode": "workspace_write"})
        assert mode.status_code == permission.status_code == 200
        assert mode.json()["running_mode"] == "plan"
        assert permission.json()["permission_mode"] == "workspace_write"

        assert client.patch(f"/api/turns/{node.id}/config", json={"permission_mode": "full_access"}).status_code == 422
        full_access = client.patch(
            f"/api/turns/{node.id}/config",
            json={"permission_mode": "full_access", "full_access_acknowledged": True},
        )
        assert full_access.status_code == 200

        current = store.get_node(node.session_id, node.id)
        assert current is not None
        final_writer = NodeWriter(store)
        current = final_writer.snapshot(current)
        final_writer.finalize(current, "success")
        completed = client.patch(f"/api/turns/{node.id}/config", json={"running_mode": "agent"})
        assert completed.status_code == 409


def test_running_turn_config_patch_updates_active_runtime_pending_config(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state)
        bridge = RuntimeEventNodeBridge(
            store,
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            turn_id="turn_active_config",
            prompt="configure active runtime",
            emit=lambda _frame: None,
        )
        node = bridge.start()
        runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="configure active runtime")
        bridge.bind_runtime(runtime)
        state.active_runtime_bridges[sidebar["thread_id"]] = bridge

        assert client.patch(f"/api/turns/{node.id}/config", json={"running_mode": "plan"}).status_code == 200
        response = client.patch(
            f"/api/turns/{node.id}/config",
            json={"permission_mode": "workspace_write", "model": {"reasoning_effort": "high"}},
        )

        assert response.status_code == 200
        assert response.json()["running_mode"] == "plan"
        assert response.json()["permission_mode"] == "workspace_write"
        assert response.json()["model"]["reasoning_effort"] == "high"
        assert runtime.services.pending_runtime_config == {
            "running_mode": "plan",
            "permission_mode": "workspace_write",
            "model": response.json()["model"],
        }
        assert runtime.apply_pending_runtime_config() is True
        assert runtime.state.running_mode == "plan"
        assert runtime.state.permission_mode == "workspace_write"
        assert runtime.state.model_snapshot["reasoning_effort"] == "high"


def test_plan_decision_endpoint_rejects_old_choices(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        for choice in ("implement_clear_session", "cancel"):
            decision_id = f"decision_{choice}"
            registry.register(decision_id, request_kind="plan")
            response = client.post("/api/decisions", json={"decision_id": decision_id, "choice": choice})
            assert response.status_code == 422
            registry.discard(decision_id)
