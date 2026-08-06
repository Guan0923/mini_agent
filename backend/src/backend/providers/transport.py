"""Schema-neutral JSON HTTP and event-aware SSE transport."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from time import perf_counter
from typing import Any

import requests

from .errors import ModelTransportError

_TERMINAL_EVENTS = frozenset({
    "response.completed", "response.failed", "response.incomplete",
    "message_stop", "error",
})


class JsonHttpTransport:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.last_metadata: dict[str, Any] = {}

    def post_json(self, endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        started = perf_counter()
        response: requests.Response | None = None
        try:
            response = self.session.post(
                endpoint, headers=headers, json=payload, timeout=timeout_seconds, allow_redirects=False
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise _transport_error(exc, "Model request failed") from exc
        except ValueError as exc:
            raise ModelTransportError("Model response is not valid JSON.", retryable=True) from exc
        finally:
            self.last_metadata = {
                "http_status": getattr(response, "status_code", None),
                "response_headers": _safe_response_headers(getattr(response, "headers", None)),
                "transport_duration_ms": round((perf_counter() - started) * 1000, 3),
            }
        if not isinstance(data, dict):
            raise ModelTransportError("Model response must be a JSON object.", retryable=True)
        return data

    def stream_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> Iterator[dict[str, Any]]:
        started = perf_counter()
        response: requests.Response | None = None
        saw_done = False
        saw_event = False
        try:
            response = self.session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
            response.raise_for_status()
            pending_event: str | None = None
            for line in response.iter_lines(decode_unicode=False):
                if not line:
                    pending_event = None
                    continue
                if isinstance(line, bytes):
                    try:
                        line = line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ModelTransportError(
                            "Stream event is not valid UTF-8.",
                            retryable=not saw_event,
                            stream_started=saw_event,
                        ) from exc
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    pending_event = line.removeprefix("event:").strip() or None
                    continue
                if not line.startswith("data:"):
                    continue
                raw_event = line.removeprefix("data:").strip()
                if raw_event == "[DONE]":
                    saw_done = True
                    return
                try:
                    event = json.loads(raw_event)
                except json.JSONDecodeError as exc:
                    raise ModelTransportError(
                        "Stream event is not valid JSON.",
                        retryable=not saw_event,
                        stream_started=saw_event,
                    ) from exc
                if not isinstance(event, dict):
                    raise ModelTransportError(
                        "Stream event must be a JSON object.",
                        retryable=not saw_event,
                        stream_started=saw_event,
                    )
                event_name = pending_event or (event.get("type") if isinstance(event.get("type"), str) else None)
                if event_name:
                    event = {**event, "__sse_event": event_name}
                pending_event = None
                saw_event = True
                if event_name in _TERMINAL_EVENTS:
                    saw_done = True
                yield event
                if saw_done:
                    return
            if not saw_done:
                raise ModelTransportError(
                    "Model stream ended before [DONE] or a completion event.",
                    retryable=not saw_event,
                    stream_started=saw_event,
                )
        except requests.RequestException as exc:
            raise _transport_error(exc, "Model stream failed", stream_started=saw_event) from exc
        finally:
            if response is not None:
                response.close()
            self.last_metadata = {
                "http_status": getattr(response, "status_code", None),
                "response_headers": _safe_response_headers(getattr(response, "headers", None)),
                "transport_duration_ms": round((perf_counter() - started) * 1000, 3),
                "stream_completed": saw_done,
            }


def _safe_response_headers(headers: Any) -> dict[str, str]:
    if not hasattr(headers, "items"):
        return {}
    allowed = {"content-type", "request-id", "x-request-id", "retry-after"}
    return {str(key).lower(): str(value) for key, value in headers.items() if str(key).lower() in allowed}


_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _response_detail(response: Any) -> str:
    if response is None:
        return ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("error", payload.get("message", payload))
        detail = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        detail = str(getattr(response, "text", "") or "")
        if not detail and isinstance(getattr(response, "body", None), (str, bytes)):
            detail = getattr(response, "body")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
    return detail[:500].replace("\n", " ").strip()


def _transport_error(error: requests.RequestException, label: str, *, stream_started: bool = False) -> ModelTransportError:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    headers = getattr(response, "headers", None)
    retry_after: float | None = None
    if headers is not None:
        try:
            retry_after = min(30.0, max(0.0, float(headers.get("Retry-After"))))
        except (TypeError, ValueError):
            retry_after = None
    request_id = ""
    if headers is not None:
        request_id = str(headers.get("x-request-id") or headers.get("request-id") or "")
    detail = _response_detail(response)
    suffix = []
    if isinstance(status_code, int):
        suffix.append(f"status={status_code}")
    if request_id:
        suffix.append(f"request_id={request_id}")
    if detail:
        suffix.append(f"detail={detail}")
    context = f" ({'; '.join(suffix)})" if suffix else ""
    retryable = (
        isinstance(error, (requests.Timeout, requests.ConnectionError)) or status_code in _RETRYABLE_STATUS_CODES
    )
    return ModelTransportError(
        f"{label}: {error.__class__.__name__}{context}",
        retryable=retryable and not stream_started,
        status_code=status_code if isinstance(status_code, int) else None,
        retry_after=retry_after,
        stream_started=stream_started,
        diagnostics={"request_id": request_id} if request_id else None,
    )


class _RecordedStream:
    def __init__(self, source: Iterator[dict[str, Any]]) -> None:
        self._source = source
        self.events: list[dict[str, Any]] = []
        self.completed = False

    def __iter__(self) -> _RecordedStream:
        return self

    def __next__(self) -> dict[str, Any]:
        try:
            event = next(self._source)
        except StopIteration:
            self.completed = True
            raise
        self.events.append(copy.deepcopy(event))
        return event

    def close(self) -> None:
        close = getattr(self._source, "close", None)
        if callable(close):
            close()