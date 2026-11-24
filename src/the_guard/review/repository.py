"""Git facts an architecture review is anchored on: reviewed scope, deltas, and receipts."""

from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import GuardConfig, load_config
from ..git_helpers import clean_git_env, diff_has_content, git_ok, stream_patch_id, tree_entries_map
from ..policy import Policy, load_policy
from .contract import (
    CODE_EXTENSIONS,
    CONFIG_I18N_EXTENSIONS,
    LOCK_FILENAMES,
    POLICY_REVIEWABLE_EXACT_PATHS,
    POLICY_REVIEWABLE_PREFIXES,
    PROMPT_SKILL_PATH_MARKERS,
    REVIEW_CONTRACT_VERSION,
    SERVICE_CONFIG_EXTENSIONS,
    TEST_DIR_PARTS,
    TEST_FILE_SUFFIXES,
    IncrementalReviewScope,
    ReviewScope,
    SubmoduleChange,
)
from .documents import (
    architecture_document_paths,
    boundary_document_paths,
    changed_document_paths,
    changed_task_blueprints,
    contract_document_paths,
    is_architecture_contract,
    is_configured_blueprint_path,
    merge_task_blueprint_anchors,
    normalize_task_blueprint_anchors,
    resolve_scope_modules,
    resolved_contract_paths,
    resolved_document_paths,
)
from .session import (
    UTC,
    ReviewResult,
    ReviewSessionState,
    active_guard_runtime,
    compute_review_contract_digest,
    list_review_sessions,
    receipt_payload,
)
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
    """Run a snapshot-only Git command isolated from the host worktree's fsmonitor.

    Review reads immutable objects and never consults the worktree index, so a wedged
    or cross-version `fsmonitor--daemon` cannot be allowed to stall it.
    """
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=repo_root,
        env=clean_git_env(),
        check=check,
        capture_output=True,
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


def save_receipt(
    repo_root: Path, result: ReviewResult, *, contract_digest: str,
    task_blueprint_anchors: dict[str, tuple[str, ...]],
) -> Path:
    return _save_receipt_record(
        git_common_dir(repo_root), result, contract_digest=contract_digest,
        task_blueprint_anchors=task_blueprint_anchors,
    )


def load_review_session(
    repo_root: Path, branch: str | None = None
) -> ReviewSessionState | None:
    return _load_session_record(
        git_common_dir(repo_root), repo_root, branch or current_branch_name(repo_root)
    )


def save_review_session(repo_root: Path, state: ReviewSessionState) -> Path:
    return _save_session_record(git_common_dir(repo_root), state)


def _ordered_review_sessions(repo_root: Path) -> list[ReviewSessionState]:
    current = load_review_session(repo_root)
    retained = list_review_sessions(git_common_dir(repo_root))
    if current is None:
        return retained
    current_key = (current.worktree_path, current.branch)
    return [current, *(s for s in retained if (s.worktree_path, s.branch) != current_key)]


def review_logs_dir(repo_root: Path, env: dict[str, str] | None = None) -> Path:
    return _resolve_logs_dir(git_common_dir(repo_root), env)


def resolve_base_ref(repo_root: Path, main_branch: str = "main") -> str:
    if len(main_branch) in (40, 64) and all(c in "0123456789abcdef" for c in main_branch):
        if git_ok(repo_root, ["git", "cat-file", "-e", f"{main_branch}^{{commit}}"]):
            return main_branch
        raise RuntimeError(f"Cannot resolve base commit '{main_branch}'")
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


def is_reviewable_path(
    rel_path: str,
    config: GuardConfig | None = None,
    *,
    contract_paths: set[str] | None = None,
) -> bool:
    cfg = config or GuardConfig()
    settings = cfg.review
    if (
        rel_path == ".guard/config.json"
        or _is_scope_contract(rel_path, cfg, contract_paths)
        or is_configured_blueprint_path(rel_path, cfg)
    ):
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


def _is_scope_contract(rel_path: str, config: GuardConfig, contract_paths: set[str] | None) -> bool:
    return (
        is_architecture_contract(rel_path, config)
        if contract_paths is None
        else rel_path in contract_paths
    )


