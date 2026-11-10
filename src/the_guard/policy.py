from __future__ import annotations

import posixpath
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from .config import DEFAULT_DETAIL_MANIFEST as DEFAULT_DETAIL_MANIFEST
from .config import DEFAULT_MANIFEST_TAG as DEFAULT_MANIFEST_TAG
from .config import DEFAULT_RULE_PREFIX as DEFAULT_RULE_PREFIX
from .config import DEFAULT_SCHEMA as DEFAULT_SCHEMA
from .config import GuardConfig, load_config, repository_path

RULE_FIELDS = frozenset({"id", "severity", "scope", "summary", "details", "checker"})
ALLOWED_SEVERITIES = frozenset({"block"})


class PolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    scope: tuple[str, ...]
    summary: str
    details: str
    checker: str
    detail_body: str = ""


@dataclass(frozen=True)
class Policy:
    schema: str
    rules: tuple[Rule, ...]


def load_policy(
    root: Path | str, *, revision: str | None = None, config: GuardConfig | None = None
) -> Policy:
    repo_root = Path(root)
    cfg = config or load_config(repo_root, revision=revision)
    manifest_text = _read_policy_file(repo_root, cfg.manifest_file, revision=revision)
    yaml_text = _extract_policy_yaml(manifest_text, tags=(cfg.manifest_tag,))
    parsed = _parse_manifest(yaml_text)
    policy = _validate_manifest(parsed, config=cfg)
    enriched_rules = _validate_rule_details(
        repo_root, policy.rules, revision=revision, config=cfg
    )
    return Policy(schema=policy.schema, rules=enriched_rules)


def _read_policy_file(
    repo_root: Path,
    rel_path: str,
    *,
    revision: str | None,
    _seen: frozenset[str] = frozenset(),
) -> str:
    if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
        raise PolicyError(f"Unsafe policy path: {rel_path}")
    if rel_path in _seen:
        raise PolicyError(f"Cyclic policy symlink: {rel_path}")
    if revision is not None:
        try:
            entry = subprocess.run(
                ["git", "ls-tree", revision, "--", f":(literal){rel_path}"],
                cwd=repo_root,
                capture_output=True,
                check=True,
                timeout=15,
            )
            if entry.stdout.startswith(b"120000 "):
                link = subprocess.run(
                    ["git", "show", f"{revision}:{rel_path}"],
                    cwd=repo_root,
                    capture_output=True,
                    check=True,
                    timeout=15,
                ).stdout.decode("utf-8")
                if posixpath.isabs(link):
                    raise PolicyError(f"Policy symlink escapes repository: {rel_path}")
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(rel_path), link)
                )
                return _read_policy_file(
                    repo_root, target, revision=revision, _seen=_seen | {rel_path}
                )
            proc = subprocess.run(
                ["git", "show", f"{revision}:{rel_path}"],
                cwd=repo_root,
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PolicyError(
                f"Cannot read {rel_path} at revision {revision}: {exc}"
            ) from exc
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise PolicyError(
                f"Rule or policy file does not exist at revision {revision}: {rel_path} ({err})"
            )
        try:
            return proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyError(
                f"Invalid UTF-8 in {rel_path} at revision {revision}: {exc}"
            ) from exc

    try:
        target = repository_path(repo_root, rel_path)
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    if not target.is_file():
        raise PolicyError(f"Policy or rule details file does not exist: {rel_path}")
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"Cannot read policy file {target}: {exc}") from exc


def _extract_policy_yaml(
    text: str, tags: tuple[str, ...] = (DEFAULT_MANIFEST_TAG,)
) -> str:
    present = [tag for tag in tags if f"<!-- {tag}:start -->" in text]
    if len(present) != 1:
        raise PolicyError(f"Expected exactly one {tags} policy manifest block")
    start_tag = f"<!-- {present[0]}:start -->"
    end_tag = f"<!-- {present[0]}:end -->"
    if text.count(start_tag) != 1 or text.count(end_tag) != 1:
        raise PolicyError(f"Duplicate or incomplete {tags} policy block")
    start = text.find(start_tag)
    end = text.find(end_tag, start + len(start_tag)) if start != -1 else -1
    if start == -1 or end == -1:
        raise PolicyError(f"Missing {tags} policy block")

    block = text[start + len(start_tag) : end]
    match = re.search(r"```yaml\s*\n(?P<yaml>.*?)\n```", block, re.DOTALL)
    if not match:
        raise PolicyError(f"Policy block {tags} must contain fenced yaml")
    return match.group("yaml")


