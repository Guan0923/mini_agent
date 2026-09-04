from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.storage import sqlite_sidebar_threads


def _create(client: TestClient, title: str) -> dict[str, object]:
    response = client.post("/api/sidebar-threads", json={"title": title})
    assert response.status_code == 201
    time.sleep(0.002)
    return response.json()


def _active_ids(client: TestClient, project_id: str | None = None) -> list[str]:
    response = client.get("/api/sidebar-threads", params={"state": "active"})
    assert response.status_code == 200
    return [item["thread_id"] for item in response.json() if item.get("project_id") == project_id]


def test_sidebar_order_persists_sorts_once_and_restores_archived_slot(tmp_path: Path) -> None:
    root = tmp_path / "web"
    state = WebAppState(root)
    try:
        with TestClient(create_app(state)) as client:
            first = _create(client, "first")
            second = _create(client, "second")
            third = _create(client, "third")
            first_id = str(first["thread_id"])
            second_id = str(second["thread_id"])
            third_id = str(third["thread_id"])

            assert _active_ids(client) == [third_id, second_id, first_id]
            manual = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": None, "ordered_thread_ids": [first_id, third_id, second_id]},
            )
            assert manual.status_code == 200
            assert _active_ids(client) == [first_id, third_id, second_id]

            assert client.post(f"/api/sidebar-threads/{third_id}/archive").status_code == 200
            moved = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": None, "ordered_thread_ids": [second_id, first_id]},
            )
            assert moved.status_code == 200
            assert client.post(f"/api/sidebar-threads/{third_id}/restore").status_code == 200
            assert _active_ids(client) == [second_id, third_id, first_id]

            fourth = _create(client, "fourth")
            fourth_id = str(fourth["thread_id"])
            assert _active_ids(client) == [fourth_id, second_id, third_id, first_id]

            by_created = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": None, "sort_by": "created_at"},
            )
            assert by_created.status_code == 200
            assert by_created.json()["ordered_thread_ids"] == [fourth_id, third_id, second_id, first_id]

            store = session_store(state)
            store.touch_sidebar_thread_activity(first_id, timestamp="2030-01-01T00:00:00+00:00")
            by_activity = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": None, "sort_by": "recent_activity"},
            )
            assert by_activity.status_code == 200
            assert by_activity.json()["ordered_thread_ids"][0] == first_id

            store.touch_sidebar_thread_activity(second_id, timestamp="2031-01-01T00:00:00+00:00")
            assert _active_ids(client)[0] == first_id
    finally:
        state.close()

    reopened = WebAppState(root)
    try:
        with TestClient(create_app(reopened)) as client:
            assert _active_ids(client)[0] == first_id
    finally:
        reopened.close()


def test_sidebar_order_rejects_incomplete_duplicate_and_cross_group_ids(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    state = WebAppState(tmp_path / "web-groups")
    try:
        with TestClient(create_app(state)) as client:
            ordinary = _create(client, "ordinary")
            project_first = _create(client, "project-first")
            project_second = _create(client, "project-second")
            project = state.projects.create(project_dir)
            state.projects.create_session(project.project_id, str(project_first["session_id"]))
            state.projects.create_session(project.project_id, str(project_second["session_id"]))

            project_ids = [str(project_second["thread_id"]), str(project_first["thread_id"])]
            assert _active_ids(client, project.project_id) == project_ids
            saved = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": project.project_id, "ordered_thread_ids": list(reversed(project_ids))},
            )
            assert saved.status_code == 200
            assert _active_ids(client, project.project_id) == list(reversed(project_ids))
            assert _active_ids(client) == [str(ordinary["thread_id"])]

            incomplete = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": project.project_id, "ordered_thread_ids": [project_ids[0]]},
            )
            duplicate = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": project.project_id, "ordered_thread_ids": [project_ids[0], project_ids[0]]},
            )
            crossed = client.put(
                "/api/sidebar-threads/order",
                json={
                    "project_id": project.project_id,
                    "ordered_thread_ids": [project_ids[0], str(ordinary["thread_id"])],
                },
            )
            assert incomplete.status_code == duplicate.status_code == crossed.status_code == 409
    finally:
        state.close()


def test_sidebar_metadata_activity_is_independent_from_rename_time(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web-activity")
    try:
        with TestClient(create_app(state)) as client:
            created = _create(client, "before")
            thread_id = str(created["thread_id"])
            activity_at = str(created["last_activity_at"])
            renamed = client.patch(f"/api/sidebar-threads/{thread_id}", json={"title": "after"})
            assert renamed.status_code == 200
            assert renamed.json()["last_activity_at"] == activity_at
            assert renamed.json()["updated_at"] != created["updated_at"]
    finally:
        state.close()


def test_sidebar_order_request_requires_exactly_one_operation(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web-request-shape")
    try:
        with TestClient(create_app(state)) as client:
            created = _create(client, "only")
            thread_id = str(created["thread_id"])
            neither = client.put("/api/sidebar-threads/order", json={"project_id": None})
            both = client.put(
                "/api/sidebar-threads/order",
                json={
                    "project_id": None,
                    "ordered_thread_ids": [thread_id],
                    "sort_by": "created_at",
                },
            )
            assert neither.status_code == both.status_code == 422
    finally:
        state.close()


def test_sidebar_time_sorts_use_thread_id_as_a_stable_final_tiebreaker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixed_time = "2026-09-04T00:00:00+00:00"
    monkeypatch.setattr(sqlite_sidebar_threads, "utc_now", lambda: fixed_time)
    state = WebAppState(tmp_path / "web-stable-ties")
    try:
        with TestClient(create_app(state)) as client:
            first = _create(client, "first")
            second = _create(client, "second")
            expected = sorted(
                [str(first["thread_id"]), str(second["thread_id"])],
                reverse=True,
            )

            by_created = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": None, "sort_by": "created_at"},
            )
            by_activity = client.put(
                "/api/sidebar-threads/order",
                json={"project_id": None, "sort_by": "recent_activity"},
            )

            assert by_created.json()["ordered_thread_ids"] == expected
            assert by_activity.json()["ordered_thread_ids"] == expected
    finally:
        state.close()
