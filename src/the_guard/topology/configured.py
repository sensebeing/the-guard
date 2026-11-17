"""Declared ownership, layering, and delete-only evolution constraints."""

from __future__ import annotations

import ast
import fnmatch
import re
from collections.abc import Sequence

from ..config import GuardConfig
from ..finding import Finding
from .imports import extract_imports, owner_module, resolve_target_module
from .paths import is_excluded_topology_path
from .rules import _line_exempts_rule


def in_scope(path: str, prefixes: Sequence[str]) -> bool:
    return any(
        path.startswith(prefix) or fnmatch.fnmatchcase(path, prefix)
        for prefix in prefixes
    )


def sql_pattern(table: str) -> re.Pattern[str]:
    return re.compile(
        r'\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+(?:ONLY\s+)?(?:public\.)?["\x27]?'
        + re.escape(table)
        + r"\b",
        re.I,
    )


def sql_spans(text: str, path: str) -> list[tuple[int, int, str]]:
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        return [
            (n.lineno, n.end_lineno, n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
    return [
        (i, i, line)
        for i, line in enumerate(text.splitlines(), 1)
        if not line.lstrip().startswith(("#", "//", "--"))
    ]


def check_configured(
    path: str,
    text: str,
    base_text: str | None,
    changed_lines: set[int] | None,
    config: GuardConfig,
) -> list[Finding]:
    findings = []
    topology = config.topology
    prefix = config.rule_id_prefix
    spans = sql_spans(text, path)
    lines = text.splitlines()

    for rule in topology.get("exclusive_sql_owners", []):
        if path in rule["owners"]:
            continue
        rule_id = rule.get("rule_id", prefix + "single-source-of-truth")
        pattern = sql_pattern(rule["table"])
        for start, end, value in spans:
            if (
                pattern.search(value)
                and _is_range_touched(changed_lines, start, end)
                and not _line_exempts_rule(
                    lines[start - 1], rule_id, exempt_tag=config.exempt_tag
                )
            ):
                findings.append(
                    Finding(
                        rule.get("rule_id", prefix + "single-source-of-truth"),
                        path,
                        start,
                        rule.get(
                            "message",
                            f"SQL for '{rule['table']}' belongs exclusively to {rule['owners']}",
                        ),
                    )
                )
    retired = topology.get("retired_tables", [])
    if isinstance(retired, dict):
        retired = [retired]
    for rule in retired:
        if not in_scope(path, rule.get("scope_prefixes", [""])):
            continue
        for table in rule["tables"]:
            pattern = sql_pattern(table)
            current_count = sum(len(pattern.findall(value)) for _, _, value in spans)
            base_count = sum(
                len(pattern.findall(value))
                for _, _, value in sql_spans(base_text or "", path)
            )
            if current_count >= base_count:
                rule_id = rule.get("rule_id", prefix + "data-modeling")
                for number, line in enumerate(lines, 1):
                    if not _is_range_touched(
                        changed_lines, number, number
                    ) or line.lstrip().startswith(("#", "//", "--")):
                        continue
                    if _line_exempts_rule(line, rule_id, exempt_tag=config.exempt_tag):
                        continue
                    if pattern.search(line):
                        findings.append(
                            Finding(
                                rule_id,
                                path,
                                number,
                                f"Retired table '{table}' is delete-only ({base_count} -> {current_count} SQL references).",
                            )
                        )
                        break
    for rule in topology.get("forbidden_patterns", []):
        if not in_scope(path, rule.get("scope_prefixes", [""])):
            continue
        rule_id = rule.get("rule_id", prefix + "single-source-of-truth")
        expression = rule["literal_or_regex"]
        pattern = re.compile(
            re.escape(expression) if rule.get("literal", False) else expression
        )
        for number, line in enumerate(text.splitlines(), 1):
            if (
                _is_range_touched(changed_lines, number, number)
                and not line.lstrip().startswith(("#", "//"))
                and pattern.search(line)
                and not _line_exempts_rule(line, rule_id, exempt_tag=config.exempt_tag)
            ):
                findings.append(
                    Finding(
                        rule.get("rule_id", prefix + "single-source-of-truth"),
                        path,
                        number,
                        rule.get(
                            "message", f"Forbidden dependency pattern: {expression}"
                        ),
                    )
                )
    imports = extract_imports(path, text, config.module_doc_roots)
    if not is_excluded_topology_path(path):
        findings.extend(
            _check_module_imports(path, imports, lines, changed_lines, config)
        )
    for rule in [
        *topology.get("forbidden_imports", []),
        *topology.get("module_boundaries", []),
    ]:
        if not in_scope(path, rule.get("scope_prefixes", [rule.get("from", "")])):
            continue
        forbidden = rule.get(
            "imports", rule.get("forbidden_imports", [rule.get("to", "")])
        )
        rule_id = rule.get("rule_id", prefix + "service-boundaries")
        reported_lines = set()
        for start, end, name in imports:
            number = _import_finding_line(
                start, end, lines, changed_lines, rule_id, config
            )
            if (
                number is not None
                and number not in reported_lines
                and any(_matches_import_prefix(name, p) for p in forbidden)
            ):
                reported_lines.add(number)
                findings.append(
                    Finding(
                        rule.get("rule_id", prefix + "service-boundaries"),
                        path,
                        number,
                        rule.get(
                            "message",
                            f"Forbidden import across module boundary: {name}",
                        ),
                    )
                )
    return sorted(
        dict.fromkeys(findings),
        key=lambda finding: (finding.line, finding.rule_id, finding.message),
    )


def _is_range_touched(changed: set[int] | None, start: int, end: int) -> bool:
    return changed is None or bool(changed.intersection(range(start, end + 1)))


def _check_module_imports(
    path: str,
    imports: list[tuple[int, int, str]],
    lines: list[str],
    changed_lines: set[int] | None,
    config: GuardConfig,
) -> list[Finding]:
    topology = config.topology
    modules = {m["path"]: m for m in topology.get("modules", [])}
    paths = list(modules)
    source_path = owner_module(path, paths)
    if source_path is None:
        return []
    source = modules[source_path]
    layers = topology.get("layers", {})
    rule_id = config.rule_id_prefix + "service-boundaries"
    findings = []
    for start, end, spec in imports:
        target_path = resolve_target_module(path, spec, paths, config.module_doc_roots)
        number = _import_finding_line(start, end, lines, changed_lines, rule_id, config)
        if target_path is None or number is None:
            continue
        target = modules[target_path]
        source_layer, target_layer = source.get("layer"), target.get("layer")
        if (
            layers
            and source_layer
            and target_layer
            and target_layer not in layers[source_layer]
        ):
            message = (
                f"Layer '{source_layer}' module '{source_path}' must not import "
                f"layer '{target_layer}' module '{target_path}'."
            )
        elif topology.get(
            "enforce_module_dependencies"
        ) and target_path not in source.get("dependencies", []):
            message = (
                f"Module '{source_path}' imports undeclared dependency '{target_path}' "
                f"(allowed: {source.get('dependencies', [])})."
            )
        else:
            continue
        findings.append(Finding(rule_id, path, number, message))
    return findings


def _import_finding_line(
    start: int,
    end: int,
    lines: list[str],
    changed_lines: set[int] | None,
    rule_id: str,
    config: GuardConfig,
) -> int | None:
    """Apply one touched-span/exemption policy to all import boundary checks."""
    if not _is_range_touched(changed_lines, start, end):
        return None
    if any(
        _line_exempts_rule(lines[line - 1], rule_id, exempt_tag=config.exempt_tag)
        for line in {start, end}
    ):
        return None
    return end if changed_lines is not None and start not in changed_lines else start


def _matches_import_prefix(name: str, prefix: str) -> bool:
    if not prefix:
        return False
    base = prefix.rstrip("/.")
    return (
        (prefix.endswith(("/", ".")) and name.startswith(prefix))
        or name == base
        or (bool(base) and name.startswith((base + ".", base + "/")))
    )
