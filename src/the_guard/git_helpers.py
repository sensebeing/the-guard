"""Read-only Git plumbing for changed files, historical blobs and submodule ownership."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
from typing import Literal


GIT_TIMEOUT_SECONDS = 30


def submodule_paths(repo_root: Path) -> tuple[str, ...]:
    gitmodules = repo_root / ".gitmodules"
    if not gitmodules.is_file():
        return ()
    paths: list[str] = []
    for raw_line in gitmodules.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("path"):
            continue
        _, separator, value = line.partition("=")
        if not separator:
            continue
        path = value.strip()
        if path:
            paths.append(path)
    return tuple(sorted(paths, key=len, reverse=True))


def owning_submodule(path: str, submodules: tuple[str, ...]) -> str | None:
    for submodule in submodules:
        if path == submodule or path.startswith(f"{submodule}/"):
            return submodule
    return None


def resolve_changed_file(root: Path, changed_file: str) -> tuple[str, Path] | None:
    path = Path(changed_file)
    candidate = path if path.is_absolute() else root / path
    absolute_path = candidate.resolve()
    try:
        rel_path = absolute_path.relative_to(root)
    except ValueError:
        return None
    return rel_path.as_posix(), absolute_path


def git_ok(cwd: Path, command: list[str]) -> bool:
    completed = git(cwd, command)
    return completed is not None and completed.returncode == 0


def git_stdout(cwd: Path, command: list[str]) -> str | None:
    completed = git(cwd, command)
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout


def git(cwd: Path, command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            env=dict(os.environ, GIT_NO_LAZY_FETCH="1"),
            check=False,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def imported_submodule_trees(root: Path, base: str) -> dict[str, str]:
    """Map retired gitlinks to their original imported trees, not today's content."""
    revision = git_stdout(root, ["git", "merge-base", base, "HEAD"])
    if not revision:
        revision = git_stdout(root, ["git", "rev-parse", "--verify", f"{base}^{{commit}}"])
    if not revision:
        return {}
    revision = revision.strip()
    entries = git_stdout(root, ["git", "ls-tree", "-r", "-z", revision])
    if entries is None:
        raise RuntimeError("Cannot inspect base gitlinks")
    result: dict[str, str] = {}
    for entry in entries.split("\0"):
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if not separator or fields[:2] != ["160000", "commit"]:
            continue
        current = git_stdout(root, ["git", "ls-tree", "HEAD", "--", path])
        if current is None:
            raise RuntimeError(f"Cannot inspect imported path {path}")
        if current.split()[:2] != ["040000", "tree"]:
            continue
        tree = git_stdout(root, ["git", "rev-parse", "--verify", f"{fields[2]}^{{tree}}"])
        if not tree:
            commits = git_stdout(root, ["git", "rev-list", "--reverse", "--ancestry-path",
                                       f"{revision}..HEAD", "--", path])
            if commits is None:
                raise RuntimeError(f"Cannot inspect import history for {path}")
            for commit in commits.splitlines():
                imported = git_stdout(root, ["git", "ls-tree", commit, "--", path])
                if imported is None:
                    raise RuntimeError(f"Cannot inspect import commit for {path}")
                values = imported.split()
                if values[:2] == ["040000", "tree"]:
                    tree = values[2]
                    break
        if not tree:
            raise RuntimeError(f"Imported baseline unavailable for {path}")
        result[path] = tree.strip()
    return result


@dataclass(frozen=True)
class BaseFileEntry:
    status: Literal["present", "absent", "unavailable"]
    content: str | None = None
    detail: str | None = None


