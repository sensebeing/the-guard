"""Deterministic module inventory; discovery reports facts, never invents ownership."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..config import GuardConfig, load_config
from .imports import JS_EXTENSIONS, extract_imports, resolve_target_module
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
    "build_docs",
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


def discover_topology(
    root: Path, config: GuardConfig | None = None
) -> dict[str, list[dict[str, Any]]]:
    root = Path(root).resolve()
    config = config or load_config(root)
    files = [
        p
        for p in source_files(root)
        if not is_excluded_topology_path(p.relative_to(root).as_posix())
    ]
    rooted_candidates = set()
    fallback_candidates = set()
    for path in files:
        rel = path.relative_to(root)
        parts = rel.parts
        if (
            len(parts) >= 3
            and parts[0] in config.module_doc_roots
            and not parts[1].startswith(".")
            and parts[1] not in {"tests", "test"}
        ):
            rooted_candidates.add("/".join(parts[:2]))
        elif (
            len(parts) >= 2
            and not parts[0].startswith(".")
            and parts[0] not in NON_MODULE_TOP_DIRS
            and (
                path.suffix in LANGUAGES
                or path.name
                in (
                    "AGENTS.md",
                    "CLAUDE.md",
                    "README.md",
                    "pyproject.toml",
                    "package.json",
                )
            )
        ):
            fallback_candidates.add(parts[0])
    candidates = rooted_candidates | (
        fallback_candidates - {p.split("/")[0] for p in rooted_candidates}
    )
    modules = []
    module_imports = {}
    for directory in sorted(candidates):
        owned = [
            p
            for p in files
            if p.relative_to(root).as_posix().startswith(directory + "/")
        ]
        imports = set()
        for path in owned:
            if path.suffix not in JS_EXTENSIONS | {".py"}:
                continue
            relative = path.relative_to(root).as_posix()
            for _, spec in extract_imports(
                relative, path.read_text(errors="replace"), config.module_doc_roots
            ):
                target = resolve_target_module(
                    relative, spec, sorted(candidates), config.module_doc_roots
                )
                if target:
                    imports.add(target)
        module_imports[directory] = imports
        modules.append(
            {
                "path": directory,
                "languages": sorted(
                    {LANGUAGES[p.suffix] for p in owned if p.suffix in LANGUAGES}
                ),
                "boundary_docs": [
                    f"{directory}/{name}"
                    for name in ("AGENTS.md", "CLAUDE.md", "README.md")
                    if (root / directory / name).is_file()
                ],
            }
        )
    for module in modules:
        module["dependencies"] = sorted(module_imports[module["path"]])
    return {"modules": modules}
