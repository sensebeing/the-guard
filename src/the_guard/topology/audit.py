"""Whole-repository topology evidence, independent of changed-line ratchets."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..finding import Finding
from ..git_helpers import BaseFileReader, git_ok, git_stdout, submodule_paths
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
    all_text_files = sorted(
        p.relative_to(root).as_posix()
        for p in source_files(root)
        if b"\x00" not in p.read_bytes()
    )
    _, stale = _dependency_evidence(
        config, all_text_files, lambda p: (root / p).read_text(errors="replace")
    )
    unregistered = sorted(
        m["path"]
        for m in discover_topology(root, config)["modules"]
        if m["path"] not in modules
    )
    missing = _missing_docs(root, config)
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


def _dependency_evidence(
    config: GuardConfig, files: Sequence[str], read: Callable[[str], str | None]
) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    """Shared deterministic coverage and stale-edge evidence for audit and ratchets."""
    modules = {m["path"]: m for m in config.topology.get("modules", [])}
    paths = list(modules)
    observed: dict[str, set[str]] = {m: set() for m in modules}
    source_extensions: dict[str, set[str]] = {m: set() for m in modules}
    for path in files:
        if is_excluded_topology_path(path):
            continue
        source = owner_module(path, paths)
        if source is None:
            continue
        source_extensions[source].add(Path(path).suffix)
        if Path(path).suffix not in DETERMINISTIC_IMPORT_EXTENSIONS:
            continue
        for _, _, spec in extract_imports(
            path, read(path) or "", config.module_doc_roots
        ):
            target = resolve_target_module(path, spec, paths, config.module_doc_roots)
            if target:
                observed[source].add(target)
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
    return observed, stale


def _missing_docs(root: Path, config: GuardConfig) -> list[dict[str, str]]:
    declarations = [("<root>", config.topology.get("architecture_docs", []))]
    declarations.extend(
        (m["path"], m.get("boundary_docs", []))
        for m in config.topology.get("modules", [])
    )
    return [
        {"module": module, "doc": doc}
        for module, docs in sorted(declarations)
        for doc in sorted(set(docs))
        if not repository_path(root, doc).is_file()
    ]


def check_contract_drift(
    root: Path | str,
    changed_files: Sequence[str],
    *,
    base: str | None = None,
    config: GuardConfig | None = None,
) -> list[Finding]:
    """Block newly broken contracts while grandfathering untouched baseline debt."""
    root = Path(root).resolve()
    config = config or load_config(root)
    base = base or "origin/" + config.main_branch
    available = git_ok(
        root, ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"]
    )
    reader = BaseFileReader(root, base, submodule_paths(root))
    config_path = ".guard/config.json"
    baseline = None
    if available:
        try:
            baseline = load_config(root, revision=base)
        except ValueError:
            # Missing/invalid historical declarations cannot grandfather new permissions.
            baseline = None
    old_declared_docs = set()
    if baseline is not None:
        old_declared_docs.update(
            ("<root>", doc) for doc in baseline.topology.get("architecture_docs", [])
        )
        old_declared_docs.update(
            (module["path"], doc)
            for module in baseline.topology.get("modules", [])
            for doc in module.get("boundary_docs", [])
        )
    rule_id = config.rule_id_prefix + "single-source-of-truth"
    findings = [
        Finding(
            rule_id,
            config_path,
            1,
            f"Declared boundary document '{item['doc']}' for '{item['module']}' is missing; update its contract.",
        )
        for item in _missing_docs(root, config)
        if not available
        or baseline is None
        or (item["module"], item["doc"]) not in old_declared_docs
        or reader.read(item["doc"]) is not None
    ]
    if not (
        config.topology.get("enforce_module_dependencies")
        or config.topology.get("layers")
    ):
        return findings
    modules = {m["path"]: m for m in config.topology.get("modules", [])}
    old_modules = (
        {m["path"]: m for m in baseline.topology.get("modules", [])} if baseline else {}
    )
    candidates = {
        m
        for m in modules
        if set(modules[m].get("dependencies", []))
        != set(old_modules.get(m, {}).get("dependencies", []))
    }
    deleted = ""
    if available:
        deleted = git_stdout(
            root, ["git", "diff", "--name-only", "--diff-filter=D", "-z", base, "--"]
        )
        if deleted is None:
            raise ValueError("Cannot inspect deleted module files against baseline")
    for path in {*changed_files, *filter(None, deleted.split("\0"))}:
        owner = owner_module(path, list(modules))
        if owner is None:
            continue
        current = repository_path(root, path)
        text = current.read_text(errors="replace") if current.is_file() else None
        if not available or reader.read(path) != text:
            candidates.add(owner)
    files = [
        p.relative_to(root).as_posix()
        for p in source_files(root)
        if b"\x00" not in p.read_bytes()
    ]
    _, stale = _dependency_evidence(
        config, files, lambda p: (root / p).read_text(errors="replace")
    )
    old_observed: dict[str, set[str]] = {}
    if baseline and candidates:
        output = git_stdout(
            root,
            [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                base,
                "--",
                *[f":(literal){p}" for p in sorted(candidates)],
            ],
        )
        if output is None:
            raise ValueError("Cannot inspect baseline module imports")
        old_observed, _ = _dependency_evidence(
            baseline, [p for p in output.split("\0") if p], reader.read
        )
    for item in stale:
        module, dependency = item["module"], item["unused_dependency"]
        if module in candidates and (
            not available
            or dependency not in old_modules.get(module, {}).get("dependencies", [])
            or dependency in old_observed.get(module, set())
        ):
            findings.append(
                Finding(
                    rule_id,
                    config_path,
                    1,
                    f"Stale dependency '{module}' -> '{dependency}'; remove the unused declared dependency.",
                )
            )
    return findings
