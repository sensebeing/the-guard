"""Read-only Git plumbing for changed files, historical blobs and submodule ownership."""

from __future__ import annotations

import os
import difflib
import posixpath
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

GIT_TIMEOUT_SECONDS = 30


def submodule_paths(repo_root: Path) -> tuple[str, ...]:
    gitmodules = repo_root / ".gitmodules"
    if not gitmodules.is_file():
        return ()
    paths: list[str] = []
    for raw_line in gitmodules.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
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
        revision = git_stdout(
            root, ["git", "rev-parse", "--verify", f"{base}^{{commit}}"]
        )
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
        tree = git_stdout(
            root, ["git", "rev-parse", "--verify", f"{fields[2]}^{{tree}}"]
        )
        if not tree:
            commits = git_stdout(
                root,
                [
                    "git",
                    "rev-list",
                    "--reverse",
                    "--ancestry-path",
                    f"{revision}..HEAD",
                    "--",
                    path,
                ],
            )
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

    def __init__(self, root: Path, base: str, submodules: tuple[str, ...] = ()):
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
            value = git_stdout(
                self.root,
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{self.base}^{{commit}}",
                ],
            )
            self._base_revision = value.strip() if value else None
            self._base_checked = True
        if self._base_revision is None:
            return BaseFileEntry(
                "unavailable", detail=f"Base commit {self.base!r} is unavailable"
            )
        cwd, revision, inner = self.root, self._base_revision, path
        if self._imported_trees is None:
            try:
                self._imported_trees = imported_submodule_trees(self.root, self.base)
            except RuntimeError as exc:
                return BaseFileEntry("unavailable", detail=str(exc))
        imported = owning_submodule(path, tuple(self._imported_trees))
        if imported is not None:
            revision = self._imported_trees[imported]
            inner = path[len(imported) + 1 :]
        submodule = (
            None if imported is not None else owning_submodule(path, self.submodules)
        )
        if submodule is not None:
            tree = git_stdout(
                self.root,
                [
                    "git",
                    "ls-tree",
                    "--full-tree",
                    "-z",
                    revision,
                    "--",
                    submodule,
                ],
            )
            if tree is None:
                return BaseFileEntry(
                    "unavailable", detail=f"Cannot read base gitlink for {submodule}"
                )
            if not tree:
                return BaseFileEntry("absent")
            metadata = tree.split("\t", 1)[0].split()
            if len(metadata) != 3 or metadata[:2] != ["160000", "commit"]:
                return BaseFileEntry(
                    "unavailable", detail=f"Base path {submodule} is not a gitlink"
                )
            revision = metadata[2]
            cwd, inner = self.root / submodule, path[len(submodule) + 1 :]
            if not (cwd / ".git").exists() or not git_ok(
                cwd,
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{revision}^{{commit}}",
                ],
            ):
                return BaseFileEntry(
                    "unavailable",
                    detail=f"Submodule base commit {revision} unavailable in {submodule}",
                )
        tree = git_stdout(
            cwd, ["git", "ls-tree", "--full-tree", "-z", revision, "--", inner]
        )
        if tree is None:
            return BaseFileEntry(
                "unavailable", detail=f"Cannot inspect base path {path}"
            )
        if not tree:
            key = (cwd, revision)
            if key not in self._renames:
                self._renames[key] = self._rename_sources(
                    cwd,
                    revision,
                    f"HEAD:{imported}" if imported is not None else None,
                )
            renames = self._renames[key]
            if renames is None:
                return BaseFileEntry(
                    "unavailable", detail=f"Cannot inspect base renames for {path}"
                )
            previous = renames.get(inner)
            if previous is None:
                return BaseFileEntry("absent")
            inner = previous
        content = git_stdout(cwd, ["git", "show", f"{revision}:{inner}"])
        if content is None:
            return BaseFileEntry(
                "unavailable", detail=f"Cannot read base blob for {path}"
            )
        return BaseFileEntry("present", content=content)

    @staticmethod
    def _rename_sources(
        cwd: Path, revision: str, current: str | None = None
    ) -> dict[str, str] | None:
        command = [
            "git",
            "diff",
            "--find-renames",
            "--diff-filter=R",
            "--name-status",
            "-z",
            revision,
        ]
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
            entry = fields[offset : offset + 3]
            if len(entry) != 3 or not entry[0].startswith("R"):
                return None
            _, old, new = entry
            renames[new] = old
        return renames


