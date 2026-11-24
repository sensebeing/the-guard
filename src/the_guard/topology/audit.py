"""Whole-repository topology evidence, independent of changed-line ratchets."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..finding import Finding
from ..git_helpers import BaseFileReader, compute_changed_lines, git_ok, git_stdout, submodule_paths
from ..config import GuardConfig, load_config, repository_path
from .configured import check_module_imports
from .discovery import IGNORED, discover_topology, source_files
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
_EVIDENCE_BATCH_SIZE = 128


def _import_targets(
    path: str, text: str, module_paths: Sequence[str], doc_roots: Sequence[str]
) -> Iterator[str]:
    for _, _, spec in extract_imports(path, text, doc_roots):
        target = resolve_target_module(path, spec, module_paths, doc_roots)
        if target:
            yield target


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
        observed[source].update(
            _import_targets(path, read(path) or "", paths, config.module_doc_roots)
        )
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


def _module_inventory(root: Path, revision: str | None, paths: set[str] | None) -> set[str]:
    if revision is not None:
        return _snapshot_paths(root, revision, paths, recursive=True, files_only=True)
    roots = [root] if paths is None else [repository_path(root, path) for path in paths]
    return {
        p.relative_to(root).as_posix()
        for source in roots
        for p in ([source] if source.is_file() else source_files(source))
        if not p.is_symlink()
    }


def _dependency_changes(
    root: Path, base: str, revision: str | None, modules: set[str], hints: Sequence[str]
) -> tuple[set[str], set[str]]:
    """Collect touched paths and tracked paths whose worktree visibility may differ."""
    module_paths = sorted(modules)
    pathspecs = [f":(literal){module}" for module in module_paths]
    diff = git_stdout(root, [
        "git", "-c", "core.fsmonitor=false", "diff", "--name-only", "--no-renames",
        "--ignore-submodules=none", "-z", base,
        *([revision] if revision is not None else []), "--", *pathspecs,
    ])
    if diff is None:
        raise ValueError("Cannot inspect changed module files against baseline")
    paths = set(hints)
    paths.update(filter(None, diff.split("\0")))
    if revision is not None:
        return paths, set()
    indexed = git_stdout(root, [
        "git", "ls-files", "-v", "--stage", "--cached", "--others",
        "--exclude-standard", "-z", "--", *pathspecs,
    ])
    # Conversion attributes can make Git report clean content that differs from raw source.
    raw_sources = git_stdout(root, [
        "git", "ls-files", "--cached", "-z", "--", *pathspecs,
        *[
            f":(exclude,literal,attr:!filter !ident !working-tree-encoding){module}"
            for module in module_paths
        ],
    ])
    if indexed is None or raw_sources is None:
        raise ValueError("Cannot inspect working-tree module files")
    paths.update(filter(None, raw_sources.split("\0")))
    different_inventory = set()
    for record in filter(None, indexed.split("\0")):
        status, entry = record[0], record[2:]
        if status == "?":
            paths.add(entry)
            continue
        metadata, _, path = entry.partition("\t")
        # Git diff can omit working-tree changes to these index entries.
        if status.islower() or status == "S":
            paths.add(path)
        if metadata.startswith("120000 ") or _has_pruned_directory(path):
            different_inventory.add(path)
    return paths, different_inventory


def _has_pruned_directory(path: str) -> bool:
    return any(
        part in IGNORED or part.endswith((".egg-info", ".dist-info"))
        for part in path.split("/")[:-1]
    )


def _worktree_source_is_visible(root: Path, module: str, path: str) -> bool:
    if path != module and _has_pruned_directory(path.removeprefix(module + "/")):
        return False
    current = root / path
    return all(
        not entry.is_symlink()
        for entry in (current, *current.parents)
        if entry != root and entry.is_relative_to(root)
    )


def _non_binary_sources(
    paths: Sequence[str],
    read: Callable[[str], str | None],
    reader: BaseFileReader | None = None,
) -> Iterator[tuple[str, str]]:
    for start in range(0, len(paths), _EVIDENCE_BATCH_SIZE):
        batch = paths[start : start + _EVIDENCE_BATCH_SIZE]
        if reader is not None:
            reader.preload(batch)
        for path in batch:
            text = read(path)
            if text is not None and "\x00" not in text:
                yield path, text


def _evidence_inventory(
    inventory: set[str], modules: Sequence[str], owner: str
) -> set[str]:
    return {
        path for path in inventory
        if not is_excluded_topology_path(path)
        and owner_module(path, modules) == owner
        and Path(path).suffix not in INERT_MODULE_FILE_EXTENSIONS
    }


def _incremental_stale_dependencies(
    root: Path,
    config: GuardConfig,
    modules: dict[str, dict[str, Any]],
    old_modules: dict[str, dict[str, Any]],
    changed_files: Sequence[str],
    *,
    reader: BaseFileReader,
    snapshot_reader: BaseFileReader | None,
    read_current: Callable[[str], str | None],
) -> list[tuple[str, str]]:
    """Find newly unused declared edges from changed sources and remaining witnesses."""
    declared = {
        module: set(entry["dependencies"])
        for module, entry in modules.items()
        if entry.get("dependencies")
    }
    if not declared:
        return []
    base = reader.base
    revision = snapshot_reader.base if snapshot_reader is not None else None
    module_paths = list(modules)
    changed_by_module: dict[str, set[str]] = {}
    changed_paths, different_inventory = _dependency_changes(
        root, base, revision, set(declared), changed_files
    )
    for path in changed_paths:
        owner = owner_module(path, module_paths)
        if owner in declared:
            changed_by_module.setdefault(owner, set()).add(path)

    added_dependencies = {
        module: dependencies - set(old_modules[module].get("dependencies", []))
        for module, dependencies in declared.items()
    }
    for path in different_inventory:
        owner = owner_module(path, module_paths)
        if owner is None or not (owner in changed_by_module or added_dependencies.get(owner)):
            continue
        if not _worktree_source_is_visible(root, owner, path):
            # A blob excluded by the worktree inventory contributes only on the base side.
            changed_by_module.setdefault(owner, set()).add(path)

    deterministic = {
        module: sorted(
            path for path in paths
            if not is_excluded_topology_path(path)
            and Path(path).suffix in DETERMINISTIC_IMPORT_EXTENSIONS
        )
        for module, paths in changed_by_module.items()
    }
    changed_sources = [path for paths in deterministic.values() for path in paths]
    # Gitlink contents are outside the parent commit's evidence inventory.
    base_sources = (
        _snapshot_paths(root, base, set(changed_sources), files_only=True)
        if reader.submodules else set(changed_sources)
    )
    reader.preload(base_sources)
    if snapshot_reader is not None:
        snapshot_sources = (
            _snapshot_paths(root, snapshot_reader.base, set(changed_sources), files_only=True)
            if snapshot_reader.submodules else set(changed_sources)
        )
        snapshot_reader.preload(snapshot_sources)

    stale: list[tuple[str, str]] = []
    for module, dependencies in sorted(declared.items()):
        added = added_dependencies[module]
        changed = changed_by_module.get(module, set())
        if not changed and not added:
            continue
        paths = deterministic.get(module, [])
        before = list(_non_binary_sources([path for path in paths if path in base_sources], reader.read))
        current_paths = (
            [path for path in paths if path in snapshot_sources]
            if snapshot_reader is not None
            else [path for path in paths if _worktree_source_is_visible(root, module, path)]
        )
        after = list(_non_binary_sources(current_paths, read_current))
        old_imports = {
            target for path, text in before
            for target in _import_targets(path, text, module_paths, config.module_doc_roots)
        }
        new_imports = {
            target for path, text in after
            for target in _import_targets(path, text, module_paths, config.module_doc_roots)
        }
        # An unchanged file that used an edge at base still uses it now.
        suspect = (added | ((dependencies - added) & old_imports)) - new_imports
        if not suspect:
            continue

        current_files = _evidence_inventory(
            _module_inventory(root, revision, {module}), module_paths, module
        )
        base_files = _evidence_inventory(
            _module_inventory(root, base, {module}), module_paths, module
        )
        current_unknown = sorted(
            path for path in current_files if Path(path).suffix not in DETERMINISTIC_IMPORT_EXTENSIONS
        )
        base_unknown = sorted(
            path for path in base_files if Path(path).suffix not in DETERMINISTIC_IMPORT_EXTENSIONS
            and (path not in current_files or path in changed)
        )
        if (
            next(_non_binary_sources(current_unknown, read_current, snapshot_reader), None) is not None
            or next(_non_binary_sources(base_unknown, reader.read, reader), None) is not None
        ):
            continue

        has_non_binary_det = bool(before or after)
        remaining = sorted(
            path for path in current_files - changed
            if Path(path).suffix in DETERMINISTIC_IMPORT_EXTENSIONS
        )
        for path, text in _non_binary_sources(remaining, read_current, snapshot_reader):
            has_non_binary_det = True
            suspect.difference_update(_import_targets(path, text, module_paths, config.module_doc_roots))
            if not suspect:
                break
        if has_non_binary_det:
            stale.extend((module, dep) for dep in sorted(suspect))
    return stale


def _full_stale_dependencies(
    root: Path,
    config: GuardConfig,
    baseline: GuardConfig | None,
    old_modules: dict[str, dict[str, Any]],
    candidates: set[str],
    inventory: set[str],
    *,
    available: bool,
    reader: BaseFileReader,
    snapshot_reader: BaseFileReader | None,
    read_current: Callable[[str], str | None],
) -> list[tuple[str, str]]:
    module_paths = [module["path"] for module in config.topology.get("modules", [])]
    evidence_candidates = [
        path for path in sorted(inventory)
        if not is_excluded_topology_path(path)
        and owner_module(path, module_paths) in candidates
        and Path(path).suffix not in INERT_MODULE_FILE_EXTENSIONS
    ]
    if snapshot_reader is not None:
        snapshot_reader.preload(evidence_candidates)
    old_observed: dict[str, set[str]] = {}
    baseline_files: list[str] = []
    if baseline is not None:
        # Baseline evidence uses baseline ownership so a new nested owner cannot
        # hide edges the parent module used to own.
        baseline_owner_paths = list(old_modules)
        baseline_candidates = [
            path
            for path in sorted(_module_inventory(root, reader.base, candidates))
            if owner_module(path, baseline_owner_paths) in candidates
            and Path(path).suffix not in INERT_MODULE_FILE_EXTENSIONS
        ]
        reader.preload(baseline_candidates)
        baseline_files = [
            path for path in baseline_candidates if "\x00" not in (reader.read(path) or "")
        ]
        old_observed, _ = _dependency_evidence(baseline, baseline_files, reader.read)
    stale_files = [
        path for path in evidence_candidates if "\x00" not in (read_current(path) or "")
    ]
    _, stale = _dependency_evidence(
        config, stale_files, read_current, coverage_files=baseline_files,
    )
    result = []
    for item in stale:
        module, dependency = item["module"], item["unused_dependency"]
        if module in candidates and (
            not available
            or dependency not in old_modules.get(module, {}).get("dependencies", [])
            or dependency in old_observed.get(module, set())
        ):
            result.append((module, dependency))
    return result


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
    base_submodules = submodule_paths(root, revision=base) if available else submodules
    reader = BaseFileReader(
        root,
        base,
        base_submodules if revision is not None else submodules,
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
    incremental = (
        available
        and baseline is not None
        and modules.keys() == old_modules.keys()
        and config.module_doc_roots == baseline.module_doc_roots
        and submodules == base_submodules
    )
    candidates = set()
    if not incremental:
        candidates = {
            module
            for module in modules
            if set(modules[module].get("dependencies", []))
            != set(old_modules.get(module, {}).get("dependencies", []))
        }
        changed_paths, _ = (
            _dependency_changes(root, base, revision, set(modules), changed_files)
            if available and modules else (changed_files, set())
        )
        changed_owned = {
            path for path in changed_paths if owner_module(path, module_paths) is not None
        }
        if available:
            reader.preload(changed_owned)
        if snapshot_reader is not None:
            snapshot_reader.preload(changed_owned)
        for path in sorted(changed_owned):
            text = read_current(path)
            if not available or reader.read(path) != text:
                candidates.add(owner_module(path, module_paths))
    inventory = _module_inventory(
        root, revision, None if scan_all_callers else candidates | tightened
    )
    # Inert documents and data carry no import edges; other text keeps extract_imports coverage.
    if scan_all_callers:
        import_candidates = [
            p
            for p in sorted(inventory)
            if Path(p).suffix not in INERT_MODULE_FILE_EXTENSIONS
        ]
    else:
        import_candidates = [
            p
            for p in sorted(inventory)
            if not is_excluded_topology_path(p)
            and owner_module(p, module_paths) in tightened
            and Path(p).suffix not in INERT_MODULE_FILE_EXTENSIONS
        ]
    baseline_has_retirements = any(m.get("retired", False) for m in old_modules.values())
    if snapshot_reader is not None:
        snapshot_reader.preload(import_candidates)
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
    if incremental:
        stale = _incremental_stale_dependencies(
            root, config, modules, old_modules, changed_files,
            reader=reader if reader.exact else BaseFileReader(root, base, base_submodules, exact=True),
            snapshot_reader=snapshot_reader, read_current=read_current,
        )
    elif candidates:
        stale = _full_stale_dependencies(
            root, config, baseline, old_modules, candidates, inventory,
            available=available, reader=reader, snapshot_reader=snapshot_reader, read_current=read_current,
        )
    else:
        stale = []
    for module, dependency in stale:
        findings.append(
            Finding(
                rule_id,
                config_path,
                1,
                f"Stale dependency '{module}' -> '{dependency}'; remove the unused declared dependency.",
            )
        )
    return findings
