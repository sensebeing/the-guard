"""Git facts an architecture review is anchored on: reviewed scope, deltas, and receipts."""

from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import GuardConfig, load_config
from ..policy import load_policy
from .contract import (
    CODE_EXTENSIONS,
    CONFIG_I18N_EXTENSIONS,
    LOCK_FILENAMES,
    POLICY_REVIEWABLE_EXACT_PATHS,
    POLICY_REVIEWABLE_PREFIXES,
    PROMPT_SKILL_PATH_MARKERS,
    SERVICE_CONFIG_EXTENSIONS,
    TEST_DIR_PARTS,
    TEST_FILE_SUFFIXES,
    IncrementalReviewScope,
    ReviewScope,
    SubmoduleChange,
)
from .session import (
    RECEIPT_SCHEMA,
    UTC,
    ReviewResult,
    ReviewSessionState,
)
from .session import compute_review_contract_digest, contract_digest_matches
from .session import (
    load_review_session as _load_session_record,
)
from .session import (
    load_valid_receipt as _load_receipt_record,
)
from .session import (
    review_logs_dir as _resolve_logs_dir,
)
from .session import (
    save_receipt as _save_receipt_record,
)
from .session import (
    save_review_session as _save_session_record,
)


def _git(
    repo_root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=check, capture_output=True
    )


def _git_text(repo_root: Path, *args: str, check: bool = True) -> str:
    return (
        _git(repo_root, *args, check=check)
        .stdout.decode("utf-8", errors="replace")
        .strip()
    )


def _literal_pathspecs(paths: tuple[str, ...]) -> tuple[str, ...]:
    return ("--", *(f":(literal){p}" for p in paths)) if paths else ()