def resolve_changed_base(
    root: Path, base: str | None = None, *, main_branch: str = "main"
) -> str:
    candidates = (
        (base,) if base is not None else (f"origin/{main_branch}", main_branch, "HEAD")
    )
    for candidate in candidates:
        if git_ok(
            root, ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"]
        ):
            return candidate
    raise ValueError(
        f"Invalid Git base reference: {base if base is not None else 'HEAD'}"
    )


def changed_files(
    root: Path,
    base: str | None = None,
    *,
    main_branch: str = "main",
    head: str | None = None,
) -> list[str]:
    reference = resolve_changed_base(root, base, main_branch=main_branch)
    outputs = []
    commands = [
        [
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            "-z",
            reference,
            *([head] if head is not None else []),
            "--",
        ]
    ]
    if head is None:
        commands.append(["ls-files", "--others", "--exclude-standard", "-z"])
    for args in commands:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        outputs.append(result.stdout)
    return sorted(
        {
            path.decode("utf-8")
            for output in outputs
            for path in output.split(b"\0")
            if path
        }
    )


def read_repository_file(
    repo_root: Path,
    rel_path: str,
    *,
    revision: str | None,
    _seen: frozenset[str] = frozenset(),
    loaded_paths: set[str] | None = None,
    snapshot_modes: dict[str, str] | None = None,
) -> str:
    if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
        raise ValueError(f"Unsafe policy path: {rel_path}")
    if rel_path in _seen:
        raise ValueError(f"Cyclic policy symlink: {rel_path}")
    if loaded_paths is not None:
        loaded_paths.add(rel_path)
    if revision is not None:
        try:
            mode = (snapshot_modes or {}).get(rel_path)
            if mode is None:
                entry = subprocess.run(
                    ["git", "ls-tree", revision, "--", f":(literal){rel_path}"],
                    cwd=repo_root,
                    capture_output=True,
                    check=True,
                    timeout=15,
                )
                mode = entry.stdout.split(b" ", 1)[0].decode("ascii")
            if mode == "120000":
                link = subprocess.run(
                    ["git", "show", f"{revision}:{rel_path}"],
                    cwd=repo_root,
                    capture_output=True,
                    check=True,
                    timeout=15,
                ).stdout.decode("utf-8")
                if posixpath.isabs(link):
                    raise ValueError(f"Policy symlink escapes repository: {rel_path}")
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(rel_path), link)
                )
                return read_repository_file(
                    repo_root,
                    target,
                    revision=revision,
                    _seen=_seen | {rel_path},
                    loaded_paths=loaded_paths,
                    snapshot_modes=snapshot_modes,
                )
            proc = subprocess.run(
                ["git", "show", f"{revision}:{rel_path}"],
                cwd=repo_root,
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(
                f"Cannot read {rel_path} at revision {revision}: {exc}"
            ) from exc
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(
                f"Rule or policy file does not exist at revision {revision}: {rel_path} ({err})"
            )
        try:
            return proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Invalid UTF-8 in {rel_path} at revision {revision}: {exc}"
            ) from exc

    try:
        target = repo_root / rel_path
        if not target.resolve().is_relative_to(repo_root.resolve()):
            raise ValueError(f"Path escapes repository: {rel_path}")
    except (ValueError, RuntimeError) as exc:
        raise ValueError(str(exc)) from exc
    if not target.is_file():
        raise ValueError(f"Policy or rule details file does not exist: {rel_path}")
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Cannot read policy file {target}: {exc}") from exc


def compute_changed_lines(base_text: str, current_text: str) -> set[int]:
    base_lines = base_text.splitlines()
    current_lines = current_text.splitlines()
    matcher = difflib.SequenceMatcher(None, base_lines, current_lines)
    changed: set[int] = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "insert"}:
            changed.update(range(j1 + 1, j2 + 1))
    return changed


_compute_changed_lines = compute_changed_lines
