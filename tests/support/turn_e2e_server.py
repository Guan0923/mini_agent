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
from uuid import uuid4

import uvicorn
from redis import ConnectionPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.api.app import create_app  # noqa: E402
from backend.api.chat import routes as chat_routes  # noqa: E402
from backend.api.routes import turns as turn_routes  # noqa: E402
from backend.api.session_files.routes import _store_for as session_file_store  # noqa: E402
from backend.api.session_store import session_store  # noqa: E402
from backend.api.state import WebAppState  # noqa: E402
from backend.domain import AssistantMessage, ToolMessage, TurnTrace, TurnTraceContext, TurnTraceItem  # noqa: E402
from backend.domain.runtime_state import NodeWriter, RuntimeState, terminal_error_payload  # noqa: E402
from backend.mcp.client import start_external_tools  # noqa: E402
from backend.mcp.config import McpServerConfig  # noqa: E402
from backend.planning import LLMPlanner, RuleBasedPlanner  # noqa: E402
from backend.providers import LLMClient, ModelConfig  # noqa: E402
from backend.runtime import AgentRunner, build_application  # noqa: E402
from backend.runtime.application import factory as application_factory  # noqa: E402
from backend.runtime.core.context import PreparedResponse  # noqa: E402
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME  # noqa: E402
from backend.sandbox import BrokerStatus  # noqa: E402
from backend.tools import Tool, ToolRegistry, delegation_tools  # noqa: E402
from backend.tools.default_tools.filesystem import filesystem_read_tools  # noqa: E402
from backend.tools.default_tools.todo import todo_tools  # noqa: E402
from backend.tools.filesystem import WorkspaceFiles  # noqa: E402

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
    def from_system(cls, *, expected_proxy_port: int = 17831):
        _ = expected_proxy_port
        return sandbox_broker


application_factory.WindowsBrokerClient = TestBrokerFactory
state = WebAppState(_root / "web", sandbox_broker=sandbox_broker)
state.model_config = lambda _provider_name=None: ModelConfig(
    "test-key",
    "https://example.test/v1",
    "deterministic-e2e",
)

_redis_key_prefix = os.environ.get("MINI_AGENT_REDIS_KEY_PREFIX", "")
_redis_client = state.message_queue.client
_online_redis_pool = _redis_client.connection_pool
_offline_redis_pool: ConnectionPool | None = None
_close_state = state.close


