from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Timer
from types import SimpleNamespace

import pytest

from mini_agent.domain import RunState
from mini_agent.providers import ModelConfigurationError
from mini_agent.runtime import RuntimeEvent, TaskPreparationError
from mini_agent.runtime.core.contracts import InterruptRequest, QuestionOption, UserQuestion
from mini_agent.tui import cli
from mini_agent.tui.approval import TerminalApproval


class StubConversation:
    def __init__(self, result: RunState | Exception) -> None:
        self.active_session = None
        self.pending_session_title = None
        self.next_active_session = None
        self.result = result

    def run_task(self, task: str, **kwargs) -> RunState:
        if isinstance(self.result, Exception):
            raise self.result
        if self.next_active_session is not None:
            self.active_session = self.next_active_session
        return self.result


def build_terminal_app(result: RunState | Exception) -> cli.TerminalApp:
    app = object.__new__(cli.TerminalApp)
    app.last_state = None
    app.mode = "agent"
    app._conversation_service = StubConversation(result)
    app._event_sink = lambda event: None
    app._approval = TerminalApproval()
    return app


def test_run_task_returns_run_state() -> None:
    state = RunState(task="calculate", mode="agent", status="completed")
    app = build_terminal_app(state)

    assert app.run_task("calculate") is state
    assert app.last_state is state


def test_run_task_returns_none_when_preparation_fails(capsys) -> None:
    app = build_terminal_app(TaskPreparationError("missing.txt does not exist"))

    assert app.run_task("summarize @missing.txt") is None
    assert capsys.readouterr().out == "REFERENCE ERROR missing.txt does not exist\n"


def test_run_task_prints_and_activates_an_isolated_handoff_session(capsys) -> None:
    state = RunState(task="Implement the plan", mode="agent", status="completed")
    app = build_terminal_app(state)
    app.mode = "plan"
    app._conversation_service.active_session = SimpleNamespace(session_id="session_old", title="Plan")
    app._conversation_service.next_active_session = SimpleNamespace(
        session_id="session_new",
        title="Implement: Plan",
    )

    assert app.run_task("make a plan") is state

    assert app.mode == "agent"
    assert app.active_session.session_id == "session_new"
    assert capsys.readouterr().out == (
        "SESSION session_new — Implement: Plan\nAgent mode enabled after Plan Review implementation handoff.\n"
    )


def test_view_status_includes_permission_mode_for_every_state() -> None:
    statuses: list[tuple[str, bool]] = []

    class StatusView:
        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            statuses.append((status, interrupt_enabled))

    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._approval = TerminalApproval(permission_mode="approval_for_me")
    view = StatusView()
    idle_approval = SimpleNamespace(pending=False, status="REVIEW")
    review_approval = SimpleNamespace(pending=True, status="TOOL REVIEW | Select action")

    app._update_view_state(view, False, idle_approval, False)
    app._update_view_state(view, True, idle_approval, False)
    app._update_view_state(view, True, idle_approval, False, cancelling=True)
    app._update_view_state(view, True, review_approval, False)
    app._update_view_state(view, False, idle_approval, True)

    assert statuses == [
        ("AGENT | IDLE | PERMISSION: APPROVAL FOR ME", False),
        ("AGENT | RUNNING | PERMISSION: APPROVAL FOR ME", True),
        ("AGENT | CANCELLING | PERMISSION: APPROVAL FOR ME", False),
        ("TOOL REVIEW | Select action | PERMISSION: APPROVAL FOR ME", True),
        ("PERMISSION | Select mode | PERMISSION: APPROVAL FOR ME", False),
    ]


