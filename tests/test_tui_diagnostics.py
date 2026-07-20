from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mini_agent.runtime import RuntimeEvent
from mini_agent.tui import cli
from mini_agent.tui.approval import TerminalApproval
from mini_agent.tui.diagnostics import TuiDiagnosticLogger
from mini_agent.tui.exit_reporting import classify_tui_exit
from mini_agent.tui.view import TerminalView


def _records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _terminal_app(log_dir) -> cli.TerminalApp:
    app = object.__new__(cli.TerminalApp)
    app.last_state = None
    app.mode = "agent"
    app._view = None
    app._log_dir = log_dir
    app._tui_diagnostics = None
    app._conversation_service = SimpleNamespace(
        active_session=None,
        pending_session_title=None,
        conversation=[],
    )
    app._event_sink = lambda _event: None
    app._approval = TerminalApproval()
    return app


class _ImmediateExitView:
    error: BaseException | None = None
    return_code: int | None = None
    task_error: BaseException | None = None

    def __init__(self, _loop, *, diagnostic_sink=None, **_kwargs) -> None:
        self.submissions = asyncio.Queue()
        self.interrupts = asyncio.Queue()
        self.unhandled_exception = type(self).error
        self.return_code = type(self).return_code
        self._diagnostic_sink = diagnostic_sink

    async def run_async(self) -> None:
        if self.task_error is not None:
            raise self.task_error

    def diagnostic_snapshot(self) -> dict[str, object]:
        return {"transcript_nodes": 3, "response_chars": 17}

    def set_ui(self, **_kwargs) -> None:
        pass

    def stop(self) -> None:
        pass

    def write(self, _text: str, _end: str = "\n") -> None:
        pass


@pytest.mark.parametrize(
    ("error", "return_code", "task_error", "reason", "error_text"),
    [
        (RuntimeError("render failed"), 1, None, "textual_exception", "render failed"),
        (None, None, ValueError("task failed"), "task_exception", "task failed"),
        (None, None, None, "unexpected_exit", None),
    ],
)
def test_interactive_exit_failure_is_logged_and_returned_once(
    tmp_path,
    monkeypatch,
    capsys,
    error: BaseException | None,
    return_code: int | None,
    task_error: BaseException | None,
    reason: str,
    error_text: str | None,
) -> None:
    _ImmediateExitView.error = error
    _ImmediateExitView.return_code = return_code
    _ImmediateExitView.task_error = task_error
    monkeypatch.setattr(cli, "TerminalView", _ImmediateExitView)
    app = _terminal_app(tmp_path / "logs")

    assert app.start() == 1

    output = capsys.readouterr().out
    assert output.count("TUI ERROR") == 1
    assert reason in output
    assert output.count("TUI DIAGNOSTICS") == 1
    paths = list((tmp_path / "logs").glob("tui_*.jsonl"))
    assert len(paths) == 1
    records = _records(paths[0])
    assert records[-1]["kind"] == "tui_exit"
    assert records[-1]["data"]["reason"] == reason
    assert records[-1]["data"]["transcript_nodes"] == 3
    if error_text is not None:
        assert error_text in records[-1]["error"]["traceback"]


def test_normal_interactive_exit_returns_zero_without_error(tmp_path, monkeypatch, capsys) -> None:
    class NormalExitView(_ImmediateExitView):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.stopped = asyncio.Event()
            self.submissions.put_nowait(None)

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

    monkeypatch.setattr(cli, "TerminalView", NormalExitView)
    app = _terminal_app(tmp_path / "logs")

    assert app.start() == 0

    assert "TUI ERROR" not in capsys.readouterr().out
    records = _records(next((tmp_path / "logs").glob("tui_*.jsonl")))
    assert records[-1]["data"]["reason"] == "normal"


def test_real_textual_callback_exception_is_preserved_and_logged(tmp_path) -> None:
    async def scenario():
        logger = TuiDiagnosticLogger(tmp_path / "logs")
        view = TerminalView(diagnostic_sink=logger.record)
        view_task = asyncio.create_task(view.run_async(headless=True, size=(80, 20)))
        await view._mounted_event.wait()

        def fail() -> None:
            raise RuntimeError("response callback failed")

        view._run_on_owner(fail, diagnostic_name="runtime_event:response_delta")
        await asyncio.wait_for(view_task, 1)
        report = classify_tui_exit(
            view,
            task_error=None,
            view_ended_early=True,
            normal_exit_requested=False,
        )
        logger.record("tui_exit", {"reason": report.reason, **report.snapshot}, report.error)
        logger.close()
        return logger.path, report

    path, report = asyncio.run(scenario())

    assert report.reason == "textual_exception"
    assert report.exit_code == 1
    records = _records(path)
    assert [record["kind"] for record in records].count("owner_callback_failed") == 1
    assert records[-1]["kind"] == "tui_exit"
    assert "response callback failed" in records[-1]["error"]["traceback"]


