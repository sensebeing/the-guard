"""Evidence for agent-assisted rule distillation; no inferred rules are silently installed."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from .topology.discovery import LANGUAGES, discover_topology, source_files


def distill(root: Path | str) -> dict[str, object]:
    root = Path(root).resolve()
    files = list(source_files(root))
    languages = sorted({LANGUAGES[p.suffix] for p in files if p.suffix in LANGUAGES})
    docs = [
        p.relative_to(root).as_posix()
        for p in files
        if p.name in {"AGENTS.md", "CLAUDE.md", "README.md"}
    ]
    tools = []
    if (root / "pyproject.toml").exists():
        pyproject = tomllib.loads((root / "pyproject.toml").read_text())
        tools.extend(sorted(pyproject.get("tool", {})))
    if (root / "package.json").exists():
        package = json.loads((root / "package.json").read_text())
        tools.extend(
            sorted(
                set(package.get("devDependencies", {}))
                & {"eslint", "typescript", "prettier", "biome", "jest", "vitest"}
            )
        )
    samples = []
    for language in languages:
        selected = [p for p in files if LANGUAGES.get(p.suffix) == language][:3]
        samples.extend(
            {
                "path": p.relative_to(root).as_posix(),
                "language": language,
                "excerpt": p.read_text(errors="replace")[:2000],
            }
            for p in selected
        )
    return {
        "schema": "the-guard-distillation/v1",
        "languages": languages,
        "directories": sorted(
            p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
        ),
        "boundary_docs": docs,
        "tools": tools,
        "topology": discover_topology(root),
        "samples": samples,
        "next_step": "Review sampled code and boundary documents with an agent; confirm ownership and conventions before installing tailored rules.",
    }
