from __future__ import annotations

import asyncio

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

            await asyncio.sleep(0.15)
            await pilot.pause()
            assert node.collapsed is True
            assert node.display_title != first_title

            view.handle_runtime_event(RuntimeEvent("thinking_end", data={"run_id": "run-1"}))
            await pilot.pause()
            assert node.activity is False
            assert node.display_title == "▶ think_content"

    asyncio.run(scenario())
