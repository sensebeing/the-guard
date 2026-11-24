"""Whole-repository topology evidence, independent of changed-line ratchets."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..finding import Finding
from ..git_helpers import BaseFileReader, compute_changed_lines, git_ok, git_stdout, submodule_paths
from ..config import GuardConfig, load_config, repository_path
from .configured import check_module_imports
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
    retired = sorted(p for p, module in modules.items() if module.get("retired", False))
    missing_modules = sorted(
        p for p in modules if p not in retired and not repository_path(root, p).exists()
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
        "retired_modules": retired,
        "missing_boundary_docs": missing,
        "missing_modules": missing_modules,
        "stale_dependencies": stale,
        "findings": findings,
        "passed": not (
            findings
            or missing
            or missing_modules
            or (governed and (unregistered or stale))
        ),
    }


def _dependency_evidence(
    config: GuardConfig,
    files: Sequence[str],
    read: Callable[[str], str | None],
    *,
    coverage_files: Sequence[str] = (),
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
    for path in coverage_files:
        if is_excluded_topology_path(path):
            continue
        source = owner_module(path, paths)
        if source is not None:
            source_extensions[source].add(Path(path).suffix)
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


def _missing_docs(
    root: Path, config: GuardConfig, *, revision: str | None = None
) -> list[dict[str, str]]:
    declarations = [("<root>", config.topology.get("architecture_docs", []))]
    declarations.extend(
        (m["path"], m.get("boundary_docs", []))
        for m in config.topology.get("modules", [])
        if not m.get("retired", False)
    )
    existing = None
    if revision is not None:
        paths = {doc for _, docs in declarations for doc in docs}
        existing = _snapshot_paths(root, revision, paths, files_only=True)
    return [
        {"module": module, "doc": doc}
        for module, docs in sorted(declarations)
        for doc in sorted(set(docs))
        if (
            doc not in existing
            if existing is not None
            else not repository_path(root, doc).is_file()
        )
    ]


def _snapshot_paths(
    root: Path,
    revision: str,
    paths: set[str] | None,
    *,
    recursive: bool = False,
    files_only: bool = False,
) -> set[str]:
    """Inventory snapshot paths, or only blob paths when files_only is set."""
    if paths is not None and not paths:
        return set()
    output = git_stdout(
        root,
        [
            "git",
            "ls-tree",
            *(["-r"] if recursive else []),
            "-z",
            revision,
            "--",
            *[f":(literal){p}" for p in sorted(paths or ())],
        ],
    )
    if output is None:
        raise ValueError(f"Cannot inspect module paths at {revision}")
    result = set()
    for entry in output.split("\0"):
        metadata, separator, path = entry.partition("\t")
        if separator and (not files_only or metadata.split()[1:2] == ["blob"]):
            result.add(path)
            if paths is not None and not recursive and not files_only:
                # With nested pathspecs, ls-tree can emit children in place of a tree.
                parent = path.rpartition("/")[0]
                while parent:
                    if parent in paths:
                        result.add(parent)
                    parent = parent.rpartition("/")[0]
    return result


def _tightened_import_scope(
    config: GuardConfig, baseline: GuardConfig | None
) -> tuple[set[str], bool]:
    """Return tightened active owners and whether retirements also require a repository scan."""
    if baseline is None:
        return set(), False
    current, old = config.topology, baseline.topology
    modules = {m["path"]: m for m in current.get("modules", [])}
    old_modules = {m["path"]: m for m in old.get("modules", [])}
    resolution_changed = (
        modules.keys() != old_modules.keys() or config.module_doc_roots != baseline.module_doc_roots
    )
    retired = {p for p, m in modules.items() if m.get("retired", False)}
    new_retirement = any(not old_modules.get(p, {}).get("retired", False) for p in retired)
    scan_all_callers = new_retirement or (bool(retired) and resolution_changed)
    if (
        resolution_changed
        or new_retirement
        or (current.get("enforce_module_dependencies") and not old.get("enforce_module_dependencies"))
        or (current.get("layers") and not old.get("layers"))
    ):
        # Owner additions/removals change longest-prefix and alias resolution globally.
        return set(modules), scan_all_callers
    tightened = {
        path
        for path, module in modules.items()
        if path in old_modules
        and (
            module.get("layer") != old_modules[path].get("layer")
            or set(old_modules[path].get("dependencies", []))
            - set(module.get("dependencies", []))
            or set(old.get("layers", {}).get(module.get("layer"), []))
            - set(current.get("layers", {}).get(module.get("layer"), []))
        )
    }

    # A target-layer change can tighten an unchanged caller's permissions too.
    for target, module in modules.items():
        previous = old_modules.get(target)
        if previous is None or previous.get("layer") == module.get("layer"):
            continue
        for path, source in modules.items():
            old_source = old_modules.get(path, {})
            before = old.get("layers", {}).get(old_source.get("layer"), [])
            after = current.get("layers", {}).get(source.get("layer"), [])
            if previous.get("layer") in before and module.get("layer") not in after:
                tightened.add(path)
    return tightened, False


def check_contract_drift(
    root: Path | str,
    changed_files: Sequence[str],
    *,
    base: str | None = None,
    config: GuardConfig | None = None,
    revision: str | None = None,
) -> list[Finding]:
    """Block newly broken contracts while grandfathering untouched baseline debt."""
    root = Path(root).resolve()
    config = config or load_config(root, revision=revision)
    base = base or "origin/" + config.main_branch
    available = git_ok(
        root, ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"]
    )
    submodules = submodule_paths(root, revision=revision)
    reader = BaseFileReader(
        root,
        base,
        submodule_paths(root, revision=base)
        if revision is not None and available
        else submodules,
        exact=revision is not None,
    )
    config_path = ".guard/config.json"
    baseline = None
    if available:
        try:
            baseline = load_config(root, revision=base, allow_historical=True)
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
            if not module.get("retired", False)
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
        for item in _missing_docs(root, config, revision=revision)
        if not available
        or baseline is None
        or (item["module"], item["doc"]) not in old_declared_docs
        or reader.read(item["doc"]) is not None
    ]
    modules = {m["path"]: m for m in config.topology.get("modules", [])}
    old_modules = (
        {m["path"]: m for m in baseline.topology.get("modules", [])} if baseline else {}
    )
    existing = (
        _snapshot_paths(root, revision, set(modules))
        if revision is not None
        else {p for p in modules if repository_path(root, p).exists()}
    )
    missing_modules = {p for p, m in modules.items() if not m.get("retired", False)} - existing
    base_existing = _snapshot_paths(root, base, missing_modules) if available else set()
    for path in sorted(missing_modules):
        if available and (
            path in base_existing or (baseline is not None and path not in old_modules)
        ):
            findings.append(
                Finding(
                    rule_id,
                    config_path,
                    1,
                    f"Declared module '{path}' does not exist in repository; remove or update its declaration in .guard/config.json.",
                )
            )
    governed = bool(
        config.topology.get("enforce_module_dependencies")
        and config.topology.get("layers")
    )
    if (
        governed
        and available
        and baseline is not None
        and (
            (
                baseline.topology.get("enforce_module_dependencies")
                and baseline.topology.get("layers")
            )
            or any(m.get("role") for m in modules.values())
        )
    ):
        for path in sorted(set(modules) - set(old_modules)):
            if not modules[path].get("retired", False) and not modules[path].get("role"):
                findings.append(
                    Finding(
                        config.rule_id_prefix + "service-boundaries",
                        config_path,
                        1,
                        f"Governed module '{path}' requires a nonempty 'role' declaration in .guard/config.json.",
                    )
                )
    if not (
        config.topology.get("enforce_module_dependencies")
        or config.topology.get("layers")
        or any(m.get("retired", False) for m in modules.values())
    ):
        return findings
    tightened, scan_all_callers = _tightened_import_scope(config, baseline)
    snapshot_reader = (
        BaseFileReader(root, revision, submodules, exact=True)
        if revision is not None
        else None
    )

    def read_current(path: str) -> str | None:
        if snapshot_reader is not None:
            return snapshot_reader.read(path)
        current = repository_path(root, path)
        return current.read_text(errors="replace") if current.is_file() else None

    module_paths = list(modules)
    candidates = {
        m
        for m in modules
        if set(modules[m].get("dependencies", []))
        != set(old_modules.get(m, {}).get("dependencies", []))
    }
    deleted = ""
    if available:
        deleted = git_stdout(
            root,
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=D",
                "-z",
                base,
                *([revision] if revision is not None else []),
                "--",
            ],
        )
        if deleted is None:
            raise ValueError("Cannot inspect deleted module files against baseline")
    changed_owned = [
        path
        for path in {*changed_files, *filter(None, deleted.split("\0"))}
        if owner_module(path, module_paths) is not None
    ]
    if available:
        reader.preload(changed_owned)
    if snapshot_reader is not None:
        snapshot_reader.preload(changed_owned)
    for path in changed_owned:
        owner = owner_module(path, module_paths)
        text = read_current(path)
        if not available or reader.read(path) != text:
            candidates.add(owner)
    scan_modules = candidates | tightened
    if not scan_modules:
        return findings
    if revision is not None:
        inventory = _snapshot_paths(
            root, revision, None if scan_all_callers else scan_modules,
            recursive=True, files_only=True,
        )
    else:
        roots = [root] if scan_all_callers else [repository_path(root, module) for module in scan_modules]
        inventory = {
            p.relative_to(root).as_posix()
            for directory in roots
            for p in ([directory] if directory.is_file() else source_files(directory))
            if not p.is_symlink()
        }
    # Only these source languages can carry deterministic module import edges.
    if scan_all_callers:
        import_candidates = [
            p
            for p in sorted(inventory)
            if Path(p).suffix in DETERMINISTIC_IMPORT_EXTENSIONS
        ]
    else:
        import_candidates = [
            p
            for p in sorted(inventory)
            if not is_excluded_topology_path(p)
            and owner_module(p, module_paths) in tightened
            and Path(p).suffix in DETERMINISTIC_IMPORT_EXTENSIONS
        ]
    # Stale evidence reports candidate modules only, and inert files carry no imports.
    evidence_candidates = [
        p
        for p in sorted(inventory)
        if not is_excluded_topology_path(p)
        and owner_module(p, module_paths) in candidates
        and Path(p).suffix not in INERT_MODULE_FILE_EXTENSIONS
    ]
    baseline_has_retirements = any(m.get("retired", False) for m in old_modules.values())
    if snapshot_reader is not None:
        snapshot_reader.preload([*import_candidates, *evidence_candidates])
    if baseline_has_retirements:
        reader.preload(import_candidates)
    if scan_all_callers:
        import_paths = import_candidates
    else:
        import_paths = [
            p for p in import_candidates if "\x00" not in (read_current(p) or "")
        ]
    for path in import_paths:
        text = read_current(path)
        if text is None or "\x00" in text:
            continue
        prior_text = reader.read(path) if baseline_has_retirements else None
        findings.extend(
            check_module_imports(
                path,
                text,
                None,
                config,
                check_unregistered=False,
                baseline_config=baseline,
                baseline_changed_lines=(
                    compute_changed_lines(prior_text, text) if prior_text is not None else None
                ),
            )
        )
    old_observed: dict[str, set[str]] = {}
    baseline_files: list[str] = []
    if baseline and candidates:
        baseline_candidates = [
            p
            for p in sorted(
                _snapshot_paths(root, base, candidates, recursive=True, files_only=True)
            )
            if owner_module(p, module_paths) in candidates
            and Path(p).suffix not in INERT_MODULE_FILE_EXTENSIONS
        ]
        reader.preload(baseline_candidates)
        baseline_files = [
            p
            for p in baseline_candidates
            if "\x00" not in (reader.read(p) or "")
        ]
        old_observed, _ = _dependency_evidence(baseline, baseline_files, reader.read)
    stale_files = [
        p for p in evidence_candidates if "\x00" not in (read_current(p) or "")
    ]
    _, stale = _dependency_evidence(
        config,
        stale_files,
        read_current,
        coverage_files=baseline_files,
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