def git_common_dir(repo_root: Path) -> Path:
    return Path(
        _git_text(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )


def current_branch_name(repo_root: Path) -> str:
    branch = _git_text(repo_root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    return branch if branch else "HEAD"


def load_valid_receipt(repo_root: Path, patch_digest: str) -> dict[str, Any] | None:
    return _load_receipt_record(git_common_dir(repo_root), patch_digest)


def save_receipt(repo_root: Path, result: ReviewResult) -> Path:
    return _save_receipt_record(git_common_dir(repo_root), result)


def load_review_session(
    repo_root: Path, branch: str | None = None
) -> ReviewSessionState | None:
    return _load_session_record(
        git_common_dir(repo_root), repo_root, branch or current_branch_name(repo_root)
    )


def save_review_session(repo_root: Path, state: ReviewSessionState) -> Path:
    return _save_session_record(git_common_dir(repo_root), state)


def review_logs_dir(repo_root: Path, env: dict[str, str] | None = None) -> Path:
    return _resolve_logs_dir(git_common_dir(repo_root), env)


def resolve_base_ref(repo_root: Path, main_branch: str = "main") -> str:
    for ref in (
        f"refs/remotes/origin/{main_branch}",
        f"refs/heads/{main_branch}",
        main_branch,
    ):
        if (
            _git(
                repo_root, "rev-parse", "--verify", "--quiet", ref, check=False
            ).returncode
            == 0
        ):
            return ref
    raise RuntimeError(
        f"Cannot resolve base branch '{main_branch}' or 'origin/{main_branch}'"
    )


def is_reviewable_path(rel_path: str, config: GuardConfig | None = None) -> bool:
    cfg = config or GuardConfig()
    settings = cfg.review
    if rel_path == ".guard/config.json" or rel_path == cfg.manifest_file:
        return True
    if any(
        fnmatch.fnmatchcase(rel_path, p)
        for p in settings.get("extra_reviewable_paths", [])
    ):
        return True
    policy_prefixes = (
        *POLICY_REVIEWABLE_PREFIXES,
        cfg.detail_dir.rstrip("/") + "/",
        *cfg.review.get("reviewable_prefixes", []),
    )
    posix = PurePosixPath(rel_path)
    if posix.name in LOCK_FILENAMES:
        return False
    as_posix = posix.as_posix()
    if as_posix in POLICY_REVIEWABLE_EXACT_PATHS or as_posix.startswith(
        policy_prefixes
    ):
        return True
    normalized = f"/{as_posix}"
    if any(marker in normalized for marker in PROMPT_SKILL_PATH_MARKERS):
        return True
    suffix = posix.suffix.lower()
    if suffix in settings.get("reviewable_extensions", CODE_EXTENSIONS):
        return True
    return bool(
        posix.parts
        and posix.parts[0]
        in settings.get("service_config_roots", ["services", "packages", "apps", "src"])
        and suffix in SERVICE_CONFIG_EXTENSIONS
    )


def _classify_file_tier(rel_path: str, config: GuardConfig | None = None) -> int:
    """Return 0 for core production, 1 for config/i18n/parity, 2 for tests/fixtures/lockfiles."""
    cfg = config or GuardConfig()
    policy_prefixes = (
        *POLICY_REVIEWABLE_PREFIXES,
        cfg.detail_dir.rstrip("/") + "/",
        *cfg.review.get("reviewable_prefixes", []),
    )
    posix = PurePosixPath(rel_path)
    name_low = posix.name.lower()
    if posix.name in LOCK_FILENAMES or name_low == "conftest.py":
        return 2
    if any(part.lower() in TEST_DIR_PARTS for part in posix.parts):
        return 2
    if name_low.startswith("test_") or name_low.endswith(TEST_FILE_SUFFIXES):
        return 2
    as_posix = posix.as_posix()
    if not as_posix.startswith(policy_prefixes) and (
        posix.suffix.lower() in CONFIG_I18N_EXTENSIONS or name_low.endswith("-map.md")
    ):
        return 1
    return 0


def _is_test_or_fixture_path(rel_path: str) -> bool:
    return _classify_file_tier(rel_path) == 2


def _compute_stable_patch_id(repo_root: Path, diff_bytes: bytes) -> str:
    if not diff_bytes.strip():
        return "0" * 40
    proc = subprocess.run(
        ["git", "patch-id", "--stable"],
        cwd=repo_root,
        input=diff_bytes,
        check=True,
        capture_output=True,
    )
    out = proc.stdout.decode("ascii", errors="ignore").strip()
    return out.split()[0] if out else "0" * 40


def _parse_raw_diff_lines(
    raw_diff: str, config: GuardConfig | None = None
) -> tuple[list[str], list[str], list[SubmoduleChange]]:
    changed_files: list[str] = []
    reviewable_files: list[str] = []
    submodules: list[SubmoduleChange] = []
    for line in raw_diff.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, path = line[1:].partition("\t")
        parts = meta.split()
        if len(parts) < 5 or not path:
            continue
        old_mode, new_mode, old_sha, new_sha = parts[0], parts[1], parts[2], parts[3]
        if old_mode == "160000" and new_mode == "000000":
            # Removing a gitlink is a root-repository change, not a revision
            # range inside a repository that no longer exists in the target.
            changed_files.append(path)
            reviewable_files.append(path)
            continue
        if old_mode == "160000" or new_mode == "160000":
            submodules.append(
                SubmoduleChange(path=path, old_sha=old_sha, new_sha=new_sha)
            )
            continue
        changed_files.append(path)
        if is_reviewable_path(path, config):
            reviewable_files.append(path)
    return changed_files, reviewable_files, submodules


def _parse_raw_file_blobs(raw_diff: str) -> dict[str, tuple[str, str]]:
    blobs: dict[str, tuple[str, str]] = {}
    for line in raw_diff.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, path = line[1:].partition("\t")
        parts = meta.split()
        if len(parts) < 5 or not path:
            continue
        old_mode, new_mode, _, new_sha = parts[0], parts[1], parts[2], parts[3]
        if old_mode == "160000" or new_mode == "160000":
            continue
        blobs[path] = (new_mode, new_sha)
    return blobs


def _is_ancestor(repo_root: Path, older_sha: str, newer_sha: str) -> bool:
    if not older_sha or not newer_sha:
        return False
    return (
        _git(
            repo_root, "merge-base", "--is-ancestor", older_sha, newer_sha, check=False
        ).returncode
        == 0
    )


def _is_same_per_file_branch_patch(
    repo_root: Path, *, old_range: str, new_range: str, path: str
) -> bool:
    """Return True only when both per-file diffs succeed, are non-empty, and share the same stable patch-id."""
    path_args = _literal_pathspecs((path,))
    old_proc = _git(
        repo_root,
        "diff",
        "--binary",
        "--full-index",
        old_range,
        *path_args,
        check=False,
    )
    new_proc = _git(
        repo_root,
        "diff",
        "--binary",
        "--full-index",
        new_range,
        *path_args,
        check=False,
    )
    if old_proc.returncode != 0 or new_proc.returncode != 0:
        return False
    if not old_proc.stdout.strip() or not new_proc.stdout.strip():
        return False
    old_patch_id = _compute_stable_patch_id(repo_root, old_proc.stdout)
    new_patch_id = _compute_stable_patch_id(repo_root, new_proc.stdout)
    if old_patch_id == "0" * 40 or new_patch_id == "0" * 40:
        return False
    return old_patch_id == new_patch_id


def _tree_entry_matches(
    repo_root: Path, commit_sha: str, path: str, expected_mode: str, expected_sha: str
) -> bool:
    """Return True only when `path` in `commit_sha` has the exact `expected_mode` and `expected_sha`."""
    if not expected_sha or expected_sha == "0" * 40:
        return False
    proc = _git(
        repo_root,
        "ls-tree",
        "--full-tree",
        commit_sha,
        *_literal_pathspecs((path,)),
        check=False,
    )
    if proc.returncode != 0:
        return False
    line = proc.stdout.decode("utf-8", errors="replace").strip()
    if not line:
        return False
    meta, _, _ = line.partition("\t")
    parts = meta.split()
    if len(parts) < 3:
        return False
    return parts[0] == expected_mode and parts[2] == expected_sha


def _require_submodule_checkout(repo_root: Path, sub: SubmoduleChange) -> Path:
    sub_repo = repo_root / sub.path
    if not (sub_repo.is_dir() and (sub_repo / ".git").exists()):
        raise RuntimeError(
            f"Submodule checkout '{sub.path}' is not initialized; cannot audit submodule diff {sub.old_sha[:10]}..{sub.new_sha[:10]}"
        )
    return sub_repo


def _is_unchanged_rebuttal_round(
    scope: ReviewScope, session: ReviewSessionState | None, reviewer_note: str
) -> bool:
    return bool(
        session is not None
        and reviewer_note.strip()
        and session.merge_base == scope.merge_base
        and session.last_head_sha == scope.head_sha
        and session.last_patch_digest == scope.patch_digest
    )


def collect_review_scope(repo_root: Path, main_branch: str = "main") -> ReviewScope:
    base_ref = resolve_base_ref(repo_root, main_branch)
    config = load_config(repo_root, revision="HEAD")
    head_sha = _git_text(repo_root, "rev-parse", "HEAD")
    merge_base = _git_text(repo_root, "merge-base", base_ref, head_sha)

    raw_diff = _git_text(
        repo_root, "diff", "--raw", "--no-renames", f"{merge_base}..{head_sha}"
    )
    changed_files, reviewable_files, submodules = _parse_raw_diff_lines(
        raw_diff, config
    )

    diff_bytes = _git(
        repo_root,
        "diff",
        "--binary",
        "--full-index",
        "--find-renames",
        f"{merge_base}..{head_sha}",
    ).stdout
    root_patch_id = _compute_stable_patch_id(repo_root, diff_bytes)

    sub_digests: list[str] = []
    for sub in sorted(submodules, key=lambda item: item.path):
        sub_repo = _require_submodule_checkout(repo_root, sub)
        sub_diff = _git(
            sub_repo,
            "diff",
            "--binary",
            "--full-index",
            "--find-renames",
            f"{sub.old_sha}..{sub.new_sha}",
            check=False,
        )
        if sub_diff.returncode != 0:
            err = sub_diff.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Failed to read submodule '{sub.path}' diff ({sub.old_sha[:10]}..{sub.new_sha[:10]}): {err or sub_diff.returncode}"
            )
        sub_patch_id = (
            _compute_stable_patch_id(sub_repo, sub_diff.stdout)
            if sub_diff.stdout.strip()
            else f"{sub.old_sha}->{sub.new_sha}"
        )
        sub_digests.append(f"{sub.path}:{sub_patch_id}")

    hasher = hashlib.sha256(f"root:{root_patch_id}\n".encode("utf-8"))
    for entry in sub_digests:
        hasher.update(f"sub:{entry}\n".encode("utf-8"))

    return ReviewScope(
        base_ref=base_ref,
        merge_base=merge_base,
        head_sha=head_sha,
        changed_files=tuple(sorted(changed_files)),
        reviewable_files=tuple(sorted(reviewable_files)),
        submodule_changes=tuple(sorted(submodules, key=lambda item: item.path)),
        patch_digest=hasher.hexdigest(),
    )


def _collect_branch_delta(
    repo_root: Path, scope: ReviewScope, session: ReviewSessionState | None
) -> IncrementalReviewScope | None:
    """Compute verified branch delta since `session`, or return None when session continuation is inapplicable.

    Raises RuntimeError if git diff commands fail after commit existence is verified (fail-closed).
    Returns an IncrementalReviewScope (which may have `has_changes == False` when zero files/submodules changed).
    """
    config = load_config(repo_root, revision=scope.head_sha)
    if session is None or not session.last_head_sha:
        return None
    if (
        session.last_head_sha == scope.head_sha
        or session.last_patch_digest == scope.patch_digest
    ):
        return None
    if (
        _git(
            repo_root,
            "cat-file",
            "-e",
            f"{session.last_head_sha}^{{commit}}",
            check=False,
        ).returncode
        != 0
    ):
        return None

    changed_files: list[str] = []
    reviewable_files: list[str] = []
    inc_subs: dict[str, SubmoduleChange] = {}

    if session.merge_base == scope.merge_base:
        raw_proc = _git(
            repo_root,
            "diff",
            "--raw",
            "--no-renames",
            f"{session.last_head_sha}..{scope.head_sha}",
            check=False,
        )
        if raw_proc.returncode != 0:
            err = raw_proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"git diff --raw failed ({session.last_head_sha[:10]}..{scope.head_sha[:10]}): "
                f"{err or raw_proc.returncode}"
            )
        raw_diff = raw_proc.stdout.decode("utf-8", errors="replace").strip()
        changed_files, reviewable_files, raw_subs = _parse_raw_diff_lines(
            raw_diff, config
        )
        inc_subs = {sub.path: sub for sub in raw_subs}
    elif _is_ancestor(repo_root, session.merge_base, scope.merge_base):
        old_raw_proc = _git(
            repo_root,
            "diff",
            "--raw",
            "--no-renames",
            f"{session.merge_base}..{session.last_head_sha}",
            check=False,
        )
        new_raw_proc = _git(
            repo_root,
            "diff",
            "--raw",
            "--no-renames",
            f"{scope.merge_base}..{scope.head_sha}",
            check=False,
        )
        if old_raw_proc.returncode != 0 or new_raw_proc.returncode != 0:
            err = (
                (old_raw_proc.stderr or new_raw_proc.stderr)
                .decode("utf-8", errors="replace")
                .strip()
            )
            raise RuntimeError(
                f"git diff --raw failed across merge_base ({session.merge_base[:10]}..{scope.merge_base[:10]}): "
                f"{err or old_raw_proc.returncode or new_raw_proc.returncode}"
            )
        old_blobs = _parse_raw_file_blobs(
            old_raw_proc.stdout.decode("utf-8", errors="replace")
        )
        new_blobs = _parse_raw_file_blobs(
            new_raw_proc.stdout.decode("utf-8", errors="replace")
        )
        for path, (new_mode, new_sha) in new_blobs.items():
            if old_blobs.get(path) == (new_mode, new_sha):
                continue
            if path in old_blobs and _is_same_per_file_branch_patch(
                repo_root,
                old_range=f"{session.merge_base}..{session.last_head_sha}",
                new_range=f"{scope.merge_base}..{scope.head_sha}",
                path=path,
            ):
                continue
            changed_files.append(path)
            if is_reviewable_path(path, config):
                reviewable_files.append(path)
        for path, (old_mode, old_sha) in old_blobs.items():
            if path in new_blobs:
                continue
            if not _tree_entry_matches(
                repo_root, scope.head_sha, path, old_mode, old_sha
            ):
                changed_files.append(path)
                if is_reviewable_path(path, config):
                    reviewable_files.append(path)
    else:
        return None

    for sub in scope.submodule_changes:
        prev_sha = session.last_submodule_shas.get(sub.path, sub.old_sha)
        if prev_sha != sub.new_sha:
            inc_subs[sub.path] = SubmoduleChange(
                path=sub.path, old_sha=prev_sha, new_sha=sub.new_sha
            )

    return IncrementalReviewScope(
        base_head_sha=session.last_head_sha,
        target_head_sha=scope.head_sha,
        changed_files=tuple(sorted(changed_files)),
        reviewable_files=tuple(sorted(reviewable_files)),
        submodule_changes=tuple(sorted(inc_subs.values(), key=lambda item: item.path)),
    )


