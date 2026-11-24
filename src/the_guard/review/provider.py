"""Discovery and ordering of the local agent CLIs that can serve as architecture reviewers."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contract import (
    CODEX_DISABLED_FEATURES,
    CODEX_ISOLATED_HOMES_DIRNAME,
    CODEX_RELATIVE_PATH_CONFIG_KEYS,
    CODEX_ROUTING_CONFIG_KEYS,
    CONFIG_SCHEMA,
    DEFAULT_PROVIDER_COOLDOWN_SECONDS,
    DEFAULT_REVIEW_EFFORT,
    PROVIDERS,
)
from .repository import git_common_dir
from .session import MAX_RETAINED_SESSIONS, worktree_session_key

ISOLATED_CODEX_HOME_MAX_AGE_SECONDS = 24 * 3600


def _config_file_path(repo_root: Path, env: dict[str, str] | None = None) -> Path:
    eff_env = dict(os.environ if env is None else env)
    override = eff_env.get("THE_GUARD_REVIEW_CONFIG", "").strip()
    if override:
        return Path(override)
    home = Path(eff_env.get("HOME", str(Path.home())))
    candidate = home / ".config" / "the-guard" / "review-agent.json"
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        return git_common_dir(repo_root) / "the-guard-review-agent.json"


def _find_provider_binary(provider: str, env: dict[str, str]) -> str | None:
    path_var = env.get("PATH", "")
    home = Path(env.get("HOME", str(Path.home())))
    extra = [str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    search_path = (
        os.pathsep.join([path_var, *extra]) if path_var else os.pathsep.join(extra)
    )
    found = shutil.which(provider, path=search_path)
    return found if found and os.access(found, os.X_OK) else None


def _has_provider_credentials(provider: str, env: dict[str, str]) -> bool:
    home = Path(env.get("HOME", str(Path.home())))
    if provider == "agy":
        return (home / ".gemini" / "antigravity-cli").exists() or bool(
            env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
        )
    if provider == "claude":
        return (
            (home / ".claude.json").exists()
            or (home / ".claude").exists()
            or bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"))
        )
    if provider == "codex":
        codex_home = codex_home_dir(env)
        return (
            (codex_home / "auth.json").exists()
            or (codex_home / "config.toml").exists()
            or bool(env.get("OPENAI_API_KEY"))
        )
    return False


def codex_home_dir(env: dict[str, str] | None = None) -> Path:
    eff_env = dict(os.environ if env is None else env)
    configured = eff_env.get("CODEX_HOME", "").strip()
    return Path(configured) if configured else Path(eff_env.get("HOME", str(Path.home()))) / ".codex"


def discover_codex_mcp_servers(worktree_path: Path) -> tuple[str, ...]:
    """Collect server names from the Codex config layers within this worktree."""
    worktree_path = worktree_path.resolve()
    names: set[str] = set()
    for directory in (worktree_path, *worktree_path.parents):
        config_path = directory / ".codex" / "config.toml"
        try:
            with config_path.open("rb") as config_file:
                servers = tomllib.load(config_file).get("mcp_servers")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            pass
        else:
            if isinstance(servers, dict):
                names.update(servers)
        if (directory / ".git").exists():
            break
    return tuple(sorted(names))


def _format_toml_key(key: Any) -> str:
    key_text = str(key)
    return key_text if re.fullmatch(r"[A-Za-z0-9_-]+", key_text) else json.dumps(key_text, ensure_ascii=False)


def _format_toml_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "[]"
        formatted = [_format_toml_value(item) for item in value]
        if any(item is None for item in formatted) or len({type(item) for item in value}) > 1:
            return None
        return "[" + ", ".join(item for item in formatted if item is not None) + "]"
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            rendered = _format_toml_value(item)
            if rendered is None:
                continue
            parts.append(f"{_format_toml_key(key)} = {rendered}")
        return "{" + ", ".join(parts) + "}"
    return None


def codex_isolation_scope_key(
    worktree_path: Path, branch: str, env: dict[str, str] | None = None
) -> str:
    session_key = worktree_session_key(worktree_path, branch)
    source_key = str(codex_home_dir(env).resolve())
    return hashlib.sha256(f"{session_key}:{source_key}".encode("utf-8")).hexdigest()[:16]


def _prune_isolated_codex_homes(
    homes_root: Path,
    keep_name: str,
    gc_fd: int | None = None,
    *,
    now: float | None = None,
) -> None:
    owns_gc_fd = gc_fd is None
    if owns_gc_fd:
        gc_path = homes_root / ".gc.lock"
        gc_fd = os.open(gc_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    assert gc_fd is not None
    try:
        if owns_gc_fd:
            fcntl.flock(gc_fd, fcntl.LOCK_EX)
        current_time = time.time() if now is None else now
        dirs = sorted(
            (
                (item.stat().st_mtime, item)
                for item in homes_root.iterdir()
                if item.is_dir() and item.name != keep_name
            ),
            key=lambda entry: entry[0],
            reverse=True,
        )
        for index, (mtime, item) in enumerate(dirs):
            expired = (
                index >= MAX_RETAINED_SESSIONS - 1
                or current_time - mtime > ISOLATED_CODEX_HOME_MAX_AGE_SECONDS
            )
            lock_path = item / ".review.lock"
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if expired:
                    shutil.rmtree(item, ignore_errors=True)
                elif (item / "tmp").is_dir():
                    shutil.rmtree(item / "tmp", ignore_errors=True)
                    with contextlib.suppress(OSError):
                        os.utime(item, (item.stat().st_atime, mtime))
            finally:
                os.close(lock_fd)
    finally:
        if owns_gc_fd:
            fcntl.flock(gc_fd, fcntl.LOCK_UN)
            os.close(gc_fd)


def _write_snapshot(target: Path, data: bytes) -> None:
    if target.is_file() and not target.is_symlink() and (target.stat().st_mode & 0o777) == 0o600:
        try:
            if target.read_bytes() == data:
                return
        except OSError:
            pass
    fd, tmp_str = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
    tmp_path = Path(tmp_str)
    try:
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            handle.write(data)
        os.replace(tmp_path, target)
    finally:
        if fd != -1:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)


@contextlib.contextmanager
def isolated_codex_home(
    common_dir: Path,
    *,
    worktree_path: Path,
    branch: str = "",
    env: dict[str, str] | None = None,
):
    homes_root = common_dir / CODEX_ISOLATED_HOMES_DIRNAME
    homes_root.mkdir(parents=True, exist_ok=True)
    homes_root.chmod(0o700)
    gc_path = homes_root / ".gc.lock"
    gc_fd = os.open(gc_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    review_fd: int | None = None
    review_locked = False
    try:
        fcntl.flock(gc_fd, fcntl.LOCK_EX)
        target_home = homes_root / codex_isolation_scope_key(worktree_path, branch, env)
        target_home.mkdir(parents=True, exist_ok=True)
        target_home.chmod(0o700)
        os.utime(target_home, None)
        review_fd = os.open(
            target_home / ".review.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        _prune_isolated_codex_homes(homes_root, keep_name=target_home.name, gc_fd=gc_fd)
        fcntl.flock(gc_fd, fcntl.LOCK_UN)
        fcntl.flock(review_fd, fcntl.LOCK_EX)
        review_locked = True
        if not target_home.is_dir():
            raise RuntimeError("Codex isolation home was pruned while acquiring its lock")
        source_home = codex_home_dir(env)
        if source_home.resolve() != target_home.resolve():
            for name in ("auth.json", ".credentials.json"):
                source = source_home / name
                target = target_home / name
                if source.is_file():
                    _write_snapshot(target, source.read_bytes())
                else:
                    target.unlink(missing_ok=True)
            src_cfg_path = source_home / "config.toml"
            try:
                raw_cfg = src_cfg_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                parsed_cfg: dict[str, Any] = {}
            except (OSError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"Invalid Codex configuration at {src_cfg_path}: {exc}") from exc
            else:
                try:
                    parsed_cfg = tomllib.loads(raw_cfg)
                except tomllib.TOMLDecodeError as exc:
                    raise RuntimeError(f"Invalid Codex configuration at {src_cfg_path}: {exc}") from exc
            lines = ["check_for_update_on_startup = false"]
            for key in CODEX_ROUTING_CONFIG_KEYS:
                if key == "model_providers" or key not in parsed_cfg:
                    continue
                value = parsed_cfg[key]
                if key in CODEX_RELATIVE_PATH_CONFIG_KEYS and isinstance(value, str) and value.strip():
                    expanded = Path(value.strip()).expanduser()
                    value = str((source_home / expanded).resolve()) if not expanded.is_absolute() else str(expanded.resolve())
                if not isinstance(value, (bool, int, float, str, list)):
                    continue
                rendered = _format_toml_value(value)
                if rendered is not None:
                    lines.append(f"{key} = {rendered}")
            lines += ["", "[features]"]
            lines.extend(f"{feature} = false" for feature in CODEX_DISABLED_FEATURES)
            trusted_projects = [str(worktree_path.resolve())]
            if common_dir.name == ".git" and common_dir.parent.is_dir():
                trusted_projects.append(str(common_dir.parent.resolve()))
            for project in dict.fromkeys(trusted_projects):
                lines.extend(
                    ["", f"[projects.{json.dumps(project)}]", 'trust_level = "trusted"']
                )
            providers = parsed_cfg.get("model_providers")
            if isinstance(providers, dict):
                for provider_name, provider_cfg in providers.items():
                    if not isinstance(provider_cfg, dict):
                        continue
                    lines.extend(["", f"[model_providers.{json.dumps(str(provider_name), ensure_ascii=False)}]"])
                    for key, value in provider_cfg.items():
                        rendered = _format_toml_value(value)
                        if rendered is not None:
                            lines.append(f"{_format_toml_key(key)} = {rendered}")
            rendered_cfg = "\n".join(lines) + "\n"
            try:
                tomllib.loads(rendered_cfg)
            except tomllib.TOMLDecodeError as exc:
                raise RuntimeError(f"Invalid isolated Codex configuration: {exc}") from exc
            _write_snapshot(target_home / "config.toml", rendered_cfg.encode("utf-8"))
        yield target_home
    finally:
        if review_fd is not None:
            if review_locked:
                shutil.rmtree(target_home / "tmp", ignore_errors=True)
                fcntl.flock(review_fd, fcntl.LOCK_UN)
            os.close(review_fd)
        try:
            fcntl.flock(gc_fd, fcntl.LOCK_UN)
        finally:
            os.close(gc_fd)


def _verify_binary_runs(binary_path: str, env: dict[str, str]) -> bool:
    try:
        return (
            subprocess.run(
                [binary_path, "--version"],
                env=env,
                capture_output=True,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _normalize_priority_order(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return list(dict.fromkeys(p for p in raw if isinstance(p, str) and p in PROVIDERS))


def _normalize_provider_models(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for provider, settings in raw.items():
        if provider not in PROVIDERS or not isinstance(settings, dict):
            continue
        values = {
            key: settings[key].strip()
            for key in ("model", "effort")
            if isinstance(settings.get(key), str) and settings[key].strip()
        }
        if values:
            normalized[provider] = values
    return normalized


def _load_config(cfg_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return (
        data if isinstance(data, dict) and data.get("schema") == CONFIG_SCHEMA else {}
    )


def _active_cooldowns(raw: Any, now: datetime) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    active = {}
    for provider, entry in raw.items():
        if provider not in PROVIDERS or not isinstance(entry, dict):
            continue
        until, reason = entry.get("until"), entry.get("reason")
        if not isinstance(until, str) or not isinstance(reason, str):
            continue
        try:
            expiry = datetime.fromisoformat(until)
        except ValueError:
            continue
        if expiry.tzinfo is not None and expiry > now:
            active[provider] = {"until": until, "reason": reason}
    return active


def _set_provider_cooldowns(payload: dict[str, Any], cooldowns: dict[str, dict[str, str]]) -> None:
    payload.pop("fallback_expires_at", None)
    if cooldowns:
        payload["provider_cooldowns"] = cooldowns
    else:
        payload.pop("provider_cooldowns", None)


def _cooldown_expiry(now: datetime, env: dict[str, str]) -> datetime:
    try:
        seconds = float(env.get("THE_GUARD_REVIEW_COOLDOWN_SECONDS", str(DEFAULT_PROVIDER_COOLDOWN_SECONDS)))
        if math.isfinite(seconds) and seconds > 0:
            return now + timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        pass
    return now + timedelta(seconds=DEFAULT_PROVIDER_COOLDOWN_SECONDS)


@contextlib.contextmanager
def _provider_config_lock(cfg_path: Path):
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cfg_path.with_name(f".{cfg_path.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_provider_config(cfg_path: Path, payload: dict[str, Any]) -> None:
    _write_snapshot(cfg_path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def resolve_provider_model_config(
    provider: str,
    config_payload: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    eff_env = dict(os.environ if env is None else env)
    settings = _normalize_provider_models(
        (config_payload or {}).get("provider_models")
    ).get(provider, {})
    model = eff_env.get("THE_GUARD_REVIEW_MODEL", "").strip() or settings.get(
        "model", ""
    )
    effort = (
        eff_env.get("THE_GUARD_REVIEW_EFFORT", "").strip()
        or settings.get("effort", "")
        or DEFAULT_REVIEW_EFFORT
    )
    return model, effort


def probe_environment(
    repo_root: Path,
    env: dict[str, str] | None = None,
    *,
    priority_order: list[str] | None = None,
    provider_models: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    eff_env = dict(os.environ if env is None else env)
    cfg_path = _config_file_path(repo_root, eff_env)
    payload: dict[str, Any] | None = None
    with contextlib.suppress(OSError), _provider_config_lock(cfg_path):
        payload = _probe_provider_config(_load_config(cfg_path), eff_env, priority_order, provider_models)
        _write_provider_config(cfg_path, payload)
    if payload is None:
        return _probe_provider_config(_load_config(cfg_path), eff_env, priority_order, provider_models)
    return payload


def _probe_provider_config(
    existing: dict[str, Any],
    env: dict[str, str],
    priority_order: list[str] | None = None,
    provider_models: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    order = _normalize_priority_order(
        existing.get("priority_order") if priority_order is None else priority_order
    )
    if priority_order is not None and not order:
        raise ValueError("priority_order must contain at least one supported provider")
    order = order or list(PROVIDERS)
    models = _normalize_provider_models(existing.get("provider_models"))
    if provider_models is not None:
        for provider, settings in _normalize_provider_models(provider_models).items():
            models[provider] = {**models.get(provider, {}), **settings}
    available: list[str] = []
    binaries: dict[str, str] = {}
    for provider in order:
        binary = _find_provider_binary(provider, env)
        if (
            binary
            and _has_provider_credentials(provider, env)
            and _verify_binary_runs(binary, env)
        ):
            available.append(provider)
            binaries[provider] = binary

    payload: dict[str, Any] = {
        "schema": CONFIG_SCHEMA,
        "priority_order": order,
        "preferred_provider": available[0] if available else "",
        "available_providers": available,
        "provider_binaries": binaries,
        "probed_at": datetime.now(UTC).isoformat(),
    }
    if models:
        payload["provider_models"] = models
    return payload


def _provider_binaries(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("provider_binaries")
    if not isinstance(raw, dict):
        return {}
    return {
        provider: path for provider, path in raw.items()
        if provider in PROVIDERS and isinstance(path, str)
    }


def resolve_provider_order(
    repo_root: Path,
    *,
    explicit_provider: str | None = None,
    reprobe: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    eff_env = dict(os.environ if env is None else env)
    forced = (explicit_provider or eff_env.get("THE_GUARD_REVIEW_PROVIDER", "")).strip()
    cfg_path = _config_file_path(repo_root, eff_env)

    payload = _load_config(cfg_path)
    priority = _normalize_priority_order(payload.get("priority_order"))
    binaries = _provider_binaries(payload)
    ordered: list[tuple[str, str]] = []
    cooldowns: dict[str, dict[str, str]] = {}
    if priority and not reprobe:
        cooldowns = _active_cooldowns(payload.get("provider_cooldowns"), datetime.now(UTC))
        for prov in sorted(priority, key=cooldowns.__contains__):
            bin_path = binaries.get(prov) or _find_provider_binary(prov, eff_env)
            if bin_path and os.access(bin_path, os.X_OK) and _has_provider_credentials(prov, eff_env):
                ordered.append((prov, bin_path))
    if not ordered:
        payload = (
            _probe_provider_config(payload, eff_env)
            if forced
            else probe_environment(repo_root, eff_env)
        )
        binaries = _provider_binaries(payload)
        ordered = [(prov, binaries[prov]) for prov in payload["available_providers"]]
        cooldowns = {}
    preferred = next((provider for provider, _ in ordered if provider not in cooldowns), "")
    effective = {**payload, "preferred_provider": preferred}
    _set_provider_cooldowns(effective, cooldowns)
    if forced in PROVIDERS:
        forced_bin = binaries.get(forced) or _find_provider_binary(forced, eff_env)
        if forced_bin:
            ordered = [
                (forced, forced_bin),
                *[item for item in ordered if item[0] != forced],
            ]
    return ordered, effective


def record_working_provider(
    repo_root: Path, provider: str, binary_path: str, env: dict[str, str] | None = None
) -> None:
    _record_provider_result(repo_root, provider, binary_path=binary_path, env=env)


def record_provider_cooldown(
    repo_root: Path, provider: str, *, reason: str, env: dict[str, str] | None = None,
) -> None:
    _record_provider_result(repo_root, provider, reason=reason, env=env)


def _record_provider_result(
    repo_root: Path,
    provider: str,
    *,
    env: dict[str, str] | None,
    binary_path: str | None = None,
    reason: str = "",
) -> None:
    eff_env = dict(os.environ if env is None else env)
    cfg_path = _config_file_path(repo_root, eff_env)
    with contextlib.suppress(OSError), _provider_config_lock(cfg_path):
        existing = _load_config(cfg_path)
        now = datetime.now(UTC)
        cooldowns = _active_cooldowns(existing.get("provider_cooldowns"), now)
        order = _normalize_priority_order(existing.get("priority_order")) or list(PROVIDERS)
        available = _normalize_priority_order(existing.get("available_providers"))
        binaries = _provider_binaries(existing)
        if binary_path is None:
            cooldowns[provider] = {
                "until": _cooldown_expiry(now, eff_env).isoformat(),
                "reason": reason[:160],
            }
            fallback = available[0] if available else ""
        else:
            cooldowns.pop(provider, None)
            binaries[provider] = binary_path
            if provider not in available:
                available.append(provider)
            fallback = provider
        preferred = next(
            (candidate for candidate in order if candidate in available and candidate not in cooldowns),
            fallback,
        )
        data = {
            **existing,
            "schema": CONFIG_SCHEMA,
            "priority_order": order,
            "preferred_provider": preferred,
            "available_providers": available,
            "provider_binaries": binaries,
        }
        _set_provider_cooldowns(data, cooldowns)
        if data != existing:
            _write_provider_config(cfg_path, {**data, "probed_at": now.isoformat()})
