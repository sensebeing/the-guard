"""Architecture document inventory and snapshot reads shared by review consumers."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..config import GuardConfig, repository_path
from ..git_helpers import git_stdout
from ..git_helpers import read_repository_file
from .contract import CODE_EXTENSIONS

MAX_TASK_DESIGN_DOCS = 4
_OPERATIONAL_LEDGERS = {"WORKING.md", "CHANGELOG.md", "docs/pending-prod-publish.md"}
_TASK_DESIGN_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:\./)?"
    r"(?:docs/[A-Za-z0-9_./-]+|[A-Za-z0-9_./-]*(?:plan|spec|blueprint|audit|report|design)[A-Za-z0-9_./-]*)\.md"
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


def resolve_task_design_blueprints(
    repo_root: Path,
    changed_files: Iterable[str],
    *,
    base_sha: str,
    head_sha: str,
    initial_reviewed_sha: str = "",
    prior_reviewed_sha: str = "",
    reviewer_note: str = "",
    excluded_paths: Iterable[str] = (),
) -> list[TaskBlueprintSnapshot]:
    """Resolve changed Markdown and direct references against binding Git snapshots."""
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

    excluded = _OPERATIONAL_LEDGERS.union(excluded_paths)
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
    changed_candidates = dict.fromkeys(
        path for path in (*changed_files, *historical_changes)
        if path.endswith(".md") and path not in excluded
    )

    def resolve_candidates(
        paths: Collection[str], limit: int
    ) -> list[TaskBlueprintSnapshot]:
        modes_by_revision: dict[str, dict[str, str]] = {}
        for revision in dict.fromkeys((*baselines, head_sha)):
            modes: dict[str, str] = {}
            existing_document_paths(repo_root, paths, revision, snapshot_modes=modes)
            modes_by_revision[revision] = modes

        snapshots = []
        for path in paths:
            baseline_sha = next(
                (rev for rev in baselines if path in modes_by_revision[rev]), None
            )
            head_body = read_document(
                repo_root, path, head_sha, snapshot_modes=modes_by_revision[head_sha]
            )
            baseline_body = None
            if baseline_sha == head_sha:
                baseline_body = head_body
            elif baseline_sha is not None:
                baseline_body = read_document(
                    repo_root,
                    path,
                    baseline_sha,
                    snapshot_modes=modes_by_revision[baseline_sha],
                )
            if head_body is None and baseline_body is None:
                continue
            snapshots.append(
                TaskBlueprintSnapshot(path, baseline_sha, baseline_body, head_body)
            )
            if len(snapshots) == limit:
                break
        return snapshots

    changed_docs = resolve_candidates(changed_candidates, MAX_TASK_DESIGN_DOCS)
    reference_slots = MAX_TASK_DESIGN_DOCS - len(changed_docs)
    if not reference_slots:
        return changed_docs

    bodies = [
        body
        for snapshot in changed_docs
        for body in (snapshot.baseline_body, snapshot.head_body)
        if body is not None
    ]
    changed_paths = {snapshot.path for snapshot in changed_docs}
    references = {}
    for body in (*bodies, reviewer_note):
        for match in _TASK_DESIGN_REFERENCE.finditer(body):
            path = PurePosixPath(match.group())
            if path.is_absolute() or ".." in path.parts:
                continue
            reference = path.as_posix()
            if reference not in excluded and reference not in changed_paths:
                references[reference] = None
    return changed_docs + resolve_candidates(references, reference_slots)


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
