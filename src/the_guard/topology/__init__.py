"""Public topology APIs: changed-scope checks, discovery, sync and whole-repo audit."""

from .audit import audit_topology, check_contract_drift
from .discovery import discover_topology
from .runner import run_topology_guard, topology_guard_touched
from .sync import sync_topology

run_architecture_topology_guard = run_topology_guard
__all__ = [
    "audit_topology",
    "discover_topology",
    "run_architecture_topology_guard",
    "run_topology_guard",
    "check_contract_drift",
    "sync_topology",
    "topology_guard_touched",
]
