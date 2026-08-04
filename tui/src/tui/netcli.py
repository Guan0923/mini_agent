"""Standalone network client entry for the mini-agent backend.

A pure client-tier program: it imports no backend code and talks only to the
backend's HTTP/SSE API. Run:

    mini-agent-net --server http://127.0.0.1:8000 "your task"
    mini-agent-net --tools
    mini-agent-net --skills
    mini-agent-net --health
"""

from __future__ import annotations

import argparse
import json

from .client import ApiError, MiniAgentClient


def _render(message: dict) -> None:
    kind = message.get("kind")
    data = message.get("data", {})
    if kind == "thinking_delta":
        print(message.get("message", ""), end="", flush=True)
    elif kind == "tool_call":
        print(f"\nCALL  {message.get('message', '')} {json.dumps(data.get('arguments', {}), ensure_ascii=False)}")
    elif kind == "tool_result":
        print(f"RESULT\n{str(message.get('message', ''))[:200]}")
    elif kind == "tool_failed":
        print(f"TOOL FAILED  {message.get('message', '')}")
    elif kind == "response_delta":
        print(message.get("message", ""), end="", flush=True)


def _decide(request_data: dict) -> dict:
    print(f"\nAPPROVAL REQUIRED - {request_data.get('message', '')}")
    if request_data.get("kind") == "question":
        answers: dict[str, list[str]] = {}
        for question in request_data.get("questions", []):
            value = input(f"  {question.get('question')}: ").strip()
            answers[str(question.get("id"))] = [value] if value else [""]
        return {"choice": "answer", "answers": answers}
    print(
        f"  tool: {request_data.get('tool')}  "
        f"args: {json.dumps(request_data.get('arguments', {}), ensure_ascii=False)}"
    )
    choice = input("  Approve and continue? [y/N] ").strip().lower()
    return {"choice": "continue" if choice in {"y", "yes"} else "cancel"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini-agent-net", description="Mini-Agent network client (client tier)")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="Backend server URL")
    parser.add_argument("task", nargs="*", help="Run one task and exit")
    parser.add_argument("--tools", action="store_true", help="List the backend's available tools")
    parser.add_argument("--skills", action="store_true", help="List the backend's discovered skills")
    parser.add_argument("--sessions", action="store_true", help="List the backend's saved sessions")
    parser.add_argument("--health", action="store_true", help="Check backend health")
    args = parser.parse_args(argv)

    client = MiniAgentClient(args.server)
    try:
        if args.health:
            print(client.health())
            return 0
        if args.tools:
            tools = client.list_tools()
            print(f"tools ({len(tools)}):")
            for tool in tools:
                print(f"- {tool['name']}: {tool['description']}")
            return 0
        if args.skills:
            skills = client.list_skills()
            print(f"skills ({len(skills)}):")
            for skill in skills:
                print(f"- {skill['name']}: {skill['description']}")
            return 0
        if args.sessions:
            sessions = client.list_sessions()
            print(f"sessions ({len(sessions)}):")
            for session in sessions:
                print(f"- {session['session_id']}: {session['title'] or '(no title)'}")
            return 0
        task = " ".join(args.task).strip()
        if not task:
            parser.print_help()
            return 1
        print(f"[client] connecting to {client.base_url}...")
        done = client.run_task(task, on_event=_render, on_decision_requested=_decide, interactive=True)
        print()
        answer = done.get("final_answer") or ""
        if answer:
            print(f"\n{answer}\n")
        metrics = done.get("metrics", {})
        print(
            f"status: {done.get('status')} | duration: {metrics.get('duration_ms')}ms | "
            f"model_calls: {metrics.get('model_calls')} | tool_calls: {metrics.get('tool_calls')}"
        )
        return 0 if done.get("status") == "completed" else 1
    except ApiError as exc:
        print(f"[client] error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
