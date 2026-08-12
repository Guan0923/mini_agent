"""Shared API runtime state for the Web backend."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from backend.configuration import ClientPaths, validate_identity_id

from .auth.mail import NullMailer

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_ROOT = Path.home() / ".mini_agent"


class WebAppState:
    """Shared auth/settings/runtime state for the Web backend."""

    def __init__(
        self,
        data_root: Path = DEFAULT_DATA_ROOT,
        *,
        mailer=None,
        auth_repository: Any | None = None,
        settings_repository: Any | None = None,
        database_url: str | None = None,
        secret_key: str | None = None,
        cloud_url: str | None = None,
        cloud_client: Any | None = None,
        snapshot_repository: Any | None = None,
    ) -> None:
        data_root = Path(data_root)
        if data_root.is_symlink():
            raise ValueError("Web user data root cannot be a symbolic link.")
        self.data_root = data_root.resolve()
        from .user_data import ensure_data_root_access

        ensure_data_root_access(self.data_root)
        # Unauthenticated chat is no longer part of the Web contract.  Keep a
        # test/benchmark fallback outside user roots for defensive callers.
        self.chat_workspace = self.data_root.parent / ".mini_agent-cache" / "chat" / "workspace"
        self.benchmark_root = self.data_root.parent / ".mini_agent-cache" / "benchmark"
        self.cloud_client = cloud_client
        self.snapshot_manager = None
        if auth_repository is not None:
            self.auth = auth_repository
            if settings_repository is not None and settings_repository is not auth_repository:
                self.settings = settings_repository
            else:
                # Authentication storage is server-owned.  User preferences,
                # provider ciphertext and sync state remain in the canonical
                # per-identity ``<data_root>/<uuid>/user.db`` even when a
                # lightweight auth repository is injected by an embedding or
                # test harness.
                from backend.storage.user_settings import PerUserSettingsRepository

                self.settings = PerUserSettingsRepository(self.data_root)
        else:
            # Authentication authority belongs to cloud.  The local backend
            # persists only browser sessions and cached identity metadata.
            from backend.storage.auth.local import LocalAuthStore
            from backend.storage.user_settings import PerUserSettingsRepository

            self.auth = LocalAuthStore(self.data_root / "client.db")
            self.settings = PerUserSettingsRepository(data_root)
        del database_url, secret_key

        configured_cloud_url = (
            cloud_url or os.environ.get("CLOUD_URL", "") or os.environ.get("MINI_AGENT_CLOUD_URL", "")
        ).strip()
        if self.cloud_client is None and configured_cloud_url:
            from backend.cloud.client import CloudClient

            self.cloud_client = CloudClient(configured_cloud_url)

        if snapshot_repository is None and self.cloud_client is not None:
            from backend.cloud.snapshot_repository import HttpCloudSnapshotRepository

            snapshot_repository = HttpCloudSnapshotRepository(
                self.cloud_client.base_url,
                self._cloud_token_for_user,
                self._clear_cloud_token_for_user,
            )
        if snapshot_repository is not None:
            from backend.sync.snapshots import SnapshotManager

            self.snapshot_manager = SnapshotManager(
                data_root,
                self.settings,
                snapshot_repository,
                user_allowed=lambda user_id: (
                    (identity := self.auth.user_by_id(user_id)) is not None
                    and not identity.is_guest
                    and self.cloud_client is not None
                ),
            )
        if mailer is not None:
            self.mailer = mailer
        else:
            # Registration and password-reset mail is cloud-owned.  The local
            # backend never opens SMTP connections; this fallback is retained
            # for injected/offline test stores and guest-only operation.
            self.mailer = NullMailer()
        from .auth.service import AuthService

        self.auth_service = AuthService(self)

    def user_paths(self, user_id: str) -> ClientPaths:
        from .user_data import user_paths

        validate_identity_id(user_id, require_uuid=True)
        return user_paths(self.data_root, user_id)

    def user_workspace(self, user_id: str, session_id: str) -> Path:
        from .user_data import user_workspace

        return user_workspace(self.data_root, user_id, session_id)

    def copy_session_files(self, user_id: str, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_files

        copy_session_files(self.data_root, user_id, source_session_id, target_session_id)

    def mark_sync_dirty(self, user_id: str) -> None:
        """Mark account data dirty while keeping guest activity local-only."""

        identity = self.auth.user_by_id(user_id)
        if identity is None or identity.is_guest or self.snapshot_manager is None:
            return
        self.snapshot_manager.mark_dirty(user_id)

    def _cloud_token_for_user(self, user_id: str) -> str:
        from backend.cloud.client import CloudAuthExpired

        reader = getattr(self.settings, "cloud_token_for_user", None)
        value = reader(user_id) if callable(reader) else None
        if not isinstance(value, dict) or not value.get("token"):
            raise CloudAuthExpired("云端登录状态已过期，请重新登录。", status_code=401)
        expires_at = float(value.get("expires_at") or 0)
        if expires_at and expires_at <= time.time():
            clearer = getattr(self.settings, "clear_cloud_token", None)
            if callable(clearer):
                clearer(user_id)
            raise CloudAuthExpired("云端登录状态已过期，请重新登录。", status_code=401)
        return str(value["token"])

    def _clear_cloud_token_for_user(self, user_id: str) -> None:
        clearer = getattr(self.settings, "clear_cloud_token", None)
        if callable(clearer):
            clearer(user_id)

    def user_benchmark_root(self, user_id: str) -> Path:
        validate_identity_id(user_id, require_uuid=True)
        cache_root = self.benchmark_root.parent
        if cache_root.is_symlink() or (cache_root.exists() and not cache_root.is_dir()):
            raise ValueError("Benchmark cache root cannot be a symbolic link or regular file.")
        if self.benchmark_root.is_symlink() or (self.benchmark_root.exists() and not self.benchmark_root.is_dir()):
            raise ValueError("Benchmark cache directory cannot be a symbolic link or regular file.")
        path = self.benchmark_root / user_id
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("Benchmark user path must be a regular directory.")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def settings_for_user(self, user_id: str) -> dict[str, object]:
        identity = self.auth.user_by_id(user_id)
        email = (identity.email or "") if identity is not None else ""
        result = self.settings.settings_for_user(user_id, email=email)
        preferences = getattr(self.settings, "sync_preferences_for_user", None)
        sync_state = getattr(self.settings, "sync_state_for_user", None)
        result["sync_preferences"] = (
            preferences(user_id) if callable(preferences) else {"auto_save_enabled": False, "auto_save_rule": "idle_5m"}
        )
        result["sync_state"] = (
            sync_state(user_id)
            if callable(sync_state)
            else {
                "local_revision": 0,
                "uploaded_revision": 0,
                "cloud_snapshot_id": None,
                "status": "local_only",
                "last_error": "",
                "updated_at": None,
            }
        )
        result["cloud_sync_available"] = bool(
            identity is not None and getattr(identity, "kind", "account") != "guest" and self.cloud_client is not None
        )
        return result

    def model_config_for_user(self, user_id: str):
        return self.settings.model_config_for_user(user_id)

    def agent_config_for_user(self, user_id: str) -> dict[str, object]:
        return self.settings.agent_config_for_user(user_id)

    def agent_preferences_for_user(self, user_id: str) -> str:
        return self.settings.agent_preferences_for_user(user_id)

    def runtime_config_for_user(self, user_id: str) -> dict[str, object]:
        return self.settings.runtime_config_for_user(user_id)

    def close(self) -> None:
        closed: set[int] = set()
        for resource in (self.snapshot_manager, self.cloud_client, self.mailer, self.settings, self.auth):
            if resource is None:
                continue
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                close()
