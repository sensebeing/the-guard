"""Changed-scope topology guard: language checks plus explicit owner constraints."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..config import GuardConfig, load_config
from ..finding import Finding
from ..git_helpers import (
    BaseFileReader,
    compute_changed_lines,
    git_ok,
    repository_path_exists,
    resolve_changed_file,
    submodule_paths,
)
from ..policy import load_policy
from .configured import check_configured, in_scope
from .paths import is_excluded_topology_path
from .python import _check_python_file
from .swift import _check_swift_file


def run_topology_guard(
    root: Path | str,
    changed_files: list[str],
    base: str | None = None,
    config: GuardConfig | None = None,
    *,
    full_scan: bool = False,
    revision: str | None = None,
) -> list[Finding]:
    root = Path(root).resolve()
    config = config or load_config(root, revision=revision)
    active_rules = (
        {rule.id for rule in load_policy(root, revision=revision, config=config).rules}
        if repository_path_exists(root, config.manifest_file, revision)
        else set()
    )
    base = base or "origin/" + config.main_branch
    submodules = submodule_paths(root, revision=revision)
    base_available = not full_scan and git_ok(
        root, ["git", "rev-parse", "--verify", "--quiet", base]
    )
    reader = BaseFileReader(
        root,
        base,
        submodule_paths(root, revision=base)
        if revision is not None and base_available
        else submodules,
        exact=revision is not None,
    )
    snapshot_reader = (
        BaseFileReader(root, revision, submodules, exact=True)
        if revision is not None
        else None
    )
    resolved_paths: dict[str, Path] = {}
    for raw in dict.fromkeys(changed_files):
        relative = Path(raw)
        resolved = (
            (relative.as_posix(), root / relative)
            if snapshot_reader
            and not relative.is_absolute()
            and ".." not in relative.parts
            else resolve_changed_file(root, raw)
        )
        if resolved is None:
            raise ValueError(f"Changed path escapes repository: {raw}")
        path, absolute = resolved
        resolved_paths[path] = absolute

    if snapshot_reader is not None:
        snapshot_reader.preload(resolved_paths)
    present: dict[str, str] = {}
    for path, absolute in resolved_paths.items():
        if snapshot_reader is not None:
            text = snapshot_reader.read(path)
            if text is None:
                continue
        elif absolute.is_file():
            text = absolute.read_text(encoding="utf-8", errors="replace")
        elif (root / path).is_symlink():
            text = str((root / path).readlink())
        else:
            continue
        present[path] = text
    if base_available:
        reader.preload(present)

    findings = []
    for path, text in present.items():
        baseline = reader.read(path) if base_available else None
        changed = (
            compute_changed_lines(baseline, text) if baseline is not None else None
        )
        lexical = active_rules and not is_excluded_topology_path(path)
        current = []
        if (
            lexical
            and path.endswith(".py")
            and in_scope(
                path,
                config.topology.get(
                    "python_prod_prefixes",
                    ["src/", "services/", "packages/", "app/", "apps/"],
                ),
            )
        ):
            current = _check_python_file(
                path, text, baseline, changed, exempt_tag=config.exempt_tag
            )
        elif (
            lexical
            and path.endswith(".swift")
            and in_scope(path, config.topology.get("swift_prod_prefixes", [""]))
        ):
            current = _check_swift_file(
                path, text, baseline, changed, exempt_tag=config.exempt_tag
            )
        prefixed = (
            replace(f, rule_id=config.rule_id_prefix + f.rule_id.removeprefix("guard."))
            for f in current
        )
        findings.extend(f for f in prefixed if f.rule_id in active_rules)
        # Full audit reports unregistered modules through its inventory column.
        findings.extend(check_configured(
            path, text, baseline, changed, config,
            check_unregistered=not full_scan, check_retired_edits=not full_scan,
        ))
    return findings


def topology_guard_touched(changed_files: list[str]) -> bool:
    """Configured constraints can apply to any changed source or configuration."""
    return bool(changed_files)