def set_e2e_redis_available(available: bool) -> None:
    global _offline_redis_pool

    if available:
        _redis_client.connection_pool = _online_redis_pool
        if _offline_redis_pool is not None:
            _offline_redis_pool.disconnect()
            _offline_redis_pool = None
        return
    if _offline_redis_pool is None:
        _offline_redis_pool = ConnectionPool.from_url(
            "redis://127.0.0.1:63999/0",
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
    _redis_client.connection_pool = _offline_redis_pool


def close_e2e_state() -> None:
    """Delete only this Playwright run's randomized Redis namespace."""

    try:
        set_e2e_redis_available(True)
        if _redis_key_prefix.startswith("mini-agent:e2e:"):
            client = getattr(state.message_queue, "client", None)
            if client is not None:
                keys = list(client.scan_iter(f"{_redis_key_prefix}:*"))
                if keys:
                    client.delete(*keys)
    finally:
        _close_state()


state.close = close_e2e_state

TRACE_MODEL_PORT = int(os.environ.get("MINI_AGENT_E2E_MODEL_PORT", "18081"))
TRACE_MODEL_CONFIG = ModelConfig(
    "local-test-key",
    f"http://127.0.0.1:{TRACE_MODEL_PORT}/v1",
    "trace-e2e-model",
    max_tokens=512,
    context_size=128_000,
    provider_name="trace-http",
)
TRACE_MODEL_CALLS = 0
TRACE_MODEL_LAST_REQUEST: dict[str, object] = {}
RETRY_MODEL_CALLS = 0
TRACE_MCP_TOOL_NAME = ""
AGENT_THREAD_NAV_TASK = "agent thread navigation e2e"
AGENT_THREAD_DIRECT_TASK = "agent thread direct e2e"
AGENT_THREAD_NESTED_TASK = "agent thread nested e2e"
AGENT_THREAD_RESPONSE = "Agent Thread response from local HTTP."
RAW_CHUNKED_ERROR_TASK = "raw chunked error e2e"
PAUSED_CHUNKED_TASK = "pause chunked stream e2e"
RETRY_VISIBILITY_TASK = "network retry visibility e2e"


class TraceModelHandler(BaseHTTPRequestHandler):
    """Deterministic loopback Chat Completions endpoint with streaming support."""

    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        global RETRY_MODEL_CALLS, TRACE_MODEL_CALLS, TRACE_MODEL_LAST_REQUEST

        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        TRACE_MODEL_CALLS += 1
        TRACE_MODEL_LAST_REQUEST = payload
        messages = payload.get("messages", [])
        latest_user = next(
            (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"), -1
        )
        user_text = messages[latest_user].get("content", "") if latest_user >= 0 else ""
        if isinstance(user_text, str) and user_text.startswith("scoped-read-e2e "):
            tool_result = next(
                (
                    message.get("content", "")
                    for message in messages[latest_user + 1 :]
                    if message.get("role") == "tool"
                ),
                None,
            )
            message = {"role": "assistant", "content": tool_result}
            finish_reason = "stop"
            if tool_result is None:
                message["tool_calls"] = [
                    {
                        "id": f"scoped_read_{uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": user_text.removeprefix("scoped-read-e2e ").strip()}),
                        },
                    }
                ]
                finish_reason = "tool_calls"
            response = {
                "id": "scoped-path-e2e",
                "object": "chat.completion",
                "created": 1,
                "model": "trace-e2e-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            }
            if payload.get("stream"):
                if "tool_calls" in message:
                    message["tool_calls"][0]["index"] = 0
                response["choices"] = [{"index": 0, "delta": message, "finish_reason": finish_reason}]
                body = (f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps(response).encode("utf-8")
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        serialized_messages = json.dumps(payload.get("messages", []), ensure_ascii=False).lower()
        agent_thread_request = AGENT_THREAD_NESTED_TASK in serialized_messages
        raw_chunked_error_request = RAW_CHUNKED_ERROR_TASK in serialized_messages
        paused_chunked_request = PAUSED_CHUNKED_TASK in serialized_messages
        retry_visibility_request = RETRY_VISIBILITY_TASK in serialized_messages
        if retry_visibility_request:
            RETRY_MODEL_CALLS += 1
        has_tool_result = any(
            isinstance(message, dict) and message.get("role") == "tool" for message in payload.get("messages", [])
        )
        if retry_visibility_request and RETRY_MODEL_CALLS == 1:
            body = json.dumps({"error": {"message": "temporary local provider outage"}}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)
            return
        if retry_visibility_request and payload.get("stream"):
            events = [
                {
                    "id": "retry-visibility-e2e",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "trace-e2e-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Retry recovered from local HTTP."},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": None,
                },
                {"choices": [], "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11}},
            ]
            body = ("".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload.get("stream"):
            if raw_chunked_error_request or paused_chunked_request:
                event = {
                    "id": "raw-chunked-error-e2e",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "trace-e2e-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                ("content" if paused_chunked_request else "reasoning_content"): (
                                    "Partial output before pausing the HTTP stream."
                                    if paused_chunked_request
                                    else "Partial event before the connection drops."
                                ),
                            },
                            "finish_reason": None,
                        }
                    ],
                    "usage": None,
                }
                encoded = f"data: {json.dumps(event)}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(f"{len(encoded):X}\r\n".encode("ascii"))
                self.wfile.write(encoded)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                if paused_chunked_request:
                    sleep(5.0)
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except OSError:
                        pass
                    return
                self.close_connection = True
                return
            events = (
                [
                    {
                        "id": "agent-thread-e2e",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "trace-e2e-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "reasoning_content": "Agent Thread reasoning from local HTTP.",
                                },
                                "finish_reason": None,
                            }
                        ],
                        "usage": None,
                    },
                    {
                        "id": "agent-thread-e2e",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "trace-e2e-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": AGENT_THREAD_RESPONSE},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": None,
                    },
                    {"choices": [], "usage": {"input_tokens": 6, "output_tokens": 5, "total_tokens": 11}},
                ]
                if agent_thread_request
                else [
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


def _agent_succeeded(runtime, thread_path: str) -> bool:
    # A fork inherits earlier Turns. Only tool results produced for this Turn
    # may satisfy the new branch's Agent wait condition.
    current_turn_messages = runtime.run.history[runtime.run.turn_start_index :]
    for message in current_turn_messages:
        if not isinstance(message, AssistantMessage):
            continue
        for tool_message in message.tool_messages:
            if tool_message.name != "get_thread_node" or not tool_message.content:
                continue
            try:
                nodes = json.loads(tool_message.content)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(nodes, list) and any(
                isinstance(node, dict)
                and node.get("thread_path") == thread_path
                and node.get("thread_status") == "success"
                for node in nodes
            ):
                return True
    return False


def _agent_reported(runtime, thread_path: str) -> bool:
    prefix = f"thread_path: {thread_path}\nthread_status: success\n"
    return any(
        isinstance(message, AssistantMessage)
        and message.name == "subagent_report"
        and message.content.startswith(prefix)
        for message in runtime.model_messages()
    )


def _todo_id_from_result(runtime, call_id: str) -> str:
    for tool in reversed(runtime.run.actions):
        if tool.call_id != call_id or tool.name != "update_todo_list" or tool.status != "succeeded":
            continue
        result = json.loads(tool.content or "{}")
        todos = result.get("todos")
        if isinstance(todos, list) and todos and isinstance(todos[0], dict):
            todo_id = todos[0].get("id")
            if isinstance(todo_id, str):
                return todo_id
    raise RuntimeError(f"Missing successful Todo result for {call_id}.")


class AgentThreadE2EPlanner:
    """Create one nested Agent, then use the real loopback model for its Turns."""

    name = "agent-thread-e2e"

    def __init__(self) -> None:
        readonly_tool = delegation_tools()[3].spec
        self._model_planner = LLMPlanner(LLMClient(TRACE_MODEL_CONFIG), [readonly_tool], [readonly_tool])

    def decide(self, runtime):
        task = runtime.run.task.strip()
        if task != AGENT_THREAD_DIRECT_TASK:
            return self._model_planner.decide(runtime)
        if runtime.run.model_turns == 1:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="delegate_tasks",
                        call_id="delegate_nested_e2e",
                        arguments={
                            "subagent_path": "/root/direct/nested",
                            "subagent_task": AGENT_THREAD_NESTED_TASK,
                            "context_transfer_strategy": "independent",
                        },
                    )
                ]
            )
        if _agent_reported(runtime, "/root/direct/nested"):
            return AssistantMessage(content="Direct Agent observed the nested result.")
        return AssistantMessage(
            tool_messages=[
                ToolMessage(
                    name="pause_current_turn",
                    call_id=f"pause_for_nested_e2e_{runtime.run.model_turns}",
                    arguments={},
                )
            ]
        )


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
        if task == "provider parameters e2e":
            return LLMPlanner(
                LLMClient(state.settings.model_config(runtime.state.provider_name)),
                [trace_mcp_tool.spec],
                [trace_mcp_tool.spec],
            ).decide(runtime)
        if task.startswith("scoped-read-e2e "):
            files = WorkspaceFiles(
                Path(runtime.state.workspace_root),
                project_workspace=Path(runtime.state.project_cwd) if runtime.state.project_cwd else None,
            )
            specs = [tool.spec for tool in filesystem_read_tools(files)]
            return LLMPlanner(LLMClient(TRACE_MODEL_CONFIG), specs, specs).decide(runtime)
        if task == AGENT_THREAD_NAV_TASK:
            if runtime.run.model_turns == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="delegate_tasks",
                            call_id="delegate_direct_e2e",
                            arguments={
                                "subagent_path": "/root/direct",
                                "subagent_task": AGENT_THREAD_DIRECT_TASK,
                                "context_transfer_strategy": "independent",
                            },
                        )
                    ]
                )
            if _agent_succeeded(runtime, "/root/direct"):
                return AssistantMessage(content="Agent Thread tree is ready.")
            sleep(0.05)
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="get_thread_node",
                        call_id=f"wait_direct_e2e_{runtime.run.model_turns}",
                        arguments={},
                    )
                ]
            )
        if "trace audit e2e" in task:
            return self._trace_planner.decide(runtime)
        if task == RAW_CHUNKED_ERROR_TASK:
            return self._trace_planner.decide(runtime)
        if task == PAUSED_CHUNKED_TASK:
            return self._trace_planner.decide(runtime)
        if task == RETRY_VISIBILITY_TASK:
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
                            name="update_todo_list",
                            call_id="todo_abnormal_pending",
                            arguments={
                                "expected_revision": 0,
                                "operations": [
                                    {
                                        "op": "add",
                                        "content": "Inspect the abnormal Turn",
                                        "status": "in_progress",
                                    },
                                    {
                                        "op": "add",
                                        "content": "Finish the interrupted work",
                                        "status": "pending",
                                    },
                                ],
                            },
                        )
                    ]
                )
            if runtime.run.model_turns == 2:
                return AssistantMessage(content="Discard this first Todo final answer.")
            sleep(1.5)
            return AssistantMessage(content="The Turn ended before its Todo list completed.")
        if task == "todo rejected update":
            if runtime.run.model_turns == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="update_todo_list",
                            call_id="todo_rejected_stale",
                            arguments={
                                "expected_revision": 1,
                                "operations": [{"op": "add", "content": "Must never render", "status": "pending"}],
                            },
                        )
                    ]
                )
            return AssistantMessage(content="The rejected Todo update was not applied.")
        if task == "todo completed auto close":
            if runtime.run.model_turns == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="update_todo_list",
                            call_id="todo_complete_running",
                            arguments={
                                "expected_revision": 0,
                                "operations": [
                                    {
                                        "op": "add",
                                        "content": "Complete the browser lifecycle",
                                        "status": "in_progress",
                                    }
                                ],
                            },
                        )
                    ]
                )
            if runtime.run.model_turns == 2:
                sleep(1.5)
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="update_todo_list",
                            call_id="todo_complete_done",
                            arguments={
                                "expected_revision": 1,
                                "operations": [
                                    {
                                        "op": "update",
                                        "id": _todo_id_from_result(runtime, "todo_complete_running"),
                                        "status": "completed",
                                    }
                                ],
                            },
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
        project_cwd=_kwargs.get("project_cwd"),
        planner_name="rule",
        paths=state.paths,
        job_registry=state.job_registry,
        sandbox_session_id=session_id,
        agent_thread_index=state.agent_thread_index,
        subagent_coordinator=state.subagent_coordinator,
        todo_store=state.todo_store,
    )
    application.runner.planner = CooperativePausePlanner()

    def slow_tool() -> str:
        # Keep the tool boundary open long enough for a real browser to render
        # the call and dispatch Redis steering on slower Windows CI hosts.
        sleep(5.0)
        return "Slow tool completed."

    application.runner.tools = ToolRegistry(
        [
            *filesystem_read_tools(WorkspaceFiles(resolved_workspace, project_workspace=_kwargs.get("project_cwd"))),
            Tool(
                "web_search",
                "Return a deterministic local search result.",
                lambda query: f"Deterministic search result for {query}.",
                requires_confirmation=True,
            ),
            Tool("slow_tool", "Run one deterministic slow tool.", slow_tool),
            Tool("forbidden_tool", "Must be skipped after steering.", lambda: "Forbidden tool executed."),
            *todo_tools(),
            *delegation_tools(),
            trace_mcp_tool,
        ]
    )
    state.subagent_coordinator.bind_session(
        session_id,
        lambda: AgentRunner(
            AgentThreadE2EPlanner(),
            ToolRegistry(list(delegation_tools())),
            workspace_root=str(resolved_workspace),
            job_registry=state.job_registry,
        ),
        resolved_workspace,
    )
    return application


