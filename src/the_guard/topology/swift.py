"""Lexical Swift read-call checks with base-signature comparison."""

from __future__ import annotations

import re

from ..finding import Finding
from .rules import (
    SWIFT_AWAIT_CALL_RE,
    SWIFT_FUNC_DECL_RE,
    SWIFT_READ_METHOD_PREFIXES,
    SWIFT_WRITE_METHOD_PREFIXES,
    _line_exempts_rule,
)


def _check_swift_file(
    path: str,
    text: str,
    base_text: str | None,
    changed_lines: set[int] | None,
    exempt_tag: str = "guard-exempt",
) -> list[Finding]:
    current = _collect_swift_findings(path, text, exempt_tag)
    if not current:
        return []
    if base_text is None or changed_lines is None:
        return [finding for finding, _sig, _span in current]

    base_sigs = {
        sig
        for _finding, sig, _span in _collect_swift_findings(path, base_text, exempt_tag)
    }
    findings: list[Finding] = []
    for finding, sig, (func_start, func_end) in current:
        if finding.line in changed_lines:
            findings.append(finding)
            continue
        func_touched = any(
            line_no in changed_lines for line_no in range(func_start, func_end + 1)
        )
        if func_touched and sig not in base_sigs:
            findings.append(finding)
    return findings


def _collect_swift_findings(
    path: str,
    text: str,
    exempt_tag: str = "guard-exempt",
) -> list[tuple[Finding, tuple[str, str, str], tuple[int, int]]]:
    lines = text.splitlines()
    results: list[tuple[Finding, tuple[str, str, str], tuple[int, int]]] = []

    functions = _extract_swift_functions(lines)
    for func_name, start_line, end_line in functions:
        seen_reads: dict[str, int] = {}
        span = (start_line, end_line)
        func_body_lines = lines[start_line - 1 : end_line]

        for offset, line in enumerate(func_body_lines):
            line_no = start_line + offset
            stripped = line.strip()
            if stripped.startswith("//"):
                continue

            for match in SWIFT_AWAIT_CALL_RE.finditer(line):
                receiver = match.group("receiver")
                method = match.group("method")
                args = re.sub(r"\s+", " ", match.group("args").strip())

                if method.startswith(SWIFT_WRITE_METHOD_PREFIXES):
                    seen_reads.clear()
                    continue

                if not method.startswith(SWIFT_READ_METHOD_PREFIXES):
                    continue

                call_key = f"{receiver}.{method}({args})"
                rule_id = "guard.database-query-efficiency"
                if _line_exempts_rule(line, rule_id, exempt_tag):
                    continue

                if call_key in seen_reads:
                    first_line = seen_reads[call_key]
                    message = (
                        f"Duplicate awaited read '{receiver}.{method}(...)' in "
                        f"'{func_name}' (already awaited at line {first_line}); "
                        "reuse the first response within the same lifecycle flow."
                    )
                    results.append(
                        (
                            Finding(
                                rule_id=rule_id,
                                path=path,
                                line=line_no,
                                message=message,
                            ),
                            (rule_id, func_name, f"dup:{call_key}"),
                            span,
                        )
                    )
                else:
                    seen_reads[call_key] = line_no

    return results


def _extract_swift_functions(lines: list[str]) -> list[tuple[str, int, int]]:
    functions: list[tuple[str, int, int]] = []
    idx = 0
    total = len(lines)
    while idx < total:
        match = SWIFT_FUNC_DECL_RE.match(lines[idx])
        if not match:
            idx += 1
            continue
        func_name = match.group("name")
        start_line = idx + 1
        brace_depth = 0
        opened = False
        end_line = start_line
        for cursor in range(idx, total):
            code_line = lines[cursor].split("//", 1)[0]
            for ch in code_line:
                if ch == "{":
                    brace_depth += 1
                    opened = True
                elif ch == "}":
                    brace_depth -= 1
            if opened and brace_depth <= 0:
                end_line = cursor + 1
                break
        if opened:
            functions.append((func_name, start_line, end_line))
            idx = end_line
        else:
            idx += 1
    return functions
