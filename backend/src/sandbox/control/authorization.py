"""Compatibility import for session-bound approval primitives."""

from .approvals import ApprovalDecision, ApprovalGrant, ApprovalStore, authorization_hash

__all__ = ["ApprovalDecision", "ApprovalGrant", "ApprovalStore", "authorization_hash"]
