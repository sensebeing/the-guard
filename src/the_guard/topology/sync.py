"""Refresh inventory without granting observed imports architectural permission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import load_config, validate_config
from .discovery import discover_topology


def merge_topology(
    topology: dict[str, Any], discovered: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Pure merge of observed inventory into repository-owned permissions."""
    existing = {m["path"]: m for m in topology.get("modules", [])}
    governed = bool(
        topology.get("enforce_module_dependencies") or topology.get("layers")
    )
    requires_layer = bool(
        topology.get("enforce_module_dependencies") and topology.get("layers")
    )
    discovered_paths = {m["path"] for m in discovered}
    pruned = set(existing) - discovered_paths
    merged = (
        {p: m for p, m in existing.items() if p in discovered_paths} if governed else {}
    )
    unregistered = []
    for module in discovered:
        previous = existing.get(module["path"])
        if previous is None and requires_layer:
            unregistered.append(module["path"])
            continue
        entry = {**(previous or {}), **module}
        if previous:
            entry["boundary_docs"] = list(
                dict.fromkeys(
                    [
                        *previous.get("boundary_docs", []),
                        *module["boundary_docs"],
                    ]
                )
            )
        if governed:
            entry["dependencies"] = [
                p for p in (previous or {}).get("dependencies", []) if p not in pruned
            ]
        merged[module["path"]] = entry
    return {**topology, "modules": [merged[key] for key in sorted(merged)]}, sorted(
        unregistered
    )


def sync_topology(root: Path | str) -> dict[str, Any]:
    root = Path(root).resolve()
    config = load_config(root)
    topology, unregistered = merge_topology(
        config.topology, discover_topology(root, config)["modules"]
    )
    path = root / ".guard/config.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data["topology"] = topology
    validate_config(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {**topology, "unregistered_modules": unregistered}
