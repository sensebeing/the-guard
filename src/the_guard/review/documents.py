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
from ..topology.imports import owner_module
from .contract import CODE_EXTENSIONS, ReviewScope

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


def _declared_module_owners(
    paths: Iterable[str], declared_paths: list[str], config: GuardConfig
) -> set[str]:
    owners: set[str] = set()
    for path in paths:
        owner = owner_module(path, declared_paths)
        if owner is not None:
            owners.add(owner)
            continue
        parts = PurePosixPath(path).parts
        if len(parts) >= 2 and parts[0] in config.module_doc_roots:
            owners.add("/".join(parts[:2]))
    return owners


def _boundary_alias_owners(
    repo_root: Path,
    revision: str,
    declared_modules: Mapping[str, dict],
    changed_paths: set[str],
    modes: dict[str, str],
) -> set[str]:
    """Bind modules whose boundary documents or symlink targets are reviewed paths."""
    owners: set[str] = set()
    for module, declaration in declared_modules.items():
        boundary_paths = boundary_document_paths(declaration)
        aliases = set(boundary_paths)
        for path in boundary_paths:
            if modes.get(path) == "120000":
                read_document(
                    repo_root, path, revision, loaded_paths=aliases, snapshot_modes=modes
                )
        if aliases & changed_paths:
            owners.add(module)
    return owners


