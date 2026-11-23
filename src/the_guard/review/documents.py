"""Architecture document inventory and snapshot reads shared by review consumers."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path, PurePosixPath

from ..config import GuardConfig, repository_path
from ..git_helpers import git_stdout
from ..git_helpers import read_repository_file
from .contract import CODE_EXTENSIONS

MAX_TASK_DESIGN_DOCS = 4
_TASK_DESIGN_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])[A-Za-z0-9_./-]+\.md(?![\w/-]|\.[\w.])"
)


def architecture_document_paths(config: GuardConfig) -> set[str]:
    return {
        config.manifest_file,
        "ARCHITECTURE.md",
        "docs/architecture.md",
        *config.topology.get("architecture_docs", []),
    }


def boundary_document_paths(module: dict) -> set[str]:
    if module.get("retired", False):
        return set()
    return {
        *module.get("boundary_docs", []),
        *(
            module["path"] + "/" + name
            for name in ("AGENTS.md", "CLAUDE.md", "README.md")
            if PurePosixPath(module["path"]).suffix.lower() not in CODE_EXTENSIONS
        ),
    }


def contract_document_paths(config: GuardConfig) -> set[str]:
    paths = architecture_document_paths(config)
    for module in config.topology.get("modules", []):
        paths.update(boundary_document_paths(module))
    return paths


def is_architecture_contract(path: str, config: GuardConfig) -> bool:
    return path in contract_document_paths(config)


def resolved_contract_paths(root: Path, config: GuardConfig, revision: str) -> set[str]:
    """Declared/implicit contracts plus safe snapshot symlink targets, including absent paths."""
    paths = contract_document_paths(config)
    modes: dict[str, str] = {}
    existing_document_paths(root, paths, revision, snapshot_modes=modes)
    for path, mode in modes.items():
        if mode == "120000":
            read_document(
                root, path, revision, loaded_paths=paths, snapshot_modes=modes
            )
    return paths


@dataclass(frozen=True)
class TaskBlueprintSnapshot:
    path: str
    baseline_sha: str | None
    baseline_body: str | None
    head_body: str | None
    follows_symlink: bool = False
    in_merge_base: bool = False
    reviewed_revisions: tuple[tuple[str, str], ...] = ()

    @property
    def has_modifications(self) -> bool:
        return self.baseline_body is not None and (
            self.baseline_body != self.head_body
            or any(body != self.head_body for _, body in self.reviewed_revisions)
        )


@dataclass(frozen=True)
class TaskBlueprintSelection:
    """Bounded prompt previews and the snapshots to retain across reviews."""

    previews: list[TaskBlueprintSnapshot] = field(default_factory=list)
    anchor_snapshots: list[TaskBlueprintSnapshot] = field(default_factory=list)


def normalize_task_blueprint_anchors(value: object) -> dict[str, tuple[str, ...]]:
    """Load ordered anchor histories, including legacy single-revision records."""
    if not isinstance(value, Mapping):
        return {}
    anchors = {}
    for path, revisions in value.items():
        if isinstance(revisions, str):
            revisions = (revisions,)
        if (
            isinstance(path, str)
            and isinstance(revisions, (tuple, list))
            and revisions
            and all(isinstance(revision, str) for revision in revisions)
        ):
            anchors[path] = tuple(dict.fromkeys(revisions))
    return anchors


def _task_design_references(body: str, source: str) -> Iterable[tuple[str, ...]]:
    parent = PurePosixPath(source).parent
    for match in _TASK_DESIGN_REFERENCE.finditer(body):
        path = match.group()
        if posixpath.isabs(path):
            continue
        relative = posixpath.normpath(posixpath.join(parent, path))
        candidates = (relative,) if path.startswith(("./", "../")) else (
            posixpath.normpath(path), relative,
        )
        yield tuple(
            candidate for candidate in dict.fromkeys(candidates)
            if candidate != ".." and not candidate.startswith("../")
        )


def resolve_task_design_blueprints(
    repo_root: Path,
    changed_files: Iterable[str],
    *,
    base_sha: str,
    head_sha: str,
    config: GuardConfig | None = None,
    initial_reviewed_sha: str = "",
    prior_reviewed_sha: str = "",
    retained_anchors: Mapping[str, str | tuple[str, ...]] | None = None,
    reviewer_note: str = "",
    excluded_paths: Iterable[str] = (),
) -> TaskBlueprintSelection:
    """Resolve changed Markdown and direct references against binding Git snapshots."""
    config = config or GuardConfig()
    blueprint_globs = tuple(config.review.get("blueprint_globs", ()))
    anchors = normalize_task_blueprint_anchors(retained_anchors)
    if not blueprint_globs and not anchors:
        return TaskBlueprintSelection()
    operational_ledgers = frozenset(config.review.get("operational_ledgers", ["CHANGELOG.md"]))

    @cache
    def matches_blueprint(path: str) -> bool:
        return path.endswith(".md") and path not in operational_ledgers and any(
            fnmatchcase(path, pattern) for pattern in blueprint_globs
        )

    recorded_revisions = dict.fromkeys(rev for revisions in anchors.values() for rev in revisions)
    verified_revisions = {}
    for revision in dict.fromkeys((initial_reviewed_sha, *recorded_revisions, prior_reviewed_sha)):
        if revision in (base_sha, head_sha):
            commit = revision
        else:
            commit = revision and git_stdout(
                repo_root,
                ["git", "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
            )
        if commit:
            verified_revisions[revision] = commit.strip()
        elif revision in recorded_revisions:
            raise ValueError(f"Cannot inspect recorded task blueprint revision {revision}")
    anchors = {
        path: tuple(verified_revisions[revision] for revision in revisions)
        for path, revisions in anchors.items()
    }
    reviewed_commits = dict.fromkeys(
        commit for commit in verified_revisions.values() if commit != base_sha
    )
    initial_commit = verified_revisions.get(initial_reviewed_sha)

    excluded_set = set(excluded_paths)
    excluded = operational_ledgers | (excluded_set - anchors.keys())
    # A plan introduced in a reviewed round and later removed is absent from the
    # current branch diff. Use each reviewed branch's change list so an upstream
    # deletion during a rebase is not attributed to the reviewed branch.
    historical_changes = []
    for revision in reviewed_commits if blueprint_globs else ():
        if revision == head_sha:
            continue
        changed = git_stdout(
            repo_root,
            [
                "git", "diff", "--name-only", "-z", "--no-renames",
                f"{base_sha}...{revision}", "--", "*.md",
            ],
        )
        if changed is None:
            raise ValueError(f"Cannot inspect task blueprint changes at {base_sha}...{revision}")
        historical_changes.extend(changed.split("\0"))
    head_changes = dict.fromkeys(changed_files)
    markdown_changes = dict.fromkeys(
        path for path in (*head_changes, *historical_changes)
        if blueprint_globs and path.endswith(".md") and path not in excluded
    )
    changed_candidates = dict.fromkeys(path for path in markdown_changes if matches_blueprint(path))
    changed_candidates.update(dict.fromkeys(anchors))

    modes_by_revision: dict[str, dict[str, str]] = {
        revision: {} for revision in dict.fromkeys((base_sha, *reviewed_commits, head_sha))
    }

    def inventory(paths: Collection[str]) -> None:
        for revision, modes in modes_by_revision.items():
            existing_document_paths(repo_root, paths, revision, snapshot_modes=modes)

    @cache
    def read_body(path: str, revision: str) -> str | None:
        return read_document(repo_root, path, revision, snapshot_modes=modes_by_revision[revision])

    def resolve_candidates(paths: Iterable[str]) -> list[TaskBlueprintSnapshot]:
        snapshots = []
        for path in paths:
            pinned_revisions = anchors.get(path, ())
            path_revisions = dict.fromkeys((*pinned_revisions, *reviewed_commits))
            baseline_sha = next(
                (
                    rev for rev in dict.fromkeys((base_sha, initial_commit, *path_revisions))
                    if rev is not None and path in modes_by_revision[rev]
                ),
                None,
            )
            head_body = read_body(path, head_sha)
            baseline_body = read_body(path, baseline_sha) if baseline_sha is not None else None
            if head_body is None and baseline_body is None:
                continue
            reviewed_revisions = []
            seen_bodies = {baseline_body}
            for revision in path_revisions:
                if revision == base_sha or revision == baseline_sha:
                    continue
                body = read_body(path, revision)
                if body is not None and body not in seen_bodies:
                    reviewed_revisions.append((revision, body))
                    seen_bodies.add(body)
            snapshots.append(
                TaskBlueprintSnapshot(
                    path, baseline_sha, baseline_body, head_body,
                    follows_symlink=any(
                        modes_by_revision[rev].get(path) == "120000"
                        for rev in (
                            baseline_sha, *(rev for rev, _ in reviewed_revisions), head_sha,
                        )
                        if rev is not None
                    ),
                    in_merge_base=baseline_sha is not None and baseline_sha == base_sha,
                    reviewed_revisions=tuple(reviewed_revisions),
                )
            )
        return snapshots

    inventory(markdown_changes.keys() | anchors.keys())
    for path, revisions in anchors.items():
        for revision in revisions:
            if path not in modes_by_revision[revision]:
                raise ValueError(f"Recorded task blueprint missing at {revision}: {path}")
    changed_docs = resolve_candidates(changed_candidates)
    reference_candidates = {}
    for path in markdown_changes:
        for revision in modes_by_revision:
            body = read_body(path, revision)
            if body is not None:
                reference_candidates.update(dict.fromkeys(
                    (revision, candidates) for candidates in _task_design_references(body, path)
                ))
    if blueprint_globs:
        reference_candidates.update(dict.fromkeys(
            (None, candidates) for candidates in _task_design_references(reviewer_note, "")
        ))
    inventory({
        path for _, candidates in reference_candidates for path in candidates
        if matches_blueprint(path) and path not in excluded and path not in markdown_changes and path not in anchors
    })
    note_revisions = dict.fromkeys((head_sha, base_sha, *reviewed_commits))
    references = {}
    for source_revision, candidates in reference_candidates:
        revisions = note_revisions if source_revision is None else (source_revision,)
        reference = next((
            path for revision in revisions for path in candidates
            if matches_blueprint(path) and path not in excluded and path in modes_by_revision[revision]
        ), None)
        if reference is not None:
            references[reference] = None
    changed_paths = {snapshot.path for snapshot in changed_docs}
    referenced_docs = resolve_candidates(path for path in references if path not in changed_paths)

    def preview_priority(snapshot: TaskBlueprintSnapshot) -> int:
        if not snapshot.in_merge_base:
            return 2
        if (
            snapshot.path not in head_changes
            and not snapshot.has_modifications
            and (snapshot.path in references or snapshot.path in anchors)
        ):
            return 0
        return 1

    ordered = sorted((*changed_docs, *referenced_docs), key=preview_priority)
    previews = [
        snapshot for snapshot in ordered
        if snapshot.path not in excluded_set or snapshot.has_modifications
    ][:MAX_TASK_DESIGN_DOCS]
    preview_paths = {snapshot.path for snapshot in previews}
    return TaskBlueprintSelection(
        previews,
        [
            snapshot for snapshot in ordered
            if snapshot.path in changed_paths or snapshot.in_merge_base or snapshot.path in preview_paths
        ],
    )


def existing_document_paths(
    root: Path,
    paths: Iterable[str],
    revision: str | None,
    *,
    snapshot_modes: dict[str, str] | None = None,
) -> set[str]:
    """Inspect candidates in one tree query; retain modes for safe snapshot reads."""
    candidates = sorted(set(paths))
    if revision is None:
        return {path for path in candidates if repository_path(root, path).is_file()}
    if not candidates:
        return set()
    output = git_stdout(
        root,
        [
            "git",
            "ls-tree",
            "-z",
            revision,
            "--",
            *[f":(literal){path}" for path in candidates],
        ],
    )
    if output is None:
        raise ValueError(f"Cannot inspect architecture documents at {revision}")
    existing = set()
    for entry in output.split("\0"):
        metadata, separator, path = entry.partition("\t")
        if separator and not metadata.startswith("040000 "):
            existing.add(path)
            if snapshot_modes is not None:
                snapshot_modes[path] = metadata.split()[0]
    return existing


def read_document(
    root: Path,
    path: str,
    revision: str | None = None,
    *,
    loaded_paths: set[str] | None = None,
    snapshot_modes: dict[str, str] | None = None,
) -> str | None:
    """Absence is optional; unsafe links, invalid content and unavailable history are errors."""
    if revision is None:
        if not repository_path(root, path).is_file():
            return None
    elif snapshot_modes is None:
        snapshot_modes = {}
        if path not in existing_document_paths(
            root, [path], revision, snapshot_modes=snapshot_modes
        ):
            return None
    elif path not in snapshot_modes:
        return None
    return read_repository_file(
        root,
        path,
        revision=revision,
        loaded_paths=loaded_paths,
        snapshot_modes=snapshot_modes,
    )
