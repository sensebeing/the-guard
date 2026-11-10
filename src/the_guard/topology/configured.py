"""Declared ownership, layering, and delete-only evolution constraints."""

from __future__ import annotations

import ast
import fnmatch
import re
from collections.abc import Sequence

from ..config import GuardConfig
from ..finding import Finding
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
    imports = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = ast.Module(body=[], type_ignores=[])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend((node.lineno, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    parts = path.removesuffix(".py").split("/")[: -node.level]
                    if parts and parts[0] in {
                        "src",
                        "services",
                        "packages",
                        "app",
                        "apps",
                    }:
                        parts = parts[1:]
                    module = ".".join([*parts, *([module] if module else [])])
                imports.extend(
                    (node.lineno, module + "." + alias.name if module else alias.name)
                    for alias in node.names
                )
                if module:
                    imports.append((node.lineno, module))
    else:
        for number, line in enumerate(text.splitlines(), 1):
            for match in re.finditer(
                r"""(?:from\s+|import\s+|require\(\s*)["']([^"']+)["']""", line
            ):
                imports.append((number, match.group(1)))
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
        for number, name in imports:
            if (
                number not in reported_lines
                and _is_range_touched(changed_lines, number, number)
                and not _line_exempts_rule(
                    lines[number - 1], rule_id, exempt_tag=config.exempt_tag
                )
                and any(
                    p
                    and (
                        name == p
                        or name.startswith(p + ".")
                        or name.startswith(p + "/")
                    )
                    for p in forbidden
                )
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