class BaseFileReader:
    """Read base files with proven absence distinct from unavailable Git history.

    Caches belong to one check. The content-only read API remains available to
    callers that do not distinguish absence from unavailable history.
    """

    def __init__(self, root: Path, base: str, submodules: tuple[str, ...]):
        self.root = root
        self.base = base
        self.submodules = submodules
        self._base_revision: str | None = None
        self._base_checked = False
        self._renames: dict[tuple[Path, str], dict[str, str] | None] = {}
        self._entries: dict[str, BaseFileEntry] = {}
        self._imported_trees: dict[str, str] | None = None

    def read(self, path: str) -> str | None:
        return self.read_entry(path).content

    def read_entry(self, path: str) -> BaseFileEntry:
        if path not in self._entries:
            self._entries[path] = self._read_entry(path)
        return self._entries[path]

    def _read_entry(self, path: str) -> BaseFileEntry:
        if not self._base_checked:
            value = git_stdout(self.root, [
                "git", "rev-parse", "--verify", "--quiet", f"{self.base}^{{commit}}",
            ])
            self._base_revision = value.strip() if value else None
            self._base_checked = True
        if self._base_revision is None:
            return BaseFileEntry("unavailable", detail=f"Base commit {self.base!r} is unavailable")
        cwd, revision, inner = self.root, self._base_revision, path
        if self._imported_trees is None:
            try:
                self._imported_trees = imported_submodule_trees(self.root, self.base)
            except RuntimeError as exc:
                return BaseFileEntry("unavailable", detail=str(exc))
        imported = owning_submodule(path, tuple(self._imported_trees))
        if imported is not None:
            revision = self._imported_trees[imported]
            inner = path[len(imported) + 1:]
        submodule = None if imported is not None else owning_submodule(path, self.submodules)
        if submodule is not None:
            tree = git_stdout(self.root, [
                "git", "ls-tree", "--full-tree", "-z", revision, "--", submodule,
            ])
            if tree is None:
                return BaseFileEntry("unavailable", detail=f"Cannot read base gitlink for {submodule}")
            if not tree:
                return BaseFileEntry("absent")
            metadata = tree.split("\t", 1)[0].split()
            if len(metadata) != 3 or metadata[:2] != ["160000", "commit"]:
                return BaseFileEntry("unavailable", detail=f"Base path {submodule} is not a gitlink")
            revision = metadata[2]
            cwd, inner = self.root / submodule, path[len(submodule) + 1:]
            if not (cwd / ".git").exists() or not git_ok(cwd, [
                "git", "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}",
            ]):
                return BaseFileEntry("unavailable", detail=f"Submodule base commit {revision} unavailable in {submodule}")
        tree = git_stdout(cwd, ["git", "ls-tree", "--full-tree", "-z", revision, "--", inner])
        if tree is None:
            return BaseFileEntry("unavailable", detail=f"Cannot inspect base path {path}")
        if not tree:
            key = (cwd, revision)
            if key not in self._renames:
                self._renames[key] = self._rename_sources(
                    cwd, revision, f"HEAD:{imported}" if imported is not None else None,
                )
            renames = self._renames[key]
            if renames is None:
                return BaseFileEntry("unavailable", detail=f"Cannot inspect base renames for {path}")
            previous = renames.get(inner)
            if previous is None:
                return BaseFileEntry("absent")
            inner = previous
        content = git_stdout(cwd, ["git", "show", f"{revision}:{inner}"])
        if content is None:
            return BaseFileEntry("unavailable", detail=f"Cannot read base blob for {path}")
        return BaseFileEntry("present", content=content)

    @staticmethod
    def _rename_sources(cwd: Path, revision: str, current: str | None = None) -> dict[str, str] | None:
        command = ["git", "diff", "--find-renames", "--diff-filter=R", "--name-status", "-z", revision]
        if current is not None:
            command.append(current)
        output = git_stdout(cwd, command)
        if output is None:
            return None
        if not output:
            return {}
        fields = output.rstrip("\0").split("\0")
        renames: dict[str, str] = {}
        for offset in range(0, len(fields), 3):
            entry = fields[offset:offset + 3]
            if len(entry) != 3 or not entry[0].startswith("R"):
                return None
            _, old, new = entry
            renames[new] = old
        return renames