def _classify_file_tier(
    rel_path: str,
    config: GuardConfig | None = None,
    *,
    contract_paths: set[str] | None = None,
) -> int:
    """Return 0 for core production, 1 for config/i18n/parity, 2 for tests/fixtures/lockfiles."""
    cfg = config or GuardConfig()
    policy_prefixes = (
        *POLICY_REVIEWABLE_PREFIXES,
        cfg.detail_dir.rstrip("/") + "/",
        *cfg.review.get("reviewable_prefixes", []),
    )
    posix = PurePosixPath(rel_path)
    if _is_scope_contract(rel_path, cfg, contract_paths):
        return 0
    name_low = posix.name.lower()
    if posix.name in LOCK_FILENAMES or name_low == "conftest.py":
        return 2
    if any(part.lower() in TEST_DIR_PARTS for part in posix.parts):
        return 2
    if name_low.startswith("test_") or name_low.endswith(TEST_FILE_SUFFIXES):
        return 2
    as_posix = posix.as_posix()
    if not as_posix.startswith(policy_prefixes) and posix.suffix.lower() in CONFIG_I18N_EXTENSIONS:
        return 1
    return 0


def _is_test_or_fixture_path(rel_path: str) -> bool:
    return _classify_file_tier(rel_path) == 2


def _raw_diff_records(raw_diff: str) -> Iterator[tuple[list[str], str]]:
    for line in raw_diff.splitlines():
        if line.startswith(":"):
            meta, _, path = line[1:].partition("\t")
            fields = meta.split()
            if len(fields) >= 5 and path:
                yield fields, path


def _parse_raw_diff_lines(
    raw_diff: str,
    config: GuardConfig | None = None,
    *,
    contract_paths: set[str] | None = None,
) -> tuple[list[str], list[str], list[SubmoduleChange]]:
    changed_files: list[str] = []
    reviewable_files: list[str] = []
    submodules: list[SubmoduleChange] = []
    for parts, path in _raw_diff_records(raw_diff):
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
        if is_reviewable_path(path, config, contract_paths=contract_paths):
            reviewable_files.append(path)
    return changed_files, reviewable_files, submodules


def _parse_raw_file_blobs(raw_diff: str) -> dict[str, tuple[str, str]]:
    blobs: dict[str, tuple[str, str]] = {}
    for parts, path in _raw_diff_records(raw_diff):
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
    old_patch_id = stream_patch_id(repo_root, ("--binary", "--full-index", old_range, *path_args), check=False)
    new_patch_id = stream_patch_id(repo_root, ("--binary", "--full-index", new_range, *path_args), check=False)
    return old_patch_id != "0" * 40 and old_patch_id == new_patch_id


def _tree_entry_matches(
    entry: tuple[str, str] | None, expected: tuple[str, str],
) -> bool:
    return bool(expected[1]) and (entry is None if expected[0] == "000000" else entry == expected)


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


def _is_boundary_refresh_round(
    repo_root: Path, scope: ReviewScope, session: ReviewSessionState | None, *,
    receipt_contract_changed: bool,
) -> bool:
    return (
        session is not None
        and receipt_contract_changed
        and session.last_patch_digest == scope.patch_digest
        and session.last_head_sha != scope.head_sha
        and (
            session.merge_base == scope.merge_base
            or _is_ancestor(repo_root, session.merge_base, scope.merge_base)
        )
        and git_ok(repo_root, ["git", "cat-file", "-e", f"{session.last_head_sha}^{{commit}}"])
    )


