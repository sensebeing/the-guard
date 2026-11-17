"""Repository-owned configuration. No project-specific policy lives here."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

DEFAULT_MANIFEST_TAG = "the-guard-policy"
DEFAULT_SCHEMA = "the-guard-policy/v1"
DEFAULT_DETAIL_MANIFEST = "AGENTS.md#engineering-quality-policy"
DEFAULT_RULE_PREFIX = "guard."
REVIEW_INTEGRATION_MODES = ("local-hooks", "github-pr", "both")


@dataclass(frozen=True)
class GuardConfig:
    manifest_file: str = "AGENTS.md"
    manifest_tag: str = DEFAULT_MANIFEST_TAG
    detail_dir: str = "docs/standards/rules"
    manifest_schema: str = DEFAULT_SCHEMA
    detail_manifest_ref: str | None = None
    rule_id_prefix: str = DEFAULT_RULE_PREFIX
    exempt_tag: str = "guard-exempt"
    main_branch: str = "main"
    module_doc_roots: tuple[str, ...] = (
        "services",
        "packages",
        "apps",
        "modules",
        "src",
    )
    review: dict[str, Any] = field(default_factory=dict)
    topology: dict[str, Any] = field(default_factory=dict)


def load_config(root: Path | str, *, revision: str | None = None) -> GuardConfig:
    root = Path(root)
    if revision is None:
        path = root / ".guard/config.json"
        raw = path.read_text() if path.exists() else "{}"
    else:
        proc = subprocess.run(
            ["git", "show", f"{revision}:.guard/config.json"],
            cwd=root,
            capture_output=True,
        )
        if proc.returncode:
            exists = subprocess.run(
                ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                cwd=root,
                capture_output=True,
            )
            if exists.returncode:
                raise ValueError(f"Cannot read configuration revision {revision}")
            raw = "{}"
        else:
            raw = proc.stdout.decode("utf-8")
    return validate_config(json.loads(raw))


def validate_config(data: dict[str, Any]) -> GuardConfig:
    """Validate serialized configuration before reading or publishing it."""
    if not isinstance(data, dict):
        raise ValueError("Guard configuration must be a JSON object")
    data = dict(data)
    unknown = set(data) - set(GuardConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown guard configuration keys: {sorted(unknown)}")
    for key in ("review", "topology"):
        if key in data and not isinstance(data[key], dict):
            raise ValueError(f"{key} must be an object")
    for key in (
        "rule_id_prefix",
        "exempt_tag",
        "main_branch",
        "manifest_schema",
        "manifest_file",
        "manifest_tag",
        "detail_dir",
    ):
        if key in data and (not isinstance(data[key], str) or not data[key]):
            raise ValueError(f"{key} must be a nonempty string")
    for key in ("detail_manifest_ref",):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError(f"{key} must be a string or null")
    if "module_doc_roots" in data:
        if not isinstance(data["module_doc_roots"], list) or not all(
            isinstance(x, str) for x in data["module_doc_roots"]
        ):
            raise ValueError("module_doc_roots must be an array of paths")
        data["module_doc_roots"] = tuple(data["module_doc_roots"])
    _validate_sections(data)
    return GuardConfig(**data)


def repository_path(root: Path, relative: str) -> Path:
    """Reject traversal and symlinks escaping the repository for managed files."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Path must stay inside repository: {relative}")
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes repository: {relative}")
    return target


def _string_list(value: object, label: str) -> None:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(x, str) for x in value
    ):
        raise ValueError(f"{label} must be an array of strings")


