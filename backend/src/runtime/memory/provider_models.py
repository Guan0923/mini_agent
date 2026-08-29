"""Provider-backed Memory model ports with stable availability classification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from backend.domain import SystemMessage, UserMessage
from backend.providers import LLMClient, ModelConfig, ModelConfigurationError, ModelTransportError
from backend.runtime.core.context import AgentRuntime, RuntimeServices, RuntimeState

from .consolidation import MemoryConsolidationRequest
from .extraction import EpisodicExtractionRequest


class MemoryModelUnavailable(RuntimeError):
    """The configured provider cannot currently run Memory work."""


class MemoryQuotaUnavailable(MemoryModelUnavailable):
    """The provider rejected Memory work because quota is unavailable."""


class ProviderMemoryModel:
    """Adapt the existing provider client to the Phase-1 and Phase-2 ports."""

    def __init__(self, config: ModelConfig) -> None:
        self._config = config

    def extract_episodic(self, request: EpisodicExtractionRequest) -> Mapping[str, object]:
        payload = {
            "session_id": request.session_id,
            "project_id": request.project_id,
            "start_position": request.start_position,
            "end_position": request.end_position,
            "messages": [
                {
                    "message_id": value.message_id,
                    "position": value.position,
                    "role": value.role,
                    "content": value.content,
                }
                for value in request.messages
            ],
        }
        return self._complete_json(
            instructions=request.instructions,
            schema=request.output_schema,
            payload=payload,
            model_name=request.model_name,
        )

    def consolidate_memories(self, request: MemoryConsolidationRequest) -> Mapping[str, object]:
        payload = {
            "project_id": request.project_id,
            "candidates": [
                {
                    "candidate_id": value.candidate_id,
                    "episodic_memory_id": value.episodic_memory_id,
                    "content": value.content,
                    "summary": value.summary,
                    "project_id": value.project_id,
                    "confidence": value.confidence,
                    "evidence": [source.excerpt for source in value.evidence],
                }
                for value in request.candidates
            ],
            "existing": [
                {
                    "memory_id": value.memory_id,
                    "kind": value.kind.value,
                    "title": value.title,
                    "content": value.content,
                    "summary": value.summary,
                    "scope": value.scope.value,
                    "project_id": value.project_id,
                    "confidence": value.confidence,
                    "tags": list(value.tags),
                }
                for value in request.existing
            ],
        }
        return self._complete_json(
            instructions=request.instructions,
            schema=request.output_schema,
            payload=payload,
            model_name=request.model_name,
        )

    def _complete_json(
        self,
        *,
        instructions: str,
        schema: Mapping[str, object],
        payload: Mapping[str, object],
        model_name: str,
    ) -> Mapping[str, object]:
        config = replace(self._config, model=model_name) if model_name else self._config
        if not config.api_key:
            raise MemoryModelUnavailable("provider_unavailable")
        client: LLMClient | None = None
        try:
            client = LLMClient(config)
            state = RuntimeState(
                session_id="memory_automation",
                provider=config.provider,
                provider_name=config.provider_name,
                model=config.model,
                model_snapshot={
                    "current_model": config.model,
                    "context_length": config.context_size,
                    "output_length": min(config.max_tokens, 8192),
                    "thinking": "disable",
                    "temperature": 0,
                },
                request_parameters={"temperature": 0, "max_tokens": min(config.max_tokens, 8192)},
            )
            runtime = AgentRuntime(state, RuntimeServices(planner=None, tools=None))
            runtime.exchange.operation = "summarize"
            runtime.exchange.output_mode = "json"
            runtime.exchange.stream = False
            runtime.exchange.messages = [
                SystemMessage(
                    content=(
                        f"{instructions}\n\nReturn one JSON object matching this JSON Schema exactly:\n"
                        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
                    )
                ),
                UserMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            ]
            result = client.run(runtime)
            content = (result.message.content or "").strip()
            if content.startswith("```"):
                lines = content.splitlines()
                content = "\n".join(lines[1:-1]).strip()
            decoded = json.loads(content)
            if not isinstance(decoded, dict):
                raise ValueError("Memory model output must be a JSON object.")
            return decoded
        except MemoryModelUnavailable:
            raise
        except ModelConfigurationError as exc:
            raise MemoryModelUnavailable("provider_unavailable") from exc
        except ModelTransportError as exc:
            if exc.status_code in {402, 429}:
                raise MemoryQuotaUnavailable("quota_unavailable") from exc
            if exc.status_code in {401, 403, 404}:
                raise MemoryModelUnavailable("provider_unavailable") from exc
            raise
        finally:
            if client is not None:
                client.transport.close()


__all__ = [
    "MemoryModelUnavailable",
    "MemoryQuotaUnavailable",
    "ProviderMemoryModel",
]