def test_view_status_refreshes_after_permission_mode_changes() -> None:
    statuses: list[str] = []

    class StatusView:
        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            del interrupt_enabled
            statuses.append(status)

    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._approval = TerminalApproval(permission_mode="approval_for_me")
    approval = SimpleNamespace(pending=False, status="REVIEW")

    app._update_view_state(StatusView(), False, approval, False)
    app._approval.set_permission("full_access")
    app._update_view_state(StatusView(), False, approval, False)

    assert statuses == [
        "AGENT | IDLE | PERMISSION: APPROVAL FOR ME",
        "AGENT | IDLE | PERMISSION: FULL ACCESS",
    ]


def test_interactive_start_uses_full_screen_view_and_prints_resume_status(monkeypatch, capsys) -> None:
    commands: list[str] = []

    class ExitView:
        last = None

        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            type(self).last = self
            self.submissions = asyncio.Queue()
            self.submissions.put_nowait(None)
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def set_ui(self, **kwargs) -> None:
            del kwargs

        def clear(self) -> None:
            self.writes.clear()

    monkeypatch.setattr(cli.os, "system", lambda command: commands.append(command) or 0)
    monkeypatch.setattr(cli, "TerminalView", ExitView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))

    app.start()

    assert commands == []
    assert ExitView.last is not None
    assert ExitView.last.writes[0].startswith("Mini-Agent TUI")
    assert capsys.readouterr().out == "No saved session.\n"


