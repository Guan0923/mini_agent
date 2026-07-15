"""Shared HTTP transport for Chat Completions, Responses, and Messages APIs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import requests

from .errors import ModelRequestError


class ChatCompletionsClient:
    """Performs provider-aware JSON POSTs without assuming a response schema.

    The methods intentionally return raw JSON. Provider adapters own payload
    construction and response parsing because DeepSeek Chat Completions,
    OpenAI Responses, and Anthropic Messages have different schemas.
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def post_json(
        self,
        endpoint: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Compatibility entry point for Bearer-authenticated provider adapters."""
        return self._post_json(
            endpoint,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload,
            timeout_seconds,
        )

    def create_chat_completion(
        self,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int = 45,
    ) -> dict[str, Any]:
        """POST a Chat Completions request to ``/v1/chat/completions``.

        This suits OpenAI-compatible APIs such as DeepSeek, MiniMax, and GLM,
        but it deliberately does not parse their different response formats.
        """
        return self.post_json(
            self._endpoint(base_url, "chat/completions"),
            api_key,
            payload,
            timeout_seconds,
        )

    def create_openai_response(
        self,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int = 45,
    ) -> dict[str, Any]:
        """POST an OpenAI Responses API request to ``/v1/responses``.

        The caller provides the native Responses payload (for example
        ``input``, ``instructions``, or ``tools``); no Chat Completions
        conversion is performed here.
        """
        return self.post_json(
            self._endpoint(base_url, "responses"),
            api_key,
            payload,
            timeout_seconds,
        )

    def create_anthropic_message(
        self,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int = 45,
        anthropic_version: str = "2023-06-01",
    ) -> dict[str, Any]:
        """POST an Anthropic Messages API request to ``/v1/messages``.

        Anthropic uses ``x-api-key`` instead of Bearer authentication and
        requires an ``anthropic-version`` header. The caller supplies the
        native Messages payload, including its required ``max_tokens`` field.
        """
        return self._post_json(
            self._endpoint(base_url, "messages"),
            {
                "x-api-key": api_key,
                "anthropic-version": anthropic_version,
                "Content-Type": "application/json",
            },
            payload,
            timeout_seconds,
        )

    def stream_json(
        self,
        endpoint: str,
        api_key: str,
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> Iterator[dict[str, Any]]:
        """Yield JSON objects from an OpenAI-compatible SSE response."""
        response: requests.Response | None = None
        try:
            response = self.session.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout_seconds,
                stream=True,
            )
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ModelRequestError("Stream event is not valid JSON.") from exc
                if not isinstance(event, dict):
                    raise ModelRequestError("Stream event must be a JSON object.")
                yield event
        except requests.RequestException as exc:
            raise ModelRequestError(f"Model stream failed: {exc.__class__.__name__}") from exc
        finally:
            if response is not None:
                response.close()

    def _post_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        try:
            response = self.session.post(endpoint, headers=headers, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise ModelRequestError(f"Model request failed: {exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise ModelRequestError("Model response is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise ModelRequestError("Model response must be a JSON object.")
        return data

    @staticmethod
    def _endpoint(base_url: str, resource: str) -> str:
        """Accept either a host URL, a ``/v1`` base URL, or a full endpoint."""
        base = base_url.rstrip("/")
        resource_suffix = f"/{resource}"
        if base.endswith(resource_suffix):
            return base
        if base.endswith("/v1"):
            return f"{base}{resource_suffix}"
        return f"{base}/v1{resource_suffix}"
