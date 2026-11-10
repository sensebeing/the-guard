"""Changed-scope topology guard: language checks plus explicit owner constraints."""
from dataclasses import replace
from pathlib import Path
import re
from ..config import load_config
from ..git_helpers import BaseFileReader, git_ok, resolve_changed_file, submodule_paths
from .git import _compute_changed_lines
from .python import _check_python_file
from .swift import _check_swift_file
from .configured import check_configured, in_scope
from .discovery import discover_topology


def run_topology_guard(root, changed_files, base='origin/main', config=None):
    root = Path(root).resolve()
    config = config or load_config(root)
    reader = BaseFileReader(root, base, submodule_paths(root))
    base_available = git_ok(root, ['git','rev-parse','--verify','--quiet',base])
    findings = []
    for raw in dict.fromkeys(changed_files):
        resolved = resolve_changed_file(root, raw)
        if resolved is None:
            raise ValueError(f'Changed path escapes repository: {raw}')
        path, absolute = resolved
        if not absolute.is_file():
            continue
        text = absolute.read_text(encoding='utf-8', errors='replace')
        baseline = reader.read(path) if base_available else None
        changed = _compute_changed_lines(baseline, text) if baseline is not None else None
        parts = set(Path(path).parts)
        excluded = parts & {'tests','test','migrations','alembic','.venv','__pycache__','scripts','Tests','UITests','Snapshots','__Snapshots__'} or Path(path).name.startswith('test_') or path.endswith(('_test.py','Tests.swift','Test.swift'))
        current = []
        # Normalize only exemption annotations before AST checks; line numbers stay unchanged.
        normalized = re.sub(re.escape(config.exempt_tag)+r'\s+', 'guard-exempt ', text)
        if not excluded and path.endswith('.py') and in_scope(path, config.topology.get('python_prod_prefixes', ['src/','services/','packages/','app/','apps/'])):
            current = _check_python_file(path, normalized, baseline, changed)
        elif not excluded and path.endswith('.swift') and in_scope(path, config.topology.get('swift_prod_prefixes', [''])):
            current = _check_swift_file(path, normalized, baseline, changed, config.topology.get('swift_discarded_history_check', False))
        findings.extend(replace(f, rule_id=config.rule_id_prefix+f.rule_id.removeprefix('guard.')) for f in current)
        findings.extend(check_configured(path, text, baseline, changed, config))
    return findings


run_architecture_topology_guard = run_topology_guard
__all__ = ['run_topology_guard', 'discover_topology']


def topology_guard_touched(changed_files):
    """Configured constraints can apply to any changed source or configuration."""
    return bool(changed_files)
