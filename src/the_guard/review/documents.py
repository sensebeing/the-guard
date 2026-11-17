"""Architecture document inventory and snapshot reads shared by review consumers."""

from __future__ import annotations

from pathlib import Path

from ..config import GuardConfig, repository_path
from ..git_helpers import git_stdout
from ..policy import _read_policy_file


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
        ),
    }


def is_architecture_contract(path: str, config: GuardConfig) -> bool:
    return path in architecture_document_paths(config) or any(
        path
        in {
            *module.get("boundary_docs", []),
            *(module["path"] + "/" + name for name in ("AGENTS.md", "CLAUDE.md")),
        }
        for module in config.topology.get("modules", [])
    )


def read_document(
    root: Path, path: str, revision: str | None, *, loaded_paths: set[str] | None = None
) -> str | None:
    """Absence is optional; unsafe links, invalid content and unavailable history are errors."""
    if revision is None:
        if not repository_path(root, path).exists():
            return None
    else:
        entry = git_stdout(
            root, ["git", "ls-tree", revision, "--", f":(literal){path}"]
        )
        if entry is None:
            raise ValueError(
                f"Cannot inspect architecture document {path} at {revision}"
            )
        if not entry or entry.startswith("040000 "):
            return None
    return _read_policy_file(root, path, revision=revision, loaded_paths=loaded_paths)
