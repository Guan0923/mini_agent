from __future__ import annotations

import asyncio
import inspect

from mini_agent.runtime import RuntimeEvent
from mini_agent.tui.view import TerminalView


def _assistant_message(*, content: str | None = None, reasoning: str | None = None, tools=None) -> dict:
    return {
        "name": "assistant",
        "role": "assistant",
        "content": content,
        "reasoning": reasoning,
        "tool_messages": list(tools or []),
    }


def _tool(name: str, call_id: str, arguments: dict) -> dict:
    return {
        "name": name,
        "role": "tool",
        "call_id": call_id,
        "arguments": arguments,
        "content": None,
        "status": "pending",
    }


def test_external_async_task_can_submit_and_render_markdown() -> None:
    async def scenario() -> None:
        view = TerminalView()
        view_task = asyncio.create_task(view.run_async(headless=True, size=(100, 30)))
        try:
            for _ in range(100):
                if view._owner_ready:
                    break
                await asyncio.sleep(0.01)
            assert view._owner_ready

            view.begin_conversation("你好")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response", "你好！", {"run_id": "run-1"}))
            await asyncio.sleep(0.2)

            assert not view_task.done()
            assert [node.title_text for node in view._top_level_nodes] == ["USER", "ASSISTANT"]
            assert [body.markdown_text for body in view.markdown_bodies] == ["你好", "你好！"]
        finally:
            view.exit()
            await view_task

    asyncio.run(scenario())


def test_stream_render_coroutine_is_created_only_when_worker_starts() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("initial")
            await pilot.pause()
            body = view.markdown_bodies[0]
            scheduled: list[object] = []
            body.run_worker = lambda work, **_kwargs: scheduled.append(work)  # type: ignore[method-assign]

            for index in range(100):
                body.set_markdown(f"updated {index}")
            await pilot.pause()

            assert body.markdown_text == "updated 99"
            assert len(scheduled) == 1
            assert callable(scheduled[0])
            assert not inspect.isawaitable(scheduled[0])

    asyncio.run(scenario())

def test_stream_render_serializes_in_flight_markdown_updates() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("initial")
            await pilot.pause()
            body = view.markdown_bodies[0]

            first_started = asyncio.Event()
            release_first = asyncio.Event()
            calls: list[str] = []
            was_cancelled = False

            async def fake_update(markdown: str) -> None:
                nonlocal was_cancelled
                calls.append(markdown)
                if len(calls) == 1:
                    first_started.set()
                    try:
                        await release_first.wait()
                    except asyncio.CancelledError:
                        was_cancelled = True
                        raise

            body.update = fake_update  # type: ignore[method-assign]
            body.set_markdown("first revision")
            await asyncio.wait_for(first_started.wait(), 1)

            body.set_markdown("second revision")
            await asyncio.sleep(0.05)
            assert calls == ["first revision"]
            assert was_cancelled is False

            release_first.set()
            for _ in range(100):
                if calls == ["first revision", "second revision"]:
                    break
                await asyncio.sleep(0.01)

            assert calls == ["first revision", "second revision"]
            assert was_cancelled is False

    asyncio.run(scenario())



