"""Refresh inventory without granting observed imports architectural permission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import load_config
from .discovery import discover_topology


def sync_topology(root: Path | str) -> dict[str, Any]:
    root = Path(root).resolve()
    config = load_config(root)
    discovered = discover_topology(root, config)["modules"]
    existing = {m["path"]: m for m in config.topology.get("modules", [])}
    governed = bool(
        config.topology.get("enforce_module_dependencies")
        or config.topology.get("layers")
    )
    requires_layer = bool(
        config.topology.get("enforce_module_dependencies")
        and config.topology.get("layers")
    )
    merged = dict(existing) if governed else {}
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
            entry["dependencies"] = (previous or {}).get("dependencies", [])
        merged[module["path"]] = entry
    path = root / ".guard/config.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    topology = data.setdefault("topology", {})
    topology["modules"] = [merged[key] for key in sorted(merged)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {**topology, "unregistered_modules": sorted(unregistered)}
