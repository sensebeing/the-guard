"""Canonical import extraction and repository module ownership resolution."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from pathlib import PurePosixPath

from ..config import GuardConfig

JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
DETERMINISTIC_IMPORT_EXTENSIONS = JS_EXTENSIONS | {".py"}
_JS_TOKEN = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|"
    r"'(?:\\[\s\S]|[^'\\])*'|\"(?:\\[\s\S]|[^\"\\])*\"|"
    r"[A-Za-z_$][\w$]*|\s+|[^\s]"
)


def _js_tokens(text: str) -> Iterator[tuple[str, int]]:
    """Skip template text but tokenize executable interpolations at any nesting depth."""
    # None marks template text; integers count braces in code/interpolations.
    contexts: list[int | None] = [0]
    index, line = 0, 1
    while index < len(text):
        if contexts[-1] is None:
            char = text[index]
            size = 1
            if char == "\\":
                size = 2
            elif char == "`":
                contexts.pop()
                yield "`", line
            elif text.startswith("${", index):
                contexts.append(0)
                yield "{", line
                size = 2
            line += text[index : index + size].count("\n")
            index += size
            continue
        match = _JS_TOKEN.match(text, index)
        if match is None:
            break
        value = match.group()
        start_line = line
        line += value.count("\n")
        index = match.end()
        if value.isspace() or value.startswith(("//", "/*")):
            continue
        if value == "`":
            contexts.append(None)
        elif value == "{":
            contexts[-1] += 1
        elif value == "}":
            if contexts[-1] == 0 and len(contexts) > 1:
                contexts.pop()
            else:
                contexts[-1] = max(0, contexts[-1] - 1)
        yield value, start_line


def extract_imports(
    path: str, text: str, module_doc_roots: Sequence[str] = GuardConfig.module_doc_roots
) -> list[tuple[int, int, str]]:
    """Return (statement start line, end line, specifier), including Python from-import aliases."""
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(
                    (node.lineno, node.end_lineno or node.lineno, alias.name)
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    parts = path.removesuffix(".py").split("/")[: -node.level]
                    if parts and parts[0] in {
                        *GuardConfig.module_doc_roots,
                        *module_doc_roots,
                        "app",
                    }:
                        parts = parts[1:]
                    module = ".".join([*parts, *([module] if module else [])])
                imports.extend(
                    (
                        node.lineno,
                        node.end_lineno or node.lineno,
                        module + "." + alias.name if module else alias.name,
                    )
                    for alias in node.names
                )
                if module:
                    imports.append(
                        (node.lineno, node.end_lineno or node.lineno, module)
                    )
        return sorted(set(imports))
    if PurePosixPath(path).suffix not in JS_EXTENSIONS:
        # Preserve configured import-prefix checks for other language text.
        return [
            (number, number, match.group(1))
            for number, line in enumerate(text.splitlines(), 1)
            for match in re.finditer(
                r"""(?:from\s+|import\s+|require\(\s*)["']([^"']+)["']""", line
            )
        ]
    tokens = list(_js_tokens(text))
    imports = []
    for i, (value, number) in enumerate(tokens):
        if value not in {"import", "export", "require"}:
            continue
        if i and tokens[i - 1][0] in {".", "?."}:
            continue
        tail = i + 1
        if tail >= len(tokens):
            continue
        if tokens[tail][0] == "(" and value in {"import", "require"}:
            tail += 1
            terminators = {",", ")"} if value == "import" else {")"}
            if tail + 1 >= len(tokens) or tokens[tail + 1][0] not in terminators:
                continue
        elif tokens[tail][0] == ".":
            continue
        elif value in {"import", "export"} and tokens[tail][0][0] not in "\"'":
            # Static declarations may span lines, but not other statements.
            while tail < len(tokens) and tokens[tail][0] not in {
                ";",
                "import",
                "export",
                "=",
                "(",
            }:
                if (
                    tokens[tail][0] == "from"
                    and tail + 1 < len(tokens)
                    and tokens[tail + 1][0].startswith(("'", '"'))
                ):
                    break
                tail += 1
            if tail >= len(tokens) or tokens[tail][0] != "from":
                continue
            tail += 1
        elif value == "require":
            continue
        if tail < len(tokens):
            spec = tokens[tail][0]
            if spec.startswith(("'", '"')):
                imports.append((number, tokens[tail][1], spec[1:-1]))
    return sorted(set(imports))


def owner_module(file_path: str, module_paths: Sequence[str]) -> str | None:
    return next(
        (
            m
            for m in sorted(module_paths, key=lambda m: (-len(m), m))
            if file_path == m or file_path.startswith(m + "/")
        ),
        None,
    )


def resolve_target_module(
    file_path: str,
    spec: str,
    module_paths: Sequence[str],
    module_doc_roots: Sequence[str] = GuardConfig.module_doc_roots,
) -> str | None:
    """Resolve literal repo paths or unambiguous package aliases; exclude self edges."""
    paths = sorted(module_paths, key=lambda m: (-len(m), m))
    source = owner_module(file_path, paths)
    if spec.startswith(("./", "../")):
        parts = list(PurePosixPath(file_path).parent.parts)
        for part in spec.split("/"):
            if part == "..":
                if not parts:
                    return None
                parts.pop()
            elif part not in {"", "."}:
                parts.append(part)
        target = owner_module("/".join(parts), paths)
    else:
        target = next(
            (
                m
                for m in paths
                if spec == m
                or spec == m.replace("/", ".")
                or spec.startswith((m + "/", m.replace("/", ".") + "."))
            ),
            None,
        )
        if target is None:
            alias = (
                spec.split("/", 1)[1] if spec.startswith("@") and "/" in spec else spec
            )
            # Slash paths preserve file extensions; dotted Python paths become segments.
            alias_path = alias if "/" in alias else alias.replace(".", "/")
            name, _, suffix = alias_path.partition("/")
            roots = {
                "/".join(parts[:2])
                for module in paths
                if len(parts := module.split("/")) >= 2
                and parts[0] in module_doc_roots
                and parts[1] == name
            }
            if len(roots) == 1:
                prefix = next(iter(roots))
                target = owner_module(prefix + ("/" + suffix if suffix else ""), paths)
    return target if target != source else None