def test_transcript_preserves_assistant_event_order_and_markdown_content() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(100, 30)) as pilot:
            view.begin_conversation("do the work")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_delta", "**first thought**", {"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_end", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_delta", "first response", {"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_end", data={"run_id": "run-1"}))
            view.handle_runtime_event(
                RuntimeEvent(
                    "assistant_message",
                    data={
                        "run_id": "run-1",
                        "exchange_id": "exchange-1",
                        "reasoning_streamed": True,
                        "content_streamed": True,
                        "message": _assistant_message(
                            tools=[_tool("tool_a", "call-a", {"path": "a.txt"})]
                        ),
                    },
                )
            )
            view.handle_runtime_event(RuntimeEvent("tool_call", "tool_a", {"run_id": "run-1", "call_id": "call-a", "arguments": {"path": "a.txt"}}))
            view.handle_runtime_event(RuntimeEvent("tool_result", "**result**", {"run_id": "run-1", "call_id": "call-a", "tool": "tool_a"}))
            view.handle_runtime_event(RuntimeEvent("thinking_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_delta", "second thought", {"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_end", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_delta", "final response", {"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_end", data={"run_id": "run-1"}))
            view.handle_runtime_event(
                RuntimeEvent(
                    "assistant_message",
                    data={
                        "run_id": "run-1",
                        "exchange_id": "exchange-2",
                        "reasoning_streamed": True,
                        "content_streamed": True,
                        "message": _assistant_message(content="final response"),
                    },
                )
            )
            await pilot.pause()

            labels = [node.title_text for node in view.transcript_nodes]
            assert labels == [
                "USER",
                "ASSISTANT",
                "think_content",
                "response_content",
                "tool_call: tool_a",
                "think_content",
                "response_content",
            ]
            assert view.transcript_nodes[2].collapsed is True
            assert view.transcript_nodes[4].collapsed is True
            markdown = [body for body in view.markdown_bodies if "first thought" in body.markdown_text]
            assert markdown and "**first thought**" in markdown[0].markdown_text
            assert markdown[0]._markdown == "**first thought**"

    asyncio.run(scenario())


def test_collapsed_streaming_node_animates_without_expanding() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("think")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_delta", "working", {"run_id": "run-1"}))
            await pilot.pause()

            node = next(item for item in view.transcript_nodes if item.title_text == "think_content")
            assert node.collapsed is True
            assert node.activity is True
            first_title = node.display_title

            node._tick_activity()
            assert node.collapsed is True
            assert node.display_title != first_title

            view.handle_runtime_event(RuntimeEvent("thinking_end", data={"run_id": "run-1"}))
            await pilot.pause()
            assert node.activity is False
            assert node.display_title == "▶ think_content"

    asyncio.run(scenario())


def test_non_streamed_message_is_completed_once_and_same_name_tools_use_call_id() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(100, 30)) as pilot:
            view.begin_conversation("run twice")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(
                RuntimeEvent(
                    "assistant_message",
                    data={
                        "run_id": "run-1",
                        "exchange_id": "exchange-1",
                        "reasoning_streamed": False,
                        "content_streamed": False,
                        "message": _assistant_message(
                            reasoning="**reasoning**",
                            content="## answer",
                            tools=[
                                _tool("read_file", "call-a", {"path": "a.md"}),
                                _tool("read_file", "call-b", {"path": "b.md"}),
                            ],
                        ),
                    },
                )
            )
            view.handle_runtime_event(
                RuntimeEvent(
                    "tool_result",
                    "first result",
                    {"run_id": "run-1", "call_id": "call-a", "tool": "read_file"},
                )
            )
            view.handle_runtime_event(
                RuntimeEvent(
                    "tool_failed",
                    "second failed",
                    {"run_id": "run-1", "call_id": "call-b", "tool": "read_file"},
                )
            )
            view.handle_runtime_event(RuntimeEvent("response", "## answer", {"run_id": "run-1"}))
            await pilot.pause()

            labels = [node.title_text for node in view.transcript_nodes]
            assert labels == [
                "USER",
                "ASSISTANT",
                "think_content",
                "response_content",
                "tool_call: read_file",
                "tool_call: read_file",
            ]
            first = view._tools_by_call[("run-1", "call-a")]
            second = view._tools_by_call[("run-1", "call-b")]
            assert first.result.markdown_text == "first result"
            assert first.status.status == "succeeded"
            assert second.result.markdown_text == "second failed"
            assert second.status.status == "failed"

    asyncio.run(scenario())


def test_manual_collapse_is_preserved_during_response_streaming() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("answer")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_start", data={"run_id": "run-1"}))
            node, body = view._response_by_run["run-1"]
            node.collapsed = True
            view.handle_runtime_event(RuntimeEvent("response_delta", "streamed", {"run_id": "run-1"}))
            await pilot.pause()

            assert node.collapsed is True
            assert node.activity is True
            assert body._markdown == "streamed"

    asyncio.run(scenario())


def test_transcript_limit_only_removes_completed_top_level_nodes() -> None:
    async def scenario() -> None:
        view = TerminalView(transcript_limit=30)
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("u" * 50)
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_delta", "a" * 50, {"run_id": "run-1"}))
            await pilot.pause()

            assert [node.title_text for node in view._top_level_nodes] == ["USER", "ASSISTANT"]
            assert len(view.transcript_text) > 30

            view.handle_runtime_event(RuntimeEvent("run_finished", data={"run_id": "run-1"}))
            await pilot.pause()
            assert view._top_level_nodes == []
            assert view.transcript_text == ""

    asyncio.run(scenario())



def test_run_finished_completes_assistant_without_rendering_a_child() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("finish")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("thinking_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(
                RuntimeEvent("run_finished", "completed", {"run_id": "run-1"})
            )
            await pilot.pause()

            labels = [node.title_text for node in view.transcript_nodes]
            assert "run_finished" not in labels
            assert view._assistant_by_run["run-1"] in view._completed_top_levels
            assert all(not node.activity for node in view.transcript_nodes)

    asyncio.run(scenario())


def test_strategy_event_is_not_rendered_as_an_assistant_child() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("choose a strategy")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(
                RuntimeEvent(
                    "strategy",
                    "reactive",
                    {"run_id": "run-1", "reason": "Simple task."},
                )
            )
            view.handle_runtime_event(RuntimeEvent("response", "done", {"run_id": "run-1"}))
            await pilot.pause()

            labels = [node.title_text for node in view.transcript_nodes]
            assert "strategy" not in labels
            assert "response_content" in labels

    asyncio.run(scenario())


def test_model_repair_event_is_hidden_while_following_response_is_rendered() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("answer")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(
                RuntimeEvent(
                    "model_repair",
                    "Malformed model output was repaired automatically.",
                    {"run_id": "run-1", "phase": "decision", "outcome": "repaired"},
                )
            )
            view.handle_runtime_event(RuntimeEvent("response", "corrected answer", {"run_id": "run-1"}))
            await pilot.pause()

            labels = [node.title_text for node in view.transcript_nodes]
            assert "model_repair" not in labels
            assert "response_content" in labels
            assert "Malformed model output" not in view.transcript_text
            assert "corrected answer" in view.transcript_text

    asyncio.run(scenario())


def test_transcript_node_limit_trims_only_complete_conversation_groups() -> None:
    async def scenario() -> None:
        view = TerminalView(transcript_node_limit=3)
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("first")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response", "first answer", {"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("run_finished", "completed", {"run_id": "run-1"}))
            view.begin_conversation("second")
            await pilot.pause()

            assert [node.title_text for node in view._top_level_nodes] == ["USER", "ASSISTANT"]
            assert "second" in view.transcript_text
            assert "first answer" not in view.transcript_text

    asyncio.run(scenario())

def test_system_message_before_view_mount_is_logged_without_rendering() -> None:
    async def scenario() -> None:
        records: list[tuple[str, dict[str, object]]] = []
        view = TerminalView(
            diagnostic_sink=lambda kind, data, _error: records.append((kind, dict(data or {})))
        )
        view.write_system("Mini-Agent TUI startup")

        async with view.run_test(size=(80, 20)) as pilot:
            await pilot.pause()

            assert view._top_level_nodes == []
            assert view.markdown_bodies == []
            assert view.transcript_text == ""

        hidden = [data for kind, data in records if kind == "system_output_hidden"]
        assert hidden == [
            {
                "hidden": True,
                "message_chars": len("Mini-Agent TUI startup"),
                "end": "\n",
                "message": "Mini-Agent TUI startup",
            }
        ]

    asyncio.run(scenario())



def test_multiple_system_writes_never_create_transcript_nodes() -> None:
    async def scenario() -> None:
        records: list[tuple[str, dict[str, object]]] = []
        view = TerminalView(
            diagnostic_sink=lambda kind, data, _error: records.append((kind, dict(data or {}))),
            log_full_messages=False,
        )
        async with view.run_test(size=(80, 20)) as pilot:
            view.write_system("first")
            view.write_system("second")
            await pilot.pause()

            assert view._top_level_nodes == []
            assert view.markdown_bodies == []
            assert view.transcript_text == ""

        hidden = [data for kind, data in records if kind == "system_output_hidden"]
        assert hidden == [
            {"hidden": True, "message_chars": 5, "end": "\n"},
            {"hidden": True, "message_chars": 6, "end": "\n"},
        ]

    asyncio.run(scenario())


def test_history_load_skips_system_and_keeps_role_content_only() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("stale conversation")
            view.input.value = "preserved draft"
            view.load_history(
                [
                    {"role": "system", "content": "hidden notice"},
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ]
            )
            await pilot.pause()

            assert [node.title_text for node in view._top_level_nodes] == ["USER", "ASSISTANT"]
            assert all(node.has_class("transcript-role") for node in view._top_level_nodes)
            assert view.transcript_text == "question\nanswer"
            assert "hidden notice" not in view.transcript_text
            assert "stale conversation" not in view.transcript_text
            assert view.input.value == "preserved draft"

    asyncio.run(scenario())

def test_background_runtime_events_wait_for_assistant_mount_before_adding_children() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(100, 30)) as pilot:
            view.begin_conversation("你好")
            events = [
                RuntimeEvent("run_started", data={"run_id": "run-1"}),
                RuntimeEvent("thinking_start", data={"run_id": "run-1"}),
                RuntimeEvent("thinking_delta", "你好！", {"run_id": "run-1"}),
                RuntimeEvent("thinking_end", data={"run_id": "run-1"}),
                RuntimeEvent("response_start", data={"run_id": "run-1"}),
                RuntimeEvent("response_delta", "你好！有什么可以帮你？", {"run_id": "run-1"}),
                RuntimeEvent("response_end", data={"run_id": "run-1"}),
            ]
            await asyncio.gather(*(asyncio.to_thread(view.handle_runtime_event, event) for event in events))
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            assistant_children = view.transcript_nodes[2:]
            assert assistant_children
            assert all(node.is_attached for node in assistant_children)
            assert all(node.is_mounted for node in assistant_children)

    asyncio.run(scenario())
