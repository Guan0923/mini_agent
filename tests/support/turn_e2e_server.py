"""Local deterministic backend used by the Playwright Turn protocol flow."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.api.app import create_app  # noqa: E402
from backend.api.chat import routes as chat_routes  # noqa: E402
from backend.api.routes import turns as turn_routes  # noqa: E402
from backend.api.state import WebAppState  # noqa: E402
from backend.domain import AssistantMessage, ToolMessage  # noqa: E402
from backend.mcp.client import start_external_tools  # noqa: E402
from backend.mcp.config import McpServerConfig  # noqa: E402
from backend.planning import LLMPlanner, RuleBasedPlanner  # noqa: E402
from backend.providers import LLMClient, ModelConfig  # noqa: E402
from backend.runtime import build_application  # noqa: E402
from backend.runtime.application import factory as application_factory  # noqa: E402
from backend.runtime.core.context import PreparedResponse  # noqa: E402
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME  # noqa: E402
from backend.sandbox import BrokerStatus  # noqa: E402
from backend.tools import Tool, ToolRegistry  # noqa: E402
from backend.tools.default_tools.todo import todo_tools  # noqa: E402

_temporary_root = tempfile.TemporaryDirectory(prefix="mini-agent-turn-e2e-")
_root = Path(_temporary_root.name)


class SwitchableTestBroker:
    """In-memory Broker control plane; it never performs privileged host operations."""

    def __init__(self) -> None:
        self._status = BrokerStatus(True, True, version="e2e", installation_id="e2e")

    def status(self) -> BrokerStatus:
        return self._status

    def set_status(self, *, installed: bool, healthy: bool, detail: str | None = None) -> BrokerStatus:
        self._status = BrokerStatus(installed, healthy, version="e2e", installation_id="e2e", detail=detail)
        return self._status

    def install(self) -> BrokerStatus:
        return self.set_status(installed=True, healthy=True)

    def repair(self) -> BrokerStatus:
        return self.set_status(installed=True, healthy=True)


sandbox_broker = SwitchableTestBroker()


class TestBrokerFactory:
    @classmethod
    def from_system(cls):
        return sandbox_broker


application_factory.WindowsBrokerClient = TestBrokerFactory
state = WebAppState(_root / "web", sandbox_broker=sandbox_broker)
state.model_config = lambda _provider_name=None: ModelConfig(
    "test-key",
    "https://example.test/v1",
    "deterministic-e2e",
)

TRACE_MODEL_PORT = int(os.environ.get("MINI_AGENT_E2E_MODEL_PORT", "18081"))
TRACE_MODEL_CONFIG = ModelConfig(
    "local-test-key",
    f"http://127.0.0.1:{TRACE_MODEL_PORT}/v1",
    "trace-e2e-model",
    max_tokens=512,
    context_size=8_192,
    provider_name="trace-http",
)
TRACE_MODEL_CALLS = 0
TRACE_MCP_TOOL_NAME = ""


class TraceModelHandler(BaseHTTPRequestHandler):
    """Deterministic loopback Chat Completions endpoint with streaming support."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        global TRACE_MODEL_CALLS

        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        TRACE_MODEL_CALLS += 1
        has_tool_result = any(
            isinstance(message, dict) and message.get("role") == "tool" for message in payload.get("messages", [])
        )
        if payload.get("stream"):
            events = (
                [
                    {
                        "id": "trace-e2e",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "trace-e2e-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "trace_mcp_call",
                                            "function": {
                                                "name": TRACE_MCP_TOOL_NAME,
                                                "arguments": json.dumps({"label": "incremental trace"}),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": None,
                    },
                    {"choices": [], "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}},
                ]
                if not has_tool_result
                else [
                    {
                        "id": "trace-e2e",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "trace-e2e-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "reasoning_content": "Trace reasoning from HTTP."},
                                "finish_reason": None,
                            }
                        ],
                        "usage": None,
                    },
                    {
                        "id": "trace-e2e",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "trace-e2e-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "Trace response from HTTP."},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": None,
                    },
                    {"choices": [], "usage": {"input_tokens": 8, "output_tokens": 6, "total_tokens": 14}},
                ]
            )
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        body = json.dumps(
            {
                "id": "trace-e2e",
                "object": "chat.completion",
                "created": 1,
                "model": "trace-e2e-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "Trace reasoning from HTTP.",
                            "content": "Trace response from HTTP.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 6, "total_tokens": 14},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


trace_model_server = ThreadingHTTPServer(("127.0.0.1", TRACE_MODEL_PORT), TraceModelHandler)
threading.Thread(target=trace_model_server.serve_forever, name="trace-e2e-model", daemon=True).start()

trace_skill = state.paths.skills_dir / "trace-audit"
trace_skill.mkdir(parents=True, exist_ok=True)
(trace_skill / "SKILL.md").write_text(
    "---\nname: trace-audit\ndescription: Audit the Trace page.\n---\n\n"
    "Confirm that the complete local Skill instructions are visible in Trace.\n",
    encoding="utf-8",
)
trace_mcp_resources = start_external_tools(
    (
        McpServerConfig(
            "trace",
            sys.executable,
            (str(ROOT / "tests" / "support" / "trace_mcp_server.py"),),
            cwd=str(ROOT),
        ),
    )
)
trace_mcp_tool = trace_mcp_resources[0]
TRACE_MCP_TOOL_NAME = trace_mcp_tool.spec.name


STRUCTURED_CHECKPOINT = """## Primary Request and Intent
- Preserve the browser task.

## Key Technical Concepts
- RuntimeState Turn tree and synchronous Compact.

## Files and Code
- frontend/e2e/turn-flow.spec.ts: real browser Compact flow.

## Errors and Fixes
- (none)

## Pending Jobs
- (none)

## Current Work
- Compacting the selected Turn.

## Next Step
- Continue from the checkpoint.

## Critical Context
- The summary was generated by the deterministic test model."""

PLAN_REVIEW_TASK = "plan review compact"
PLAN_REVIEW_MARKDOWN = """# Compact implementation plan

1. Preserve the reviewed Plan Turn.
2. Compact the conversation context.
3. Implement from this exact plan text."""
APPROVED_PLAN_TASK = f"<approved_plan>\n{PLAN_REVIEW_MARKDOWN}\n</approved_plan>"

ORDERED_REASONING = "推理内容持续更新并保持右侧最新字符可见。" * 12


class DeterministicCompactionClient:
    """Local fake model that exercises the real non-streaming LLM summary path."""

    context_size = 100_000

    def estimate_tokens(self, messages, tools, request_parameters) -> int:
        return 100

    def run(self, runtime) -> PreparedResponse:
        if runtime.exchange.operation == "title":
            return PreparedResponse(AssistantMessage(content="“浏览器生成的新标题很长”"), {"total_tokens": 1})
        if runtime.exchange.operation != "summarize":
            raise RuntimeError("The E2E model only supports title and compaction requests.")
        sleep(0.5)
        return PreparedResponse(AssistantMessage(content=STRUCTURED_CHECKPOINT), {"total_tokens": 1})


class CooperativePausePlanner(LLMPlanner):
    """Hold one deterministic request until the browser asks to pause it."""

    def __init__(self) -> None:
        super().__init__(DeterministicCompactionClient(), [], [])
        self._rule_planner = RuleBasedPlanner()
        self._trace_planner = LLMPlanner(
            LLMClient(TRACE_MODEL_CONFIG),
            [trace_mcp_tool.spec],
            [trace_mcp_tool.spec],
            user_preferences="Trace E2E preference: concise local audit.",
        )

    def decide(self, runtime):
        task = runtime.run.task.strip()
        if "trace audit e2e" in task:
            return self._trace_planner.decide(runtime)
        if runtime.run.mode == "plan" and task == PLAN_REVIEW_TASK:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name=REQUEST_PLAN_REVIEW_NAME,
                        call_id="review_compact_e2e",
                        arguments={"plan": PLAN_REVIEW_MARKDOWN},
                    )
                ]
            )
        if runtime.run.mode == "agent" and task == APPROVED_PLAN_TASK:
            return AssistantMessage(content="Implemented the exact reviewed plan after compaction.")
        if task == "steering fifo":
            if runtime.run.model_turns <= 2:
                sleep(3.0 if runtime.run.model_turns == 1 else 0.5)
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="read_file",
                            call_id=f"stale_{runtime.run.model_turns}",
                            arguments={"path": "README.md"},
                        )
                    ]
                )
            return AssistantMessage(content="FIFO steering complete.")
        if task == "steering merge":
            if runtime.run.model_turns == 1:
                sleep(4.0)
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(name="read_file", call_id="stale_merge", arguments={"path": "README.md"})
                    ]
                )
            return AssistantMessage(content="Merged steering complete.")
        if task == "steering during tool":
            if runtime.run.model_turns == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(name="slow_tool", call_id="slow_steering", arguments={}),
                        ToolMessage(name="forbidden_tool", call_id="forbidden_steering", arguments={}),
                    ]
                )
            return AssistantMessage(content="Tool-boundary steering complete.")
        if task == "delayed reconnect":
            if runtime.exchange.on_reasoning is not None:
                runtime.exchange.on_reasoning("Streaming began before refresh.")
            sleep(4.0)
            if runtime.exchange.on_content is not None:
                runtime.exchange.on_content("Streaming finished after refresh.")
            return AssistantMessage(
                reasoning="Streaming began before refresh.",
                content="Streaming finished after refresh.",
            )
        if task == "approval presentation":
            if runtime.run.model_turns == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="web_search",
                            call_id="approval_search",
                            arguments={"query": "local approval"},
                        )
                    ]
                )
            return AssistantMessage(content="Approval flow complete.")
        if task == "ordered items":
            model_turn = runtime.run.model_turns
            if model_turn == 1:
                if runtime.exchange.on_reasoning is not None:
                    runtime.exchange.on_reasoning(ORDERED_REASONING)
                sleep(1.0)
                return AssistantMessage(
                    reasoning=ORDERED_REASONING,
                    tool_messages=[ToolMessage(name="slow_tool", call_id="ordered_read", arguments={})],
                )
            if model_turn == 2:
                if runtime.exchange.on_content is not None:
                    runtime.exchange.on_content("The first tool completed. ")
                return AssistantMessage(
                    content="The first tool completed. ",
                    tool_messages=[ToolMessage(name="glob", call_id="ordered_glob", arguments={"pattern": "*.md"})],
                )
            if runtime.exchange.on_reasoning is not None:
                runtime.exchange.on_reasoning("Combine both tool results.")
            if runtime.exchange.on_content is not None:
                runtime.exchange.on_content("Ordered flow complete.")
            return AssistantMessage(
                reasoning="Combine both tool results.",
                content="Ordered flow complete.",
            )
        if task == "todo abnormal close":
            if runtime.run.model_turns == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="todo_write",
                            call_id="todo_abnormal_pending",
                            arguments={
                                "todos": [
                                    {"content": "Inspect the abnormal Turn", "status": "in_progress"},
                                    {"content": "Finish the interrupted work", "status": "pending"},
                                ]
                            },
                        )
                    ]
                )
            sleep(1.5)
            return AssistantMessage(content="The Turn ended before its Todo list completed.")
        if task == "todo completed auto close":
            if runtime.run.model_turns == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="todo_write",
                            call_id="todo_complete_running",
                            arguments={
                                "todos": [{"content": "Complete the browser lifecycle", "status": "in_progress"}]
                            },
                        )
                    ]
                )
            if runtime.run.model_turns == 2:
                sleep(1.5)
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="todo_write",
                            call_id="todo_complete_done",
                            arguments={"todos": [{"content": "Complete the browser lifecycle", "status": "completed"}]},
                        )
                    ]
                )
            sleep(1.0)
            return AssistantMessage(content="The Todo list completed normally.")
        if task != "pause and resume":
            return self._rule_planner.decide(runtime)
        if runtime.run.provenance.attempt > 1:
            return AssistantMessage(content="Resumed the same Turn successfully.")
        partial = "Partial output preserved before pause."
        if runtime.exchange.on_content is not None:
            runtime.exchange.on_content(partial)
        deadline = monotonic() + 15
        while monotonic() < deadline:
            stop_requested = runtime.services.suspend_requested or runtime.services.cancel_requested
            if stop_requested is not None and stop_requested():
                return AssistantMessage(content=partial)
            sleep(0.02)
        return AssistantMessage(content="Pause request timed out.")


