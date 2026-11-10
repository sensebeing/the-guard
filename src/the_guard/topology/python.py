"""Read-path purity and retired-table checks for backend production Python."""

from __future__ import annotations

import ast
from typing import Iterable, Sequence

from ..finding import Finding
from .rules import (
    BATCH_OR_INFRA_CALL_MARKERS,
    FOR_UPDATE_SQL_RE,
    LOCK_OR_WRITE_FUNCTION_MARKERS,
    NON_RESOURCE_READ_CALLS,
    READ_ACTION_SUFFIXES,
    READ_CALL_EXACT,
    READ_CALL_PREFIXES,
    READ_FUNCTION_EXACT,
    READ_FUNCTION_PREFIXES,
    RETRY_LOOP_VAR_NAMES,
    WRITE_CALL_MARKERS,
    _line_exempts_rule,
)


def _check_python_file(
    path: str,
    text: str,
    base_text: str | None,
    changed_lines: set[int] | None,
    exempt_tag: str = "guard-exempt",
) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    current_ast_findings = _collect_python_ast_findings(path, text, lines, exempt_tag)
    if not current_ast_findings:
        return findings

    if base_text is None or changed_lines is None:
        findings.extend(finding for finding, _sig, _span in current_ast_findings)
        return findings

    base_lines = base_text.splitlines()
    base_signatures = {
        sig
        for _finding, sig, _span in _collect_python_ast_findings(
            path, base_text, base_lines, exempt_tag
        )
    }

    for finding, sig, (func_start, func_end) in current_ast_findings:
        if finding.line in changed_lines:
            findings.append(finding)
            continue
        func_touched = any(
            line_no in changed_lines for line_no in range(func_start, func_end + 1)
        )
        if func_touched and sig not in base_signatures:
            findings.append(finding)

    return findings


def _collect_python_ast_findings(
    path: str,
    text: str,
    lines: list[str],
    exempt_tag: str = "guard-exempt",
) -> list[tuple[Finding, tuple[str, str, str], tuple[int, int]]]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return []

    results: list[tuple[Finding, tuple[str, str, str], tuple[int, int]]] = []
    for func in _iter_functions(tree):
        func_start = getattr(func, "lineno", 1)
        func_end = getattr(func, "end_lineno", func_start)
        span = (func_start, func_end)

        if _is_read_function_or_endpoint(func):
            for lock_line in _find_for_update_lines(func):
                line_text = lines[lock_line - 1] if 1 <= lock_line <= len(lines) else ""
                rule_id = "guard.architecture-simplicity"
                if _line_exempts_rule(line_text, rule_id, exempt_tag):
                    continue
                message = (
                    f"Read path '{func.name}' must not acquire FOR UPDATE row write "
                    "locks; move state convergence and locking to write/event paths."
                )
                results.append(
                    (
                        Finding(
                            rule_id=rule_id, path=path, line=lock_line, message=message
                        ),
                        (rule_id, func.name, "FOR_UPDATE"),
                        span,
                    )
                )

        if isinstance(func, ast.AsyncFunctionDef):
            for (
                dup_line,
                first_line,
                call_label,
                call_key,
            ) in _find_duplicate_awaited_reads(func):
                line_text = lines[dup_line - 1] if 1 <= dup_line <= len(lines) else ""
                rule_id = "guard.database-query-efficiency"
                if _line_exempts_rule(line_text, rule_id, exempt_tag):
                    continue
                message = (
                    f"Duplicate awaited read call '{call_label}' in '{func.name}' "
                    f"(already awaited at line {first_line}); reuse the first "
                    "response within the same request/flow."
                )
                results.append(
                    (
                        Finding(
                            rule_id=rule_id, path=path, line=dup_line, message=message
                        ),
                        (rule_id, func.name, f"dup:{call_key}"),
                        span,
                    )
                )

            for loop_line, call_label, call_key in _find_loop_n_plus_one_reads(func):
                line_text = lines[loop_line - 1] if 1 <= loop_line <= len(lines) else ""
                rule_id = "guard.database-query-efficiency"
                if _line_exempts_rule(line_text, rule_id, exempt_tag):
                    continue
                message = (
                    f"Single-entity awaited read '{call_label}' inside a loop/comprehension "
                    f"in '{func.name}' causes N+1 fan-out; use a batch read API or "
                    "pre-fetched map."
                )
                results.append(
                    (
                        Finding(
                            rule_id=rule_id, path=path, line=loop_line, message=message
                        ),
                        (rule_id, func.name, f"n+1:{call_key}"),
                        span,
                    )
                )

    return results


