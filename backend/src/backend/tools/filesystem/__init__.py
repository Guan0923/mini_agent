"""Workspace-confined text-file discovery, search, and mutation tools."""

from .paths import normalized_workspace_path
from .workspace import WorkspaceFiles

__all__ = ["WorkspaceFiles", "normalized_workspace_path"]
