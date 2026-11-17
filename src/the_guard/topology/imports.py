"""Canonical import extraction and repository module ownership resolution."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import PurePosixPath

from ..config import GuardConfig

JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
# Strings are tokens, so comment markers and import-like text inside them stay inert.
_JS_TOKEN = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|"
    r"'(?:\\[\s\S]|[^'\\])*'|\"(?:\\[\s\S]|[^\"\\])*\"|"
    r"`(?:\\[\s\S]|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]"
)


def extract_imports(
    path: str, text: str, module_doc_roots: Sequence[str] = GuardConfig.module_doc_roots
) -> list[tuple[int, str]]:
    """Return (statement start line, specifier), including Python from-import aliases."""
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend((node.lineno, alias.name) for alias in node.names)
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
                    (node.lineno, module + "." + alias.name if module else alias.name)
                    for alias in node.names
                )
                if module:
                    imports.append((node.lineno, module))
        return sorted(set(imports))
    if PurePosixPath(path).suffix not in JS_EXTENSIONS:
        # Preserve configured import-prefix checks for other language text.
        return [
            (number, match.group(1))
            for number, line in enumerate(text.splitlines(), 1)
            for match in re.finditer(
                r"""(?:from\s+|import\s+|require\(\s*)["']([^"']+)["']""", line
            )
        ]
    tokens = []
    line = 1
    end = 0
    for match in _JS_TOKEN.finditer(text):
        line += text[end : match.start()].count("\n")
        value = match.group()
        if not value.startswith(("//", "/*")):
            tokens.append((value, line))
        line += value.count("\n")
        end = match.end()
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
                "from",
                ";",
                "import",
                "export",
                "=",
                "(",
            }:
                tail += 1
            if tail >= len(tokens) or tokens[tail][0] != "from":
                continue
            tail += 1
        elif value == "require":
            continue
        if tail < len(tokens):
            spec = tokens[tail][0]
            if spec.startswith(("'", '"')):
                imports.append((number, spec[1:-1]))
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
            matches = []
            for module in paths:
                name = PurePosixPath(module).name
                bare = module.split("/")[0] in module_doc_roots and (
                    spec == name or spec.startswith((name + "/", name + "."))
                )
                scoped = (
                    spec.startswith("@")
                    and "/" in spec
                    and (
                        spec.split("/", 1)[1] == name
                        or spec.split("/", 1)[1].startswith(name + "/")
                    )
                )
                if bare or scoped:
                    matches.append(module)
            target = matches[0] if len(matches) == 1 else None
    return target if target != source else None
