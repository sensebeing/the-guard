"""Public API for repository-owned engineering policy and review."""

from __future__ import annotations

from .config import GuardConfig, load_config
from .finding import Finding, exempt_pattern
from .policy import Policy, PolicyError, Rule, load_policy
from .review.engine import run_architecture_review
from .topology import discover_topology, run_topology_guard

__version__ = "0.1.0"
__all__ = [
    "__version__",
    "GuardConfig",
    "load_config",
    "Finding",
    "exempt_pattern",
    "Policy",
    "PolicyError",
    "Rule",
    "load_policy",
    "run_topology_guard",
    "discover_topology",
    "run_architecture_review",
]
