"""Declared contract of an independent architecture review: providers, limits, and record types."""

from __future__ import annotations

import json
from dataclasses import dataclass
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

    @property
    def has_changes(self) -> bool:
        return bool(self.reviewable_files or self.submodule_changes)


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
