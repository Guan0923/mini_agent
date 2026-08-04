"""Standalone verifier entry point.

This file intentionally uses only Python's standard library.  The benchmark
runner invokes it with ``python -I`` so grading is independent of the agent's
workspace imports and never needs to install or download a package.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import math
import re
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any


def _result(passed: bool, detail: str) -> None:
    print(json.dumps({"passed": passed, "detail": detail}, ensure_ascii=False))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _clear_workspace_modules(workspace: Path) -> None:
    """Prevent repeated in-process verifier tests from reusing old fixture imports."""
    root = workspace.resolve()
    for name, module in list(sys.modules.items()):
        module_path = getattr(module, "__file__", None)
        if not isinstance(module_path, str):
            continue
        try:
            resolved = Path(module_path).resolve()
        except OSError:
            continue
        if resolved == root or root in resolved.parents:
            sys.modules.pop(name, None)


def verify_terminal_access_log(workspace: Path, _answer: str) -> tuple[bool, str]:
    log_path = workspace / "access_log"
    report_path = workspace / "report.txt"
    if not log_path.exists() or not report_path.exists():
        return False, "access_log or report.txt is missing"
    urls: list[str] = []
    ips: set[str] = set()
    errors = 0
    pattern = re.compile(r'^(\S+)\s+\S+\s+\S+\s+\[[^]]+\]\s+"[A-Z]+\s+(\S+)\s+[^\"]+"\s+(\d{3})\b')
    for line in _read(log_path).splitlines():
        if not line.strip():
            continue
        match = pattern.match(line)
        if match is None:
            return False, f"could not parse access log line: {line[:100]}"
        ip, url, status = match.groups()
        ips.add(ip)
        urls.append(url)
        errors += status == "404"
    top = Counter(urls).most_common()
    top.sort(key=lambda item: (-item[1], item[0]))
    expected = [
        f"Total requests: {len(urls)}",
        f"Unique IP addresses: {len(ips)}",
        "Top 3 URLs:",
        *(f"  {url}: {count}" for url, count in top[:3]),
        f"404 errors: {errors}",
    ]
    actual = _read(report_path).splitlines()
    if actual != expected:
        return False, f"report mismatch; expected {expected!r}, got {actual!r}"
    return True, "access-log report matches independently computed counts"


async def _run_async_verifier(run_tasks: Any) -> tuple[bool, str]:
    active = 0
    peak = 0
    cleanup = 0

    async def normal_job() -> None:
        nonlocal active, peak, cleanup
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.002)
        finally:
            active -= 1
            cleanup += 1

    try:
        await asyncio.wait_for(run_tasks([normal_job] * 8, 3), timeout=2)
    except Exception as exc:
        return False, f"normal async run raised {type(exc).__name__}: {exc}"
    if peak > 3 or active != 0 or cleanup != 8:
        return False, f"concurrency/cleanup mismatch: peak={peak}, active={active}, cleanup={cleanup}"

    started = asyncio.Event()
    cleanup_cancelled = 0

    async def cancellable_job() -> None:
        nonlocal cleanup_cancelled
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            cleanup_cancelled += 1

    # Let every child enter its body before cancelling the parent.  The first
    # phase already proves the max-concurrency limit; this phase isolates
    # cancellation propagation and makes each child's finally observable.
    parent = asyncio.create_task(run_tasks([cancellable_job] * 4, 4))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        parent.cancel()
        try:
            await asyncio.wait_for(parent, timeout=2)
        except asyncio.CancelledError:
            pass
    finally:
        if not parent.done():
            parent.cancel()
            try:
                await parent
            except BaseException:
                # A verifier cleanup path must not turn an expected parent
                # cancellation into a crashed subprocess.
                pass
    if cleanup_cancelled != 4:
        return False, f"outer cancellation did not clean up all children: {cleanup_cancelled}/4"

    cleanup_error = 0

    async def failing_job() -> None:
        nonlocal cleanup_error
        try:
            await asyncio.sleep(0.01)
            raise ValueError("expected verifier failure")
        finally:
            cleanup_error += 1

    failure_propagated = False
    try:
        await asyncio.wait_for(run_tasks([failing_job, failing_job], 2), timeout=2)
    except ValueError:
        failure_propagated = True
    except Exception as exc:
        return False, f"worker exception changed type: {type(exc).__name__}: {exc}"
    if not failure_propagated:
        return False, "worker exception was swallowed instead of propagated"
    if cleanup_error != 2:
        return False, f"worker exception did not clean up siblings: {cleanup_error}/2"
    return True, "async concurrency, cancellation, and cleanup behavior passed"


def verify_terminal_cancel_async(workspace: Path, _answer: str) -> tuple[bool, str]:
    path = workspace / "run.py"
    if not path.exists():
        return False, "run.py is missing"
    sys.path.insert(0, str(workspace))
    try:
        module = __import__("run")
        run_tasks = getattr(module, "run_tasks", None)
        if not callable(run_tasks):
            return False, "run.py does not expose callable run_tasks"
        return asyncio.run(_run_async_verifier(run_tasks))
    except Exception as exc:
        return False, f"could not import or verify run_tasks: {type(exc).__name__}: {exc}"
    finally:
        _clear_workspace_modules(workspace)
        try:
            sys.path.remove(str(workspace))
        except ValueError:
            pass


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)


def verify_terminal_countdown(workspace: Path, _answer: str) -> tuple[bool, str]:
    path = workspace / "output.txt"
    if not path.exists():
        return False, "output.txt is missing"
    raw = _read(path)
    # Accept either no final newline or exactly one POSIX/Windows final
    # newline, while rejecting embedded newlines and extra output lines.
    match = re.fullmatch(r"([^\r\n]*)(?:\r\n|\n)?", raw)
    if match is None:
        return False, "output must contain one expression line"
    expression = match.group(1).strip()
    if not expression:
        return False, "output expression is empty"
    numbers = [3, 8, 11, 19, 25, 75]
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return False, f"invalid arithmetic expression: {exc.msg}"
    used: list[int] = []

    def evaluate(node: ast.AST) -> Fraction:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            if node.value not in numbers or node.value in used:
                raise ValueError(f"number {node.value} is not allowed or is repeated")
            used.append(node.value)
            return Fraction(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ValueError("division by zero")
            return left / right
        raise ValueError(f"unsupported syntax: {type(node).__name__}")

    try:
        value = evaluate(tree)
    except ValueError as exc:
        return False, str(exc)
    if value != 462:
        return False, f"expression evaluates to {value}, expected 462"
    return True, f"valid expression using {len(used)} allowed numbers"


def verify_swe_requests(workspace: Path, _answer: str) -> tuple[bool, str]:
    try:
        sys.path.insert(0, str(workspace))
        from requests_like.sessions import PreparedRequest

        request = PreparedRequest()
        request.prepare_method(b"GET")
        if request.method != "GET":
            return False, f"bytes method became {request.method!r}"
        request.prepare_method("POST")
        if request.method != "POST":
            return False, "ordinary string method regressed"
        return True, "Requests method normalization regression passed"
    except Exception as exc:
        return False, f"Requests regression failed: {type(exc).__name__}: {exc}"
    finally:
        _clear_workspace_modules(workspace)
        try:
            sys.path.remove(str(workspace))
        except ValueError:
            pass


def verify_swe_pytest(workspace: Path, _answer: str) -> tuple[bool, str]:
    try:
        sys.path.insert(0, str(workspace))
        from mini_pytest.rewrite import first_statement_kind, rewrite_assertions

        if first_statement_kind("1\nassert value") != "expression":
            return False, "numeric first expression was treated as docstring"
        if first_statement_kind("'module docs'\nassert value") != "docstring":
            return False, "real string docstring was not preserved"
        rewritten = rewrite_assertions("assert total == 3")
        if "assert total == 3" not in rewritten or "AssertionError" not in rewritten:
            return False, "assertion rewrite behavior regressed"
        return True, "Pytest assertion-rewrite regression passed"
    except Exception as exc:
        return False, f"Pytest regression failed: {type(exc).__name__}: {exc}"
    finally:
        _clear_workspace_modules(workspace)
        try:
            sys.path.remove(str(workspace))
        except ValueError:
            pass


def verify_swe_astropy(workspace: Path, _answer: str) -> tuple[bool, str]:
    try:
        sys.path.insert(0, str(workspace))
        from mini_astropy.qdp import parse_commands, parse_data_rows

        mixed = parse_commands("read serr 1 2\nREAD SERR 3 4\n1 2 3\n")
        if mixed != [("READ", ["SERR", "1", "2"]), ("READ", ["SERR", "3", "4"])]:
            return False, f"mixed-case commands parsed incorrectly: {mixed!r}"
        if parse_commands("1 2 3\n") != []:
            return False, "data rows were treated as commands"
        rows = parse_data_rows("1 NO nan\n2.5 no NaN\n")
        if len(rows) != 2 or rows[0][0] != 1.0 or rows[0][1] is not None:
            return False, f"NO token was not parsed as a missing value: {rows!r}"
        if not isinstance(rows[0][2], float) or not math.isnan(rows[0][2]):
            return False, f"nan token was not preserved as NaN: {rows!r}"
        if rows[1][1] is not None or not math.isnan(rows[1][2]):
            return False, f"mixed-case NO/nan parsing regressed: {rows!r}"
        return True, "Astropy QDP command case regression passed"
    except Exception as exc:
        return False, f"Astropy regression failed: {type(exc).__name__}: {exc}"
    finally:
        _clear_workspace_modules(workspace)
        try:
            sys.path.remove(str(workspace))
        except ValueError:
            pass


def _audit(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "mcp_audit.jsonl"
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError(f"invalid MCP audit JSON on line {line_number}")
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise ValueError(f"invalid MCP audit entry on line {line_number}")
        if not isinstance(value.get("arguments"), dict) or not isinstance(value.get("mutating"), bool):
            raise ValueError(f"invalid MCP audit entry shape on line {line_number}")
        entries.append(value)
    return entries


def _calls(audit: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [entry for entry in audit if entry.get("name") == name]


def _has_call(audit: list[dict[str, Any]], name: str, arguments: dict[str, Any]) -> bool:
    return any(call.get("arguments") == arguments for call in _calls(audit, name))


def verify_tau_retail_exchange(workspace: Path, _answer: str) -> tuple[bool, str]:
    try:
        audit = _audit(workspace)
    except (OSError, ValueError) as exc:
        return False, f"invalid MCP audit: {exc}"
    exchange = _calls(audit, "exchange_delivered_order_items")
    if len(exchange) != 1:
        return False, f"expected one exchange mutation, got {len(exchange)}"
    required_reads = (
        ("find_user_id_by_name_zip", {"first_name": "Yusuf", "last_name": "Rossi", "zip": "19122"}),
        ("get_user_details", {"user_id": "yusuf_rossi_19122"}),
        ("get_order_details", {"order_id": "#W2378156"}),
    )
    if any(not _has_call(audit, name, arguments) for name, arguments in required_reads):
        return False, "customer, user, and order verification queries are incomplete"
    if any(
        not _has_call(audit, "get_product_details", {"product_id": product_id})
        for product_id in ("1151293680", "4983901480", "7706410293", "7747408585")
    ):
        return False, "source and replacement product details were not inspected"
    args = exchange[0].get("arguments", {})
    expected = {
        "order_id": "#W2378156",
        "item_ids": ["1151293680", "4983901480"],
        "new_item_ids": ["7706410293", "7747408585"],
        "payment_method_id": "credit_card_9513926",
    }
    if args != expected:
        return False, f"exchange arguments mismatch: {args!r}"
    writes = [entry for entry in audit if entry.get("mutating")]
    if writes != exchange:
        return False, "unexpected mutating MCP calls were made"
    try:
        payload = json.loads(_read(workspace / "retail_state.json"))
        order = payload["orders"]["#W2378156"]
        items = order["items"]
        user = payload["users"]["yusuf_rossi_19122"]
        products = payload["products"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return False, f"retail state is invalid: {type(exc).__name__}"
    if items != ["7706410293", "7747408585"]:
        return False, f"retail order state mismatch: {items!r}"
    if (
        not isinstance(user, dict)
        or not isinstance(order, dict)
        or user.get("first_name") != "Yusuf"
        or user.get("last_name") != "Rossi"
        or str(user.get("zip")) != "19122"
        or order.get("user_id") != "yusuf_rossi_19122"
        or order.get("status") != "delivered"
        or order.get("payment_method_id") != "credit_card_9513926"
        or not isinstance(products, dict)
        or not isinstance(products.get("7706410293"), dict)
        or not isinstance(products.get("7747408585"), dict)
        or products["7706410293"].get("switch") != "clicky"
        or products["7706410293"].get("size") != "full"
        or products["7706410293"].get("backlight") != "rgb"
        or products["7747408585"].get("compatibility") != "Google Home"
    ):
        return False, "retail exchange state does not match the authorized policy"
    return True, "τ³ retail exchange state and mutation trace passed"


def verify_tau_retail_cancel_all(workspace: Path, _answer: str) -> tuple[bool, str]:
    try:
        audit = _audit(workspace)
    except (OSError, ValueError) as exc:
        return False, f"invalid MCP audit: {exc}"
    calls = _calls(audit, "cancel_pending_order")
    expected = [
        {"order_id": "#W5056519", "reason": "ordered by mistake"},
        {"order_id": "#W5995614", "reason": "ordered by mistake"},
    ]
    if [call.get("arguments") for call in calls] != expected:
        return False, f"cancel sequence mismatch: {[call.get('arguments') for call in calls]!r}"
    if not _has_call(
        audit,
        "find_user_id_by_name_zip",
        {"first_name": "Yara", "last_name": "Muller", "zip": "85041"},
    ) or not _has_call(audit, "get_user_details", {"user_id": "yara_muller_85041"}):
        return False, "customer verification queries are incomplete"
    if not _has_call(audit, "list_pending_orders", {"user_id": "yara_muller_85041"}):
        return False, "pending-order query is missing"
    writes = [entry for entry in audit if entry.get("mutating")]
    if writes != calls:
        return False, "unexpected mutating MCP calls were made"
    try:
        payload = json.loads(_read(workspace / "retail_state.json"))
        orders = payload["orders"]
        user = payload["users"]["yara_muller_85041"]
        statuses = [orders[item["order_id"]]["status"] for item in expected]
        reasons = [orders[item["order_id"]].get("cancellation_reason") for item in expected]
        delivered_status = orders["#W7777777"]["status"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return False, f"retail state is invalid: {type(exc).__name__}"
    if statuses != ["cancelled", "cancelled"]:
        return False, f"pending orders were not both cancelled: {statuses!r}"
    if reasons != ["ordered by mistake", "ordered by mistake"]:
        return False, f"cancellation reasons mismatch: {reasons!r}"
    if delivered_status != "delivered":
        return False, "delivered order was modified"
    if (
        not isinstance(user, dict)
        or user.get("first_name") != "Yara"
        or user.get("last_name") != "Muller"
        or str(user.get("zip")) != "85041"
    ):
        return False, "retail customer state does not match the task"
    return True, "τ³ retail cancellation state and mutation trace passed"


def verify_tau_airline_baggage(workspace: Path, answer: str) -> tuple[bool, str]:
    try:
        audit = _audit(workspace)
        state = json.loads(_read(workspace / "airline_state.json"))
        reservation_data = state["reservations"]["JMO1MG"]
        user_data = state["users"]["anya_garcia_5901"]
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return False, f"invalid airline state or audit: {type(exc).__name__}: {exc}"
    reservation = _calls(audit, "get_reservation_details")
    user = _calls(audit, "get_user_details")
    if not any(call.get("arguments") == {"reservation_id": "JMO1MG"} for call in reservation):
        return False, "reservation JMO1MG was not queried"
    if not any(call.get("arguments") == {"user_id": "anya_garcia_5901"} for call in user):
        return False, "user anya_garcia_5901 was not queried"
    if any(entry.get("mutating") for entry in audit):
        return False, "airline task made an unexpected mutation"
    if (
        reservation_data.get("user_id") != "anya_garcia_5901"
        or reservation_data.get("cabin") != "economy"
        or reservation_data.get("passengers") != 2
        or user_data.get("membership") != "Silver"
    ):
        return False, "airline fixture does not support the expected policy calculation"
    normalized = answer.casefold()
    if "silver" not in normalized or not re.search(r"\b4\b", normalized):
        return False, "final answer must identify Silver membership and 4 suitcases"
    return True, "τ³ airline verification and communication passed"


VERIFIERS = {
    "terminal_access_log": verify_terminal_access_log,
    "terminal_cancel_async": verify_terminal_cancel_async,
    "terminal_countdown": verify_terminal_countdown,
    "swe_requests": verify_swe_requests,
    "swe_pytest": verify_swe_pytest,
    "swe_astropy": verify_swe_astropy,
    "tau_retail_exchange": verify_tau_retail_exchange,
    "tau_retail_cancel_all": verify_tau_retail_cancel_all,
    "tau_airline_baggage": verify_tau_airline_baggage,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--final-answer", default="")
    args = parser.parse_args()
    verifier = VERIFIERS.get(args.name)
    if verifier is None:
        _result(False, f"unknown verifier: {args.name}")
        return 2
    try:
        passed, detail = verifier(args.workspace.resolve(), args.final_answer)
    except Exception as exc:
        _result(False, f"verifier crashed: {type(exc).__name__}: {exc}")
        return 0
    _result(bool(passed), str(detail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
