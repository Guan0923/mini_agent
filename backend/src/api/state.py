"""Shared API runtime state for the Web backend."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

from backend.configuration import ClientPaths, validate_identity_id
from backend.domain.terminal import TERMINAL_LABELS
from backend.jobs import JobRegistry
from backend.sandbox import WindowsBrokerClient
from backend.storage.projects import ProjectStore
from backend.tools.terminal import available_terminal_executables, effective_terminal_type

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
        event_repository: Any | None = None,
        project_picker: Callable[[], Path | None] | None = None,
        job_registry: JobRegistry | None = None,
        sandbox_broker: WindowsBrokerClient | None = None,
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
        self.project_picker = project_picker
        self.job_registry = job_registry or JobRegistry()
        self.sandbox_broker = sandbox_broker or WindowsBrokerClient.from_system()
        self.system_job_scope = self.job_registry.root_scope()
        # Process-local active dynamic-node configuration registry.  It is
        # intentionally not persisted; the durable node remains the source of
        # truth and a crashed run leaves its failed placeholder recoverable.
        # Registries are scoped by both identity and session.  A session id is
        # only unique inside one user's settings/runtime root, so using it as
        # the sole key could let one authenticated user observe or mutate
        # another user's active run in a shared process.
        self.active_runtime_configs: dict[tuple[str, str], dict[str, object]] = {}
        self.active_runtime_bridges: dict[tuple[str, str], object] = {}
        # A PATCH and the worker's next-boundary consumption can arrive on
        # different threads.  Serialize those transitions per active session
        # so a partial update can never be interleaved with another update.
        self.active_runtime_config_locks: dict[tuple[str, str], RLock] = {}
        self.snapshot_manager = None
        self.event_sync_manager = None
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

            # A browser can present a session before the request identifies a
            # user, so this device-level index cannot live under one user's
            # directory.  Keep it outside the authenticated data root; the
            # root itself is reserved for ``<user_id>`` directories.
            auth_path = self.data_root.parent / ".mini_agent-cache" / "auth" / "client.db"
            self.auth = LocalAuthStore(auth_path)
            self.settings = PerUserSettingsRepository(data_root)
        del database_url, secret_key

        configured_cloud_url = (
            cloud_url or os.environ.get("CLOUD_URL", "") or os.environ.get("MINI_AGENT_CLOUD_URL", "")
        ).strip()
        if self.cloud_client is None and configured_cloud_url:
            from backend.cloud.client import CloudClient

            self.cloud_client = CloudClient(configured_cloud_url)

        if event_repository is None and self.cloud_client is not None:
            from backend.cloud.event_repository import HttpCloudEventRepository

            event_repository = HttpCloudEventRepository(
                self.cloud_client.base_url,
                self._cloud_token_for_user,
                self._clear_cloud_token_for_user,
            )
        if event_repository is not None:
            from backend.sync.events_manager import EventSyncManager

            self.snapshot_manager = EventSyncManager(
                data_root,
                self.settings,
                event_repository,
                job_registry=self.job_registry,
                user_allowed=lambda user_id: (
                    (identity := self.auth.user_by_id(user_id)) is not None
                    and not identity.is_guest
                    and self.cloud_client is not None
                ),
            )
            self.event_sync_manager = self.snapshot_manager
        if mailer is not None:
            self.mailer = mailer
        else:
            # Registration and password-reset mail is cloud-owned.  The local
            # backend never opens SMTP connections; this fallback is retained
            # for injected/offline test stores and guest-only operation.
            self.mailer = NullMailer()
        from .auth.service import AuthService

        self.auth_service = AuthService(self)
        self._project_stores: dict[str, ProjectStore] = {}

    def user_paths(self, user_id: str) -> ClientPaths:
        from .user_data import user_paths

        validate_identity_id(user_id, require_uuid=True)
        paths = user_paths(self.data_root, user_id)
        identity = self.auth.user_by_id(user_id)
        if identity is not None:
            self.settings.ensure_profile(
                user_id,
                display_name_default="游客用户" if identity.is_guest else (identity.email or "用户"),
            )
        return paths

    def projects(self, user_id: str) -> ProjectStore:
        validate_identity_id(user_id, require_uuid=True)
        store = self._project_stores.get(user_id)
        if store is None:
            store = ProjectStore(self.user_paths(user_id).projects_db)
            self._project_stores[user_id] = store
        return store

    def user_workspace(self, user_id: str, session_id: str) -> Path:
        from .user_data import user_workspace

        return user_workspace(self.data_root, user_id, session_id)

    def session_workspace(self, user_id: str, session_id: str) -> Path:
        """Resolve the effective cwd for a session and validate project access."""

        bound = self.projects(user_id).session_project(session_id)
        if bound is None:
            return self.user_workspace(user_id, session_id)
        if bound.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后再运行。")
        path = Path(bound.cwd)
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
            return resolved
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。") from exc

    def copy_session_files(self, user_id: str, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_files

        copy_session_files(self.data_root, user_id, source_session_id, target_session_id)

    def copy_session_uploads(self, user_id: str, source_session_id: str, target_session_id: str) -> None:
        from .user_data import copy_session_uploads

        copy_session_uploads(self.data_root, user_id, source_session_id, target_session_id)

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
        if identity is not None:
            self.settings.ensure_profile(
                user_id,
                display_name_default="游客用户" if identity.is_guest else (email or "用户"),
            )
        result = self.settings.settings_for_user(user_id, email=email)
        if os.name == "nt":
            available = available_terminal_executables(is_windows=True)
            runtime = result.get("runtime_config")
            current = dict(runtime) if isinstance(runtime, dict) else {}
            requested = current.get("terminal_type", "cmd")
            effective = effective_terminal_type(requested, is_windows=True)
            notice: str | None = None
            if not available:
                notice = "未检测到当前系统可用的终端。"
            elif effective != requested:
                notice = "已保存的终端当前不可用，本次已回退到可用终端。"
            current["terminal_type"] = effective
            result["runtime_config"] = current
            result["terminal_options"] = [{"value": name, "label": TERMINAL_LABELS[name]} for name in available]
            result["terminal_notice"] = notice
        else:
            result["terminal_options"] = []
            result["terminal_notice"] = None
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
                "cloud_revision": 0,
                "pending_event_count": 0,
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

    def model_config_for_provider_name(self, user_id: str, provider_name: str | None):
        resolver = getattr(self.settings, "model_config_for_provider_name", None)
        return resolver(user_id, provider_name) if callable(resolver) else self.model_config_for_user(user_id)

    def agent_config_for_user(self, user_id: str) -> dict[str, object]:
        return self.settings.agent_config_for_user(user_id)

    def agent_preferences_for_user(self, user_id: str) -> str:
        return self.settings.agent_preferences_for_user(user_id)

    def runtime_config_for_user(self, user_id: str) -> dict[str, object]:
        return self.settings.runtime_config_for_user(user_id)

    def close(self) -> None:
        # Stop all registered carriers before closing stores they may still
        # reference.  The registry is process-owned and therefore closes
        # exactly once even when an injected resource aliases another close.
        self.job_registry.close_all(reason="web application closed", timeout=5.0)
        closed: set[int] = set()
        for resource in (
            self.snapshot_manager,
            self.cloud_client,
            self.mailer,
            self.settings,
            self.auth,
            *self._project_stores.values(),
        ):
            if resource is None:
                continue
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                close()
