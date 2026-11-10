"""Persistence for architecture review receipts, incremental review sessions, and bounded run logs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

RECEIPT_SCHEMA = "the-guard-receipt/v1"
SESSION_SCHEMA = "the-guard-session/v1"
LOG_META_SCHEMA = "the-guard-log-meta/v1"
UTC = timezone.utc
MAX_RETAINED_RUN_LOGS = 90
MAX_RETAINED_LOGS_PER_WORKTREE = 8
MAX_RETAINED_SESSIONS = 30
MAX_RETAINED_RECEIPTS = 200


@dataclass(frozen=True)
class BlockingFinding:
    rule_id: str
    file: str
    line: int
    issue: str
    required_fix: str
    finding_origin: str = "initial_full_scope"


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
    round_count: int = 1
    updated_at: str = ""


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


def save_receipt(common_dir: Path, result: ReviewResult) -> Path:
    path = common_dir / "the-guard-receipts" / f"{result.patch_digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
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
        "reviewed_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _prune_directory_files(path.parent, "*.json", MAX_RETAINED_RECEIPTS)
    return path


def load_review_session(
    common_dir: Path, worktree_path: Path, branch: str
) -> ReviewSessionState | None:
    key = worktree_session_key(worktree_path, branch)
    path = common_dir / "the-guard-sessions" / f"{key}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SESSION_SCHEMA:
        return None
    findings = tuple(
        BlockingFinding(
            rule_id=str(item.get("rule_id") or "guard.architecture-simplicity"),
            file=str(item.get("file") or "<unknown>"),
            line=int(item.get("line") or 1),
            issue=str(item.get("issue") or ""),
            required_fix=str(item.get("required_fix") or ""),
            finding_origin=str(item.get("finding_origin") or "initial_full_scope"),
        )
        for item in (raw.get("last_blocking_findings") or [])
        if isinstance(item, dict)
    )
    sub_shas_raw = raw.get("last_submodule_shas")
    sub_shas = (
        {
            str(k): str(v)
            for k, v in sub_shas_raw.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        if isinstance(sub_shas_raw, dict)
        else {}
    )
    return ReviewSessionState(
        worktree_path=str(raw.get("worktree_path") or ""),
        branch=str(raw.get("branch") or ""),
        merge_base=str(raw.get("merge_base") or ""),
        provider=str(raw.get("provider") or ""),
        session_id=str(raw.get("session_id") or ""),
        last_head_sha=str(raw.get("last_head_sha") or ""),
        last_patch_digest=str(raw.get("last_patch_digest") or ""),
        last_verdict=str(raw.get("last_verdict") or ""),
        last_summary=str(raw.get("last_summary") or ""),
        last_blocking_findings=findings,
        last_submodule_shas=sub_shas,
        round_count=int(raw.get("round_count") or 1),
        updated_at=str(raw.get("updated_at") or ""),
    )


def _prune_directory_files(directory: Path, pattern: str, max_keep: int) -> None:
    try:
        files = sorted(
            directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for old_file in files[max_keep:]:
            old_file.unlink(missing_ok=True)
    except OSError:
        pass


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
        "last_patch_digest": state.last_patch_digest,
        "last_verdict": state.last_verdict,
        "last_summary": state.last_summary,
        "last_blocking_findings": [asdict(f) for f in state.last_blocking_findings],
        "last_submodule_shas": dict(state.last_submodule_shas),
        "round_count": state.round_count,
        "updated_at": state.updated_at or datetime.now(UTC).isoformat(),
    }
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _prune_directory_files(sessions_dir, "*.json", MAX_RETAINED_SESSIONS)
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
