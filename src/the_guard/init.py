"""Non-destructive onboarding: materialize a preset and a single policy manifest."""

from __future__ import annotations

import json
from pathlib import Path

from .config import load_config, repository_path
from .hooks import install_hooks
from .topology import discover_topology

CORE = (
    "architecture-simplicity",
    "single-source-of-truth",
    "service-boundaries",
    "code-organization",
)


def initialize(
    root: Path | str,
    *,
    preset: str = "core",
    rules_dir: str = "docs/standards/rules",
    manifest_file: str = "AGENTS.md",
    with_hooks: bool = False,
) -> dict[str, object]:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if preset not in {"core", "full"}:
        raise ValueError("preset must be core or full")
    cfg = load_config(root)
    manifest = repository_path(root, manifest_file)
    destination = repository_path(root, rules_dir)
    config_path = repository_path(root, ".guard/config.json")
    templates = Path(__file__).parent / "presets/rules"
    slugs = (
        CORE
        if preset == "core"
        else (
            *CORE,
            *sorted(p.stem for p in templates.glob("*.md") if p.stem not in CORE),
        )
    )
    current = (
        manifest.read_text()
        if manifest.exists()
        else "# Agent instructions\n\nRead the engineering policy and module boundaries before changing code.\n"
    )
    start = f"<!-- {cfg.manifest_tag}:start -->"
    end = f"<!-- {cfg.manifest_tag}:end -->"
    entries = []
    files = {}
    ref = cfg.detail_manifest_ref or manifest_file + "#engineering-quality-policy"
    for slug in slugs:
        target = repository_path(root, rules_dir + "/" + slug + ".md")
        content = (
            (templates / (slug + ".md"))
            .read_text()
            .replace("AGENTS.md#engineering-quality-policy", ref)
            .replace("guard.", cfg.rule_id_prefix)
        )
        if target.exists() and target.read_text() != content:
            raise ValueError(f"Refusing to overwrite customized rule: {target}")
        files[target] = content
        body = content.split("---", 2)[2].strip().split("\n\n", 1)[1]
        summary = body.split("\n\n")[0].split(". ")[0].rstrip(".") + "."
        entries.append(
            f"  - id: {cfg.rule_id_prefix}{slug}\n"
            "    severity: block\n"
            "    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]\n"
            f"    summary: {summary}\n"
            f"    details: {rules_dir}/{slug}.md\n"
            "    checker: static-plus-review"
        )
    block = (
        start
        + f"\n```yaml\nschema: {cfg.manifest_schema}\nrules:\n"
        + "\n".join(entries)
        + "\n```\n"
        + end
    )
    if start in current:
        if current.count(start) != 1 or current.count(end) != 1:
            raise ValueError("Malformed existing policy block")
        a, b = current.index(start), current.index(end) + len(end)
        current = current[:a] + block + current[b:]
    else:
        current = (
            current.rstrip() + "\n\n## Engineering Quality Policy\n\n" + block + "\n"
        )
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    config.update(
        {
            "manifest_file": manifest_file,
            "manifest_tag": cfg.manifest_tag,
            "detail_manifest_ref": ref,
            "detail_dir": rules_dir,
        }
    )
    config.setdefault("topology", {}).update(discover_topology(root))
    config.setdefault("review", {}).setdefault(
        "extra_reviewable_paths", [rules_dir + "/**"]
    )
    destination.mkdir(parents=True, exist_ok=True)
    for target, content in files.items():
        target.write_text(content)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(current)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    alias = root / "CLAUDE.md"
    if manifest_file == "AGENTS.md" and not alias.exists() and not alias.is_symlink():
        alias.symlink_to("AGENTS.md")
    if with_hooks:
        install_hooks(root)
    return {
        "preset": preset,
        "rules": len(slugs),
        "manifest": manifest_file,
        "config": ".guard/config.json",
    }
