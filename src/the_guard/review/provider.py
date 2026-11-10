"""Discovery and ordering of the local agent CLIs that can serve as architecture reviewers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .contract import CONFIG_SCHEMA, PROVIDERS
from .repository import git_common_dir
from .session import UTC


def _config_file_path(repo_root: Path, env: dict[str, str] | None = None) -> Path:
    eff_env = dict(os.environ if env is None else env)
    override = eff_env.get("THE_GUARD_REVIEW_CONFIG", "").strip()
    if override:
        return Path(override)
    home = Path(eff_env.get("HOME", str(Path.home())))
    candidate = home / ".config" / "the-guard" / "arch-review-agent.json"
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        return git_common_dir(repo_root) / "arch-review-agent.json"


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
        return (
            (home / ".codex" / "auth.json").exists()
            or (home / ".codex" / "config.toml").exists()
            or bool(env.get("OPENAI_API_KEY"))
        )
    return False


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
    except (OSError, json.JSONDecodeError):
        return {}
    return (
        data if isinstance(data, dict) and data.get("schema") == CONFIG_SCHEMA else {}
    )


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
    effort = eff_env.get("THE_GUARD_REVIEW_EFFORT", "").strip() or settings.get(
        "effort", ""
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
    existing = _load_config(cfg_path)
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
        binary = _find_provider_binary(provider, eff_env)
        if (
            binary
            and _has_provider_credentials(provider, eff_env)
            and _verify_binary_runs(binary, eff_env)
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
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    return payload


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

    cached = {} if reprobe else _load_config(cfg_path)
    priority = _normalize_priority_order(cached.get("priority_order"))
    ordered: list[tuple[str, str]] = []
    if priority:
        preferred = str(cached.get("preferred_provider") or "")
        binaries = (
            cached.get("provider_binaries")
            if isinstance(cached.get("provider_binaries"), dict)
            else {}
        )
        order = (
            [preferred, *[p for p in priority if p != preferred]]
            if preferred in priority
            else priority
        )
        for prov in order:
            bin_path = binaries.get(prov) or _find_provider_binary(prov, eff_env)
            if (
                isinstance(bin_path, str)
                and os.access(bin_path, os.X_OK)
                and _has_provider_credentials(prov, eff_env)
            ):
                ordered.append((prov, bin_path))
    payload = cached
    if not ordered:
        payload = probe_environment(repo_root, eff_env)
    binaries = payload.get("provider_binaries", {})
    if not ordered:
        ordered = [
            (prov, binaries[prov])
            for prov in payload.get("available_providers", [])
            if prov in binaries
        ]
    if forced in PROVIDERS:
        forced_bin = binaries.get(forced) or _find_provider_binary(forced, eff_env)
        if forced_bin:
            ordered = [
                (forced, forced_bin),
                *[item for item in ordered if item[0] != forced],
            ]
    return ordered, payload


def record_working_provider(
    repo_root: Path, provider: str, binary_path: str, env: dict[str, str] | None = None
) -> None:
    eff_env = dict(os.environ if env is None else env)
    cfg_path = _config_file_path(repo_root, eff_env)
    existing = _load_config(cfg_path)
    avail = [provider]
    bins = {provider: binary_path}
    if existing:
        avail = _normalize_priority_order(existing.get("available_providers"))
        if provider not in avail:
            avail.insert(0, provider)
        bins = {**dict(existing.get("provider_binaries") or {}), provider: binary_path}
    data = {
        "schema": CONFIG_SCHEMA,
        "priority_order": _normalize_priority_order(existing.get("priority_order"))
        or list(PROVIDERS),
        "preferred_provider": provider,
        "available_providers": avail,
        "provider_binaries": bins,
        "probed_at": datetime.now(UTC).isoformat(),
    }
    models = _normalize_provider_models(existing.get("provider_models"))
    if models:
        data["provider_models"] = models
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
