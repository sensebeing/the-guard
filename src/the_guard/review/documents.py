"""Architecture document inventory and snapshot reads shared by review consumers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from ..config import GuardConfig, repository_path
from ..git_helpers import git_stdout
from ..git_helpers import read_repository_file
from .contract import CODE_EXTENSIONS


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