def collect_review_scope(
    repo_root: Path,
    main_branch: str = "main",
    *,
    head_ref: str = "HEAD",
    config: GuardConfig | None = None,
) -> ReviewScope:
    """Collect the committed review scope, reusing configuration from that head if supplied."""
    base_ref = resolve_base_ref(repo_root, main_branch)
    head_sha = _git_text(repo_root, "rev-parse", "--verify", "--end-of-options", f"{head_ref}^{{commit}}")
    config = config or load_config(repo_root, revision=head_sha)
    merge_base = _git_text(repo_root, "merge-base", base_ref, head_sha)

    contract_paths = resolved_contract_paths(repo_root, config, head_sha)
    if merge_base != head_sha:
        contract_paths.update(
            resolved_contract_paths(
                repo_root, load_config(repo_root, revision=merge_base, allow_historical=True), merge_base,
            )
        )
    raw_diff = _git_text(
        repo_root, "diff", "--raw", "--no-renames", "--no-abbrev", f"{merge_base}..{head_sha}"
    )
    changed_files, reviewable_files, submodules = _parse_raw_diff_lines(
        raw_diff, config, contract_paths=contract_paths
    )

    patch_options = ("--binary", "--full-index", "--find-renames")
    root_patch_id = stream_patch_id(repo_root, (*patch_options, f"{merge_base}..{head_sha}"))

    sub_digests: list[str] = []
    for sub in sorted(submodules, key=lambda item: item.path):
        sub_repo = _require_submodule_checkout(repo_root, sub)
        diff_args = (*patch_options, f"{sub.old_sha}..{sub.new_sha}")
        try:
            sub_patch_id = stream_patch_id(sub_repo, diff_args)
            if sub_patch_id == "0" * 40 and not diff_has_content(sub_repo, diff_args):
                sub_patch_id = f"{sub.old_sha}->{sub.new_sha}"
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Failed to read submodule '{sub.path}' diff ({sub.old_sha[:10]}..{sub.new_sha[:10]}): {err or exc.returncode}"
            ) from exc
        sub_digests.append(f"{sub.path}:{sub_patch_id}")

    hasher = hashlib.sha256(f"root:{root_patch_id}\n".encode("utf-8"))
    for entry in sub_digests:
        hasher.update(f"sub:{entry}\n".encode("utf-8"))

    scope = ReviewScope(
        base_ref=base_ref,
        merge_base=merge_base,
        head_sha=head_sha,
        changed_files=tuple(sorted(changed_files)),
        reviewable_files=tuple(sorted(reviewable_files)),
        submodule_changes=tuple(sorted(submodules, key=lambda item: item.path)),
        patch_digest=hasher.hexdigest(),
    )
    return _include_retained_blueprints(repo_root, scope, config)


def _include_retained_blueprints(repo_root: Path, scope: ReviewScope, config: GuardConfig) -> ReviewScope:
    sessions = [s for s in _ordered_review_sessions(repo_root) if s.task_blueprint_anchors]
    if not sessions:
        return scope
    checked_out = _git_text(repo_root, "rev-parse", "--verify", "HEAD", check=False) == scope.head_sha
    owner_key = (str(repo_root.resolve()), current_branch_name(repo_root)) if checked_out else None
    anchors = merge_task_blueprint_anchors(*(
        _applicable_session_anchors(repo_root, scope, config, s, owner_key) for s in sessions
    ))
    if not anchors:
        return scope
    paths = resolved_document_paths(repo_root, scope.head_sha, anchors)
    paths.update(resolved_document_paths(repo_root, scope.merge_base, anchors))
    return replace(
        scope,
        task_blueprint_anchors=anchors,
        reviewable_files=tuple(sorted(set(scope.reviewable_files) | paths.intersection(scope.changed_files))),
    )


def _applicable_session_anchors(
    repo_root: Path, scope: ReviewScope, config: GuardConfig, session: ReviewSessionState,
    owner_key: tuple[str, str] | None,
) -> dict[str, tuple[str, ...]]:
    """Select the anchor history one persisted session may contribute to this scope.

    A session already landed in the merge base keeps only the anchors the candidate branch
    changes, so merged unrelated branches cannot pollute later scopes or their receipts.
    """
    if _session_landed_in_merge_base(repo_root, scope, session, owner_key):
        return _changed_anchor_entries(repo_root, scope, session.task_blueprint_anchors)
    if _blueprint_history_applies(repo_root, scope, config, session, owner_key):
        return dict(session.task_blueprint_anchors)
    return {}


def _session_landed_in_merge_base(
    repo_root: Path, scope: ReviewScope, session: ReviewSessionState,
    owner_key: tuple[str, str] | None,
) -> bool:
    """Return True when the session's reviewed branch already landed in the target merge base."""
    if scope.merge_base == scope.head_sha and (session.worktree_path, session.branch) == owner_key:
        return False
    if _is_ancestor(repo_root, session.last_head_sha, scope.merge_base):
        return True
    if (
        session.merge_base == scope.merge_base
        or not _is_ancestor(repo_root, session.merge_base, scope.merge_base)
        or session.last_patch_digest == scope.patch_digest
        or session.last_submodule_shas
    ):
        return False
    # An object-only replay lands the same blobs under a commit the session never saw.
    old_blobs = _parse_raw_file_blobs(_git_text(
        repo_root, "diff", "--raw", "--no-renames", "--no-abbrev",
        f"{session.merge_base}..{session.last_head_sha}", check=False,
    ))
    try:
        entries = tree_entries_map(repo_root, old_blobs, scope.merge_base)
    except ValueError:
        return False
    return bool(old_blobs) and all(
        _tree_entry_matches(entries.get(path), expected) for path, expected in old_blobs.items()
    )