def local_application(_state, *, session_id: str, workspace=None, **_kwargs):
    resolved_workspace = Path(workspace or state.session_workspace(session_id))
    resolved_workspace.mkdir(parents=True, exist_ok=True)
    (resolved_workspace / "README.md").write_text(
        "Mini-Agent is a local-first Agent application.\n",
        encoding="utf-8",
    )
    application = build_application(
        resolved_workspace,
        planner_name="rule",
        paths=state.paths,
    )
    application.runner.planner = CooperativePausePlanner()

    def slow_tool() -> str:
        sleep(2.0)
        return "Slow tool completed."

    application.runner.tools = ToolRegistry(
        [
            Tool(
                "read_file",
                "Read a deterministic workspace file.",
                lambda path: (resolved_workspace / path).read_text(encoding="utf-8"),
            ),
            Tool(
                "glob",
                "List deterministic workspace files.",
                lambda pattern: "\n".join(
                    str(path.relative_to(resolved_workspace)) for path in resolved_workspace.glob(pattern)
                ),
            ),
            Tool(
                "web_search",
                "Return a deterministic local search result.",
                lambda query: f"Deterministic search result for {query}.",
                requires_confirmation=True,
            ),
            Tool("slow_tool", "Run one deterministic slow tool.", slow_tool),
            Tool("forbidden_tool", "Must be skipped after steering.", lambda: "Forbidden tool executed."),
            *todo_tools(),
            trace_mcp_tool,
        ]
    )
    return application


chat_routes.build_local_application = local_application
turn_routes.build_local_application = local_application
app = create_app(state)


@app.post("/api/test/trace-model-reset")
def reset_trace_model_calls() -> dict[str, int]:
    global TRACE_MODEL_CALLS

    TRACE_MODEL_CALLS = 0
    return {"calls": TRACE_MODEL_CALLS}


@app.get("/api/test/trace-model-calls")
def get_trace_model_calls() -> dict[str, int]:
    return {"calls": TRACE_MODEL_CALLS}


@app.post("/api/test/sandbox-status")
def set_sandbox_status(values: dict[str, object]) -> dict[str, object]:
    return sandbox_broker.set_status(
        installed=values.get("installed") is True,
        healthy=values.get("healthy") is True,
        detail=str(values["detail"]) if values.get("detail") else None,
    ).to_dict()


# create_app may mount a built frontend at "/". Keep the three test-only
# control routes ahead of that catch-all mount in the Starlette route table.
for _test_route in range(3):
    app.router.routes.insert(0, app.router.routes.pop())


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("MINI_AGENT_E2E_PORT", "18080")),
        log_level="warning",
    )