chat_routes.build_local_application = local_application
turn_routes.build_local_application = local_application
app = create_app(state)


@app.post("/api/test/trace-model-reset")
def reset_trace_model_calls() -> dict[str, int]:
    global RETRY_MODEL_CALLS, TRACE_MODEL_CALLS, TRACE_MODEL_LAST_REQUEST

    TRACE_MODEL_CALLS = 0
    RETRY_MODEL_CALLS = 0
    TRACE_MODEL_LAST_REQUEST = {}
    return {"calls": TRACE_MODEL_CALLS, "retry_calls": RETRY_MODEL_CALLS}


@app.get("/api/test/trace-model-calls")
def get_trace_model_calls() -> dict[str, int]:
    return {"calls": TRACE_MODEL_CALLS, "retry_calls": RETRY_MODEL_CALLS}


@app.get("/api/test/trace-model-last-request")
def get_trace_model_last_request() -> dict[str, object]:
    return TRACE_MODEL_LAST_REQUEST


@app.post("/api/test/legacy-unknown-error")
def create_legacy_unknown_error(values: dict[str, object]) -> dict[str, str]:
    session_id = str(values.get("session_id") or "")
    if not session_id:
        raise ValueError("session_id is required")
    store = session_store(state)
    root = store.ensure_root_node(session_id, id=f"legacy-root-{uuid4()}")
    writer = NodeWriter(store)
    turn = writer.create(
        RuntimeState.create(
            session_id=session_id,
            thread_id=session_id,
            id=f"legacy-turn-{uuid4()}",
            parent=root,
            user_content=[{"type": "text", "text": "legacy unknown error"}],
            provider_name="local",
        )
    )
    turn = writer.append_item(
        turn,
        terminal_error_payload(
            "agent",
            "An unknown error caused the system to encounter an exception.",
            retryable=False,
        ),
    )
    turn = writer.finalize(turn, "failed")
    assistant_idx = len(turn.data[turn.current_data_idx]) - 1
    error_item = turn.data[turn.current_data_idx][assistant_idx]["content"][0]
    store.initialize_turn_trace(
        session_id,
        TurnTrace(
            turn_id=turn.id,
            thread_id=turn.thread_id,
            data_idx=turn.current_data_idx,
            context=TurnTraceContext(
                system_message="Legacy diagnostic record",
                active_skills=[],
                tools=[],
                initialized_at=turn.timestamp,
            ),
            items=[
                TurnTraceItem(
                    sequence=1,
                    message_idx=assistant_idx,
                    item_idx=0,
                    role="assistant",
                    item=error_item,
                    completed_at=turn.timestamp,
                )
            ],
            last_sequence=1,
            updated_at=turn.timestamp,
        ),
    )
    return {"turn_id": turn.id}