def _validate_sections(data: dict[str, Any]) -> None:
    review = data.get("review", {})
    if (
        "integration_mode" in review
        and review["integration_mode"] not in REVIEW_INTEGRATION_MODES
    ):
        raise ValueError(
            "review.integration_mode must be local-hooks, github-pr, or both"
        )
    for key in (
        "reviewable_extensions",
        "reviewable_prefixes",
        "extra_reviewable_paths",
        "service_config_roots",
    ):
        if key in review:
            _string_list(review[key], "review." + key)
    if "project_title" in review and not isinstance(review["project_title"], str):
        raise ValueError("review.project_title must be a string")
    topology = data.get("topology", {})
    if "enforce_module_dependencies" in topology and not isinstance(
        topology["enforce_module_dependencies"], bool
    ):
        raise ValueError("topology.enforce_module_dependencies must be a bool")
    layers = topology.get("layers", {})
    if not isinstance(layers, dict) or any(
        not isinstance(k, str) or not k.strip() for k in layers
    ):
        raise ValueError("topology.layers must be an object with nonempty layer names")
    for layer, targets in layers.items():
        _string_list(targets, f"topology.layers.{layer}")
        if any(target not in layers for target in targets):
            raise ValueError(f"topology.layers.{layer} references an unknown layer")
    for key in ("python_prod_prefixes", "swift_prod_prefixes"):
        if key in topology:
            _string_list(topology[key], "topology." + key)
    for key in (
        "exclusive_sql_owners",
        "retired_tables",
        "forbidden_patterns",
        "forbidden_imports",
        "module_boundaries",
        "modules",
    ):
        entries = topology.get(key, [])
        if key == "retired_tables" and isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list) or not all(
            isinstance(x, dict) for x in entries
        ):
            raise ValueError(f"topology.{key} must be an array of objects")
        for rule in entries:
            for name in ("rule_id", "message"):
                if name in rule and (not isinstance(rule[name], str) or not rule[name]):
                    raise ValueError(f"{key}.{name} must be a nonempty string")
            if "scope_prefixes" in rule:
                _string_list(rule["scope_prefixes"], key + ".scope_prefixes")
            if key == "exclusive_sql_owners":
                if not isinstance(rule.get("table"), str) or not rule["table"]:
                    raise ValueError("exclusive_sql_owners requires table")
                _string_list(rule.get("owners"), "exclusive_sql_owners.owners")
            elif key == "retired_tables":
                _string_list(rule.get("tables"), "retired_tables.tables")
                if not rule["tables"] or not all(rule["tables"]):
                    raise ValueError("retired_tables requires nonempty table names")
            elif key == "forbidden_patterns":
                expression = rule.get("literal_or_regex")
                if not isinstance(expression, str) or not expression:
                    raise ValueError("forbidden_patterns requires literal_or_regex")
                if not rule.get("literal", False):
                    try:
                        re.compile(expression)
                    except re.error as exc:
                        raise ValueError(f"Invalid forbidden pattern: {exc}") from exc
            elif key in {"forbidden_imports", "module_boundaries"}:
                imports = rule.get(
                    "imports", rule.get("forbidden_imports", [rule.get("to")])
                )
                _string_list(imports, key + ".imports")
                if not imports or not all(imports):
                    raise ValueError(f"{key} requires at least one import prefix")
                if "from" in rule and not isinstance(rule["from"], str):
                    raise ValueError(f"{key}.from must be a string")
            elif key == "modules":
                _validate_module(rule, topology)
    modules = topology.get("modules", [])
    by_path = {module["path"]: module for module in modules}
    if len(by_path) != len(modules):
        raise ValueError("topology.modules paths must be unique")
    if topology.get("enforce_module_dependencies") or layers:
        for module in modules:
            for dependency in module.get("dependencies", []):
                if dependency not in by_path or dependency == module["path"]:
                    raise ValueError(
                        f"Module '{module['path']}' has unknown or self dependency '{dependency}'"
                    )
                source_layer = module.get("layer")
                target_layer = by_path[dependency].get("layer")
                if (
                    layers
                    and source_layer
                    and target_layer
                    and target_layer not in layers[source_layer]
                ):
                    raise ValueError(
                        f"Layer '{source_layer}' must not depend on layer '{target_layer}' ({module['path']} -> {dependency})"
                    )


def _safe_relative_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not PurePosixPath(value).is_absolute()
        and not PureWindowsPath(value).drive
        and "\\" not in value
        and not any(part in {"", ".", ".."} for part in value.split("/"))
    )


def _validate_module(module: dict[str, Any], topology: dict[str, Any]) -> None:
    if not _safe_relative_path(module.get("path")):
        raise ValueError("Each module requires a normalized repository-relative path")
    for key in ("languages", "boundary_docs", "dependencies"):
        if key in module:
            _string_list(module[key], "modules." + key)
    if any(not _safe_relative_path(doc) for doc in module.get("boundary_docs", [])):
        raise ValueError("Module boundary_docs must be safe repository-relative paths")
    for key in ("layer", "role"):
        if key in module and (
            not isinstance(module[key], str) or not module[key].strip()
        ):
            raise ValueError(f"modules.{key} must be a nonempty string")
    layers = topology.get("layers", {})
    if layers and "layer" in module and module["layer"] not in layers:
        raise ValueError(f"Unknown module layer: {module['layer']}")
    if layers and topology.get("enforce_module_dependencies") and "layer" not in module:
        raise ValueError(f"Module '{module['path']}' requires a layer")
