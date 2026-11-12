"""Non-destructive onboarding: materialize a preset and a single policy manifest."""

from __future__ import annotations

import json
from pathlib import Path

from .config import REVIEW_INTEGRATION_MODES, load_config, repository_path
from .hooks import hook_install_plan, install_hooks
from .topology import discover_topology

CORE = (
    "architecture-simplicity",
    "single-source-of-truth",
    "service-boundaries",
    "code-organization",
)

WORKFLOW_TEMPLATE = """name: the-guard review
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: sensebeing/the-guard@main
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          app-id: ${{ secrets.THE_GUARD_APP_ID }}
          private-key: ${{ secrets.THE_GUARD_PRIVATE_KEY }}
"""

WORKFLOW_PATH = ".github/workflows/the-guard-review.yml"


def workflow_install_plan(root: Path | str) -> Path:
    """Resolve the managed review workflow without writing; refuse to replace a customized one."""
    target = repository_path(Path(root).resolve(), WORKFLOW_PATH)
    if target.exists():
        existing = target.read_text()
        if existing != WORKFLOW_TEMPLATE and "uses: ./" not in existing:
            raise ValueError(f"Refusing to overwrite customized workflow: {target}")
    return target


def install_github_workflow(root: Path | str) -> Path:
    target = workflow_install_plan(root)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(WORKFLOW_TEMPLATE)
    return target


def initialize(
    root: Path | str,
    *,
    preset: str = "core",
    rules_dir: str = "docs/standards/rules",
    manifest_file: str = "AGENTS.md",
    with_hooks: bool = False,
    with_github_workflow: bool = False,
    integration_mode: str | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if preset not in {"core", "full"}:
        raise ValueError("preset must be core or full")
    if integration_mode is not None and integration_mode not in REVIEW_INTEGRATION_MODES:
        raise ValueError("integration_mode must be local-hooks, github-pr, or both")
    effective_mode = integration_mode
    if effective_mode is None:
        if with_hooks and with_github_workflow:
            effective_mode = "both"
        elif with_github_workflow:
            effective_mode = "github-pr"
        elif with_hooks:
            effective_mode = "local-hooks"
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
    if effective_mode is not None:
        config.setdefault("review", {})["integration_mode"] = effective_mode
    install_workflow = with_github_workflow or effective_mode in {"github-pr", "both"}
    install_local_hooks = (
        with_hooks or effective_mode in {"local-hooks", "github-pr", "both"}
    ) and (root / ".git").exists()
    if install_workflow:
        workflow_install_plan(root)
    if install_local_hooks:
        hook_install_plan(root)
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
    if install_workflow:
        install_github_workflow(root)
    if install_local_hooks:
        install_hooks(root)
    return {
        "preset": preset,
        "rules": len(slugs),
        "manifest": manifest_file,
        "config": ".guard/config.json",
        "integration_mode": effective_mode,
    }