def test_message_loop_cancelled_error_keeps_origin_traceback(tmp_path) -> None:
    async def scenario() -> TuiDiagnosticLogger:
        logger = TuiDiagnosticLogger(tmp_path / "logs")
        view = TerminalView(diagnostic_sink=logger.record)
        view_task = asyncio.create_task(view.run_async(headless=True, size=(80, 20)))
        await view._mounted_event.wait()

        def cancel_dispatch() -> None:
            raise asyncio.CancelledError("synthetic dispatch cancellation")

        view._run_on_owner(cancel_dispatch, diagnostic_name="synthetic_cancel")
        await asyncio.wait_for(view_task, 1)
        logger.close()
        return logger

    logger = asyncio.run(scenario())
    records = _records(logger.path)
    cancelled = next(record for record in records if record["kind"] == "message_loop_cancelled")

    assert cancelled["data"]["task_cancelling"] == 0
    assert "cancel_dispatch" in cancelled["data"]["traceback"]
    assert "synthetic dispatch cancellation" in cancelled["error"]["traceback"]


def test_textual_quit_action_records_exit_call_stack(tmp_path) -> None:
    async def scenario() -> TuiDiagnosticLogger:
        logger = TuiDiagnosticLogger(tmp_path / "logs")
        view = TerminalView(diagnostic_sink=logger.record)
        view_task = asyncio.create_task(view.run_async(headless=True, size=(80, 20)))
        await view._mounted_event.wait()

        await view.action_quit()
        await asyncio.wait_for(view_task, 1)
        logger.close()
        return logger

    logger = asyncio.run(scenario())
    records = _records(logger.path)
    kinds = [record["kind"] for record in records]

    assert kinds.count("quit_action") == 1
    assert kinds.count("view_exit_called") == 1
    assert kinds.count("exit_app_message") == 1
    exit_record = next(record for record in records if record["kind"] == "view_exit_called")
    assert "action_quit" in exit_record["data"]["stack"]




@pytest.mark.parametrize(("log_full_messages", "contains_message"), [(True, True), (False, False)])
def test_hidden_system_output_respects_full_message_policy(
    tmp_path,
    log_full_messages: bool,
    contains_message: bool,
) -> None:
    secret = "SYSTEM_SECRET_TEXT"
    logger = TuiDiagnosticLogger(tmp_path / "logs")
    view = TerminalView(
        diagnostic_sink=logger.record,
        log_full_messages=log_full_messages,
    )

    view.write_system(secret, end="")
    logger.close()

    raw = logger.path.read_text(encoding="utf-8")
    record = next(item for item in _records(logger.path) if item["kind"] == "system_output_hidden")
    assert record["data"]["hidden"] is True
    assert record["data"]["message_chars"] == len(secret)
    assert record["data"]["end"] == ""
    assert ("message" in record["data"]) is contains_message
    assert (secret in raw) is contains_message


def test_stream_and_tool_diagnostics_exclude_content_and_arguments(tmp_path) -> None:
    secret_response = "SECRET_RESPONSE_CONTENT"
    secret_argument = "SECRET_TOOL_ARGUMENT"

    async def scenario() -> TuiDiagnosticLogger:
        logger = TuiDiagnosticLogger(tmp_path / "logs")
        view = TerminalView(diagnostic_sink=logger.record)
        async with view.run_test(size=(80, 20)) as pilot:
            view.begin_conversation("safe task")
            view.handle_runtime_event(RuntimeEvent("run_started", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_start", data={"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_delta", secret_response, {"run_id": "run-1"}))
            view.handle_runtime_event(RuntimeEvent("response_end", data={"run_id": "run-1"}))
            view.handle_runtime_event(
                RuntimeEvent(
                    "tool_call",
                    "read_file",
                    {"run_id": "run-1", "call_id": "call-1", "arguments": {"path": secret_argument}},
                )
            )
            await pilot.pause()
            await pilot.pause()
        logger.close()
        return logger

    logger = asyncio.run(scenario())
    raw = logger.path.read_text(encoding="utf-8")
    records = _records(logger.path)

    assert secret_response not in raw
    assert secret_argument not in raw
    final_stream = [record for record in records if record["kind"] == "runtime_stream_progress"][-1]
    assert final_stream["data"]["characters"] == len(secret_response)
    assert final_stream["data"]["final"] is True


def test_diagnostic_logger_serializes_concurrent_records(tmp_path) -> None:
    logger = TuiDiagnosticLogger(tmp_path / "logs")
    logger.set_context(session_id="session-1", run_id="run-1")

    def write(worker: int) -> None:
        for item in range(25):
            logger.record("concurrent", {"worker": worker, "item": item})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(4)))
    logger.close()

    records = _records(logger.path)
    assert len(records) == 100
    assert [record["sequence"] for record in records] == list(range(1, 101))
    assert {record["session_id"] for record in records} == {"session-1"}
    assert {record["run_id"] for record in records} == {"run-1"}


def test_exit_classifier_distinguishes_normal_and_nonzero_return_code() -> None:
    normal = SimpleNamespace(unhandled_exception=None, return_code=0)
    failed = SimpleNamespace(unhandled_exception=None, return_code=3)
    eof = SimpleNamespace(unhandled_exception=EOFError(), return_code=1)

    assert (
        classify_tui_exit(
            normal,
            task_error=None,
            view_ended_early=False,
            normal_exit_requested=True,
        ).reason
        == "normal"
    )
    assert (
        classify_tui_exit(
            failed,
            task_error=None,
            view_ended_early=False,
            normal_exit_requested=True,
        ).reason
        == "unexpected_exit"
    )
    assert (
        classify_tui_exit(
            eof,
            task_error=None,
            view_ended_early=True,
            normal_exit_requested=False,
        ).reason
        == "normal"
    )
