"""Provider-specific failures, separate from planning and runtime failures."""


class ModelConfigurationError(ValueError):
    """The local model configuration is incomplete."""


class ModelRequestError(RuntimeError):
    """The model endpoint could not provide a usable response."""