@app.post("/api/test/session-file")
def create_session_file(values: dict[str, object]) -> dict[str, str]:
    session_id = str(values.get("session_id") or "")
    relative = Path(str(values.get("display_path") or ""))
    if not session_id or not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("A confined session file path is required.")
    files = session_file_store(state, session_id)
    source = str(values.get("source") or "workspace")
    root = files.file_paths.root(source).resolve()
    target = (root / relative).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("The session file must stay inside its workspace.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(values.get("content") or "reference"), encoding="utf-8")
    metadata = files.metadata(target, source)
    return {key: str(metadata[key]) for key in ("source", "path", "display_path")}


@app.post("/api/test/project")
def create_test_project() -> dict[str, str]:
    directory = _root / f"project-{uuid4().hex}"
    directory.mkdir()
    project = state.projects.create(directory)
    return {"project_id": project.project_id}


@app.post("/api/test/sandbox-status")
def set_sandbox_status(values: dict[str, object]) -> dict[str, object]:
    return sandbox_broker.set_status(
        installed=values.get("installed") is True,
        healthy=values.get("healthy") is True,
        detail=str(values["detail"]) if values.get("detail") else None,
    ).to_dict()


@app.post("/api/test/redis-available")
def set_redis_available(values: dict[str, object]) -> dict[str, bool]:
    available = values.get("available") is True
    set_e2e_redis_available(available)
    return {"available": available}


@app.post("/api/test/sidebar-project")
def create_sidebar_project(values: dict[str, object]) -> dict[str, object]:
    raw_titles = values.get("titles")
    if not isinstance(raw_titles, list) or not raw_titles or not all(isinstance(title, str) for title in raw_titles):
        raise ValueError("titles must be a non-empty string list")
    project_name = str(values.get("name") or "Sidebar E2E Project")
    project_dir = _root / f"sidebar-project-{uuid4().hex}"
    project_dir.mkdir()
    project = state.projects.create(project_dir, name=project_name)
    store = session_store(state)
    threads: list[dict[str, object]] = []
    for raw_title in raw_titles:
        title = raw_title.strip() or "新对话"
        session = store.create_session(title)
        thread = store.create_sidebar_thread(
            session_id=session.session_id,
            thread_id=session.session_id,
            title=title,
            title_is_custom=True,
        )
        state.projects.create_session(project.project_id, session.session_id)
        threads.append(thread.to_dict())
    return {
        "project": {"project_id": project.project_id, "name": project.name},
        "threads": threads,
    }


# Keep every test control route ahead of the API fallback and frontend mount.
app.router.routes.sort(key=lambda route: not getattr(route, "path", "").startswith("/api/test/"))


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("MINI_AGENT_E2E_PORT", "18080")),
        log_level="warning",
    )