def _parse_manifest(text: str) -> dict[str, object]:
    schema = None
    rules: list[dict[str, object]] = []
    current_rule: dict[str, object] | None = None
    in_rules = False

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line:
            raise PolicyError(f"Unsupported manifest indentation on line {lineno}")

        line = raw_line.strip()
        if not line:
            continue

        if not in_rules:
            if _indent(raw_line) != 0:
                raise PolicyError(f"Unsupported manifest indentation on line {lineno}")
            if line == "rules:":
                in_rules = True
                continue
            key, value = _parse_key_value(line, lineno)
            if key != "schema":
                raise PolicyError(
                    f"Unsupported top-level manifest field {key!r} on line {lineno}"
                )
            if schema is not None:
                raise PolicyError(f"Duplicate top-level field {key!r} on line {lineno}")
            schema = value
            continue

        indent = _indent(raw_line)
        if indent == 2 and raw_line[2:].startswith("- "):
            if current_rule is not None:
                rules.append(current_rule)
            current_rule = {}
            item = raw_line[4:].strip()
            if item:
                key, value = _parse_key_value(item, lineno)
                _validate_rule_field_name(key, lineno)
                _set_rule_field(current_rule, key, value, lineno)
            continue

        if indent == 4:
            if current_rule is None:
                raise PolicyError(
                    f"Rule field appeared before a rule item on line {lineno}"
                )
            key, value = _parse_key_value(raw_line[4:].strip(), lineno)
            _validate_rule_field_name(key, lineno)
            _set_rule_field(current_rule, key, value, lineno)
            continue

        if indent == 0:
            key = line.split(":", 1)[0]
            raise PolicyError(
                f"Unsupported top-level manifest field {key!r} after rules on line {lineno}"
            )

        raise PolicyError(f"Unsupported manifest indentation on line {lineno}")

    if current_rule is not None:
        rules.append(current_rule)
    return {"schema": schema, "rules": rules}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _validate_rule_field_name(key: str, lineno: int) -> None:
    if key not in RULE_FIELDS:
        raise PolicyError(f"Unknown rule field {key!r} on line {lineno}")


def _set_rule_field(rule: dict[str, object], key: str, value: str, lineno: int) -> None:
    if key in rule:
        raise PolicyError(f"Duplicate rule field {key!r} on line {lineno}")
    rule[key] = _parse_value(key, value, lineno)


