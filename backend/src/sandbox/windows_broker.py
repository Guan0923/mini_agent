"""Compatibility import for the Windows Broker client."""

from .control.broker import BrokerStatus, WindowsBrokerClient

__all__ = ["BrokerStatus", "WindowsBrokerClient"]
