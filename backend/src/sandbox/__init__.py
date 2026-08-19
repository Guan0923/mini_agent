"""Windows-native sandbox subsystem."""

from .admission import AggregateLimits, ResourceRequest, SandboxAdmission, SandboxAdmissionTimeout
from .approvals import ApprovalDecision, ApprovalGrant, ApprovalStore, authorization_hash
from .broker import BrokerStatus, WindowsBrokerClient
from .broker_service import (
    BROKER_VERSION,
    AccountLease,
    AccountPool,
    BrokerConfiguration,
    DpapiKeyStore,
    UserAccountPools,
    WindowsBrokerService,
    WindowsDpapiProvider,
    WindowsNamedPipeServer,
    WindowsServiceInstaller,
)
from .errors import (
    SandboxCleanupPending,
    SandboxError,
    SandboxFailureCode,
    SandboxInitializationError,
    SandboxPolicyError,
    SandboxResourceExceeded,
)
from .launcher import SandboxLauncher
from .manifest import ResourceManifest, ResourceRecord
from .policy import (
    NetworkMode,
    NetworkRule,
    PermissionMode,
    SandboxLimits,
    SandboxPolicy,
    TerminalKind,
    ensure_disk_reserve,
    migrate_legacy_permission_mode,
    normalize_permission_mode,
    resolve_network_rules,
)
from .resources import NullResourceProvider, ResourceMonitor, ResourceUsage

__all__ = [
    "ApprovalDecision",
    "ApprovalGrant",
    "ApprovalStore",
    "AccountLease",
    "AccountPool",
    "AggregateLimits",
    "BrokerStatus",
    "BROKER_VERSION",
    "BrokerConfiguration",
    "DpapiKeyStore",
    "NetworkMode",
    "NetworkRule",
    "NullResourceProvider",
    "PermissionMode",
    "ResourceMonitor",
    "ResourceManifest",
    "ResourceRecord",
    "ResourceRequest",
    "ResourceUsage",
    "SandboxCleanupPending",
    "SandboxAdmission",
    "SandboxAdmissionTimeout",
    "SandboxError",
    "SandboxFailureCode",
    "SandboxInitializationError",
    "SandboxLauncher",
    "SandboxLimits",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxResourceExceeded",
    "TerminalKind",
    "WindowsBrokerClient",
    "WindowsBrokerService",
    "WindowsDpapiProvider",
    "WindowsNamedPipeServer",
    "WindowsServiceInstaller",
    "UserAccountPools",
    "authorization_hash",
    "normalize_permission_mode",
    "migrate_legacy_permission_mode",
    "resolve_network_rules",
    "ensure_disk_reserve",
]