def collect_incremental_scope(
    repo_root: Path, scope: ReviewScope, session: ReviewSessionState | None
) -> IncrementalReviewScope | None:
    try:
        delta = _collect_branch_delta(repo_root, scope, session)
    except RuntimeError:
        return None
    return delta if (delta is not None and delta.has_changes) else None


def resolve_or_carry_forward_receipt(
    repo_root: Path, scope: ReviewScope, *, persist: bool = True
) -> dict[str, Any] | None:
    existing = load_valid_receipt(repo_root, scope.patch_digest)
    if existing is not None:
        return existing
    session = load_review_session(repo_root)
    if (
        session is None
        or session.last_verdict != "GO"
        or not session.last_patch_digest
        or not session.last_head_sha
    ):
        return None
    current_policy = load_policy(repo_root, revision=scope.head_sha)
    current_config = load_config(repo_root, revision=scope.head_sha)
    current_contract_digest = compute_review_contract_digest(current_policy, current_config)
    if not contract_digest_matches(session.contract_digest, current_policy, current_config):
        return None
    prior_receipt = load_valid_receipt(repo_root, session.last_patch_digest)
    if prior_receipt is None:
        return None
    if session.merge_base == scope.merge_base or not _is_ancestor(
        repo_root, session.merge_base, scope.merge_base
    ):
        return None
    try:
        delta = _collect_branch_delta(repo_root, scope, session)
    except RuntimeError:
        return None
    if delta is None or delta.reviewable_files or delta.submodule_changes:
        return None

    carried = ReviewResult(
        verdict="GO",
        summary=str(prior_receipt.get("summary") or session.last_summary),
        blocking_findings=(),
        provider=str(prior_receipt.get("provider") or session.provider),
        patch_digest=scope.patch_digest,
        head_sha=scope.head_sha,
        merge_base=scope.merge_base,
        mode="carried-forward",
        session_id=str(prior_receipt.get("session_id") or session.session_id),
        log_path=str(prior_receipt.get("log_path") or ""),
    )
    if not persist:
        return {
            "schema": RECEIPT_SCHEMA,
            "verdict": "GO",
            "provider": carried.provider,
            "mode": carried.mode,
            "session_id": carried.session_id,
            "log_path": carried.log_path,
            "patch_digest": carried.patch_digest,
            "head_sha": carried.head_sha,
            "merge_base": carried.merge_base,
            "summary": carried.summary,
        }
    save_receipt(repo_root, carried)
    save_review_session(
        repo_root,
        ReviewSessionState(
            worktree_path=session.worktree_path,
            branch=session.branch,
            merge_base=scope.merge_base,
            provider=carried.provider,
            session_id=carried.session_id,
            last_head_sha=scope.head_sha,
            last_patch_digest=scope.patch_digest,
            last_verdict="GO",
            last_summary=carried.summary,
            last_blocking_findings=(),
            last_submodule_shas={
                sub.path: sub.new_sha for sub in scope.submodule_changes
            },
            contract_digest=current_contract_digest,
            round_count=session.round_count,
            updated_at=datetime.now(UTC).isoformat(),
        ),
    )
    return load_valid_receipt(repo_root, scope.patch_digest)
