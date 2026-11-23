"""Deterministic module inventory; discovery reports facts, never invents ownership."""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import GuardConfig, load_config
from .imports import (
    DETERMINISTIC_IMPORT_EXTENSIONS,
    extract_imports,
    owner_module,
    resolve_target_module,
)
from .paths import is_excluded_topology_path

IGNORED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".build",
    "dist",
    "build",
    ".worktrees",
    ".guard",
}
NON_MODULE_TOP_DIRS = {
    "docs",
    "examples",
    "benchmarks",
    "scripts",
    "tests",
    "test",
    "bin",
    "migrations",
    "alembic",
    "skills",
}
LANGUAGES = {
    ".py": "Python",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
}


def source_files(root: Path) -> Iterator[Path]:
    root = Path(root).resolve()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in IGNORED
            and not d.endswith((".egg-info", ".dist-info"))
            and not (Path(directory) / d).is_symlink()
        )
        for name in sorted(files):
            path = Path(directory) / name
            if not path.is_symlink():
                yield path


def candidate_module_for_path(
    rel_posix: str, module_doc_roots: Sequence[str]
) -> str | None:
    """Propose an owner for uncovered production source using inventory rules."""
    path = PurePosixPath(rel_posix)
    if path.suffix not in LANGUAGES or is_excluded_topology_path(rel_posix):
        return None
    parts = path.parts
    if (
        len(parts) >= 3
        and parts[0] in module_doc_roots
        and not parts[1].startswith(".")
        and parts[1] not in {"tests", "test"}
    ):
        return "/".join(parts[:2])
    if (
        len(parts) >= 2
        and not parts[0].startswith(".")
        and parts[0] not in NON_MODULE_TOP_DIRS
    ):
        return parts[0]
    return None


def discover_topology(
    root: Path, config: GuardConfig | None = None
) -> dict[str, list[dict[str, Any]]]:
    # Declared owners anchor inventory; only uncovered production source proposes new owners.
    root = Path(root).resolve()
    config = config or load_config(root)
    files = [
        p
        for p in source_files(root)
        if not is_excluded_topology_path(p.relative_to(root).as_posix())
    ]
    declarations = {m["path"]: m for m in config.topology.get("modules", [])}
    declared = list(declarations)
    retired = {path for path, module in declarations.items() if module.get("retired", False)}
    rooted_candidates = set()
    fallback_candidates = set()
    for path in files:
        rel = path.relative_to(root)
        if (
            path.suffix not in LANGUAGES
            or owner_module(rel.as_posix(), declared) is not None
        ):
            continue
        candidate = candidate_module_for_path(rel.as_posix(), config.module_doc_roots)
        if candidate is not None:
            (rooted_candidates if "/" in candidate else fallback_candidates).add(
                candidate
            )
    candidates = rooted_candidates | (
        fallback_candidates - {p.split("/")[0] for p in rooted_candidates}
    )
    candidates.update(path for path in declared if (root / path).exists())
    candidates.update(retired)
    modules = []
    for directory in sorted(candidates):
        if directory in retired:
            modules.append(dict(declarations[directory]))
            continue
        owned = [
            p
            for p in files
            if owner_module(p.relative_to(root).as_posix(), sorted(candidates))
            == directory
        ]
        imports = set()
        for path in owned:
            if path.suffix not in DETERMINISTIC_IMPORT_EXTENSIONS:
                continue
            relative = path.relative_to(root).as_posix()
            for _, _, spec in extract_imports(
                relative, path.read_text(errors="replace"), config.module_doc_roots
            ):
                target = resolve_target_module(
                    relative, spec, sorted(candidates), config.module_doc_roots
                )
                if target:
                    imports.add(target)
        modules.append(
            {
                "path": directory,
                "languages": sorted(
                    {LANGUAGES[p.suffix] for p in owned if p.suffix in LANGUAGES}
                ),
                "boundary_docs": [
                    f"{directory}/{name}"
                    for name in ("AGENTS.md", "CLAUDE.md", "README.md")
                    if (root / directory).is_dir()
                    and (root / directory / name).is_file()
                ],
                "dependencies": sorted(imports),
            }
        )
    return {"modules": modules}
