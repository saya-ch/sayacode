"""静态权限与官方 HITL 的产品适配出口。"""

from .middleware import PolicyMiddleware, build_approval_middleware
from .policy import (
    QUERY_TOOLS,
    READ_ONLY_ALLOWED_TOOLS,
    READ_ONLY_ROLES,
    Policy,
    PolicyDecision,
    hooks_allowed,
    normalize_trust,
)

__all__ = [
    "Policy",
    "PolicyDecision",
    "PolicyMiddleware",
    "QUERY_TOOLS",
    "READ_ONLY_ALLOWED_TOOLS",
    "READ_ONLY_ROLES",
    "build_approval_middleware",
    "hooks_allowed",
    "normalize_trust",
]
