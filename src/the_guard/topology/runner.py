"""Changed-scope topology guard: language checks plus explicit owner constraints."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..config import GuardConfig, load_config
from ..finding import Finding
from ..git_helpers import (
    BaseFileReader,
    git_ok,
    resolve_changed_file,
    submodule_paths,
)
from .configured import check_configured, in_scope
from ..git_helpers import compute_changed_lines
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
    base = base or "origin/" + config.main_branch
    reader = BaseFileReader(root, base, submodule_paths(root))
    base_available = not full_scan and git_ok(
        root, ["git", "rev-parse", "--verify", "--quiet", base]
    )
    snapshot_reader = (
        BaseFileReader(root, revision, submodule_paths(root))
        if revision is not None
        else None
    )
    findings = []
    for raw in dict.fromkeys(changed_files):
        resolved = (
            (raw, root / raw)
            if snapshot_reader
            and not Path(raw).is_absolute()
            and ".." not in Path(raw).parts
            else resolve_changed_file(root, raw)
        )
        if resolved is None:
            raise ValueError(f"Changed path escapes repository: {raw}")
        path, absolute = resolved
        if snapshot_reader is not None:
            text = snapshot_reader.read(path)
            if text is None:
                continue
        else:
            if not absolute.is_file():
                continue
            text = absolute.read_text(encoding="utf-8", errors="replace")
        baseline = reader.read(path) if base_available else None
        changed = (
            compute_changed_lines(baseline, text) if baseline is not None else None
        )
        excluded = is_excluded_topology_path(path)
        current = []
        if (
            not excluded
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
            not excluded
            and path.endswith(".swift")
            and in_scope(path, config.topology.get("swift_prod_prefixes", [""]))
        ):
            current = _check_swift_file(
                path, text, baseline, changed, exempt_tag=config.exempt_tag
            )
        findings.extend(
            replace(f, rule_id=config.rule_id_prefix + f.rule_id.removeprefix("guard."))
            for f in current
        )
        # Full audit reports unregistered modules through its inventory column.
        findings.extend(check_configured(
            path, text, baseline, changed, config, check_unregistered=not full_scan
        ))
    return findings


def topology_guard_touched(changed_files: list[str]) -> bool:
    """Configured constraints can apply to any changed source or configuration."""
    return bool(changed_files)
