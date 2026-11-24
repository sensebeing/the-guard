"""Persistence for architecture review receipts, incremental review sessions, and bounded run logs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from ..auto_update import (
    _installed_vcs_commit,
    compute_package_tree_digest,
    load_auto_update_status,
)
from ..config import GuardConfig
from ..git_helpers import git_stdout
from ..policy import Policy
from .contract import BlockingFinding, ReviewScope
from .documents import (
    architecture_document_paths,
    boundary_document_paths,
    contract_document_paths,
    existing_document_paths,
    normalize_task_blueprint_anchors,
    read_document,
    resolve_scope_modules,
)

RECEIPT_SCHEMA = "the-guard-receipt/v1"
SESSION_SCHEMA = "the-guard-session/v1"
LOG_META_SCHEMA = "the-guard-log-meta/v1"
UTC = timezone.utc
MAX_RETAINED_RUN_LOGS = 30
MAX_RETAINED_LOGS_PER_WORKTREE = 8
MAX_RETAINED_SESSIONS = 12
MAX_RETAINED_RECEIPTS = 200
_LOGGER = logging.getLogger(__name__)


def _snapshot_document_bodies(
    repo_root: Path | None,
    paths: set[str],
    revision: str | None,
    *,
    snapshot_modes: dict[str, str] | None = None,
) -> dict[str, str]:
    documents: dict[str, str] = {}
    if repo_root is None:
        return documents
    if snapshot_modes is None:
        modes: dict[str, str] = {}
        existing = existing_document_paths(
            repo_root, paths, revision, snapshot_modes=modes
        )
    else:
        modes = snapshot_modes
        existing = {path for path in paths if path in modes}
    for path in sorted(existing):
        body = read_document(repo_root, path, revision, snapshot_modes=modes)
        if body is not None:
            documents[path] = body
    return documents


def _scoped_config_payload(
    config: GuardConfig,
    scope: ReviewScope,
    touched: set[str],
    upstream: set[str],
    downstream: set[str],
) -> dict[str, Any]:
    """Keep the reviewed module contract while dropping unrelated module declarations."""
    payload = asdict(config)
    topology = payload.get("topology", {})
    modules = topology.pop("modules", [])
    declared_modules = {module["path"]: module for module in modules}
    if ".guard/config.json" in scope.changed_files:
        topology["modules"] = [
            {key: value for key, value in module.items() if key != "boundary_docs"}
            if module["path"] not in touched
            else module
            for module in modules
        ]
        return payload

    def neighbour_projection(module_path: str) -> dict[str, Any]:
        declared = declared_modules[module_path]
        return {
            "path": module_path,
            "layer": declared.get("layer"),
            "role": declared.get("role"),
            "dependencies": list(declared.get("dependencies", [])),
            "retired": declared.get("retired", False),
        }

    topology["modules"] = {
        "touched": [
            declared_modules.get(module, {"path": module}) for module in sorted(touched)
        ],
        "upstream": [neighbour_projection(module) for module in sorted(upstream)],
        "downstream": [neighbour_projection(module) for module in sorted(downstream)],
    }
    return payload


def _scoped_contract_payload(
    repo_root: Path, config: GuardConfig, scope: ReviewScope, revision: str | None
) -> tuple[dict[str, str], dict[str, Any]]:
    eff_rev = revision or scope.head_sha
    modes: dict[str, str] = {}
    touched, upstream, downstream = resolve_scope_modules(
        repo_root, scope, config, revision=eff_rev, snapshot_modes=modes
    )
    declared_modules = {m["path"]: m for m in config.topology.get("modules", [])}
    paths = architecture_document_paths(config)
    for module in touched:
        paths.update(
            boundary_document_paths(declared_modules.get(module, {"path": module}))
        )
    return (
        _snapshot_document_bodies(
            repo_root, paths, eff_rev, snapshot_modes=modes
        ),
        _scoped_config_payload(config, scope, touched, upstream, downstream),
    )


def compute_review_contract_digest(
    policy: Policy,
    config: GuardConfig,
    *,
    repo_root: Path | None = None,
    revision: str | None = None,
    scope: ReviewScope | None = None,
) -> str:
    """Hash the complete reviewer contract and its policy/config dependencies.

    With a review scope, the configuration and document contract is limited to the touched
    modules and their one-hop dependency neighbours so unrelated changes cannot invalidate it.
    """
    digest = hashlib.sha256()
    source_dir = Path(__file__).parent
    for path in sorted(source_dir.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    for path in (
        Path(__file__).parents[1] / "config.py",
        Path(__file__).parents[1] / "policy.py",
        Path(__file__).parents[1] / "git_helpers.py",
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    if repo_root is not None and scope is not None:
        documents, config_payload = _scoped_contract_payload(
            repo_root, config, scope, revision
        )
    else:
        documents = _snapshot_document_bodies(
            repo_root, contract_document_paths(config), revision
        )
        config_payload = asdict(config)
    payload = {
        "documents": documents,
        "config": config_payload,
        "policy": {
            "manifest_schema": policy.schema,
            "rules": [
                {
                    "id": rule.id,
                    "severity": rule.severity,
                    "scope": list(rule.scope),
                    "summary": rule.summary,
                    "details": rule.details,
                    "detail_body": rule.detail_body,
                }
                for rule in policy.rules
            ],
        },
    }
    digest.update(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return digest.hexdigest()


def contract_digest_matches(
    stored_digest: str,
    policy: Policy,
    config: GuardConfig,
    *,
    repo_root: Path | None = None,
    revision: str | None = None,
    scope: ReviewScope | None = None,
) -> bool:
    """Return whether persisted session evidence belongs to the current contract."""
    return bool(stored_digest) and stored_digest == compute_review_contract_digest(
        policy, config, repo_root=repo_root, revision=revision, scope=scope
    )


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    summary: str
    blocking_findings: tuple[BlockingFinding, ...]
    provider: str
    patch_digest: str
    head_sha: str
    merge_base: str
    mode: str = "full"
    session_id: str = ""
    log_path: str = ""


@dataclass(frozen=True)
class ReviewSessionState:
    worktree_path: str
    branch: str
    merge_base: str
    provider: str
    session_id: str
    last_head_sha: str
    last_patch_digest: str
    last_verdict: str
    last_summary: str
    last_blocking_findings: tuple[BlockingFinding, ...]
    last_submodule_shas: dict[str, str]
    contract_digest: str = ""
    round_count: int = 1
    updated_at: str = ""
    initial_head_sha: str = ""
    task_blueprint_anchors: dict[str, tuple[str, ...]] = field(default_factory=dict)


def worktree_session_key(worktree_path: Path, branch: str) -> str:
    resolved_root = str(worktree_path.resolve())
    eff_branch = branch or "HEAD"
    return hashlib.sha256(f"{resolved_root}:{eff_branch}".encode("utf-8")).hexdigest()[
        :16
    ]


def load_valid_receipt(common_dir: Path, patch_digest: str) -> dict[str, Any] | None:
    path = common_dir / "the-guard-receipts" / f"{patch_digest}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("schema") == RECEIPT_SCHEMA
        and payload.get("patch_digest") == patch_digest
        and payload.get("verdict") == "GO"
    ):
        return payload
    return None


def active_guard_runtime() -> dict[str, str]:
    """Collect best-effort provenance for the package running this review code."""
    module_path = Path(__file__)
    with suppress(OSError, RuntimeError):
        module_path = module_path.resolve()
    package_dir = module_path.parents[1]
    tree_digest = ""
    with suppress(OSError):
        tree_digest = compute_package_tree_digest(package_dir)

    sha = ""
    with suppress(OSError):
        sha = _installed_vcs_commit(package_dir) or ""
    if not sha:
        with suppress(OSError):
            checkout = package_dir.parent.parent
            git_dir = checkout / ".git"
            if git_dir.exists():
                sha = (git_stdout(checkout, [
                    "git", f"--git-dir={git_dir}", "-c", "core.fsmonitor=false",
                    "rev-parse", "--short", "HEAD",
                ]) or "").strip()
    if not sha and tree_digest:
        with suppress(OSError, RuntimeError, ValueError):
            status = load_auto_update_status() or {}
            for origin in ("installed", "source"):
                candidate = status.get(f"{origin}_sha")
                if status.get(f"{origin}_tree_digest") == tree_digest and isinstance(candidate, str):
                    sha = candidate.strip()
                    if sha:
                        break
    return {
        "guard_package_dir": str(package_dir),
        "guard_tree_digest": tree_digest,
        "guard_sha": sha,
    }


def receipt_payload(
    result: ReviewResult, *, contract_digest: str,
    task_blueprint_anchors: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "verdict": result.verdict,
        "summary": result.summary,
        "provider": result.provider,
        "patch_digest": result.patch_digest,
        "head_sha": result.head_sha,
        "merge_base": result.merge_base,
        "mode": result.mode,
        "session_id": result.session_id,
        "log_path": result.log_path,
        "contract_digest": contract_digest,
        "task_blueprint_anchors": dict(task_blueprint_anchors),
        "reviewed_at": datetime.now(UTC).isoformat(),
        **active_guard_runtime(),
    }


def save_receipt(
    common_dir: Path, result: ReviewResult, *, contract_digest: str,
    task_blueprint_anchors: Mapping[str, tuple[str, ...]],
) -> Path:
    path = common_dir / "the-guard-receipts" / f"{result.patch_digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = receipt_payload(
        result, contract_digest=contract_digest, task_blueprint_anchors=task_blueprint_anchors,
    )
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _prune_directory_files(path.parent, "*.json", MAX_RETAINED_RECEIPTS)
    return path


def load_review_session(
    common_dir: Path, worktree_path: Path, branch: str
) -> ReviewSessionState | None:
    key = worktree_session_key(worktree_path, branch)
    return _read_review_session(common_dir / "the-guard-sessions" / f"{key}.json")


def list_review_sessions(common_dir: Path) -> list[ReviewSessionState]:
    """Return retained session records, most recently updated first."""
    sessions = (
        _read_review_session(path)
        for path in (common_dir / "the-guard-sessions").glob("*.json")
    )
    return sorted(
        (state for state in sessions if state is not None),
        key=lambda state: (state.updated_at, state.worktree_path, state.branch),
        reverse=True,
    )


def _read_review_session(path: Path) -> ReviewSessionState | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SESSION_SCHEMA:
        return None
    worktree_path, branch = raw.get("worktree_path"), raw.get("branch")
    if (
        not isinstance(worktree_path, str) or not worktree_path
        or not isinstance(branch, str) or not branch
        or raw.get("last_verdict") not in ("GO", "NO-GO")
        or not all(isinstance(raw.get(key), str) and raw[key]
                   for key in ("merge_base", "last_head_sha", "last_patch_digest"))
    ):
        return None
    raw_findings = raw.get("last_blocking_findings") or []
    if not isinstance(raw_findings, list):
        return None
    try:
        if path.stem != worktree_session_key(Path(worktree_path), branch):
            return None
        round_count = int(raw.get("round_count") or 1)
        updated_at = ""
        if raw.get("updated_at"):
            timestamp = datetime.fromisoformat(raw["updated_at"])
            updated_at = (timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
        findings = tuple(
            BlockingFinding(
                rule_id=str(item.get("rule_id") or "guard.architecture-simplicity"),
                file=str(item.get("file") or "<unknown>"),
                line=int(item.get("line") or 1),
                issue=str(item.get("issue") or ""),
                required_fix=str(item.get("required_fix") or ""),
                finding_origin=str(item.get("finding_origin") or "initial_full_scope"),
            )
            for item in raw_findings
            if isinstance(item, dict)
        )
    except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
        return None

    def string_map(key: str) -> dict[str, str]:
        value = raw.get(key)
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}

    return ReviewSessionState(
        worktree_path=worktree_path,
        branch=branch,
        merge_base=str(raw.get("merge_base") or ""),
        provider=str(raw.get("provider") or ""),
        session_id=str(raw.get("session_id") or ""),
        last_head_sha=str(raw.get("last_head_sha") or ""),
        last_patch_digest=str(raw.get("last_patch_digest") or ""),
        last_verdict=str(raw.get("last_verdict") or ""),
        last_summary=str(raw.get("last_summary") or ""),
        last_blocking_findings=findings,
        last_submodule_shas=string_map("last_submodule_shas"),
        contract_digest=str(raw.get("contract_digest") or ""),
        round_count=round_count,
        updated_at=updated_at,
        initial_head_sha=str(raw.get("initial_head_sha") or raw.get("last_head_sha") or ""),
        task_blueprint_anchors=normalize_task_blueprint_anchors(raw.get("task_blueprint_anchors")),
    )


def _prune_directory_files(
    directory: Path, pattern: str, max_keep: int, *, keep: Path | None = None
) -> None:
    if keep is not None:
        max_keep -= 1
    try:
        files = sorted(
            (path for path in directory.glob(pattern) if path != keep),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old_file in files[max_keep:]:
            old_file.unlink(missing_ok=True)
    except OSError:
        pass


def _prune_orphaned_review_sessions(sessions_dir: Path, keep: Path) -> None:
    for session_path in sessions_dir.glob("*.json"):
        if session_path == keep:
            continue
        try:
            raw = json.loads(session_path.read_text(encoding="utf-8"))
            worktree_path = raw.get("worktree_path") if isinstance(raw, dict) else None
            if not isinstance(worktree_path, str):
                continue
            worktree = Path(worktree_path)
            if worktree.is_absolute() and worktree.parent.is_dir() and not worktree.exists():
                session_path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _LOGGER.warning("Could not inspect or prune review session %s: %s", session_path, exc)


def save_review_session(common_dir: Path, state: ReviewSessionState) -> Path:
    sessions_dir = common_dir / "the-guard-sessions"
    key = worktree_session_key(Path(state.worktree_path), state.branch)
    path = sessions_dir / f"{key}.json"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SESSION_SCHEMA,
        "worktree_path": state.worktree_path,
        "branch": state.branch,
        "merge_base": state.merge_base,
        "provider": state.provider,
        "session_id": state.session_id,
        "last_head_sha": state.last_head_sha,
        "initial_head_sha": state.initial_head_sha,
        "task_blueprint_anchors": state.task_blueprint_anchors,
        "last_patch_digest": state.last_patch_digest,
        "last_verdict": state.last_verdict,
        "last_summary": state.last_summary,
        "last_blocking_findings": [asdict(f) for f in state.last_blocking_findings],
        "last_submodule_shas": dict(state.last_submodule_shas),
        "contract_digest": state.contract_digest,
        "round_count": state.round_count,
        "updated_at": state.updated_at or datetime.now(UTC).isoformat(),
    }
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _prune_orphaned_review_sessions(sessions_dir, keep=path)
        _prune_directory_files(sessions_dir, "*.json", MAX_RETAINED_SESSIONS, keep=path)
    except OSError:
        pass
    return path


def review_logs_dir(common_dir: Path, env: dict[str, str] | None = None) -> Path:
    eff_env = dict(os.environ if env is None else env)
    override = eff_env.get("THE_GUARD_REVIEW_LOG_DIR", "").strip()
    if override:
        return Path(override)
    return common_dir / "the-guard-logs"


class ReviewRunLogger:
    """Streams real-time reviewer CLI events and metadata to a bounded fixed log directory."""

    def __init__(
        self,
        common_dir: Path,
        *,
        worktree_path: Path,
        branch: str,
        provider: str,
        mode: str,
        merge_base: str,
        head_sha: str,
        patch_digest: str,
        reviewable_file_count: int,
        prompt: str,
        resume_session_id: str | None = None,
        prev_head_sha: str | None = None,
        reviewer_note: str = "",
        full_review_reason: str = "",
        env: dict[str, str] | None = None,
    ) -> None:
        self.worktree = str(worktree_path.resolve())
        self.branch = branch
        self.worktree_key = worktree_session_key(worktree_path, branch)
        self.provider = provider
        self.mode = mode
        self.merge_base = merge_base
        self.head_sha = head_sha
        self.patch_digest = patch_digest
        self.reviewable_file_count = reviewable_file_count
        self.resume_session_id = resume_session_id or ""
        self.prev_head_sha = prev_head_sha or ""
        self.reviewer_note = reviewer_note.strip()
        self.full_review_reason = full_review_reason
        self.started_at = datetime.now(UTC).isoformat()
        self._start_mono = time.monotonic()

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = (
            f"{ts}-{os.getpid()}-{head_sha[:10]}-{patch_digest[:8]}-{provider}-{mode}"
        )
        self.log_dir = review_logs_dir(common_dir, env)
        self.prompt_path = self.log_dir / f"{self.run_id}.prompt.md"
        self.events_path = self.log_dir / f"{self.run_id}.events.jsonl"
        self.meta_path = self.log_dir / f"{self.run_id}.meta.json"
        self.event_count = 0
        self._stdout_line_buf = ""
        self._events_fp: IO[str] | None = None

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.prompt_path.write_text(prompt, encoding="utf-8")
            self._events_fp = self.events_path.open("a", encoding="utf-8")
            self._write_meta(status="running")
        except OSError:
            self._events_fp = None

    @staticmethod
    def _is_high_frequency_delta_line(line: str) -> bool:
        s = line.strip()
        if not s.startswith("{"):
            return False
        return (
            '"subtype":"thinking_tokens"' in s
            or '"subtype": "thinking_tokens"' in s
            or '"type":"stream_event"' in s
            or '"type": "stream_event"' in s
        )

    def append_stream_chunk(self, stream_name: str, text: str) -> None:
        if self._events_fp is None or not text:
            return
        try:
            if stream_name == "stdout":
                self._stdout_line_buf += text
                while "\n" in self._stdout_line_buf:
                    line, self._stdout_line_buf = self._stdout_line_buf.split("\n", 1)
                    if not self._is_high_frequency_delta_line(line):
                        self._events_fp.write(line + "\n")
            else:
                for line in text.splitlines(keepends=True):
                    self._events_fp.write(
                        json.dumps(
                            {"stream": "stderr", "text": line.rstrip("\r\n")},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            self._events_fp.flush()
        except OSError:
            pass

    def finish(
        self,
        *,
        status: str,
        session_id: str = "",
        event_count: int = 0,
        verdict: str = "",
        summary: str = "",
        blocking_findings: tuple[BlockingFinding, ...] = (),
        error: str = "",
    ) -> Path:
        if self._events_fp is not None:
            try:
                if (
                    self._stdout_line_buf.strip()
                    and not self._is_high_frequency_delta_line(self._stdout_line_buf)
                ):
                    self._events_fp.write(self._stdout_line_buf.rstrip("\r\n") + "\n")
                self._stdout_line_buf = ""
                self._events_fp.flush()
                self._events_fp.close()
            except OSError:
                pass
            self._events_fp = None
        meta = self._write_meta(
            status=status,
            session_id=session_id,
            event_count=event_count or self.event_count,
            verdict=verdict,
            summary=summary,
            blocking_findings=blocking_findings,
            error=error,
        )
        try:
            index_path = self.log_dir / "index.jsonl"
            with index_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(meta, ensure_ascii=False) + "\n")
            self._prune_old_runs()
        except OSError:
            pass
        return self.events_path

    def _prune_old_runs(self) -> None:
        meta_files = sorted(
            (
                p
                for p in self.log_dir.glob("*.meta.json")
                if not p.name.startswith("latest")
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        worktree_counts: dict[str, int] = {}
        protected: set[Path] = set()
        for meta_file in meta_files:
            prefix = meta_file.name.removesuffix(".meta.json")
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                worktree = (
                    str(meta.get("worktree") or prefix)
                    if isinstance(meta, dict)
                    else prefix
                )
            except (OSError, ValueError):
                worktree = prefix
            count = worktree_counts.get(worktree, 0)
            worktree_path = Path(worktree)
            worktree_active = not worktree_path.is_absolute() or worktree_path.is_dir()
            if worktree_active and count < MAX_RETAINED_LOGS_PER_WORKTREE:
                protected.add(meta_file)
            worktree_counts[worktree] = count + 1
        # The per-worktree floor wins if protected logs alone exceed the global target.
        spare_slots = max(0, MAX_RETAINED_RUN_LOGS - len(protected))
        unprotected = [p for p in meta_files if p not in protected]
        for old_meta in unprotected[spare_slots:]:
            prefix = old_meta.name[: -len(".meta.json")]
            for suffix in (".meta.json", ".prompt.md", ".events.jsonl"):
                (self.log_dir / f"{prefix}{suffix}").unlink(missing_ok=True)

        for latest in self.log_dir.glob("latest-*.json"):
            try:
                meta = json.loads(latest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                worktree_path = Path(str(meta.get("worktree") or ""))
                if worktree_path.is_absolute() and not worktree_path.is_dir():
                    latest.unlink(missing_ok=True)

    def _write_meta(
        self,
        *,
        status: str,
        session_id: str = "",
        event_count: int = 0,
        verdict: str = "",
        summary: str = "",
        blocking_findings: tuple[BlockingFinding, ...] = (),
        error: str = "",
    ) -> dict[str, Any]:
        duration = round(time.monotonic() - self._start_mono, 2)
        payload: dict[str, Any] = {
            "schema": LOG_META_SCHEMA,
            "run_id": self.run_id,
            "status": status,
            "started_at": self.started_at,
            "updated_at": datetime.now(UTC).isoformat(),
            "duration_seconds": duration,
            "worktree": self.worktree,
            "branch": self.branch,
            "provider": self.provider,
            "mode": self.mode,
            "full_review_reason": self.full_review_reason,
            "resume_session_id": self.resume_session_id,
            "session_id": session_id,
            "merge_base": self.merge_base,
            "prev_head_sha": self.prev_head_sha,
            "head_sha": self.head_sha,
            "patch_digest": self.patch_digest,
            "reviewable_file_count": self.reviewable_file_count,
            "reviewer_note": self.reviewer_note,
            "event_count": event_count,
            "verdict": verdict,
            "summary": summary,
            "blocking_findings": [asdict(f) for f in blocking_findings],
            "error": error,
            "prompt_file": str(self.prompt_path),
            "events_file": str(self.events_path),
            "meta_file": str(self.meta_path),
        }
        try:
            serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            self.meta_path.write_text(serialized, encoding="utf-8")
            (self.log_dir / f"latest-{self.worktree_key}.json").write_text(
                serialized, encoding="utf-8"
            )
            (self.log_dir / "latest.json").write_text(serialized, encoding="utf-8")
        except OSError:
            pass
        return payload
