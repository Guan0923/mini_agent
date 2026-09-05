"""Real isolated backend and deterministic loopback model for MCP browser QA."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.api.app import create_app  # noqa: E402
from backend.api.chat import routes as chat_routes  # noqa: E402
from backend.api.routes import turns as turn_routes  # noqa: E402
from backend.api.state import WebAppState  # noqa: E402
from backend.planning import LLMPlanner  # noqa: E402
from backend.providers import LLMClient, ModelConfig  # noqa: E402
from backend.runtime import build_application  # noqa: E402
from backend.runtime.application import factory as application_factory  # noqa: E402
from backend.sandbox import BrokerStatus  # noqa: E402


class TestBroker:
    def status(self):
        return BrokerStatus(True, True, version="mcp-e2e", installation_id="mcp-e2e")

    @classmethod
    def from_system(cls, **kwargs):
        return cls()


temporary = tempfile.TemporaryDirectory(prefix="mini-agent-mcp-browser-")
application_factory.WindowsBrokerClient = TestBroker
state = WebAppState(Path(temporary.name), sandbox_broker=TestBroker())
model_port = int(os.environ["MINI_AGENT_E2E_MODEL_PORT"])
steps = [
    ("list_mcp_resources", {"server": "browser"}),
    ("list_mcp_resource_templates", {"server": "browser"}),
    ("read_mcp_resource", {"server": "browser", "uri": "notes://one"}),
    ("list_mcp_prompts", {"server": "browser"}),
    ("get_mcp_prompt", {"server": "browser", "name": "review", "arguments": {"language": "Chinese"}}),
    ("subscribe_mcp_resource", {"server": "browser", "uri": "notes://one"}),
    ("get_mcp_resource_updates", {"server": "browser"}),
    ("unsubscribe_mcp_resource", {"server": "browser", "uri": "notes://one"}),
]
model_results = []


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        results = [message for message in payload.get("messages", []) if message.get("role") == "tool"]
        model_results[:] = results
        step = len(results)
        message = {"role": "assistant", "content": "MCP resource and prompt workflow completed."}
        finish = "stop"
        if payload.get("tools") and step < len(steps):
            name, arguments = steps[step]
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": f"mcp-step-{step}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            }
            finish = "tool_calls"
        if payload.get("stream"):
            if "tool_calls" in message:
                message["tool_calls"][0]["index"] = 0
            event = {
                "id": "mcp-browser",
                "object": "chat.completion.chunk",
                "model": "local",
                "created": 1,
                "choices": [{"index": 0, "delta": message, "finish_reason": finish}],
            }
            body = f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()
            content_type = "text/event-stream"
        else:
            body = json.dumps(
                {"choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": {"total_tokens": 1}}
            ).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


model_server = ThreadingHTTPServer(("127.0.0.1", model_port), ModelHandler)
threading.Thread(target=model_server.serve_forever, daemon=True).start()
model = ModelConfig("test-only-key", f"http://127.0.0.1:{model_port}/v1", "mcp-test", context_size=128000)
state.model_config = lambda _provider_name=None: model


def application(_state, *, session_id, workspace=None, **kwargs):
    app = build_application(
        Path(workspace or state.session_workspace(session_id)),
        planner_name="rule",
        paths=state.paths,
        job_registry=state.job_registry,
        job_parent_id=kwargs.get("job_parent_id"),
        sandbox_session_id=session_id,
        agent_thread_index=state.agent_thread_index,
        subagent_coordinator=state.subagent_coordinator,
        todo_store=state.todo_store,
    )
    specs = app.runner.tools.specs()
    app.runner.planner = LLMPlanner(LLMClient(model), specs, specs)
    return app


chat_routes.build_local_application = application
turn_routes.build_local_application = application
close_state = state.close


def close():
    model_server.shutdown()
    model_server.server_close()
    prefix = os.environ.get("MINI_AGENT_REDIS_KEY_PREFIX", "")
    if prefix.startswith("mini-agent:e2e:"):
        client = state.message_queue.client
        keys = list(client.scan_iter(f"{prefix}:*"))
        if keys:
            client.delete(*keys)
    close_state()


state.close = close
app = create_app(state)


@app.get("/api/test/mcp-results")
def results():
    return {"results": model_results}


app.router.routes.insert(0, app.router.routes.pop())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["MINI_AGENT_E2E_PORT"]), log_level="warning")
