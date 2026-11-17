"""Whole-repository topology evidence, independent of changed-line ratchets."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import GuardConfig, load_config, repository_path
from .discovery import discover_topology, source_files
from .imports import (
    DETERMINISTIC_IMPORT_EXTENSIONS,
    extract_imports,
    owner_module,
    resolve_target_module,
)
from .paths import is_excluded_topology_path
from .runner import run_topology_guard

INERT_MODULE_FILE_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".lock",
}


def audit_topology(
    root: Path | str, config: GuardConfig | None = None
) -> dict[str, Any]:
    root = Path(root).resolve()
    config = config or load_config(root)
    modules = {m["path"]: m for m in config.topology.get("modules", [])}
    paths = list(modules)
    all_text_files = sorted(
        p.relative_to(root).as_posix()
        for p in source_files(root)
        if b"\x00" not in p.read_bytes()
    )
    observed: dict[str, set[str]] = {m: set() for m in modules}
    source_extensions: dict[str, set[str]] = {m: set() for m in modules}
    for path in all_text_files:
        if is_excluded_topology_path(path):
            continue
        source = owner_module(path, paths)
        if source is None:
            continue
        source_extensions[source].add(Path(path).suffix)
        if Path(path).suffix not in DETERMINISTIC_IMPORT_EXTENSIONS:
            continue
        for _, _, spec in extract_imports(
            path, (root / path).read_text(errors="replace"), config.module_doc_roots
        ):
            target = resolve_target_module(path, spec, paths, config.module_doc_roots)
            if target:
                observed[source].add(target)
    unregistered = sorted(
        m["path"]
        for m in discover_topology(root, config)["modules"]
        if m["path"] not in modules
    )
    missing = sorted(
        (
            {"module": m["path"], "doc": doc}
            for m in modules.values()
            for doc in set(m.get("boundary_docs", []))
            if not repository_path(root, doc).is_file()
        ),
        key=lambda item: (item["module"], item["doc"]),
    )
    stale = sorted(
        (
            {"module": m["path"], "unused_dependency": dep}
            for m in modules.values()
            for dep in set(m.get("dependencies", []))
            if dep not in observed[m["path"]]
            and source_extensions[m["path"]] & DETERMINISTIC_IMPORT_EXTENSIONS
            and not (
                source_extensions[m["path"]]
                - DETERMINISTIC_IMPORT_EXTENSIONS
                - INERT_MODULE_FILE_EXTENSIONS
            )
        ),
        key=lambda item: (item["module"], item["unused_dependency"]),
    )
    findings = [
        asdict(f)
        for f in run_topology_guard(root, all_text_files, config=config, full_scan=True)
    ]
    governed = config.topology.get(
        "enforce_module_dependencies"
    ) or config.topology.get("layers")
    return {
        "unregistered_modules": unregistered,
        "missing_boundary_docs": missing,
        "stale_dependencies": stale,
        "findings": findings,
        "passed": not (findings or missing or (governed and (unregistered or stale))),
    }
