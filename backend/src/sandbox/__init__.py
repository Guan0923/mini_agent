"""Windows-native sandbox subsystem."""

from .broker_service import (
    BROKER_VERSION,
    BrokerConfiguration,
    DpapiKeyStore,
    WindowsBrokerService,
    WindowsDpapiProvider,
    WindowsNamedPipeServer,
    WindowsServiceInstaller,
)
from .control.approvals import ApprovalDecision, ApprovalGrant, ApprovalStore, authorization_hash
from .control.broker import BrokerManagedProcess, BrokerStatus, WindowsBrokerClient
from .control.decision import SandboxExecutionDecision
from .errors import (
    BrokerInstallationError,
    BrokerInstallFailureCode,
    BrokerStatusFailureCode,
    SandboxCleanupPending,
    SandboxError,
    SandboxFailureCode,
    SandboxInitializationError,
    SandboxPolicyError,
    SandboxResourceExceeded,
)
from .native_broker_adapter import WindowsNativeBrokerAdapter
from .native_windows import (
    WindowsAclManager,
    WindowsJobObject,
    WindowsRestrictedTokenFactory,
    WindowsSandboxAccount,
    windows_pipe_security_attributes,
    windows_service_sid,
)
from .policy import (
    FileAccessMode,
    NetworkMode,
    NetworkRule,
    PermissionMode,
    ResourceLimits,
    SandboxJobContext,
    SandboxLimits,
    SandboxPolicy,
    TerminalKind,
    ensure_disk_reserve,
    normalize_permission_mode,
)
from .runtime.admission import AggregateLimits, ResourceRequest, SandboxAdmission, SandboxAdmissionTimeout
from .runtime.launcher import SandboxLauncher
from .runtime.manifest import ResourceManifest, ResourceRecord
from .runtime.reclaimer import SandboxResourceReclaimer
from .runtime.resources import NullResourceProvider, ResourceMonitor, ResourceUsage

__all__ = [
    "ApprovalDecision",
    "ApprovalGrant",
    "ApprovalStore",
    "BrokerInstallFailureCode",
    "BrokerInstallationError",
    "BrokerStatusFailureCode",
    "AggregateLimits",
    "BrokerStatus",
    "BROKER_VERSION",
    "BrokerConfiguration",
    "BrokerManagedProcess",
    "DpapiKeyStore",
    "FileAccessMode",
    "NetworkMode",
    "NetworkRule",
    "NullResourceProvider",
    "PermissionMode",
    "ResourceMonitor",
    "ResourceLimits",
    "ResourceManifest",
    "ResourceRecord",
    "ResourceRequest",
    "ResourceUsage",
    "SandboxCleanupPending",
    "SandboxAdmission",
    "SandboxAdmissionTimeout",
    "SandboxError",
    "SandboxExecutionDecision",
    "SandboxFailureCode",
    "SandboxInitializationError",
    "SandboxLauncher",
    "SandboxJobContext",
    "SandboxLimits",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxResourceExceeded",
    "SandboxResourceReclaimer",
    "TerminalKind",
    "WindowsBrokerClient",
    "WindowsBrokerService",
    "WindowsDpapiProvider",
    "WindowsNamedPipeServer",
    "WindowsServiceInstaller",
    "WindowsAclManager",
    "WindowsJobObject",
    "WindowsNativeBrokerAdapter",
    "WindowsRestrictedTokenFactory",
    "WindowsSandboxAccount",
    "windows_pipe_security_attributes",
    "windows_service_sid",
    "authorization_hash",
    "normalize_permission_mode",
    "ensure_disk_reserve",
]
