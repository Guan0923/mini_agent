"""Provider-backed Memory model lifecycle tests without external requests."""

from __future__ import annotations

import json

from backend.providers import LLMClient, ModelConfig
from backend.providers.transport import JsonHttpTransport
from backend.runtime.memory import provider_models
from backend.runtime.memory.extraction import CleanMemoryMessage, EpisodicExtractionRequest


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    @staticmethod
    def raise_for_status() -> None:
        return None

    @staticmethod
    def json() -> dict[str, object]:
        return {
            "id": "response_fixed",
            "model": "model_fixed",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"candidates": []}),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _Session:
    def __init__(self) -> None:
        self.closed = False

    @staticmethod
    def post(*_args, **_kwargs) -> _Response:
        return _Response()

    def close(self) -> None:
        self.closed = True


def test_provider_memory_model_closes_transport_after_valid_json(monkeypatch) -> None:
    session = _Session()
    transport = JsonHttpTransport(session)  # type: ignore[arg-type]
    config = ModelConfig(
        api_key="fixed-test-key",
        base_url="https://example.invalid/v1",
        model="model_fixed",
    )

    def client_factory(value: ModelConfig) -> LLMClient:
        return LLMClient(value, transport=transport)

    monkeypatch.setattr(provider_models, "LLMClient", client_factory)
    request = EpisodicExtractionRequest(
        session_id="session_fixed",
        project_id=None,
        start_position=0,
        end_position=1,
        messages=(CleanMemoryMessage("turn_fixed", 1, "user", "A durable preference."),),
    )

    result = provider_models.ProviderMemoryModel(config).extract_episodic(request)

    assert result == {"candidates": []}
    assert session.closed is True
