"""Canonical import extraction and repository module ownership resolution."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import NamedTuple

from ..config import GuardConfig

JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
DETERMINISTIC_IMPORT_EXTENSIONS = JS_EXTENSIONS | {".py"}
_SUFFIX_IGNORES_LEADING_DOTS = not PurePosixPath("..py").suffix
_JS_TOKEN = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|"
    r"'(?:\\[\s\S]|[^'\\\r\n])*'|\"(?:\\[\s\S]|[^\"\\\r\n])*\"|"
    r"\+\+|--|=>|\.\.\.|===|!==|==|!=|<=|>=|&&|\|\||\?\?|<<|>>>|>>|\*\*|"
    r"[+\-*/%&|^~!<>=]=|\?\.|"
    r"0[xX][\da-fA-F_]+n?|0[bB][01_]+n?|0[oO][0-7_]+n?|"
    r"\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?[\d_]+)?n?|"
    r"\.\d[\d_]*(?:[eE][+-]?[\d_]+)?|"
    r"[A-Za-z_$][\w$]*|\s+|[^\s]"
)


_JS_EXPRESSION_KEYWORDS = {
    "return",
    "throw",
    "case",
    "typeof",
    "void",
    "delete",
    "in",
    "instanceof",
    "new",
    "yield",
    "await",
    "of",
    "else",
    "do",
}


def _regex_literal_end(text: str, start: int) -> int | None:
    """Find a single-line regex delimiter, respecting escapes and character classes."""
    index, in_class = start + 1, False
    while index < len(text) and text[index] not in "\r\n":
        char = text[index]
        if char == "\\":
            if index + 1 >= len(text) or text[index + 1] in "\r\n":
                return None
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(text) and (text[index].isalnum() or text[index] in "_$"):
                index += 1
            return index
        index += 1
    return None


def _js_tokens(text: str) -> Iterator[tuple[str, int]]:
    """Skip template text but tokenize executable interpolations at any nesting depth."""
    # Each code/interpolation frame owns its brace kinds; None marks template text.
    contexts: list[list[str] | None] = [[]]
    parens: list[bool] = []
    regex_allowed = True
    index, line = 0, 1
    previous = ""
    while index < len(text):
        if contexts[-1] is None:
            char = text[index]
            size = 1
            if char == "\\":
                size = 2
            elif char == "`":
                contexts.pop()
                regex_allowed = False
                previous = "`"
                yield previous, line
            elif text.startswith("${", index):
                contexts.append([])
                regex_allowed = True
                previous = "{"
                yield previous, line
                size = 2
            line += text[index : index + size].count("\n")
            index += size
            continue
        match = _JS_TOKEN.match(text, index)
        if match is None:
            break
        value = match.group()
        if value in {"/", "/="} and regex_allowed:
            regex_end = _regex_literal_end(text, index)
            if regex_end is not None:
                regex_allowed = False
                previous = text[index:regex_end]
                yield previous, line
                index = regex_end
                continue
        start_line = line
        line += value.count("\n")
        index = match.end()
        if value.isspace() or value.startswith(("//", "/*")):
            continue
        if value == "`":
            contexts.append(None)
        elif value == "(":
            parens.append(previous in {"if", "while", "for", "with"})
            regex_allowed = True
        elif value == ")":
            regex_allowed = parens.pop() if parens else False
        elif value == "{":
            is_expr_brace = regex_allowed and previous not in {
                ")",
                "else",
                "do",
                "try",
                "finally",
            }
            contexts[-1].append("expr" if is_expr_brace else "block")
            regex_allowed = True
        elif value == "}":
            if not contexts[-1] and len(contexts) > 1:
                contexts.pop()
            else:
                kind = contexts[-1].pop() if contexts[-1] else "block"
                regex_allowed = kind == "block"
        elif value in {"++", "--"}:
            # Prefix keeps expression-start context; postfix keeps division context.
            pass
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", value):
            regex_allowed = value in _JS_EXPRESSION_KEYWORDS
        elif (
            value.startswith(("'", '"'))
            or value == "]"
            or value[0].isdigit()
            or (value.startswith(".") and len(value) > 1 and value[1].isdigit())
        ):
            regex_allowed = False
        else:
            regex_allowed = value not in {".", "?."}
        previous = value
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
                modules = []
                if node.level:
                    parts = path.removesuffix(".py").split("/")[: -node.level]
                    if parts and parts[0] in module_doc_roots:
                        modules.append(
                            ".".join([*parts, *([module] if module else [])])
                        )
                        parts = parts[1:]
                    module = ".".join([*parts, *([module] if module else [])])
                modules.append(module)
                for module in modules:
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
            terminators = {",", ")"}
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


def _module_stem(path: str) -> str:
    name_start = path.rfind("/") + 1
    dot = path.rfind(".")
    if dot <= name_start or path[dot:] not in DETERMINISTIC_IMPORT_EXTENSIONS:
        return path
    # Python 3.14's pathlib ignores all leading dots when finding a suffix.
    if (
        _SUFFIX_IGNORES_LEADING_DOTS
        and path[name_start] == "."
        and not path[name_start:dot].strip(".")
    ):
        return path
    return path[:dot]


def _module_identities(path: str) -> tuple[str, ...]:
    stem = _module_stem(path)
    if path.endswith("/__init__.py"):
        return stem, stem.removesuffix("/__init__")
    return (stem,)


class _ModuleIndex(NamedTuple):
    owner_paths_by_top: Mapping[str, tuple[str, ...]]
    identities_by_top: Mapping[str, tuple[tuple[str, str], ...]]
    unique_alias_roots: Mapping[str, str]

    def owner(self, file_path: str) -> str | None:
        paths = self.owner_paths_by_top.get(file_path.partition("/")[0], ())
        return next(
            (
                path
                for path in paths
                if file_path == path or file_path.startswith(path + "/")
            ),
            None,
        )


@lru_cache(maxsize=64)
def _module_index(
    module_paths: tuple[str, ...], module_doc_roots: tuple[str, ...]
) -> _ModuleIndex:
    owner_paths_by_top: dict[str, list[str]] = {}
    for path in sorted(module_paths, key=lambda path: (-len(path), path)):
        owner_paths_by_top.setdefault(path.partition("/")[0], []).append(path)

    identities = sorted(
        (
            (identity, module)
            for module in module_paths
            for identity in _module_identities(module)
        ),
        key=lambda item: (-len(item[0]), -len(_module_stem(item[1])), item[1]),
    )
    identities_by_top: dict[str, list[tuple[str, str]]] = {}
    alias_roots: dict[str, set[str]] = {}
    for identity, module in identities:
        root, sep, rest = identity.partition("/")
        identities_by_top.setdefault(root, []).append((identity, module))
        if sep and root in module_doc_roots:
            name = rest.partition("/")[0]
            alias_roots.setdefault(name, set()).add(root + "/" + name)

    unique_alias_roots = {
        name: next(iter(roots)) for name, roots in alias_roots.items() if len(roots) == 1
    }
    return _ModuleIndex(
        MappingProxyType({top: tuple(paths) for top, paths in owner_paths_by_top.items()}),
        MappingProxyType({top: tuple(pairs) for top, pairs in identities_by_top.items()}),
        MappingProxyType(unique_alias_roots),
    )


def owner_module(file_path: str, module_paths: Sequence[str]) -> str | None:
    return _module_index(tuple(module_paths), GuardConfig.module_doc_roots).owner(file_path)


def _target_owner(path: str, index: _ModuleIndex) -> str | None:
    stem = _module_stem(path)
    identities = index.identities_by_top.get(stem.partition("/")[0], ())
    return next(
        (
            module
            for identity, module in identities
            if stem == identity or stem.startswith(identity + "/")
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
    index = _module_index(tuple(module_paths), tuple(module_doc_roots))
    if spec.startswith(("./", "../")):
        parts = [part for part in file_path.split("/") if part not in {"", "."}][:-1]
        # PurePosixPath preserves exactly two leading slashes as a distinct anchor.
        if file_path.startswith("//") and not file_path.startswith("///"):
            parts.insert(0, "//")
        elif file_path.startswith("/"):
            parts.insert(0, "/")
        for part in spec.split("/"):
            if part == "..":
                if not parts:
                    return None
                parts.pop()
            elif part not in {"", "."}:
                parts.append(part)
        target = _target_owner("/".join(parts), index)
    else:
        literal = _module_stem(spec)
        target = _target_owner(
            literal if "/" in literal else literal.replace(".", "/"), index
        )
        if target is None:
            alias = (
                spec.split("/", 1)[1] if spec.startswith("@") and "/" in spec else spec
            )
            # Slash paths preserve file extensions; dotted Python paths become segments.
            alias = _module_stem(alias)
            alias_path = alias if "/" in alias else alias.replace(".", "/")
            name, _, suffix = alias_path.partition("/")
            prefix = index.unique_alias_roots.get(name)
            if prefix is not None:
                target = _target_owner(prefix + ("/" + suffix if suffix else ""), index)
    return target if target is not None and target != index.owner(file_path) else None
