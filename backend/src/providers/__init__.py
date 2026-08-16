"""Provider strategies and the high-level LLM client."""

from .canonical import (
    CanonicalProviderAdapter,
    from_chat_completion,
    from_messages,
    from_responses,
    normalize_provider_response,
    to_chat_completions,
    to_messages,
    to_responses,
)
from .client import LLMClient, ProviderAdapter
from .config import ModelConfig
from .deepseek import (
    DeepSeek,
    DeepSeekCompletion,
    DeepSeekStreamDelta,
    DeepSeekToolCall,
    DeepSeekUsage,
)
from .errors import ModelConfigurationError, ModelRequestError, ModelTransportError
from .protocols import ChatCompletionsAdapter, MessagesAdapter, ResponsesAdapter
from .transport import JsonHttpTransport

__all__ = [
    "ChatCompletionsAdapter",
    "CanonicalProviderAdapter",
    "DeepSeek",
    "DeepSeekCompletion",
    "DeepSeekStreamDelta",
    "DeepSeekToolCall",
    "DeepSeekUsage",
    "LLMClient",
    "JsonHttpTransport",
    "MessagesAdapter",
    "ResponsesAdapter",
    "ModelConfig",
    "ModelConfigurationError",
    "ModelRequestError",
    "ModelTransportError",
    "ProviderAdapter",
    "from_chat_completion",
    "from_messages",
    "from_responses",
    "normalize_provider_response",
    "to_chat_completions",
    "to_messages",
    "to_responses",
]