def _changed_anchor_entries(
    repo_root: Path, scope: ReviewScope, anchors: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Keep only anchors whose resolved documents this candidate branch changes."""
    touched = changed_document_paths(repo_root, anchors.keys(), scope.merge_base, scope.head_sha, scope.changed_files)
    return {path: revisions for path, revisions in anchors.items() if path in touched}


def _branch_change_overlaps_scope(
    repo_root: Path, scope: ReviewScope, session: ReviewSessionState,
) -> bool:
    """Require a sibling branch to touch this scope's resolved paths before its delta can apply."""
    branch_changed = _parse_raw_file_blobs(_git_text(
        repo_root, "diff", "--raw", "--no-renames", "--no-abbrev",
        f"{session.merge_base}..{session.last_head_sha}", check=False,
    ))
    if not branch_changed:
        return False
    if set(branch_changed).intersection(scope.changed_files):
        return True
    branch_aliases = _resolved_change_aliases(
        repo_root, (session.merge_base, session.last_head_sha), set(branch_changed)
    )
    scope_aliases = _resolved_change_aliases(
        repo_root, (scope.merge_base, scope.head_sha), set(scope.changed_files)
    )
    return bool(branch_aliases.intersection(scope_aliases))


def _resolved_change_aliases(
    repo_root: Path, revisions: tuple[str, ...], changed_paths: set[str],
) -> set[str]:
    """Expand changed paths onto the snapshot symlink targets that make them the same document."""
    aliases = set(changed_paths)
    for revision in dict.fromkeys(revisions):
        if git_ok(repo_root, ["git", "cat-file", "-e", f"{revision}^{{commit}}"]):
            aliases.update(resolved_document_paths(repo_root, revision, changed_paths))
    return aliases


def _blueprint_history_applies(
    repo_root: Path, scope: ReviewScope, config: GuardConfig, session: ReviewSessionState,
    owner_key: tuple[str, str] | None,
) -> bool:
    if _is_ancestor(repo_root, session.last_head_sha, scope.head_sha):
        return True
    if (session.worktree_path, session.branch) == owner_key:
        return review_anchor_session(repo_root, scope, session, config=config) is not None
    if not _is_ancestor(repo_root, session.merge_base, scope.merge_base):
        return False
    if session.last_patch_digest == scope.patch_digest:
        return True
    if not _branch_change_overlaps_scope(repo_root, scope, session):
        return False
    # Replayed code can keep the same reviewed subject while changing its blueprint.
    paths = resolved_document_paths(repo_root, session.last_head_sha, session.task_blueprint_anchors)
    paths.update(resolved_document_paths(repo_root, scope.head_sha, session.task_blueprint_anchors))
    return _collect_branch_delta(repo_root, scope, session, config=config, blueprint_only_paths=paths) is not None


def _collect_branch_delta(
    repo_root: Path,
    scope: ReviewScope,
    session: ReviewSessionState | None,
    *,
    config: GuardConfig | None = None,
    blueprint_only_paths: set[str] | None = None,
) -> IncrementalReviewScope | None:
    """Compute verified branch delta since `session`, or return None when session continuation is inapplicable.

    Raises RuntimeError if git diff commands fail after commit existence is verified (fail-closed).
    Returns an IncrementalReviewScope, which may contain no reviewable changes.
    """
    config = config or load_config(repo_root, revision=scope.head_sha)
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

    retained = session.task_blueprint_anchors.keys() | scope.task_blueprint_anchors.keys()
    declared_contracts = contract_document_paths(config) if blueprint_only_paths is not None else set()

    def has_other_reviewable_paths(paths: Iterable[str]) -> bool:
        return blueprint_only_paths is not None and any(
            path not in blueprint_only_paths and is_reviewable_path(path, config, contract_paths=declared_contracts)
            for path in paths
        )

    def delta_contract_paths() -> set[str]:
        return resolved_contract_paths(
            repo_root, config, scope.head_sha, retained_blueprints=retained,
        ) | resolved_contract_paths(
            repo_root,
            load_config(repo_root, revision=session.last_head_sha, allow_historical=True),
            session.last_head_sha,
            retained_blueprints=retained,
        )

    changed_files: list[str] = []
    reviewable_files: list[str] = []
    inc_subs: dict[str, SubmoduleChange] = {}

    if session.merge_base == scope.merge_base:
        raw_proc = _git(
            repo_root,
            "diff",
            "--raw",
            "--no-renames",
            "--no-abbrev",
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
        if has_other_reviewable_paths(path for _, path in _raw_diff_records(raw_diff)):
            return None
        changed_files, reviewable_files, raw_subs = _parse_raw_diff_lines(
            raw_diff, config, contract_paths=delta_contract_paths(),
        )
        inc_subs = {sub.path: sub for sub in raw_subs}
    elif _is_ancestor(repo_root, session.merge_base, scope.merge_base):
        old_raw_proc = _git(
            repo_root,
            "diff",
            "--raw",
            "--no-renames",
            "--no-abbrev",
            f"{session.merge_base}..{session.last_head_sha}",
            check=False,
        )
        new_raw_proc = _git(
            repo_root,
            "diff",
            "--raw",
            "--no-renames",
            "--no-abbrev",
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
        # New paths require no patch equivalence check to disqualify this history.
        if has_other_reviewable_paths(new_blobs.keys() - old_blobs.keys()):
            return None
        contract_paths = delta_contract_paths()
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
            if is_reviewable_path(path, config, contract_paths=contract_paths):
                reviewable_files.append(path)
        previous_paths = old_blobs.keys() - new_blobs.keys()
        try:
            entries = tree_entries_map(repo_root, previous_paths, scope.head_sha)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        for path in previous_paths:
            if not _tree_entry_matches(entries.get(path), old_blobs[path]):
                changed_files.append(path)
                if is_reviewable_path(path, config, contract_paths=contract_paths):
                    reviewable_files.append(path)
    else:
        return None

    for sub in scope.submodule_changes:
        prev_sha = session.last_submodule_shas.get(sub.path, sub.old_sha)
        if prev_sha != sub.new_sha:
            inc_subs[sub.path] = SubmoduleChange(
                path=sub.path, old_sha=prev_sha, new_sha=sub.new_sha
            )

    if blueprint_only_paths is not None and (
        inc_subs or not blueprint_only_paths.issuperset(reviewable_files)
    ):
        return None
    changed_blueprints = changed_task_blueprints(
        repo_root,
        config,
        session.last_head_sha,
        scope.head_sha,
        retained,
        branch_changed_paths=(
            scope.changed_files if session.merge_base != scope.merge_base else None
        ),
    )
    if blueprint_only_paths is not None and not blueprint_only_paths.issuperset(changed_blueprints):
        return None
    return IncrementalReviewScope(
        base_head_sha=session.last_head_sha,
        target_head_sha=scope.head_sha,
        changed_files=tuple(sorted(changed_files)),
        reviewable_files=tuple(sorted(reviewable_files)),
        submodule_changes=tuple(sorted(inc_subs.values(), key=lambda item: item.path)),
        changed_blueprints=tuple(sorted(changed_blueprints)),
    )


def collect_incremental_scope(
    repo_root: Path,
    scope: ReviewScope,
    session: ReviewSessionState | None,
    *,
    config: GuardConfig | None = None,
) -> IncrementalReviewScope | None:
    try:
        delta = _collect_branch_delta(repo_root, scope, session, config=config)
    except RuntimeError:
        return None
    return delta if (delta is not None and delta.has_changes) else None


def previous_review_scope(
    repo_root: Path, session: ReviewSessionState,
) -> tuple[ReviewScope, GuardConfig]:
    revision = session.last_head_sha
    config = load_config(repo_root, revision=revision, allow_historical=True)
    scope = collect_review_scope(
        repo_root, main_branch=session.merge_base, head_ref=revision, config=config,
    )
    return scope, config


def contract_change_causes(
    repo_root: Path, scope: ReviewScope, prior_head: str, config: GuardConfig, policy: Policy,
) -> tuple[str, ...]:
    touched, _, _ = resolve_scope_modules(repo_root, scope, config, revision=scope.head_sha)
    declared_modules = {module["path"]: module for module in config.topology.get("modules", ())}
    candidates = architecture_document_paths(config) | {rule.details for rule in policy.rules}
    candidates.add(".guard/config.json")
    for module in touched:
        candidates.update(boundary_document_paths(declared_modules.get(module, {"path": module})))
    paths = resolved_document_paths(repo_root, scope.head_sha, candidates)
    changed = _git(
        repo_root, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
        f"{prior_head}..{scope.head_sha}", *_literal_pathspecs(tuple(sorted(paths))), check=False,
    )
    if changed.returncode == 0 and changed.stdout:
        return tuple(changed.stdout.decode("utf-8", errors="replace").rstrip("\0").split("\0", 5)[:5])
    return ("contract_version_or_legacy",)


def review_anchor_session(
    repo_root: Path,
    scope: ReviewScope,
    session: ReviewSessionState | None,
    *,
    config: GuardConfig,
) -> ReviewSessionState | None:
    """Keep branch blueprint evidence across provider or reviewer contract changes."""
    if session is None or not session.initial_head_sha or not git_ok(
        repo_root, ["git", "cat-file", "-e", f"{session.initial_head_sha}^{{commit}}"]
    ):
        return None
    if session.merge_base == scope.merge_base:
        return session
    if session.last_patch_digest == scope.patch_digest:
        valid_rebase = _is_ancestor(repo_root, session.merge_base, scope.merge_base)
    else:
        valid_rebase = (
            _collect_branch_delta(repo_root, scope, session, config=config) is not None
        )
    return session if valid_rebase else None


def _anchors_cover(
    reviewed: dict[str, tuple[str, ...]], required: dict[str, tuple[str, ...]],
) -> bool:
    return all(set(revisions).issubset(reviewed.get(path, ())) for path, revisions in required.items())


def _receipt_covers(
    receipt: dict[str, Any], contract_digest: str, anchors: dict[str, tuple[str, ...]],
) -> bool:
    return receipt.get("contract_digest") == contract_digest and _anchors_cover(
        normalize_task_blueprint_anchors(receipt.get("task_blueprint_anchors")), anchors,
    )


def _scope_contract_digest(
    repo_root: Path,
    scope: ReviewScope,
    *,
    config: GuardConfig | None,
    policy: Policy | None,
    contract_digest: str | None = None,
) -> str:
    config = config or load_config(repo_root, revision=scope.head_sha)
    policy = policy or load_policy(repo_root, revision=scope.head_sha, config=config)
    return contract_digest or compute_review_contract_digest(
        policy, config, repo_root=repo_root, revision=scope.head_sha, scope=scope,
    )


def diagnose_missing_receipt(
    repo_root: Path,
    scope: ReviewScope,
    *,
    config: GuardConfig | None = None,
    policy: Policy | None = None,
    contract_digest: str | None = None,
) -> str:
    """Explain a failed receipt lookup after carry-forward has been attempted."""
    missing = (
        f"NO-GO: missing review receipt for pushed range (patch {scope.patch_digest[:12]}). "
        "Run guard review, address findings or submit --note evidence, then retry."
    )
    receipt = load_valid_receipt(repo_root, scope.patch_digest)
    if receipt is None:
        return missing

    current_contract = contract_digest or _scope_contract_digest(
        repo_root, scope, config=config, policy=policy,
    )
    receipt_contract = receipt.get("contract_digest")
    if receipt_contract != current_contract:
        receipt_version = str(receipt.get("review_contract_version") or "")
        receipt_digest = str(receipt_contract or "")[:12] or "unknown"
        contracts = f"receipt contract {receipt_digest}, verifier contract {current_contract[:12]}"
        contract_changed = f"NO-GO: review contract changed ({contracts}). "
        if receipt_version == REVIEW_CONTRACT_VERSION:
            return contract_changed + "Policy, .guard/config.json, or boundary docs changed. Re-run guard review."

        receipt_sha = str(receipt.get("guard_sha") or "")
        receipt_tree = receipt.get("guard_tree_digest")
        if receipt_version or receipt_tree or receipt_sha:
            runtime = active_guard_runtime()
            verifier_sha = runtime["guard_sha"]
            verifier_tree = runtime["guard_tree_digest"]
            tree_changed = receipt_tree and receipt_tree != verifier_tree
            sha_changed = (
                receipt_sha and verifier_sha
                and not (receipt_sha.startswith(verifier_sha) or verifier_sha.startswith(receipt_sha))
            )
            if receipt_version or (verifier_tree and tree_changed) or sha_changed:
                receipt_dir = receipt.get("guard_package_dir") or "unknown package path"
                versions = (
                    f"Receipt review_contract_version={receipt_version}. "
                    f"Verifier review_contract_version={REVIEW_CONTRACT_VERSION}. "
                    if receipt_version else ""
                )
                return (
                    f"NO-GO: the-guard runtime mismatch ({contracts}). "
                    f"{versions}"
                    f"Receipt runtime {receipt_sha or 'unknown SHA'} at {receipt_dir}. "
                    f"Verifier runtime {verifier_sha or 'unknown SHA'} at {runtime['guard_package_dir']}. "
                    "Use the same guard CLI for review and verification. "
                    "Run guard self-update to align them, then re-run guard review."
                )
        return (
            contract_changed
            + "Policy, .guard/config.json, boundary docs, or the guard version may have changed. "
            "Re-run guard review."
        )
    if not _anchors_cover(
        normalize_task_blueprint_anchors(receipt.get("task_blueprint_anchors")),
        scope.task_blueprint_anchors,
    ):
        return (
            "NO-GO: blueprint-anchor drift. The review receipt does not cover all required "
            "task blueprint revisions. Re-run guard review."
        )
    if receipt.get("head_sha") != scope.head_sha or receipt.get("merge_base") != scope.merge_base:
        return (
            "NO-GO: commit/baseline drift. The review receipt covers a different HEAD or merge base "
            "and no eligible review session could carry it forward. Re-run guard review."
        )
    return missing


def resolve_or_carry_forward_receipt(
    repo_root: Path,
    scope: ReviewScope,
    *,
    persist: bool = True,
    config: GuardConfig | None = None,
    policy: Policy | None = None,
    contract_digest: str | None = None,
) -> dict[str, Any] | None:
    existing = load_valid_receipt(repo_root, scope.patch_digest)
    sessions = [s for s in _ordered_review_sessions(repo_root) if s.last_verdict == "GO"]
    if existing is None and not sessions:
        return None
    current_config = config or load_config(repo_root, revision=scope.head_sha)
    current_contract_digest = _scope_contract_digest(
        repo_root, scope, config=current_config, policy=policy, contract_digest=contract_digest,
    )
    if (
        existing is not None
        and existing.get("head_sha") == scope.head_sha
        and existing.get("merge_base") == scope.merge_base
        and _receipt_covers(existing, current_contract_digest, scope.task_blueprint_anchors)
    ):
        return existing
    for session in sessions:
        if (
            session.contract_digest != current_contract_digest
            or not _anchors_cover(session.task_blueprint_anchors, scope.task_blueprint_anchors)
        ):
            continue
        prior_receipt = load_valid_receipt(repo_root, session.last_patch_digest)
        if prior_receipt is None or not _receipt_covers(
            prior_receipt, current_contract_digest, session.task_blueprint_anchors,
        ):
            continue
        same_base = session.merge_base == scope.merge_base
        if not same_base and not _is_ancestor(repo_root, session.merge_base, scope.merge_base):
            continue
        if existing is None or session.last_patch_digest != scope.patch_digest:
            if same_base:
                continue
            try:
                delta = _collect_branch_delta(repo_root, scope, session, config=current_config)
            except RuntimeError:
                continue
            if delta is None or delta.has_changes:
                continue
        elif changed_task_blueprints(
            repo_root, current_config, session.last_head_sha, scope.head_sha,
            session.task_blueprint_anchors.keys() | scope.task_blueprint_anchors.keys(),
            branch_changed_paths=None if same_base else scope.changed_files,
        ):
            continue
        return _carry_forward_receipt(repo_root, scope, session, prior_receipt, persist=persist)
    return None


def _carry_forward_receipt(
    repo_root: Path,
    scope: ReviewScope,
    session: ReviewSessionState,
    prior_receipt: dict[str, Any],
    *,
    persist: bool,
) -> dict[str, Any] | None:
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
    anchors = session.task_blueprint_anchors
    if not persist:
        return receipt_payload(
            carried, contract_digest=session.contract_digest, task_blueprint_anchors=anchors,
        )
    save_receipt(
        repo_root, carried, contract_digest=session.contract_digest, task_blueprint_anchors=anchors,
    )
    if _git_text(repo_root, "rev-parse", "--verify", "HEAD", check=False) == scope.head_sha:
        save_review_session(
            repo_root,
            replace(
                session,
                worktree_path=str(repo_root.resolve()),
                branch=current_branch_name(repo_root),
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
                updated_at=datetime.now(UTC).isoformat(),
            ),
        )
    return load_valid_receipt(repo_root, scope.patch_digest)