def resolve_scope_modules(
    repo_root: Path,
    scope: ReviewScope,
    config: GuardConfig,
    *,
    revision: str | None = None,
    snapshot_modes: dict[str, str] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Resolve reviewed files onto the touched module set and its one-hop neighbours.

    `snapshot_modes` optionally collects the batched document inventory so callers reuse it.
    """
    rev = revision or scope.head_sha
    declared_modules = {m["path"]: m for m in config.topology.get("modules", [])}
    changed_paths = set(scope.reviewable_files)
    touched = _declared_module_owners(changed_paths, list(declared_modules), config)
    modes = snapshot_modes if snapshot_modes is not None else {}
    candidates = contract_document_paths(config)
    for module in touched:
        candidates.update(
            boundary_document_paths(declared_modules.get(module, {"path": module}))
        )
    existing_document_paths(repo_root, candidates, rev, snapshot_modes=modes)
    touched.update(
        _boundary_alias_owners(repo_root, rev, declared_modules, changed_paths, modes)
    )
    upstream = {
        dependency
        for module in touched
        for dependency in declared_modules.get(module, {}).get("dependencies", ())
        if dependency not in touched and dependency in declared_modules
    }
    downstream = {
        path
        for path, declaration in declared_modules.items()
        if path not in touched and bool(set(declaration.get("dependencies", ())) & touched)
    }
    return touched, upstream, downstream


def _blueprint_filters(config: GuardConfig) -> tuple[tuple[str, ...], frozenset[str]]:
    return (
        tuple(config.review.get("blueprint_globs", ())),
        frozenset(config.review.get("operational_ledgers", ["CHANGELOG.md"])),
    )


def _matches_blueprint(path: str, patterns: tuple[str, ...], ledgers: frozenset[str]) -> bool:
    return path.endswith(".md") and path not in ledgers and any(
        fnmatchcase(path, pattern) for pattern in patterns
    )


def is_configured_blueprint_path(path: str, config: GuardConfig) -> bool:
    return _matches_blueprint(path, *_blueprint_filters(config))


def _configured_blueprint_paths(root: Path, config: GuardConfig, revision: str) -> set[str]:
    patterns, ledgers = _blueprint_filters(config)
    if not patterns:
        return set()
    entries = git_stdout(root, ["git", "ls-tree", "-r", "--name-only", "-z", revision])
    if entries is None:
        raise ValueError(f"Cannot inventory configured task blueprints at {revision}")
    return {path for path in entries.split("\0") if _matches_blueprint(path, patterns, ledgers)}


def resolved_contract_paths(
    root: Path, config: GuardConfig, revision: str, *, retained_blueprints: Iterable[str] = ()
) -> set[str]:
    """Declared/implicit contracts plus safe snapshot symlink targets, including absent paths."""
    paths = contract_document_paths(config) | _configured_blueprint_paths(root, config, revision)
    paths.update(retained_blueprints)
    return resolved_document_paths(root, revision, paths)


def resolved_document_paths(root: Path, revision: str, paths: Iterable[str]) -> set[str]:
    paths = set(paths)
    modes: dict[str, str] = {}
    existing_document_paths(root, paths, revision, snapshot_modes=modes)
    for path, mode in modes.items():
        if mode == "120000":
            read_document(
                root, path, revision, loaded_paths=paths, snapshot_modes=modes
            )
    return paths


def _branch_touched_blueprints(
    root: Path,
    configured_paths: Collection[str],
    previous: str,
    target: str,
    branch_changed_paths: Collection[str],
) -> set[str]:
    """Keep configured blueprints only when the replayed branch changed them or their targets."""
    branch_changed = set(branch_changed_paths)
    selected: set[str] = set()
    for revision in (previous, target):
        modes: dict[str, str] = {}
        existing_document_paths(
            root, configured_paths, revision, snapshot_modes=modes
        )
        for path in configured_paths:
            aliases = {path}
            if modes.get(path) == "120000":
                read_document(
                    root, path, revision, loaded_paths=aliases, snapshot_modes=modes
                )
            if aliases & branch_changed:
                selected.add(path)
    return selected


def changed_task_blueprints(
    root: Path,
    config: GuardConfig,
    previous: str,
    target: str,
    retained_paths: Collection[str],
    *,
    branch_changed_paths: Collection[str] | None = None,
) -> set[str]:
    """Compare resolved bodies even when branch diffs omit upstream blueprint changes.

    Across a rebase (`branch_changed_paths` supplied) upstream blueprint additions that the
    replayed branch never touched are outside the branch contract; retained blueprints stay
    fail-closed. Identical regular-file entries skip snapshot reads entirely.
    """
    configured_paths = _configured_blueprint_paths(
        root, config, previous
    ) | _configured_blueprint_paths(root, config, target)
    paths = (
        configured_paths
        if branch_changed_paths is None
        else _branch_touched_blueprints(
            root, configured_paths, previous, target, branch_changed_paths
        )
    )
    paths.update(retained_paths)
    retained = set(retained_paths)
    before = _tree_entries(root, paths, previous)
    after = _tree_entries(root, paths, target)
    before_modes = {path: mode for path, (mode, _) in before.items()}
    after_modes = {path: mode for path, (mode, _) in after.items()}
    changed: set[str] = set()
    for path in paths:
        previous_entry = before.get(path)
        target_entry = after.get(path)
        if (
            path not in retained
            and target_entry == previous_entry
            and (target_entry is None or target_entry[0] != "120000")
        ):
            continue
        if (
            read_document(root, path, previous, snapshot_modes=before_modes)
            != read_document(root, path, target, snapshot_modes=after_modes)
        ):
            changed.add(path)
    return changed


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


def merge_task_blueprint_anchors(*histories: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for history in histories:
        for path, revisions in history.items():
            merged[path] = tuple(dict.fromkeys((*merged.get(path, ()), *revisions)))
    return merged


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
    blueprint_globs, operational_ledgers = _blueprint_filters(config)
    anchors = normalize_task_blueprint_anchors(retained_anchors)
    if not blueprint_globs and not anchors:
        return TaskBlueprintSelection()

    @cache
    def matches_blueprint(path: str) -> bool:
        return _matches_blueprint(path, blueprint_globs, operational_ledgers)

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
    if blueprint_globs:
        head_changes.update(dict.fromkeys(sorted(changed_task_blueprints(
            repo_root, config, base_sha, head_sha, (),
        ))))
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


def _parse_tree_entries(
    output: str, *, restrict: set[str] | None = None
) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for entry in output.split("\0"):
        metadata, separator, path = entry.partition("\t")
        if not separator or metadata.startswith("040000 "):
            continue
        if restrict is not None and path not in restrict:
            continue
        fields = metadata.split()
        entries[path] = (fields[0], fields[2] if len(fields) > 2 else "")
    return entries


def _tree_entries(
    root: Path, paths: Iterable[str], revision: str | None
) -> dict[str, tuple[str, str]]:
    """Map candidate paths to their snapshot mode and blob object id in one tree query."""
    candidates = sorted(set(paths))
    if revision is None:
        entries: dict[str, tuple[str, str]] = {}
        for path in candidates:
            candidate = repository_path(root, path)
            if candidate.is_file():
                entries[path] = ("120000" if candidate.is_symlink() else "100644", "")
        return entries
    if not candidates:
        return {}
    if len(candidates) > 128:
        output = git_stdout(root, ["git", "ls-tree", "-r", "-z", revision])
        if output is None:
            raise ValueError(f"Cannot inspect architecture documents at {revision}")
        return _parse_tree_entries(output, restrict=set(candidates))
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
    return _parse_tree_entries(output)


def existing_document_paths(
    root: Path,
    paths: Iterable[str],
    revision: str | None,
    *,
    snapshot_modes: dict[str, str] | None = None,
) -> set[str]:
    """Inspect candidates in one tree query; retain modes for safe snapshot reads."""
    entries = _tree_entries(root, paths, revision)
    if snapshot_modes is not None:
        snapshot_modes.update(
            {path: mode for path, (mode, _) in entries.items()}
        )
    return set(entries)


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
