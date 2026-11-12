"""Zero-touch self-update, global skill symlink repair, and background cooldown state."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterator

from . import __version__

AUTO_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60
SKILL_NAMES = ("the-guard", "the-guard-setup")
PACKAGE_DIR = Path(__file__).resolve().parent


def compute_package_tree_digest(package_dir: Path) -> str:
    """Hash the executable Python/preset tree deterministically."""
    digest = hashlib.sha256()
    root = Path(package_dir)
    paths = sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and (path.suffix == ".py" or "presets" in path.parts)
    )
    for path in paths:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _installed_guard_package_dir(home_dir: Path) -> Path | None:
    root = Path(home_dir) / ".local/share/uv/tools/the-guard"
    matches = sorted(root.glob("lib/python*/site-packages/the_guard"))
    return matches[0] if matches and matches[0].is_dir() else None


def state_dir_for(
    home_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    eff_env = os.environ if env is None else env
    if state_dir is not None:
        return Path(state_dir).expanduser().resolve()
    configured = eff_env.get("THE_GUARD_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    base_home = Path(home_dir).expanduser().resolve() if home_dir else Path.home()
    return base_home / ".guard"


def gemini_dir_for(
    home_dir: str | Path | None = None,
    gemini_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    eff_env = os.environ if env is None else env
    if gemini_dir is not None:
        return Path(gemini_dir).expanduser().resolve()
    configured = eff_env.get("THE_GUARD_GEMINI_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    base_home = Path(home_dir).expanduser().resolve() if home_dir else Path.home()
    return base_home / ".gemini"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _load_json_dict(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _append_log(state_dir: Path, event: str, **fields: Any) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event,
        **fields,
    }
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "auto_update.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


@contextmanager
def _try_file_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _subprocess_run(command: list[str], timeout: int) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout or result.stderr


def _is_same_git_repo(path_a: Path, path_b: Path) -> bool:
    """Treat linked worktrees sharing one git common dir as the same checkout."""
    try:
        if path_a.expanduser().resolve() == path_b.expanduser().resolve():
            return True
        common_a = _subprocess_run(["git", "-C", str(path_a), "rev-parse", "--path-format=absolute", "--git-common-dir"], 20).strip()
        common_b = _subprocess_run(["git", "-C", str(path_b), "rev-parse", "--path-format=absolute", "--git-common-dir"], 20).strip()
        return bool(common_a and common_b) and Path(common_a).resolve() == Path(common_b).resolve()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _discover_checkout(
    explicit: str | Path | None,
    home_dir: Path,
) -> tuple[Path | None, bool]:
    if explicit is not None and str(explicit).strip():
        path = Path(explicit).expanduser().resolve()
        if not (path / ".git").exists():
            raise RuntimeError(f"Checkout directory is not a Git repository: {path}")
        return path, True
    candidates = [PACKAGE_DIR.parents[1], home_dir / "the-guard"]
    for candidate in candidates:
        if (candidate / ".git").exists() and (candidate / "pyproject.toml").is_file():
            return candidate.resolve(), False
    return None, False


def sync_global_skills(
    checkout: Path,
    *,
    home_dir: Path,
    gemini_dir: Path,
) -> list[str]:
    synced: list[str] = []
    parents = (
        home_dir / ".agents/skills",
        home_dir / ".claude/skills",
        gemini_dir / "config/skills",
    )
    for skill_name in SKILL_NAMES:
        source = checkout / "skills" / skill_name
        if not source.exists():
            continue
        for parent in parents:
            target = parent / skill_name
            if not parent.is_dir() or (target.exists() and not target.is_symlink()):
                continue
            if target.is_symlink() or not target.exists():
                target.unlink(missing_ok=True)
                target.symlink_to(source)
                synced.append(str(target))
    return synced


def load_auto_update_status(
    *,
    home_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    resolved_state = state_dir_for(home_dir=home_dir, state_dir=state_dir, env=env)
    metadata_path = resolved_state / "auto_update.json"
    if not metadata_path.is_file():
        return None
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "failed", "error": str(exc)}
    return value if isinstance(value, dict) else {"status": "failed", "error": "invalid metadata"}


def guard_status(
    *,
    home_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    gemini_dir: str | Path | None = None,
    checkout_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    resolved_home = Path(home_dir).expanduser().resolve() if home_dir else Path.home()
    resolved_state = state_dir_for(home_dir=resolved_home, state_dir=state_dir, env=env)
    resolved_gemini = gemini_dir_for(home_dir=resolved_home, gemini_dir=gemini_dir, env=env)
    checkout: Path | None = None
    try:
        checkout, _ = _discover_checkout(checkout_dir, resolved_home)
    except RuntimeError:
        checkout = None
    skill_links: dict[str, str] = {}
    for parent in (
        resolved_home / ".agents/skills",
        resolved_home / ".claude/skills",
        resolved_gemini / "config/skills",
    ):
        for skill_name in SKILL_NAMES:
            target = parent / skill_name
            if target.is_symlink():
                skill_links[str(target)] = str(target.readlink())
    report: dict[str, Any] = {
        "version": __version__,
        "state_dir": str(resolved_state),
        "checkout": str(checkout) if checkout is not None else None,
        "guard_bin": shutil.which("guard") or "",
        "skill_links": skill_links,
        "auto_update": load_auto_update_status(
            home_dir=resolved_home, state_dir=resolved_state, env=env
        ),
    }
    return report


def self_update(
    *,
    checkout_dir: str | Path | None = None,
    home_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    gemini_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    resolved_home = Path(home_dir).expanduser().resolve() if home_dir else Path.home()
    resolved_state = state_dir_for(home_dir=resolved_home, state_dir=state_dir, env=env)
    resolved_gemini = gemini_dir_for(home_dir=resolved_home, gemini_dir=gemini_dir, env=env)
    resolved_state.mkdir(parents=True, exist_ok=True)
    metadata_path = resolved_state / "auto_update.json"
    existing = _load_json_dict(metadata_path)
    report: dict[str, Any] = {
        "last_checked_at": time.time(),
        "last_updated_at": existing.get("last_updated_at"),
        "status": "failed",
        "source_status": "failed",
        "head_sha": existing.get("head_sha"),
        "source_sha": existing.get("source_sha"),
        "source_tree_digest": existing.get("source_tree_digest"),
        "installed_sha": existing.get("installed_sha"),
        "installed_tree_digest": existing.get("installed_tree_digest"),
        "cli_status": "skipped",
        "synced_skills": [],
        "error": None,
    }
    with _try_file_lock(resolved_state / "auto_update.lock") as locked:
        if not locked:
            locked_report = {
                **report,
                "status": "skipped_locked",
                "source_status": "skipped_locked",
            }
            _append_log(resolved_state, "auto_update_skipped_locked", **locked_report)
            return locked_report
        try:
            checkout, explicit = _discover_checkout(checkout_dir, resolved_home)
            if checkout is not None:
                dirty = _subprocess_run(
                    ["git", "-C", str(checkout), "status", "--porcelain"], 20
                )
                branch = _subprocess_run(
                    ["git", "-C", str(checkout), "branch", "--show-current"], 20
                ).strip()
                if dirty.strip():
                    report["source_status"] = "skipped_dirty_checkout"
                elif branch != "main":
                    report["source_status"] = "skipped_non_main_branch"
                else:
                    before_sha = _subprocess_run(
                        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20
                    ).strip()
                    _subprocess_run(
                        ["git", "-C", str(checkout), "pull", "--ff-only"], 30
                    )
                    after_sha = _subprocess_run(
                        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20
                    ).strip()
                    report["source_status"] = "updated" if after_sha != before_sha else "up_to_date"
                    checkout_digest = compute_package_tree_digest(checkout / "src/the_guard")
                    report["source_sha"] = after_sha
                    report["source_tree_digest"] = checkout_digest
                    installed_dir = _installed_guard_package_dir(resolved_home)
                    installed_digest = (
                        compute_package_tree_digest(installed_dir) if installed_dir else None
                    )
                    needs_reinstall = (
                        after_sha != before_sha
                        or existing.get("installed_sha") != after_sha
                        or existing.get("installed_tree_digest") != checkout_digest
                        or (installed_digest is not None and installed_digest != checkout_digest)
                    )
                    guard_bin = resolved_home / ".local/bin/guard"
                    is_real_home = resolved_home == Path.home().resolve()
                    has_installed_guard = guard_bin.exists() or (is_real_home and bool(shutil.which("guard")))
                    manages_uv_tool = not explicit and bool(shutil.which("uv")) and has_installed_guard
                    reinstalled = False
                    if (
                        needs_reinstall and not explicit and shutil.which("uv")
                        and has_installed_guard
                    ):
                        _subprocess_run(["uv", "tool", "install", "--force", str(checkout)], 60)
                        report["cli_status"] = "reinstalled"
                        reinstalled = True
                    elif not needs_reinstall:
                        report["cli_status"] = "up_to_date"
                    if reinstalled or installed_digest == checkout_digest:
                        report["installed_sha"] = after_sha
                        report["installed_tree_digest"] = checkout_digest
                report["head_sha"] = _subprocess_run(
                    ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20
                ).strip()
                report["synced_skills"] = sync_global_skills(
                    checkout,
                    home_dir=resolved_home,
                    gemini_dir=resolved_gemini,
                )
            elif not explicit and shutil.which("uv"):
                uv_out = _subprocess_run(["uv", "tool", "upgrade", "the-guard"], 60)
                report["source_status"] = (
                    "up_to_date"
                    if "nothing to upgrade" in uv_out.lower()
                    else "updated"
                )
            else:
                report["source_status"] = "skipped_no_source"

            report["last_updated_at"] = time.time()
            if report["source_status"] in {
                "updated",
                "up_to_date",
                "skipped_dirty_checkout",
                "skipped_non_main_branch",
                "skipped_no_source",
            }:
                report["status"] = report["source_status"]
        except Exception as exc:
            report["error"] = str(exc)
            report["status"] = "failed"
        atomic_write_text(
            metadata_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        _append_log(
            resolved_state,
            "auto_update_failed"
            if report["status"] == "failed"
            else "auto_update_completed",
            **report,
        )
        if report["status"] == "failed":
            raise RuntimeError(str(report["error"]))
    return report


def _resolve_guard_command(
    home_dir: Path,
    env: dict[str, str],
) -> list[str]:
    configured = env.get("THE_GUARD_BIN", "").strip()
    if configured:
        return [configured]
    which_guard = shutil.which("guard")
    if which_guard:
        return [which_guard]
    local_bin = home_dir / ".local/bin/guard"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return [str(local_bin)]
    return [sys.executable, "-m", "the_guard.cli"]


def maybe_trigger_background_auto_update(
    *,
    active_repo_root: str | Path | None = None,
    home_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    gemini_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    force_local_sync: bool = False,
) -> bool:
    eff_env = os.environ if env is None else env
    if (
        eff_env.get("THE_GUARD_AUTO_UPDATE", "").strip() == "0"
        or eff_env.get("THE_GUARD_DISABLE_AUTO_UPDATE", "").strip() == "1"
        or eff_env.get("THE_GUARD_REVIEW_ACTIVE", "").strip() == "1"
        or eff_env.get("THE_GUARD_SESSION_MODE", "").strip() == "readonly"
    ):
        return False
    in_test_harness = (
        "PYTEST_CURRENT_TEST" in eff_env
        or "unittest" in sys.modules
        or "pytest" in sys.modules
    )
    if (
        in_test_harness
        and state_dir is None
        and eff_env.get("THE_GUARD_AUTO_UPDATE", "").strip() != "1"
    ):
        return False

    resolved_home = Path(home_dir).expanduser().resolve() if home_dir else Path.home()
    if active_repo_root is not None:
        checkout, _ = _discover_checkout(None, resolved_home)
        if checkout is not None and _is_same_git_repo(Path(active_repo_root), checkout):
            return False
    resolved_state = state_dir_for(home_dir=resolved_home, state_dir=state_dir, env=eff_env)
    resolved_gemini = gemini_dir_for(
        home_dir=resolved_home, gemini_dir=gemini_dir, env=eff_env
    )
    metadata_path = resolved_state / "auto_update.json"
    metadata = _load_json_dict(metadata_path)
    try:
        cooldown = time.time() - float(metadata.get("last_checked_at") or 0)
    except (TypeError, ValueError):
        cooldown = AUTO_UPDATE_INTERVAL_SECONDS
    local_sync_needed = False
    checkout = (resolved_home / "the-guard").resolve()
    if not (checkout / ".git").exists():
        checkout = None
    if checkout is not None:
        try:
            clean_main = (
                _subprocess_run(["git", "-C", str(checkout), "branch", "--show-current"], 20).strip() == "main"
                and not _subprocess_run(["git", "-C", str(checkout), "status", "--porcelain"], 20).strip()
            )
            checkout_sha = _subprocess_run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], 20).strip()
            local_digest = compute_package_tree_digest(checkout / "src/the_guard")
            installed_dir = _installed_guard_package_dir(resolved_home)
            installed_digest = compute_package_tree_digest(installed_dir) if installed_dir else metadata.get("installed_tree_digest")
            guard_bin = resolved_home / ".local/bin/guard"
            managed_install = guard_bin.exists() or (resolved_home == Path.home().resolve() and bool(shutil.which("guard")))
            baseline_sha = metadata.get("installed_sha") if managed_install else metadata.get("source_sha")
            baseline_digest = installed_digest if managed_install else metadata.get("source_tree_digest")
            local_sync_needed = clean_main and (
                checkout_sha != baseline_sha or local_digest != baseline_digest
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            local_sync_needed = False
    if cooldown < 30:
        return False
    if not force_local_sync and not local_sync_needed and cooldown < AUTO_UPDATE_INTERVAL_SECONDS:
        return False

    guard_cmd = _resolve_guard_command(resolved_home, eff_env)
    with _try_file_lock(resolved_state / "auto_update.lock") as locked:
        if not locked:
            return False
        metadata = _load_json_dict(metadata_path)
        try:
            cooldown = time.time() - float(metadata.get("last_checked_at") or 0)
        except (TypeError, ValueError):
            cooldown = AUTO_UPDATE_INTERVAL_SECONDS
        if cooldown < 30:
            return False
        if not force_local_sync and not local_sync_needed and cooldown < AUTO_UPDATE_INTERVAL_SECONDS:
            return False
        cmd = [
            *guard_cmd,
            "self-update",
            "--quiet",
            "--home-dir",
            str(resolved_home),
            "--state-dir",
            str(resolved_state),
            "--gemini-dir",
            str(resolved_gemini),
        ]
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            metadata["status"] = "failed"
            metadata["error"] = str(exc)
            metadata["last_failed_at"] = time.time()
            atomic_write_text(
                metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
            )
            _append_log(
                resolved_state,
                "auto_update_spawn_failed",
                command=cmd,
                error=str(exc),
            )
            return False
        metadata["last_checked_at"] = time.time()
        atomic_write_text(
            metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
        )
        _append_log(resolved_state, "auto_update_spawned", command=cmd)
        return True


def maybe_delegate_to_checkout(
    argv: list[str], *, active_repo_root: str | Path | None = None,
    home_dir: str | Path | None = None, state_dir: str | Path | None = None,
    gemini_dir: str | Path | None = None, env: dict[str, str] | None = None,
) -> int | None:
    eff_env = dict(os.environ if env is None else env)
    if not argv or argv[0] == "self-update" or any(
        eff_env.get(key) == value for key, value in (
            ("THE_GUARD_DELEGATED", "1"), ("THE_GUARD_DISABLE_AUTO_UPDATE", "1"),
            ("THE_GUARD_AUTO_UPDATE", "0"), ("THE_GUARD_REVIEW_ACTIVE", "1"),
            ("THE_GUARD_SESSION_MODE", "readonly"),
        )
    ) or "--checkout-dir" in argv or any("--checkout-dir=" in x for x in argv):
        return None
    if ("PYTEST_CURRENT_TEST" in eff_env or "unittest" in sys.modules or "pytest" in sys.modules) and eff_env.get("THE_GUARD_AUTO_UPDATE") != "1":
        return None
    resolved_home = Path(home_dir).expanduser().resolve() if home_dir else Path.home()
    checkout = resolved_home / "the-guard"
    if not (checkout / "src/the_guard").is_dir():
        return None
    if active_repo_root is not None and _is_same_git_repo(Path(active_repo_root), checkout):
        return None
    try:
        if _subprocess_run(["git", "-C", str(checkout), "branch", "--show-current"], 20).strip() != "main":
            return None
        if _subprocess_run(["git", "-C", str(checkout), "status", "--porcelain"], 20).strip():
            return None
        checkout_sha = _subprocess_run(["git", "-C", str(checkout), "rev-parse", "HEAD"], 20).strip()
        metadata = _load_json_dict(state_dir_for(home_dir=resolved_home, state_dir=state_dir, env=eff_env) / "auto_update.json")
        baseline_sha = metadata.get("installed_sha") or metadata.get("source_sha")
        if not baseline_sha:
            current_repo = PACKAGE_DIR.parents[1]
            if (current_repo / ".git").exists():
                baseline_sha = _subprocess_run(["git", "-C", str(current_repo), "rev-parse", "HEAD"], 20).strip()
        if not baseline_sha:
            return None
        if baseline_sha:
            ancestry = subprocess.run(
                ["git", "-C", str(checkout), "merge-base", "--is-ancestor", str(baseline_sha), checkout_sha],
                check=False,
                capture_output=True,
            )
            if ancestry.returncode != 0 or checkout_sha == baseline_sha:
                return None
        if _is_same_git_repo(PACKAGE_DIR.parents[1], checkout) or compute_package_tree_digest(checkout / "src/the_guard") == compute_package_tree_digest(PACKAGE_DIR):
            return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    maybe_trigger_background_auto_update(active_repo_root=active_repo_root, home_dir=resolved_home, state_dir=state_dir, gemini_dir=gemini_dir, env=eff_env, force_local_sync=True)
    child_env = {**eff_env, "PYTHONPATH": os.pathsep.join(filter(None, [str(checkout / "src"), eff_env.get("PYTHONPATH", "")])), "THE_GUARD_DELEGATED": "1"}
    return subprocess.run([sys.executable, "-m", "the_guard.cli", *argv], env=child_env, check=False).returncode