def test_interactive_start_defers_active_run_messages_to_one_follow_up_run(monkeypatch, capsys) -> None:
    calls: list[str] = []

    class BlockingConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            calls.append(task)
            assert kwargs["steering"] is None
            if task == "initial task":
                time.sleep(0.05)
            return RunState(task=task, mode="agent", status="completed")

    class InteractiveView:
        last = None

        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            type(self).last = self
            self.submissions = asyncio.Queue()
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.initial_sent = False
            self.running_messages = 0
            self.quit_sent = False
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def clear(self) -> None:
            self.writes.clear()

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            del interrupt_enabled
            if "| IDLE" in status and not self.initial_sent:
                self.initial_sent = True
                self.submissions.put_nowait("initial task")
                return
            if "| RUNNING" in status:
                if self.running_messages == 0:
                    self.running_messages += 1
                    self.submissions.put_nowait("first steering")
                    return
                if self.running_messages == 1:
                    self.running_messages += 1
                    self.submissions.put_nowait("second steering")
                    return
            if "| IDLE" in status and self.running_messages == 2 and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

    monkeypatch.setattr(cli.os, "system", lambda _command: 0)
    monkeypatch.setattr(cli, "TerminalView", InteractiveView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = BlockingConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval()

    app.start()

    assert calls == ["initial task", "first steering\n\nsecond steering"]
    assert InteractiveView.last is not None
    rendered = "".join(InteractiveView.last.writes)
    assert rendered.count("USER\ninitial task\n") == 1
    assert rendered.count("USER\nfirst steering\n") == 1
    assert rendered.count("USER\nsecond steering\n") == 1
    assert "USER\n/quit" not in rendered
    output = capsys.readouterr().out
    assert output == "No saved session.\n"



def test_interactive_review_keeps_main_input_for_queued_follow_up(monkeypatch, capsys) -> None:
    calls: list[str] = []

    class ReviewingConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            calls.append(task)
            if task == "initial task":
                decision = kwargs["interrupt"](
                    InterruptRequest("tool", "Approve tool?", {"tool": "write_file", "arguments": {}})
                )
                assert decision.choice == "continue"
            return RunState(task=task, mode="agent", status="completed")

    class ReviewQueueView:
        last = None

        def __init__(self, loop, **kwargs) -> None:
            del kwargs
            type(self).last = self
            self.loop = loop
            self.submissions = asyncio.Queue()
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.started = False
            self.queued = False
            self.quit_sent = False
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def clear(self) -> None:
            self.writes.clear()

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            del interrupt_enabled
            if "| IDLE" in status and not self.started:
                self.started = True
                self.submissions.put_nowait("initial task")
            elif "| IDLE" in status and len(calls) == 2 and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

        def begin_review(self, _title, _summary, _details, _choices, on_complete) -> None:
            if self.queued:
                return
            self.queued = True
            self.submissions.put_nowait("queued during review")
            self.loop.call_later(0.01, on_complete, "continue", None)

        def cancel_choice_prompt(self) -> None:
            pass

    monkeypatch.setattr(cli, "TerminalView", ReviewQueueView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = ReviewingConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval(write=app._write)

    app.start()

    assert calls == ["initial task", "queued during review"]
    assert ReviewQueueView.last is not None
    rendered = "".join(ReviewQueueView.last.writes)
    assert "USER\nqueued during review\n" in rendered
    assert "MESSAGE QUEUED\n" in rendered
    assert capsys.readouterr().out == "No saved session.\n"

def test_interactive_escape_cancels_active_run_without_exiting(monkeypatch, capsys) -> None:
    cancellation_seen: list[bool] = []

    class CancellableConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            assert task == "long task"
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not kwargs["cancel_requested"]():
                time.sleep(0.005)
            cancellation_seen.append(kwargs["cancel_requested"]())
            return RunState(task=task, mode="agent", status="cancelled")

    class InterruptView:
        last = None

        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            type(self).last = self
            self.submissions = asyncio.Queue()
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.started = False
            self.interrupt_sent = False
            self.quit_sent = False
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def clear(self) -> None:
            self.writes.clear()

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            del interrupt_enabled
            if "| IDLE" in status and not self.started:
                self.started = True
                self.submissions.put_nowait("long task")
            elif "| RUNNING" in status and not self.interrupt_sent:
                self.interrupt_sent = True
                self.interrupts.put_nowait(None)
            elif "| IDLE" in status and self.interrupt_sent and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

    monkeypatch.setattr(cli, "TerminalView", InterruptView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = CancellableConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval()

    app.start()

    assert cancellation_seen == [True]
    assert InterruptView.last is not None
    rendered = "".join(InterruptView.last.writes)
    assert rendered.count("CANCELLING — waiting for current operation") == 1
    assert capsys.readouterr().out == "No saved session.\n"


def test_interactive_escape_sends_queued_messages_after_cancellation(monkeypatch, capsys) -> None:
    calls: list[tuple[str, bool]] = []

    class CancellableConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            if not calls:
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline and not kwargs["cancel_requested"]():
                    time.sleep(0.005)
                time.sleep(0.05)
                calls.append((task, kwargs["cancel_requested"]()))
                return RunState(task=task, mode="agent", status="cancelled")
            calls.append((task, kwargs["cancel_requested"]()))
            return RunState(task=task, mode="agent", status="completed")

    class QueueThenInterruptView:
        last = None

        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            type(self).last = self
            self.submissions = asyncio.Queue()
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.started = False
            self.running_updates = 0
            self.cancelling_updates = 0
            self.quit_sent = False
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def clear(self) -> None:
            pass

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            del interrupt_enabled
            if "| IDLE" in status and not self.started:
                self.started = True
                self.submissions.put_nowait("initial task")
                return
            if "| RUNNING" in status and not calls:
                if self.running_updates == 0:
                    self.running_updates += 1
                    self.submissions.put_nowait("first queued")
                elif self.running_updates == 1:
                    self.running_updates += 1
                    self.submissions.put_nowait("second queued")
                elif self.running_updates == 2:
                    self.running_updates += 1
                    self.interrupts.put_nowait(None)
                return
            if "| CANCELLING" in status and not calls:
                if self.cancelling_updates == 0:
                    self.cancelling_updates += 1
                    self.submissions.put_nowait("third queued")
                elif self.cancelling_updates == 1:
                    self.cancelling_updates += 1
                    self.interrupts.put_nowait(None)
                return
            if "| IDLE" in status and len(calls) == 2 and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

    monkeypatch.setattr(cli, "TerminalView", QueueThenInterruptView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = CancellableConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval()

    app.start()

    assert calls == [
        ("initial task", True),
        ("first queued\n\nsecond queued\n\nthird queued", False),
    ]
    assert QueueThenInterruptView.last is not None
    assert QueueThenInterruptView.last.writes.count("CANCELLING — waiting for current operation\n") == 1
    assert capsys.readouterr().out == "No saved session.\n"


def test_interactive_escape_cancels_pending_tool_review(monkeypatch, capsys) -> None:
    decisions: list[tuple[str, bool]] = []

    class ReviewingConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            assert task == "review task"
            decision = kwargs["interrupt"](
                InterruptRequest("tool", "Approve tool?", {"tool": "write_file", "arguments": {}})
            )
            decisions.append((decision.choice, kwargs["cancel_requested"]()))
            return RunState(task=task, mode="agent", status="cancelled")

    class ReviewInterruptView:
        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            self.submissions = asyncio.Queue()
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.started = False
            self.interrupt_sent = False
            self.quit_sent = False

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            del text, end

        def clear(self) -> None:
            pass

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            if "| IDLE" in status and not self.started:
                self.started = True
                self.submissions.put_nowait("review task")
            elif status.startswith("TOOL REVIEW") and interrupt_enabled and not self.interrupt_sent:
                self.interrupt_sent = True
                self.interrupts.put_nowait(None)
            elif "| IDLE" in status and decisions and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

    monkeypatch.setattr(cli, "TerminalView", ReviewInterruptView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = ReviewingConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval(write=app._write)

    app.start()

    assert decisions == [("cancel", True)]
    assert capsys.readouterr().out == "No saved session.\n"


def test_interactive_cancellation_resolves_tool_review_opened_after_escape(monkeypatch, capsys) -> None:
    calls: list[str] = []
    decisions: list[tuple[str, bool]] = []
    fallback_used = False

    class LateReviewConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            calls.append(task)
            if len(calls) == 1:
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline and not kwargs["cancel_requested"]():
                    time.sleep(0.005)
                decision = kwargs["interrupt"](
                    InterruptRequest("tool", "Approve late tool?", {"tool": "write_file", "arguments": {}})
                )
                decisions.append((decision.choice, kwargs["cancel_requested"]()))
                return RunState(task=task, mode="agent", status="cancelled")
            return RunState(task=task, mode="agent", status="completed")

    class LateReviewView:
        def __init__(self, loop, **kwargs) -> None:
            del kwargs
            self.loop = loop
            self.submissions = asyncio.Queue()
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.started = False
            self.running_updates = 0
            self.quit_sent = False
            self.fallback: Timer | None = None

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            if self.fallback is not None:
                self.fallback.cancel()
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            del text, end

        def clear(self) -> None:
            pass

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            nonlocal fallback_used
            if "| IDLE" in status and not self.started:
                self.started = True
                self.submissions.put_nowait("initial task")
                return
            if "| RUNNING" in status and interrupt_enabled and len(calls) == 1:
                if self.running_updates == 0:
                    self.running_updates += 1
                    self.submissions.put_nowait("queued follow-up")
                elif self.running_updates == 1:
                    self.running_updates += 1
                    self.interrupts.put_nowait(None)

                    def force_exit() -> None:
                        nonlocal fallback_used
                        fallback_used = True
                        self.loop.call_soon_threadsafe(self.submissions.put_nowait, "/quit")

                    self.fallback = Timer(1.0, force_exit)
                    self.fallback.start()
                return
            if "| IDLE" in status and len(calls) == 2 and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

    monkeypatch.setattr(cli, "TerminalView", LateReviewView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = LateReviewConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval(write=app._write)

    app.start()

    assert fallback_used is False
    assert decisions == [("cancel", True)]
    assert calls == ["initial task", "queued follow-up"]
    assert capsys.readouterr().out == "No saved session.\n"


def test_interactive_quit_cancels_active_run_then_exits(monkeypatch, capsys) -> None:
    cancellation_seen: list[bool] = []
    calls: list[str] = []

    class CancellableConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            assert task == "long task"
            calls.append(task)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not kwargs["cancel_requested"]():
                time.sleep(0.005)
            cancellation_seen.append(kwargs["cancel_requested"]())
            return RunState(task=task, mode="agent", status="cancelled")

    class CancelView:
        last = None

        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            type(self).last = self
            self.submissions = asyncio.Queue()
            self.interrupts = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.started = False
            self.message_queued = False
            self.quit_sent = False
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def clear(self) -> None:
            self.writes.clear()

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            del interrupt_enabled
            if "| IDLE" in status and not self.started:
                self.started = True
                self.submissions.put_nowait("long task")
            elif "| RUNNING" in status and not self.message_queued:
                self.message_queued = True
                self.submissions.put_nowait("discard this queued message")
            elif "| RUNNING" in status and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

    monkeypatch.setattr(cli, "TerminalView", CancelView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = CancellableConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval()

    app.start()

    assert cancellation_seen == [True]
    assert calls == ["long task"]
    assert CancelView.last is not None
    output = "".join(CancelView.last.writes)
    assert output.count("CANCELLING — waiting for current operation") == 1
    assert "Commands are unavailable" not in output
    assert capsys.readouterr().out == "No saved session.\n"


def test_interactive_approval_bridge_resolves_on_prompt_loop(capsys) -> None:
    async def scenario():
        bridge = cli._InteractiveApproval(TerminalApproval(), asyncio.get_running_loop())
        request = InterruptRequest("tool", "Call tool?", {"tool": "write_file", "arguments": {}})
        decision_task = asyncio.create_task(asyncio.to_thread(bridge, request))
        await bridge.changed.wait()
        bridge.changed.clear()
        assert bridge.pending is True
        bridge.submit("1")
        return await decision_task

    decision = asyncio.run(scenario())

    assert decision.choice == "continue"
    assert "TOOL REVIEW" in capsys.readouterr().out


def test_interactive_approval_bridge_can_be_cancelled_for_exit() -> None:
    async def scenario():
        bridge = cli._InteractiveApproval(TerminalApproval(), asyncio.get_running_loop())
        request = InterruptRequest("tool", "Call tool?", {"tool": "run_command", "arguments": {}})
        decision_task = asyncio.create_task(asyncio.to_thread(bridge, request))
        await bridge.changed.wait()

        bridge.cancel_pending()

        return await asyncio.wait_for(decision_task, 1)

    decision = asyncio.run(scenario())

    assert decision.choice == "cancel"


def test_interactive_approval_bridge_resolves_textual_questionnaire() -> None:
    async def scenario():
        view = cli.TerminalView(asyncio.get_running_loop())
        question = UserQuestion(
            "storage",
            "Storage",
            "Where should the result be stored?",
            (
                QuestionOption("SQLite", "Use the existing database."),
                QuestionOption("JSONL", "Use the audit stream."),
            ),
        )
        async with view.run_test() as pilot:
            bridge = cli._InteractiveApproval(TerminalApproval(), asyncio.get_running_loop(), view)
            request = InterruptRequest(
                "question",
                "Answer questions.",
                {"questions": []},
                questions=(question,),
            )
            decision_task = asyncio.create_task(asyncio.to_thread(bridge, request))
            await bridge.changed.wait()
            bridge.changed.clear()
            await pilot.pause()

            assert view.questionnaire_active is True
            await pilot.press("down", "enter")
            return await asyncio.wait_for(decision_task, 1), view.questionnaire_active

    decision, active = asyncio.run(scenario())

    assert decision.choice == "answer"
    assert decision.answers == {"storage": ["JSONL"]}
    assert active is False

def test_interactive_approval_bridge_resolves_inline_tool_supplement() -> None:
    async def scenario():
        view = cli.TerminalView(asyncio.get_running_loop())
        async with view.run_test() as pilot:
            bridge = cli._InteractiveApproval(TerminalApproval(), asyncio.get_running_loop(), view)
            request = InterruptRequest(
                "tool",
                "Call tool?",
                {"tool": "write_file", "arguments": {"path": "note.txt"}},
            )
            decision_task = asyncio.create_task(asyncio.to_thread(bridge, request))
            await bridge.changed.wait()
            bridge.changed.clear()
            await pilot.pause()

            assert [row.choice.id for row in view.choice_menu.rows] == [
                "continue",
                "cancel",
                "supplement",
            ]
            assert view._top_level_nodes == []
            assert view.review_details.display is True
            await pilot.press("up", "tab")
            editor = view.choice_menu.highlighted_row.editor
            editor.value = "Use a smaller change."
            await pilot.press("enter")
            return await asyncio.wait_for(decision_task, 1)

    decision = asyncio.run(scenario())

    assert decision.choice == "supplement"
    assert decision.supplement == "Use a smaller change."


def test_interactive_approval_bridge_resolves_plan_choice_from_list() -> None:
    async def scenario():
        view = cli.TerminalView(asyncio.get_running_loop())
        async with view.run_test() as pilot:
            bridge = cli._InteractiveApproval(TerminalApproval(), asyncio.get_running_loop(), view)
            request = InterruptRequest(
                "plan",
                "Choose how to handle this plan.",
                {"plan": "1. Edit README."},
            )
            decision_task = asyncio.create_task(asyncio.to_thread(bridge, request))
            await bridge.changed.wait()
            bridge.changed.clear()
            await pilot.pause()

            assert [row.choice.id for row in view.choice_menu.rows] == [
                "implement",
                "implement_clear_session",
                "cancel",
            ]
            await pilot.press("down", "enter")
            return await asyncio.wait_for(decision_task, 1)

    decision = asyncio.run(scenario())

    assert decision.choice == "implement_clear_session"



class StubTerminalApp:
    result: RunState | None = None
    started = False

    def __init__(self, conversation, log_dir: Path) -> None:
        self.conversation = conversation
        self.log_dir = log_dir

    def run_task(self, task: str) -> RunState | None:
        return self.result

    def start(self) -> int:
        type(self).started = True
        return 0


@pytest.fixture
def stub_cli(monkeypatch):
    application = SimpleNamespace(open_conversation=lambda session_id: object())
    monkeypatch.setattr(cli, "build_application", lambda *args: application)
    monkeypatch.setattr(cli, "TerminalApp", StubTerminalApp)
    StubTerminalApp.result = None
    StubTerminalApp.started = False
    return StubTerminalApp


@pytest.mark.parametrize(
    ("status", "expected"),
    [("completed", 0), ("failed", 1), ("cancelled", 1)],
)
def test_main_maps_one_shot_status_to_exit_code(tmp_path, stub_cli, status: str, expected: int) -> None:
    stub_cli.result = RunState(task="task", mode="agent", status=status)

    assert cli.main(["--workspace", str(tmp_path), "--planner", "rule", "task"]) == expected


def test_main_returns_failure_when_task_preparation_fails(tmp_path, stub_cli) -> None:
    stub_cli.result = None

    assert cli.main(["--workspace", str(tmp_path), "--planner", "rule", "task"]) == 1


def test_main_returns_success_after_interactive_session(tmp_path, stub_cli) -> None:
    assert cli.main(["--workspace", str(tmp_path), "--planner", "rule"]) == 0
    assert stub_cli.started is True


@pytest.mark.parametrize("workspace_kind", ["missing", "file"])
def test_main_rejects_non_directory_workspace(tmp_path, capsys, workspace_kind: str) -> None:
    workspace = tmp_path / workspace_kind
    if workspace_kind == "file":
        workspace.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(workspace), "--planner", "rule"])

    error = capsys.readouterr().err
    assert exc_info.value.code == 2
    assert "workspace" in error
    assert "--planner rule" not in error


def test_main_suggests_rule_planner_only_for_model_configuration(tmp_path, monkeypatch, capsys) -> None:
    def fail_to_build(*args):
        raise ModelConfigurationError("Missing API_KEY.")

    monkeypatch.setattr(cli, "build_application", fail_to_build)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(tmp_path)])

    assert exc_info.value.code == 2
    assert "Use --planner rule for offline mode." in capsys.readouterr().err


def test_main_does_not_suggest_rule_planner_for_invalid_runtime_settings(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(tmp_path), "--max-actions", "0"])

    assert exc_info.value.code == 2
    assert "--planner rule" not in capsys.readouterr().err


def test_main_rejects_removed_plan_execute_strategy(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(tmp_path), "--strategy", "plan_execute"])

    assert exc_info.value.code == 2
    assert "invalid choice: 'plan_execute'" in capsys.readouterr().err


def test_context_usage_events_update_and_reset_the_active_view() -> None:
    class ContextView:
        def __init__(self) -> None:
            self.calls = []

        def set_context_usage(self, *args) -> None:
            self.calls.append(args)

    app = object.__new__(cli.TerminalApp)
    view = ContextView()
    app._view = view

    app._handle_runtime_event(
        RuntimeEvent(
            "context_usage",
            data={
                "estimated_tokens": 800,
                "context_size": 1_000,
                "threshold": 0.8,
            },
        )
    )
    app._handle_runtime_event(RuntimeEvent("strategy"))
    app._reset_context_usage()

    assert view.calls == [(800, 1_000, 0.8), ()]


def test_active_view_receives_runtime_events_without_presenter_output() -> None:
    class EventView:
        def __init__(self) -> None:
            self.events: list[RuntimeEvent] = []
            self.context_calls: list[tuple[object, ...]] = []

        def handle_runtime_event(self, event: RuntimeEvent) -> None:
            self.events.append(event)

        def set_context_usage(self, *args: object) -> None:
            self.context_calls.append(args)

    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._view = EventView()
    presented: list[RuntimeEvent] = []
    app.presenter = SimpleNamespace(on_event=presented.append)
    event = RuntimeEvent(
        "context_usage",
        data={"estimated_tokens": 800, "context_size": 1_000, "threshold": 0.8},
    )

    app._handle_runtime_event(event)
    app._present_runtime_event(event)

    assert app._view.events == [event]
    assert app._view.context_calls == [(800, 1_000, 0.8)]
    assert presented == []


def test_view_routes_conversations_system_output_and_history() -> None:
    class TranscriptView:
        def __init__(self) -> None:
            self.conversations: list[str] = []
            self.system: list[tuple[str, str]] = []
            self.histories: list[tuple[str, list[dict[str, str]]]] = []

        def begin_conversation(self, content: str) -> None:
            self.conversations.append(content)

        def write_system(self, text: str, end: str = "\n") -> None:
            self.system.append((text, end))

        def show_history(self, label: str, messages: list[dict[str, str]]) -> None:
            self.histories.append((label, messages))

    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    view = TranscriptView()
    app._view = view
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    app._conversation_service.session_store = object()
    app._conversation_service.active_session = SimpleNamespace(session_id="session_1", title="Session 1")
    app._conversation_service.history = lambda: history

    app._write_user_message("hello")
    app._write("SYSTEM EVENT", end="")
    app._show_history()

    assert view.conversations == ["hello"]
    assert view.system == [("SYSTEM EVENT", "")]
    assert view.histories == [("session_1 — Session 1", history)]


def test_console_output_remains_the_fallback_without_an_active_view(capsys) -> None:
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))

    app._write_user_message("hello")
    app._write("SYSTEM EVENT")

    assert capsys.readouterr().out == "USER\nhello\nSYSTEM EVENT\n"
