"""Repository-owned configuration. No project-specific policy lives here."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .git_helpers import read_repository_file, repository_path_exists

VERSION = "0.1.0"

DEFAULT_MANIFEST_TAG = "the-guard-policy"
DEFAULT_SCHEMA = "the-guard-policy/v1"
DEFAULT_DETAIL_MANIFEST = "AGENTS.md#engineering-quality-policy"
DEFAULT_RULE_PREFIX = "guard."
REVIEW_INTEGRATION_MODES = ("local-hooks", "github-pr", "both")
GITHUB_PUBLISH_MODES = ("check-run", "pr-review")
DEFAULT_GITHUB_PUBLISH_MODE = "check-run"


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


def load_config(
    root: Path | str, *, revision: str | None = None, allow_historical: bool = False
) -> GuardConfig:
    if allow_historical and revision is None:
        raise ValueError("Historical configuration requires a Git revision")
    root = Path(root)
    path = ".guard/config.json"
    raw = (
        read_repository_file(root, path, revision=revision, require_regular=True)
        if repository_path_exists(root, path, revision)
        else "{}"
    )
    data = json.loads(raw)
    if allow_historical:
        _normalize_historical_import_rules(data)
    return validate_config(data)


def _normalize_historical_import_rules(data: Any) -> None:
    """Decode previously supported import rules at an immutable baseline."""
    topology = data.get("topology") if isinstance(data, dict) else None
    if not isinstance(topology, dict):
        return
    groups = ("forbidden_imports", "module_boundaries")
    if not any(key in topology for key in groups):
        return
    rules = []
    for key in groups:
        entries = topology.pop(key, [])
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError(f"topology.{key} must be an array of objects")
        for rule in entries:
            if "from" in rule:
                source = rule.pop("from")
                if not isinstance(source, str):
                    raise ValueError(f"{key}.from must be a string")
                rule.setdefault("scope_prefixes", [source])
            target = rule.pop("to", None)
            imports = rule.pop("forbidden_imports", [target])
            rule.setdefault("imports", imports)
            rules.append(rule)
    topology["forbidden_imports"] = rules


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
    if (
        "github_publish_mode" in review
        and review["github_publish_mode"] not in GITHUB_PUBLISH_MODES
    ):
        raise ValueError(
            "review.github_publish_mode must be check-run or pr-review"
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
    for key, is_valid, label in (
        ("operational_ledgers", _safe_relative_path, "paths"),
        ("blueprint_globs", _relative_glob, "globs"),
    ):
        if key in review and (
            not isinstance(review[key], list) or not all(map(is_valid, review[key]))
        ):
            raise ValueError(f"review.{key} must be an array of safe repository-relative {label}")
    if "rule_scope_globs" in review:
        rule_scope_globs = review["rule_scope_globs"]
        if not isinstance(rule_scope_globs, dict) or any(
            not isinstance(tag, str) or not tag.strip()
            for tag in rule_scope_globs
        ):
            raise ValueError(
                "review.rule_scope_globs must be an object mapping nonempty scope tags to glob arrays"
            )
        for tag, globs in rule_scope_globs.items():
            if not isinstance(globs, list) or not all(map(_relative_glob, globs)):
                raise ValueError(
                    f"review.rule_scope_globs.{tag} must be an array of safe repository-relative globs"
                )
    topology = data.get("topology", {})
    production_scopes = ("python_prod_prefixes", "swift_prod_prefixes")
    rule_groups = (
        "exclusive_sql_owners", "retired_tables", "forbidden_patterns", "forbidden_imports", "modules",
    )
    unknown = topology.keys() - {
        "enforce_module_dependencies", "architecture_docs", "layers", *production_scopes, *rule_groups,
    }
    if unknown:
        raise ValueError(f"Unknown topology keys: {sorted(unknown)}")
    if "enforce_module_dependencies" in topology and not isinstance(
        topology["enforce_module_dependencies"], bool
    ):
        raise ValueError("topology.enforce_module_dependencies must be a bool")
    if "architecture_docs" in topology:
        _string_list(topology["architecture_docs"], "topology.architecture_docs")
        if any(not _safe_relative_path(doc) for doc in topology["architecture_docs"]):
            raise ValueError("topology.architecture_docs must be safe repository-relative paths")
    layers = topology.get("layers", {})
    if not isinstance(layers, dict) or any(
        not isinstance(k, str) or not k.strip() for k in layers
    ):
        raise ValueError("topology.layers must be an object with nonempty layer names")
    for layer, targets in layers.items():
        _string_list(targets, f"topology.layers.{layer}")
        if any(target not in layers for target in targets):
            raise ValueError(f"topology.layers.{layer} references an unknown layer")
    for key in production_scopes:
        if key in topology:
            _string_list(topology[key], "topology." + key)
    for key in rule_groups:
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
            elif key == "forbidden_imports":
                if rule.keys() - {"scope_prefixes", "imports", "rule_id", "message"}:
                    raise ValueError("Unknown forbidden_imports rule keys")
                imports = rule.get("imports")
                _string_list(imports, key + ".imports")
                if not imports or not all(imports):
                    raise ValueError(f"{key} requires at least one import prefix")
            elif key == "modules":
                _validate_module(rule, topology)
    modules = topology.get("modules", [])
    by_path = {module["path"]: module for module in modules}
    if len(by_path) != len(modules):
        raise ValueError("topology.modules paths must be unique")
    retired_prefixes = tuple(path + "/" for path, m in by_path.items() if m.get("retired", False))
    for module in modules:
        if not module.get("retired", False) and module["path"].startswith(retired_prefixes):
            raise ValueError(f"Active module '{module['path']}' cannot be inside a retired module")
        for dependency in module.get("dependencies", []):
            target = by_path.get(dependency)
            if target is not None and target.get("retired", False):
                raise ValueError(f"Module '{module['path']}' cannot depend on retired module '{dependency}'")
            if topology.get("enforce_module_dependencies") or layers:
                if target is None or dependency == module["path"]:
                    raise ValueError(
                        f"Module '{module['path']}' has unknown or self dependency '{dependency}'"
                    )
                source_layer = module.get("layer")
                target_layer = target.get("layer")
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
        and "\0" not in value
        and not any(part in {"", ".", ".."} for part in value.split("/"))
    )


def _relative_glob(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\0" not in value
        and not PurePosixPath(value).is_absolute()
        and not PureWindowsPath(value).anchor
        and ".." not in PureWindowsPath(value).parts
    )


def _validate_module(module: dict[str, Any], topology: dict[str, Any]) -> None:
    if not _safe_relative_path(module.get("path")):
        raise ValueError("Each module requires a normalized repository-relative path")
    for key in ("languages", "boundary_docs", "dependencies"):
        if key in module:
            _string_list(module[key], "modules." + key)
    retired = module.get("retired", False)
    if not isinstance(retired, bool):
        raise ValueError("modules.retired must be a boolean")
    if retired and "dependencies" in module and module["dependencies"] != []:
        raise ValueError("Retired modules must omit dependencies or declare an empty array")
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
    if not retired and layers and topology.get("enforce_module_dependencies") and "layer" not in module:
        raise ValueError(f"Module '{module['path']}' requires a layer")
