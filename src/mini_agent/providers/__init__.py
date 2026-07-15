"""External model-provider adapters and shared Chat Completions transport."""

from .client import ChatCompletionsClient
from .config import ModelConfig
from .deepseek import DeepSeekChatCompletions, DeepSeekCompletion, DeepSeekStreamDelta, DeepSeekToolCall, DeepSeekUsage
from .errors import ModelConfigurationError, ModelRequestError

__all__ = [
    "ChatCompletionsClient",
    "DeepSeekChatCompletions",
    "DeepSeekCompletion",
    "DeepSeekStreamDelta",
    "DeepSeekToolCall",
    "DeepSeekUsage",
    "ModelConfig",
    "ModelConfigurationError",
    "ModelRequestError",
]