def _parse_key_value(line: str, lineno: int) -> tuple[str, str]:
    if ":" not in line:
        raise PolicyError(f"Expected key: value on line {lineno}")
    key, value = line.split(":", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        raise PolicyError(f"Missing key on line {lineno}")
    if not value:
        raise PolicyError(f"Missing value for {key!r} on line {lineno}")
    return key, value


def _parse_value(key: str, value: str, lineno: int) -> object:
    if key == "scope":
        if not (value.startswith("[") and value.endswith("]")):
            raise PolicyError(f"Scope must use inline list syntax on line {lineno}")
        inner = value[1:-1].strip()
        if not inner:
            return tuple()
        scope = tuple(_strip_quotes(part.strip()) for part in inner.split(","))
        if not all(scope):
            raise PolicyError(f"Scope contains an empty entry on line {lineno}")
        return scope
    return _strip_quotes(value)


def _strip_quotes(value: str) -> str:
    starts_quoted = value.startswith(("'", '"'))
    ends_quoted = value.endswith(("'", '"'))
    if starts_quoted or ends_quoted:
        if len(value) < 2 or not (
            starts_quoted and ends_quoted and value[0] == value[-1]
        ):
            raise PolicyError(f"Malformed quoted value {value!r}")
        return value[1:-1]
    return value


def _validate_manifest(
    parsed: dict[str, object], config: GuardConfig | None = None
) -> Policy:
    cfg = config or GuardConfig()
    schema = parsed.get("schema")
    if schema != cfg.manifest_schema:
        raise PolicyError(
            f"Unsupported policy schema {schema!r}; expected {cfg.manifest_schema!r}"
        )

    raw_rules = parsed.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise PolicyError("Policy manifest must define at least one rule")

    rules: list[Rule] = []
    seen_ids: set[str] = set()
    required = ("id", "severity", "scope", "summary", "details", "checker")

    for index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise PolicyError(f"Rule {index} must be a mapping")

        missing = [field for field in required if field not in raw_rule]
        if missing:
            rule_label = raw_rule.get("id", f"#{index}")
            raise PolicyError(
                f"Rule {rule_label} missing required fields: {', '.join(missing)}"
            )

        rule_id = _require_str(raw_rule["id"], "id", index)
        if rule_id in seen_ids:
            raise PolicyError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        if not rule_id.startswith(cfg.rule_id_prefix):
            raise PolicyError(
                f"Manifest rule id must start with {cfg.rule_id_prefix}: {rule_id}"
            )

        scope = raw_rule["scope"]
        if (
            not isinstance(scope, tuple)
            or not scope
            or not all(isinstance(item, str) and item for item in scope)
        ):
            raise PolicyError(f"Rule {rule_id} scope must be a non-empty inline list")

        severity = _require_str(raw_rule["severity"], "severity", index)
        if severity not in ALLOWED_SEVERITIES:
            raise PolicyError(
                f"Rule {rule_id} has unsupported severity {severity!r}; "
                f"expected one of: {', '.join(sorted(ALLOWED_SEVERITIES))}"
            )

        rules.append(
            Rule(
                id=rule_id,
                severity=severity,
                scope=scope,
                summary=_require_str(raw_rule["summary"], "summary", index),
                details=_require_str(raw_rule["details"], "details", index),
                checker=_require_str(raw_rule["checker"], "checker", index),
            )
        )

    return Policy(schema=schema, rules=tuple(rules))


def _require_str(value: object, field: str, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyError(f"Rule {index} field {field!r} must be a non-empty string")
    return value


def _validate_rule_details(
    root: Path,
    rules: tuple[Rule, ...],
    *,
    revision: str | None = None,
    config: GuardConfig | None = None,
) -> tuple[Rule, ...]:
    cfg = config or GuardConfig()
    repo_root = root.resolve()
    enriched: list[Rule] = []
    for rule in rules:
        details_path = Path(rule.details)
        if details_path.is_absolute() or ".." in details_path.parts:
            raise PolicyError(
                f"Rule {rule.id} details path must stay inside the repository"
            )

        absolute_path = (
            (repo_root / details_path)
            if revision
            else (repo_root / details_path).resolve()
        )
        try:
            rel_posix = absolute_path.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise PolicyError(
                f"Rule {rule.id} details path must stay inside the repository"
            ) from exc

        raw_detail = _read_policy_file(repo_root, rel_posix, revision=revision)
        frontmatter, body = _parse_frontmatter_and_body(raw_detail, label=rel_posix)
        detail_id = frontmatter.get("id")
        if detail_id != rule.id:
            raise PolicyError(
                f"Rule {rule.id} details frontmatter id {detail_id!r} does not match manifest id"
            )

        detail_manifest = frontmatter.get("manifest")
        expected_manifest_ref = (
            cfg.detail_manifest_ref or f"{cfg.manifest_file}#engineering-quality-policy"
        )
        if not frontmatter.get("title", "").strip():
            raise PolicyError(f"Rule {rule.id} details missing title")
        if detail_manifest != expected_manifest_ref:
            raise PolicyError(
                f"Rule {rule.id} details manifest {detail_manifest!r} must be {expected_manifest_ref!r}"
            )

        if not body:
            raise PolicyError(
                f"Rule {rule.id} details file has empty body: {rule.details}"
            )

        enriched.append(replace(rule, detail_body=body))
    return tuple(enriched)


def _parse_frontmatter_and_body(
    raw_text: str, *, label: str
) -> tuple[dict[str, str], str]:
    lines = raw_text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PolicyError(f"Details file {label} missing frontmatter")

    frontmatter: dict[str, str] = {}
    for lineno, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            body = "\n".join(lines[lineno:]).strip()
            return frontmatter, body
        key, value = _parse_key_value(line.strip(), lineno)
        if key in frontmatter:
            raise PolicyError(
                f"Duplicate frontmatter field {key!r} in {label} on line {lineno}"
            )
        frontmatter[key] = _strip_quotes(value)

    raise PolicyError(f"Details file {label} missing closing frontmatter marker")
