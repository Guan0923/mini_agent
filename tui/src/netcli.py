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

from .client import ApiError, MiniAgentClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mini-agent-net", description="Mini-Agent network client (client tier)")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="Backend server URL")
    parser.add_argument("task", nargs="*", help="Run one task and exit")
    parser.add_argument("--tools", action="store_true", help="List the backend's available tools")
    parser.add_argument("--skills", action="store_true", help="List the backend's discovered skills")
    parser.add_argument("--sessions", action="store_true", help="List the backend's saved sessions")
    parser.add_argument("--health", action="store_true", help="Check backend health")
    parser.add_argument("--logout", action="store_true", help="Revoke the saved browser-authorized device session")
    args = parser.parse_args(argv)

    client = MiniAgentClient(args.server)
    try:
        if args.health:
            print(client.health())
            return 0
        if args.logout:
            client.logout()
            print("logged out")
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
            threads = client.list_sidebar_threads()
            print(f"threads ({len(threads)}):")
            for thread in threads:
                print(f"- {thread['thread_id']}: {thread['title'] or '(no title)'}")
            return 0
        task = " ".join(args.task).strip()
        if not task:
            parser.print_help()
            return 1
        print("[client] 网络 TUI 任务执行已移除；请使用 Web 客户端创建和运行 Turn。")
        return 1
    except ApiError as exc:
        print(f"[client] error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
