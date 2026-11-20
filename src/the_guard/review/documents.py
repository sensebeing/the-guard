"""Architecture document inventory and snapshot reads shared by review consumers."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

from ..config import GuardConfig, repository_path
from ..git_helpers import git_stdout
from ..git_helpers import read_repository_file
from .contract import CODE_EXTENSIONS

MAX_TASK_DESIGN_DOCS = 4
_OPERATIONAL_LEDGERS = {"WORKING.md", "CHANGELOG.md", "docs/pending-prod-publish.md"}
_TASK_DESIGN_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])"
    r"(?:(?:\.\.?/)+[A-Za-z0-9_./-]+|docs/[A-Za-z0-9_./-]+"
    r"|[A-Za-z0-9_./-]*(?:plan|spec|blueprint|audit|report|design)[A-Za-z0-9_./-]*)\.md"
    r"(?![\w/-]|\.[\w.])"
)


def architecture_document_paths(config: GuardConfig) -> set[str]:
    return {
        config.manifest_file,
        "ARCHITECTURE.md",
        "docs/architecture.md",
        *config.topology.get("architecture_docs", []),
    }


def boundary_document_paths(module: dict) -> set[str]:
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


@dataclass(frozen=True)
class TaskBlueprintSelection:
    """Bounded prompt previews and the snapshots to retain across reviews."""

    previews: list[TaskBlueprintSnapshot]
    anchor_snapshots: list[TaskBlueprintSnapshot]


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
    initial_reviewed_sha: str = "",
    prior_reviewed_sha: str = "",
    retained_anchors: Mapping[str, str] | None = None,
    reviewer_note: str = "",
    excluded_paths: Iterable[str] = (),
) -> TaskBlueprintSelection:
    """Resolve changed Markdown and direct references against binding Git snapshots."""
    anchors = retained_anchors or {}
    baselines = {base_sha: None}
    for revision in dict.fromkeys((initial_reviewed_sha, prior_reviewed_sha)):
        if not revision or revision in baselines:
            continue
        if revision == head_sha:
            baselines[head_sha] = None
            continue
        commit = git_stdout(
            repo_root,
            ["git", "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        )
        if commit:
            baselines[commit.strip()] = None

    excluded = _OPERATIONAL_LEDGERS | (set(excluded_paths) - anchors.keys())
    # A plan introduced in a reviewed round and later removed is absent from the
    # current branch diff. Use each reviewed branch's change list so an upstream
    # deletion during a rebase is not attributed to the reviewed branch.
    historical_changes = []
    for revision in baselines:
        if revision in (base_sha, head_sha):
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
    changed_candidates = dict.fromkeys(
        path for path in (*head_changes, *historical_changes, *anchors)
        if path.endswith(".md") and path not in excluded
    )

    modes_by_revision: dict[str, dict[str, str]] = {
        revision: {} for revision in dict.fromkeys((*baselines, head_sha, *anchors.values()))
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
            if path in modes_by_revision[base_sha]:
                baseline_sha = base_sha
            elif path in anchors:
                baseline_sha = anchors[path]
                if path not in modes_by_revision[baseline_sha]:
                    raise ValueError(f"Recorded task blueprint missing at {baseline_sha}: {path}")
            else:
                baseline_sha = next(
                    (rev for rev in baselines if path in modes_by_revision[rev]), None
                )
            head_body = read_body(path, head_sha)
            baseline_body = read_body(path, baseline_sha) if baseline_sha is not None else None
            if head_body is None and baseline_body is None:
                continue
            snapshots.append(
                TaskBlueprintSnapshot(
                    path, baseline_sha, baseline_body, head_body,
                    follows_symlink=any(
                        modes_by_revision[rev].get(path) == "120000"
                        for rev in (baseline_sha, head_sha)
                        if rev is not None
                    ),
                    in_merge_base=baseline_sha is not None and baseline_sha == base_sha,
                )
            )
        return snapshots

    inventory(changed_candidates)
    changed_docs = resolve_candidates(changed_candidates)
    reference_candidates = {}
    for path in changed_candidates:
        for revision in modes_by_revision:
            body = read_body(path, revision)
            if body is not None:
                reference_candidates.update(dict.fromkeys(_task_design_references(body, path)))
    reference_candidates.update(dict.fromkeys(_task_design_references(reviewer_note, "")))
    inventory({
        path for candidates in reference_candidates for path in candidates
        if path not in changed_candidates
    })
    existing_paths = set(anchors).union(
        *(modes_by_revision[revision] for revision in (*baselines, head_sha))
    )
    references = {}
    for candidates in reference_candidates:
        reference = next((path for path in candidates if path in existing_paths), None)
        if reference is not None and reference not in excluded:
            references[reference] = None
    changed_paths = {snapshot.path for snapshot in changed_docs}
    referenced_docs = resolve_candidates(path for path in references if path not in changed_paths)

    def preview_priority(snapshot: TaskBlueprintSnapshot) -> int:
        if not snapshot.in_merge_base:
            return 2
        if (
            snapshot.path not in head_changes
            and snapshot.baseline_body == snapshot.head_body
            and (snapshot.path in references or snapshot.path in anchors)
        ):
            return 0
        return 1

    ordered = sorted((*changed_docs, *referenced_docs), key=preview_priority)
    previews = ordered[:MAX_TASK_DESIGN_DOCS]
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
