"""Zero-touch self-update, global skill symlink repair, and background cooldown state."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
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
                    if after_sha != before_sha:
                        report["source_status"] = "updated"
                        guard_bin = resolved_home / ".local/bin/guard"
                        if (
                            not explicit
                            and shutil.which("uv")
                            and (guard_bin.exists() or shutil.which("guard"))
                        ):
                            _subprocess_run(
                                ["uv", "tool", "install", "--force", str(checkout)], 60
                            )
                    else:
                        report["source_status"] = "up_to_date"
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
        if checkout is not None and Path(active_repo_root).expanduser().resolve() == checkout:
            return False
    resolved_state = state_dir_for(home_dir=resolved_home, state_dir=state_dir, env=eff_env)
    resolved_gemini = gemini_dir_for(
        home_dir=resolved_home, gemini_dir=gemini_dir, env=eff_env
    )
    metadata_path = resolved_state / "auto_update.json"
    metadata = _load_json_dict(metadata_path)
    try:
        if (
            time.time() - float(metadata.get("last_checked_at") or 0)
            < AUTO_UPDATE_INTERVAL_SECONDS
        ):
            return False
    except (TypeError, ValueError):
        metadata = {}

    guard_cmd = _resolve_guard_command(resolved_home, eff_env)
    with _try_file_lock(resolved_state / "auto_update.lock") as locked:
        if not locked:
            return False
        metadata = _load_json_dict(metadata_path)
        try:
            cooldown = time.time() - float(metadata.get("last_checked_at") or 0)
        except (TypeError, ValueError):
            cooldown = AUTO_UPDATE_INTERVAL_SECONDS
        if cooldown < AUTO_UPDATE_INTERVAL_SECONDS:
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
