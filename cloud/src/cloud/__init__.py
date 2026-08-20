"""Mini-Agent cloud control plane.

The cloud package owns account identity, authentication, and encrypted
event metadata.  It deliberately has no dependency on the local Agent
Runtime or client filesystem.
"""

__all__ = ["__version__"]
__version__ = "0.3.0"
