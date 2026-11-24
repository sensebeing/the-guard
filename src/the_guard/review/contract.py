"""Declared contract of an independent architecture review: providers, limits, and record types."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROVIDERS: tuple[str, ...] = ("codex", "claude", "agy")


def build_agy_readonly_settings(common_dir: Path | None = None) -> str:
    allow = ["read_file(*)", "command(*)"]
    if common_dir is not None:
        allow.insert(1, f"read_file({common_dir})")
    return json.dumps(
        {
            "toolPermission": "proceed-in-sandbox",
            "permissions": {
                "allow": allow,
                "deny": [
                    "write_file(*)",
                    "command(git commit)",
                    "command(git push)",
                    "command(git reset)",
                    "command(git checkout)",
                    "command(git switch)",
                    "command(git merge)",
                    "command(git rebase)",
                    "command(git cherry-pick)",
                    "command(git clean)",
                    "command(rm)",
                    "command(mv)",
                    "command(cp)",
                    "command(chmod)",
                    "command(chown)",
                ],
            },
        },
        separators=(",", ":"),
    )


CONFIG_SCHEMA = "the-guard-agent-config/v1"
# Bump only for breaking changes to review semantics, prompts, scope, or output schema.
REVIEW_CONTRACT_VERSION = "v1"
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_STALL_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_RUNAWAY_SECONDS = 3600.0
POST_EOF_WAIT_TIMEOUT_SECONDS = 15.0
DEFAULT_INCREMENTAL_DIFF_MAX_CHARS = 24000
MAX_REVIEWER_NOTE_CHARS = 8000
CLAUDE_DISALLOWED_TOOLS = "Write,Edit,NotebookEdit,Task,Agent,TodoWrite"
CLAUDE_ISOLATION_SETTINGS: str = '{"disableAllHooks":true}'
CODEX_ISOLATED_HOMES_DIRNAME: str = "the-guard-codex-homes"
CODEX_DISABLED_FEATURES: tuple[str, ...] = (
    "plugins", "remote_plugin", "plugin_discovery_prefetch", "apps",
    "computer_use", "browser_use", "daemon_auto_start", "hooks", "memories",
    "multi_agent", "skill_mcp_dependency_install",
)
CODEX_ROUTING_CONFIG_KEYS: tuple[str, ...] = (
    "model", "model_provider", "model_providers", "model_reasoning_effort",
    "service_tier", "openai_base_url", "chatgpt_base_url", "model_catalog_json",
    "oss_provider", "ca_certificate", "cli_auth_credentials_store", "forced_login_method",
)
CODEX_RELATIVE_PATH_CONFIG_KEYS: tuple[str, ...] = ("model_catalog_json", "ca_certificate")


class ReviewTimeoutError(RuntimeError):
    """Raised when a review subprocess stalls, exceeds runaway limit, or fails to exit after EOF."""


@dataclass(frozen=True)
class BlockingFinding:
    """A structured architecture finding shared by review and publication adapters."""

    rule_id: str
    file: str
    line: int
    issue: str
    required_fix: str
    finding_origin: str = "initial_full_scope"


@dataclass(frozen=True)
class ReviewOutput:
    verdict: str
    summary: str
    blocking_findings: tuple[BlockingFinding, ...]
    reviewed_head_sha: str


@dataclass(frozen=True)
class SubmoduleChange:
    path: str
    old_sha: str
    new_sha: str


@dataclass(frozen=True)
class ReviewScope:
    base_ref: str
    merge_base: str
    head_sha: str
    changed_files: tuple[str, ...]
    reviewable_files: tuple[str, ...]
    submodule_changes: tuple[SubmoduleChange, ...]
    patch_digest: str
    task_blueprint_anchors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_exempt(self) -> bool:
        return not self.reviewable_files and not self.submodule_changes


@dataclass(frozen=True)
class IncrementalReviewScope:
    base_head_sha: str
    target_head_sha: str
    changed_files: tuple[str, ...]
    reviewable_files: tuple[str, ...]
    submodule_changes: tuple[SubmoduleChange, ...]
    changed_blueprints: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.reviewable_files or self.submodule_changes or self.changed_blueprints)


CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".swift",
        ".kt",
        ".kts",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".sql",
        ".sh",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
    }
)
SERVICE_CONFIG_EXTENSIONS = frozenset({".yaml", ".yml", ".json", ".toml", ".proto"})
PROMPT_SKILL_PATH_MARKERS = (
    "/skills/",
    "/prompts/",
    "/agents/",
    ".agents/skills/",
    ".claude/skills/",
    ".claude/agents/",
    ".codex/agents/",
)
POLICY_REVIEWABLE_EXACT_PATHS = frozenset(
    {"AGENTS.md", "CLAUDE.md", ".guard/config.json"}
)
POLICY_REVIEWABLE_PREFIXES = (
    ".guard/",
    "docs/standards/",
    ".claude/",
    ".agents/",
    "skills/",
)
LOCK_FILENAMES = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "Podfile.lock",
    }
)
TEST_DIR_PARTS = frozenset(
    {
        "tests",
        "test",
        "__tests__",
        "fixtures",
        "testdata",
    }
)
TEST_FILE_SUFFIXES = (
    "_test.py",
    ".test.ts",
    ".test.tsx",
    ".spec.ts",
    ".spec.tsx",
    ".test.js",
    ".spec.js",
    "tests.swift",
    "test.kt",
)
CONFIG_I18N_EXTENSIONS = frozenset(
    {
        ".strings",
        ".stringsdict",
        ".xcstrings",
        ".toml",
        ".json",
        ".yml",
        ".yaml",
    }
)

REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["GO", "NO-GO"]},
        "summary": {"type": "string"},
        "reviewed_head_sha": {"type": "string"},
        "blocking_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "issue": {"type": "string"},
                    "required_fix": {"type": "string"},
                    "finding_origin": {
                        "type": "string",
                        "enum": [
                            "initial_full_scope",
                            "prior_unresolved",
                            "introduced_by_delta",
                        ],
                    },
                },
                "required": [
                    "rule_id",
                    "file",
                    "line",
                    "issue",
                    "required_fix",
                    "finding_origin",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "blocking_findings", "reviewed_head_sha"],
    "additionalProperties": False,
}


def validate_review_output(
    data: object,
    *,
    expected_head_sha: str | None = None,
    allow_implicit_origin: bool = False,
) -> ReviewOutput:
    """Validate the canonical JSON payload accepted by review consumers."""
    if not isinstance(data, dict) or not {"verdict", "summary"} <= data.keys():
        raise ValueError("Input JSON must contain verdict and summary")
    reviewed_head_sha = verify_reviewed_head_sha(data.get("reviewed_head_sha"), expected_head_sha)
    properties = REVIEW_OUTPUT_SCHEMA["properties"]
    if (
        set(data) != set(REVIEW_OUTPUT_SCHEMA["required"])
        or data["verdict"] not in properties["verdict"]["enum"]
        or not isinstance(data["summary"], str)
    ):
        raise ValueError("Input JSON does not match the review output contract")
    findings = data["blocking_findings"]
    if not isinstance(findings, list):
        raise ValueError("Reviewer blocking_findings must be an array")
    finding_schema = properties["blocking_findings"]["items"]
    required_keys = set(finding_schema["required"])
    allowed_origins = finding_schema["properties"]["finding_origin"]["enum"]
    parsed_findings = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Every blocking finding must be an object")
        if allow_implicit_origin and "finding_origin" not in finding:
            finding = {**finding, "finding_origin": BlockingFinding.finding_origin}
        if set(finding) != required_keys:
            raise ValueError("Input JSON does not match the review output contract")
        for key in ("rule_id", "file", "issue", "required_fix"):
            if not isinstance(finding[key], str) or not finding[key].strip():
                raise ValueError(f"Blocking finding requires nonempty {key}")
        line = finding["line"]
        if type(line) is not int or line < 1:
            raise ValueError("Blocking finding line must be a positive integer")
        if finding["finding_origin"] not in allowed_origins:
            raise ValueError("Invalid finding_origin")
        parsed_findings.append(BlockingFinding(**finding))
    verdict = "NO-GO" if parsed_findings else data["verdict"]
    return ReviewOutput(verdict, data["summary"], tuple(parsed_findings), reviewed_head_sha)


def verify_reviewed_head_sha(reported: object, expected: str | None) -> str:
    if not isinstance(reported, str) or not reported.strip():
        error = RuntimeError if expected is not None else ValueError
        raise error("reviewer output requires a non-empty reviewed_head_sha string")
    normalized = reported.strip().lower()
    if expected is not None and (
        not 7 <= len(normalized) <= len(expected)
        or re.fullmatch(r"[0-9a-f]+", normalized) is None
        or not expected.lower().startswith(normalized)
    ):
        raise RuntimeError(
            f"reviewer audited mismatched HEAD snapshot (expected={expected[:12]}, reported={normalized[:12]})"
        )
    return normalized