def _iter_functions(tree: ast.AST) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _iter_nodes_without_nested_functions(node: ast.AST) -> Iterable[ast.AST]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in reversed(list(ast.iter_child_nodes(current))):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def _is_read_function_or_endpoint(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    name_lower = func.name.lower()
    if any(marker in name_lower for marker in READ_WITH_MUTATION_MARKERS):
        return False

    for decorator in func.decorator_list:
        if isinstance(decorator, ast.Call) and isinstance(
            decorator.func, ast.Attribute
        ):
            method = decorator.func.attr.lower()
            if method == "get":
                return True
            if method == "post" and decorator.args:
                first_arg = decorator.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(
                    first_arg.value, str
                ):
                    route_path = first_arg.value.lower()
                    if route_path.endswith(READ_ACTION_SUFFIXES):
                        return True

    if name_lower in READ_FUNCTION_EXACT or name_lower.startswith(
        READ_FUNCTION_PREFIXES
    ):
        return True
    if any(marker in name_lower for marker in LOCK_OR_WRITE_FUNCTION_MARKERS):
        return False
    return False


def _find_for_update_lines(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    lines: list[int] = []
    for node in _iter_nodes_without_nested_functions(func):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if FOR_UPDATE_SQL_RE.search(node.value):
                lines.append(getattr(node, "lineno", func.lineno))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "with_for_update":
                lines.append(getattr(node, "lineno", func.lineno))
    return sorted(set(lines))


def _find_duplicate_awaited_reads(
    func: ast.AsyncFunctionDef,
) -> list[tuple[int, int, str, str]]:
    duplicates: list[tuple[int, int, str, str]] = []
    _walk_stmts_for_duplicate_reads(func.body, {}, duplicates)
    return duplicates


def _walk_stmts_for_duplicate_reads(
    stmts: Sequence[ast.stmt],
    seen_reads: dict[str, tuple[int, str]],
    duplicates: list[tuple[int, int, str, str]],
) -> tuple[dict[str, tuple[int, str]], bool, bool]:
    """Walk statement sequence tracking awaited reads along the same execution path.

    Returns (updated_seen_reads, mutated_any, terminated).
    """
    current_seen = dict(seen_reads)
    mutated_any = False

    for stmt in stmts:
        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            _inspect_expr_for_reads_and_writes(stmt, current_seen, duplicates)
            return current_seen, mutated_any, True

        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            # Loop bodies repeat by design and are checked by the N+1 detector.
            if _subtree_has_write_call(stmt):
                current_seen.clear()
                mutated_any = True
            continue

        if isinstance(stmt, ast.If):
            _inspect_node_non_control_awaits(stmt.test, current_seen, duplicates)
            body_seen, body_mutated, body_term = _walk_stmts_for_duplicate_reads(
                stmt.body, current_seen, duplicates
            )
            else_seen, else_mutated, else_term = _walk_stmts_for_duplicate_reads(
                stmt.orelse, current_seen, duplicates
            )
            if body_term and else_term:
                return current_seen, mutated_any or body_mutated or else_mutated, True
            if body_mutated or else_mutated:
                current_seen.clear()
                mutated_any = True
            if not body_term and not else_term:
                # Keep pre-existing reads plus reads performed in only one non-mutating branch
                # so unconditional follow-up reads are still caught without flagging if/else peers.
                merged = dict(current_seen)
                for key, val in body_seen.items():
                    if key not in else_seen or key in current_seen:
                        merged[key] = val
                for key, val in else_seen.items():
                    if key not in body_seen or key in current_seen:
                        merged.setdefault(key, val)
                current_seen = merged
            elif not body_term:
                current_seen = body_seen
            else:
                current_seen = else_seen
            continue

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                _inspect_node_non_control_awaits(
                    item.context_expr, current_seen, duplicates
                )
            current_seen, block_mutated, block_term = _walk_stmts_for_duplicate_reads(
                stmt.body, current_seen, duplicates
            )
            mutated_any = mutated_any or block_mutated
            if block_term:
                return current_seen, mutated_any, True
            continue

        if isinstance(stmt, ast.Try):
            body_seen, body_mutated, body_term = _walk_stmts_for_duplicate_reads(
                stmt.body, current_seen, duplicates
            )
            if body_mutated:
                current_seen.clear()
                mutated_any = True
            elif not body_term:
                current_seen = body_seen
            for handler in stmt.handlers:
                _walk_stmts_for_duplicate_reads(
                    handler.body, dict(seen_reads), duplicates
                )
            if stmt.orelse and not body_term:
                current_seen, else_mutated, _ = _walk_stmts_for_duplicate_reads(
                    stmt.orelse, current_seen, duplicates
                )
                mutated_any = mutated_any or else_mutated
            if stmt.finalbody:
                current_seen, fin_mutated, fin_term = _walk_stmts_for_duplicate_reads(
                    stmt.finalbody, current_seen, duplicates
                )
                mutated_any = mutated_any or fin_mutated
                if fin_term:
                    return current_seen, mutated_any, True
            continue

        if _inspect_expr_for_reads_and_writes(stmt, current_seen, duplicates):
            mutated_any = True

    return current_seen, mutated_any, False


def _inspect_expr_for_reads_and_writes(
    stmt: ast.AST,
    current_seen: dict[str, tuple[int, str]],
    duplicates: list[tuple[int, int, str, str]],
) -> bool:
    return _inspect_node_non_control_awaits(stmt, current_seen, duplicates)


def _inspect_node_non_control_awaits(
    node: ast.AST,
    current_seen: dict[str, tuple[int, str]],
    duplicates: list[tuple[int, int, str, str]],
) -> bool:
    mutated = False
    for child in _iter_nodes_without_nested_functions(node):
        if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
            call = child.value
            if _is_write_call(call):
                current_seen.clear()
                mutated = True
                continue
            read_info = _extract_read_call_signature(call)
            if read_info is not None:
                call_key, call_label = read_info
                line_no = getattr(child, "lineno", 1)
                if call_key in current_seen:
                    first_line, _ = current_seen[call_key]
                    if line_no != first_line:
                        duplicates.append((line_no, first_line, call_label, call_key))
                else:
                    current_seen[call_key] = (line_no, call_label)
        elif isinstance(child, ast.Call) and _is_write_call(child):
            current_seen.clear()
            mutated = True
    return mutated


def _subtree_has_write_call(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call) and _is_write_call(child)
        for child in _iter_nodes_without_nested_functions(node)
    )


READ_WITH_MUTATION_MARKERS = (
    "for_update",
    "or_create",
    "or_insert",
    "or_upsert",
    "and_update",
    "and_delete",
    "and_lock",
    "and_claim",
)


def _extract_read_call_signature(call: ast.Call) -> tuple[str, str] | None:
    method_name, receiver_label = _call_name_and_receiver(call)
    if not method_name:
        return None
    lower = method_name.lower()
    if lower in NON_RESOURCE_READ_CALLS:
        return None
    if any(marker in lower for marker in READ_WITH_MUTATION_MARKERS):
        return None
    if not (lower in READ_CALL_EXACT or lower.startswith(READ_CALL_PREFIXES)):
        return None
    if not call.args and not call.keywords:
        return None
    # Require either a receiver (e.g. client.get_detail(...)) or an explicit resource read function name.
    if not receiver_label and lower == "get":
        return None
    call_label = (
        f"{receiver_label}.{method_name}(...)"
        if receiver_label
        else f"{method_name}(...)"
    )
    call_key = ast.dump(call, include_attributes=False)
    return call_key, call_label


def _is_write_call(call: ast.Call) -> bool:
    method_name, _receiver = _call_name_and_receiver(call)
    if not method_name:
        return False
    lower = method_name.lower()
    if lower in READ_CALL_EXACT or lower.startswith(READ_CALL_PREFIXES):
        return False
    return any(marker in lower for marker in WRITE_CALL_MARKERS)


def _call_name_and_receiver(call: ast.Call) -> tuple[str | None, str | None]:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr, _format_receiver(func.value)
    if isinstance(func, ast.Name):
        return func.id, None
    return None, None


def _format_receiver(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _format_receiver(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return "client"


def _find_loop_n_plus_one_reads(
    func: ast.AsyncFunctionDef,
) -> list[tuple[int, str, str]]:
    findings: list[tuple[int, str, str]] = []
    seen_lines: set[int] = set()

    for node in _iter_nodes_without_nested_functions(func):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            if _is_retry_range_loop(node):
                continue
            for child in _iter_nodes_without_nested_functions(node):
                if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
                    info = _extract_single_entity_loop_read(child.value)
                    if info is not None:
                        line_no = getattr(child, "lineno", node.lineno)
                        if line_no not in seen_lines:
                            seen_lines.add(line_no)
                            call_key, call_label = info
                            findings.append((line_no, call_label, call_key))
        elif isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            for child in _iter_nodes_without_nested_functions(node):
                if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
                    info = _extract_single_entity_loop_read(child.value)
                    if info is not None:
                        line_no = getattr(child, "lineno", node.lineno)
                        if line_no not in seen_lines:
                            seen_lines.add(line_no)
                            call_key, call_label = info
                            findings.append((line_no, call_label, call_key))

    return findings


def _is_retry_range_loop(loop: ast.For | ast.AsyncFor) -> bool:
    if isinstance(loop.iter, ast.Call) and isinstance(loop.iter.func, ast.Name):
        if loop.iter.func.id == "range":
            if (
                isinstance(loop.target, ast.Name)
                and loop.target.id.lower() in RETRY_LOOP_VAR_NAMES
            ):
                return True
    return False


def _extract_single_entity_loop_read(call: ast.Call) -> tuple[str, str] | None:
    read_info = _extract_read_call_signature(call)
    if read_info is None:
        return None
    call_key, call_label = read_info
    method_name, _receiver = _call_name_and_receiver(call)
    if not method_name:
        return None
    lower = method_name.lower()
    if any(marker in lower for marker in BATCH_OR_INFRA_CALL_MARKERS):
        return None
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            arg_lower = arg.value.lower()
            if any(
                marker in arg_lower
                for marker in ("batch", "bulk", "page", "cursor", "list")
            ):
                return None
    return call_key, call_label
