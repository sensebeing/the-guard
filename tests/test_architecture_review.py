"""Architecture review gate: provider/streaming/scope contracts.

Contract: code requires matching patch receipts, readonly providers, complete input and bounded liveness.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tomllib
import tempfile
from unittest.mock import patch

from the_guard.hooks import main_push_gate
from the_guard.review.cli import main as review_main
from the_guard.review import provider as review_provider
from the_guard.review import repository as review_repository
from the_guard.review import session as review_session
from the_guard.review.engine import (
    _provider_review_plan,
    build_review_prompt,
    collect_review_scope,
    resolve_provider_order,
    run_architecture_review,
)
from the_guard.review.prompt import (
    _render_bounded_diff_section,
    _snapshot_context,
    build_incremental_review_prompt,
)
from the_guard.review.provider import (
    _has_provider_credentials,
    _prune_isolated_codex_homes,
    isolated_codex_home,
    codex_isolation_scope_key,
    _normalize_priority_order,
    _normalize_provider_models,
    probe_environment,
    record_working_provider,
    resolve_provider_model_config,
)
from the_guard.review.repository import (
    _classify_file_tier,
    collect_incremental_scope,
    resolve_or_carry_forward_receipt,
    load_review_session,
    git_common_dir,
    is_reviewable_path,
    load_valid_receipt,
)
from the_guard.review.contract import CODEX_DISABLED_FEATURES, ReviewOutput, verify_reviewed_head_sha
from the_guard.review.runner import (
    _build_provider_cmd,
    _build_provider_env,
    _invoke_provider,
    _is_persistent_provider_failure,
    _provider_failure_detail,
    _run_streaming_cmd,
    _summarize_stream_line,
)
from the_guard.config import load_config, GuardConfig
from the_guard.git_helpers import GIT_REPO_ENV_VARS, git_stdout
from the_guard.review.documents import (
    _SnapshotDocuments,
    boundary_document_paths,
    changed_task_blueprints,
    existing_document_paths,
    read_document,
    resolve_task_design_blueprints,
)
from the_guard.review.session import ReviewSessionState, compute_review_contract_digest
from the_guard.policy import PolicyError, load_policy
from architecture_review_support import (
    ArchitectureReviewTestCase,
    _git,
    _write_exec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_CLI = REPO_ROOT / "bin/guard"
BLUEPRINT_REVIEW = {"blueprint_globs": ["*.md"]}


class ArchitectureReviewGateTests(ArchitectureReviewTestCase):
    def test_checkout_runtime_provenance_ignores_hook_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            unrelated = tmp / "unrelated"
            unrelated.mkdir()
            _git(unrelated, "init", "-b", "main")
            module_path = repo / "src/the_guard/review/session.py"
            module_path.parent.mkdir(parents=True)
            module_path.write_text("# Runtime package fixture\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Add runtime package")
            expected_sha = _git(repo, "rev-parse", "--short", "HEAD")
            with patch.object(review_session, "__file__", str(module_path)):
                expected = review_session.active_guard_runtime()
                self.assertEqual(expected["guard_sha"], expected_sha)
                with patch.dict(os.environ, {"GIT_DIR": str(unrelated / ".git")}):
                    self.assertEqual(review_session.active_guard_runtime(), expected)

    def test_main_push_gate_verifies_target_receipt_with_hook_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp, review={})
            base_sha = _git(repo, "rev-parse", "main")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "Add reviewed code")
            head_sha = _git(repo, "rev-parse", "HEAD")
            result = run_architecture_review(repo, reviewer_fn=lambda _: {
                "verdict": "GO", "summary": "Reviewed", "blocking_findings": [],
            })
            self.assertIsNotNone(load_valid_receipt(repo, result.patch_digest))
            unrelated = tmp / "unrelated"
            unrelated.mkdir()
            _git(unrelated, "init", "-b", "main")
            update = f"refs/heads/topic {head_sha} refs/heads/main {base_sha}\n"
            self.assertEqual(main_push_gate(repo, update), 0)
            with patch.dict(os.environ, {"GIT_DIR": str(unrelated / ".git")}):
                self.assertEqual(main_push_gate(repo, update), 0)

    def test_cli_deterministic_snapshot_gate_precedes_provider_and_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / ".guard").mkdir(exist_ok=True)
            (repo / ".guard/config.json").write_text(
                json.dumps(
                    {
                        "topology": {
                            "forbidden_imports": [
                                {"scope_prefixes": ["src/"], "imports": ["forbidden"]}
                            ]
                        }
                    }
                )
            )
            (repo / "src").mkdir(exist_ok=True)
            (repo / "src/main.py").write_text("import forbidden\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "deterministic violation")
            (repo / "src/main.py").write_text("value = 1\n")
            with (
                patch("the_guard.review.cli.run_architecture_review") as reviewer,
                patch(
                    "the_guard.review.cli.resolve_or_carry_forward_receipt"
                ) as receipt,
                patch(
                    "the_guard.review.cli.publish_github_pr_review",
                    return_value={"status": "published"},
                ) as publish,
            ):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(
                        review_main(
                            [
                                "--repo-root",
                                str(repo),
                                "--json",
                                "--github-pr",
                                "org/repo#1",
                            ]
                        ),
                        2,
                    )
                data = json.loads(output.getvalue())
                self.assertEqual(data["provider"], "static")
                self.assertEqual(data["mode"], "static")
                self.assertEqual(data["verdict"], "NO-GO")
                self.assertIn("forbidden", data["blocking_findings"][0]["issue"])
                publish.assert_called_once()
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(
                        review_main(["check-push", "--repo-root", str(repo)]), 2
                    )
                self.assertIn("deterministic check failed", errors.getvalue())
                reviewer.assert_not_called()
                receipt.assert_not_called()

    def test_engine_reuses_preloaded_review_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "main.py").write_text("value = 1\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "reviewable change")
            scope = collect_review_scope(repo)
            config = load_config(repo, revision=scope.head_sha)
            policy = load_policy(repo, revision=scope.head_sha, config=config)
            with patch("the_guard.review.engine.collect_review_scope", side_effect=AssertionError("scope reloaded")), patch(
                "the_guard.review.engine.load_config", side_effect=AssertionError("config reloaded")
            ), patch("the_guard.review.engine.load_policy", side_effect=AssertionError("policy reloaded")):
                result = run_architecture_review(
                    repo, scope=scope, config=config, policy=policy, force=True,
                    reviewer_fn=lambda _: {"verdict": "GO", "summary": "Reviewed", "blocking_findings": []},
                )
            self.assertEqual(result.verdict, "GO")

    def test_review_provider_does_not_inherit_git_repository_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "Reviewable change")
            env = {**dict.fromkeys(GIT_REPO_ENV_VARS, "foreign"), "GIT_CONFIG_COUNT": "0"}
            with (
                patch("the_guard.review.engine.resolve_provider_order", return_value=([("codex", "codex")], {})),
                patch(
                    "the_guard.review.engine._invoke_provider",
                    return_value=(ReviewOutput("GO", "Reviewed", (), ""), "test-session", ""),
                ) as invoke,
            ):
                result = run_architecture_review(repo, env=env)
            self.assertEqual(result.verdict, "GO")
            self.assertEqual(invoke.call_args.args[4], {"GIT_CONFIG_COUNT": "0"})
            self.assertEqual(env["GIT_DIR"], "foreign")

    def test_document_inventory_batches_tree_reads_and_skips_file_module_children(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            modules = []
            for index in range(30):
                path = f"packages/owner{index}"
                (repo / path).mkdir(parents=True)
                (repo / path / "README.md").write_text(f"Owner {index} boundary\n")
                (repo / path / "main.py").write_text("value = 1\n")
                modules.append({"path": path})
            modules.append({"path": "src/config.py", "boundary_docs": ["docs/explicit.md"]})
            (repo / ".guard").mkdir()
            (repo / ".guard/config.json").write_text(json.dumps({"topology": {"modules": modules}}))
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "many module contracts")
            config = load_config(repo)
            policy = load_policy(repo)
            scope = collect_review_scope(repo)
            self.assertEqual(boundary_document_paths(modules[-1]), {"docs/explicit.md"})
            for extension in ("py", "ts", "mjs", "cjs"):
                file_config = GuardConfig(topology={"modules": [{"path": f"src/config.{extension}"}]})
                self.assertFalse(is_reviewable_path(f"src/config.{extension}/README.md", file_config))
            self.assertIsNone(read_document(repo, "packages"))
            self.assertEqual(existing_document_paths(repo, ["packages", "AGENTS.md"], None), {"AGENTS.md"})
            for operation in (
                lambda: compute_review_contract_digest(policy, config, repo_root=repo, revision=scope.head_sha),
                lambda: _snapshot_context(repo, scope, config=config),
            ):
                with patch("subprocess.run", wraps=subprocess.run) as run:
                    operation()
                tree_calls = [call.args[0] for call in run.call_args_list if "ls-tree" in call.args[0]]
                self.assertEqual(len(tree_calls), 1, tree_calls)
                self.assertIn("-z", tree_calls[0])
                self.assertFalse(any("config.py/" in arg for arg in tree_calls[0]))
            self.assertEqual(existing_document_paths(repo, ["packages", "AGENTS.md"], scope.head_sha), {"AGENTS.md"})

    def test_unchanged_blueprints_skip_snapshot_reads_at_scale(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            blueprint_dir = repo / "docs/blueprints"
            blueprint_dir.mkdir(parents=True)
            stable = [f"docs/blueprints/plan-{index:03d}.md" for index in range(300)]
            for path in stable:
                (repo / path).write_text("Stable owner commitment\n")
            (blueprint_dir / "plan-source.md").write_text("Original shared commitment\n")
            (blueprint_dir / "linked.md").symlink_to("plan-source.md")
            _git(repo, "add", "docs")
            _git(repo, "commit", "-m", "land hundreds of stable blueprints")
            previous = _git(repo, "rev-parse", "HEAD")

            modified = stable[0]
            (repo / modified).write_text("Changed owner commitment\n")
            (blueprint_dir / "plan-source.md").write_text("Changed shared commitment\n")
            _git(repo, "add", "docs")
            _git(repo, "commit", "-m", "change a blueprint and the shared symlink target")
            target = _git(repo, "rev-parse", "HEAD")
            config = load_config(repo)

            reads: list[tuple[str, str | None]] = []

            read_snapshot_document = _SnapshotDocuments.read

            def record_read(reader, path):
                reads.append((path, reader.reader.base))
                return read_snapshot_document(reader, path)

            with patch.object(_SnapshotDocuments, "read", new=record_read):
                changed = changed_task_blueprints(repo, config, previous, target, ())

            detected = {
                modified,
                "docs/blueprints/linked.md",
                "docs/blueprints/plan-source.md",
            }
            self.assertEqual(changed, detected)
            self.assertEqual(
                set(reads),
                {(path, revision) for path in detected for revision in (previous, target)},
            )
            self.assertFalse({path for path, _ in reads} & set(stable[1:]))

            with patch("the_guard.git_helpers.git_stdout", wraps=git_stdout) as tree:
                self.assertEqual(existing_document_paths(repo, stable, target), set(stable))
            tree_calls = [call.args[1] for call in tree.call_args_list]
            self.assertEqual(tree_calls, [["git", "-c", "core.fsmonitor=false", "ls-tree", "-z", "-r", "-t", target]])

    def test_implicit_readme_and_symlink_targets_are_reviewable_contract_changes(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "packages/a").mkdir(parents=True)
            (repo / "notes").mkdir()
            (repo / "packages/a/README.md").write_text("Implicit boundary\n")
            (repo / "notes/owner.txt").write_text("Linked boundary\n")
            (repo / "packages/a/OWNER.md").symlink_to("../../notes/owner.txt")
            (repo / ".guard").mkdir()
            (repo / ".guard/config.json").write_text(json.dumps({"topology": {"modules": [
                {"path": "packages/a", "boundary_docs": ["packages/a/OWNER.md"]}]}}))
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "baseline boundaries")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            for path in ("packages/a/README.md", "notes/owner.txt"):
                (repo / path).write_text("Changed owner responsibility\n")
                _git(repo, "add", path)
                _git(repo, "commit", "-m", "contract only change")
                scope = collect_review_scope(repo)
                self.assertIn(path, scope.reviewable_files)
                self.assertFalse(scope.is_exempt)
                prompts = []
                def reviewer(prompt):
                    prompts.append(prompt)
                    return {"verdict": "GO", "summary": "reviewed", "blocking_findings": []}
                self.assertEqual(run_architecture_review(repo, reviewer_fn=reviewer).mode, "full")
                self.assertTrue(prompts)
                self.assertIn("Changed owner responsibility", prompts[0])
                self.assertIn("## Boundary reference: packages/a/OWNER.md", prompts[0])

    def test_root_blueprint_is_snapshot_bound_in_full_and_incremental_prompts(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            manifest = repo / "AGENTS.md"
            manifest.write_text(manifest.read_text() + "\nROOT SINGLE OWNER BLUEPRINT\n")
            manifest.rename(repo / "CLAUDE.md")
            manifest.symlink_to("CLAUDE.md")
            (repo / "docs/blueprint.md").write_text("DECLARED ARCHITECTURE CONTENT\n")
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("value = 1\n")
            (repo / ".guard").mkdir()
            (repo / ".guard/config.json").write_text(json.dumps({"topology": {
                "architecture_docs": ["docs/blueprint.md"], "modules": [
                    {"path": "services/a", "boundary_docs": ["AGENTS.md", "CLAUDE.md"]}]}}))
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "root and module contracts")
            run_architecture_review(repo, reviewer_fn=lambda _: {
                "verdict": "GO", "summary": "reviewed", "blocking_findings": []})
            scope = collect_review_scope(repo)
            session = load_review_session(repo)
            (repo / "CLAUDE.md").write_text("DIRTY ROOT PROSE")
            (repo / "docs/blueprint.md").write_text("DIRTY BLUEPRINT")
            prompts = [build_review_prompt(repo, scope), build_incremental_review_prompt(
                repo, scope, session, None, reviewer_note="Verify the same committed owner evidence")]
            for prompt in prompts:
                self.assertIn("## Root architecture & agent instructions: AGENTS.md", prompt)
                # It also occurs in the branch diff, so inspect the context section specifically.
                context = prompt.split("## Root architecture & agent instructions: AGENTS.md", 1)[1].split("## Architecture blueprint:", 1)[0]
                self.assertIn("ROOT SINGLE OWNER BLUEPRINT", context)
                self.assertNotIn("schema: the-guard-policy/v1", context)
                self.assertNotIn("<!-- the-guard-policy:start -->", context)
                self.assertIn("DECLARED ARCHITECTURE CONTENT", prompt)
                self.assertNotIn("DIRTY ROOT PROSE", prompt)
                self.assertNotIn("DIRTY BLUEPRINT", prompt)
                self.assertNotIn("## Boundary reference: AGENTS.md", prompt)
                self.assertNotIn("## Boundary reference: CLAUDE.md", prompt)
            self.assertIn("Holistic Root-Cause & Layer Boundary Integrity", prompts[1])
            self.assertIn("introduced_by_delta", prompts[1])

    def test_blueprints_require_opt_in_and_filter_paths_and_ledgers(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            base = _git(repo, "rev-parse", "HEAD")
            docs = repo / "docs"
            docs.mkdir(exist_ok=True)
            paths = ("docs/design.md", "docs/ledger.md", "docs/notes.md")
            for path in (*paths, "docs/reference.md", "docs/guide.md"):
                (repo / path).write_text("See docs/reference.md and docs/guide.md.\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Write design and other Markdown")
            head = _git(repo, "rev-parse", "HEAD")
            with patch("the_guard.review.documents.git_stdout") as git, patch(
                "the_guard.review.documents._task_design_references"
            ) as references:
                selection = resolve_task_design_blueprints(repo, paths, base_sha=base, head_sha=head)
                self.assertEqual(selection.previews, [])
                self.assertEqual(selection.anchor_snapshots, [])
                git.assert_not_called()
                references.assert_not_called()
            config = GuardConfig(review={"blueprint_globs": ["docs/design.md", "docs/reference.md", "docs/ledger.md"],
                                         "operational_ledgers": ["docs/ledger.md"]})
            selection = resolve_task_design_blueprints(repo, paths, base_sha=base, head_sha=head, config=config)
            self.assertEqual([doc.path for doc in selection.previews], ["docs/design.md", "docs/reference.md"])

    def test_retained_blueprint_records_new_revisions_after_discovery_filters_change(self):
        plan = "docs/plan.md"
        for settings in ({}, {"blueprint_globs": ["docs/other.md"]},
                         {**BLUEPRINT_REVIEW, "operational_ledgers": [plan]}):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
                prompts = []

                def reviewer(prompt):
                    prompts.append(prompt)
                    return {"verdict": "GO", "summary": "reviewed", "blocking_findings": []}

                (repo / plan).write_text("Initial owner commitment\n")
                (repo / "app.py").write_text("owner = 1\n")
                _git(repo, "add", plan, "app.py")
                _git(repo, "commit", "-m", "Record initial plan")
                first = run_architecture_review(repo, reviewer_fn=reviewer)
                self.assertEqual(load_review_session(repo).task_blueprint_anchors[plan], (first.head_sha,))

                config_path = repo / ".guard/config.json"
                config = json.loads(config_path.read_text())
                config["review"] = settings
                config_path.write_text(json.dumps(config))
                (repo / plan).write_text("Newly reviewed owner constraint\n")
                _git(repo, "add", ".guard/config.json", plan)
                _git(repo, "commit", "-m", "Tighten retained plan and change discovery")
                second = run_architecture_review(repo, reviewer_fn=reviewer)
                self.assertEqual(load_review_session(repo).task_blueprint_anchors[plan],
                                 (first.head_sha, second.head_sha))

                _git(repo, "rm", plan)
                _git(repo, "commit", "-m", "Remove plan")
                run_architecture_review(repo, reviewer_fn=reviewer)
                self.assertIn("Newly reviewed owner constraint", prompts[-1])

    def test_task_blueprints_are_snapshot_bound_in_full_and_incremental_prompts(self):
        plan = "docs/superpowers/plans/consolidation.md"
        anchor = "docs/reports/anchor-0.md"
        for source in ("plan", "note"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
                (repo / anchor).parent.mkdir(parents=True)
                (repo / anchor).write_text("Committed Anchor-0 audit\n")
                _git(repo, "add", anchor)
                _git(repo, "commit", "-m", "Record Anchor-0 before implementation")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

                plan_body = "Committed consolidation plan\n"
                if source == "plan":
                    plan_body += f"See [Anchor-0]({anchor}#findings).\n"
                (repo / plan).parent.mkdir(parents=True)
                (repo / plan).write_text(plan_body)
                (repo / "app.py").write_text("value = 1\n")
                _git(repo, "add", plan, "app.py")
                _git(repo, "commit", "-m", "Consolidate the owner")
                run_architecture_review(repo, reviewer_fn=lambda _: {
                    "verdict": "GO", "summary": "reviewed", "blocking_findings": []})
                session = load_review_session(repo)
                (repo / "app.py").write_text("value = 2\n")
                _git(repo, "add", "app.py")
                _git(repo, "commit", "-m", "Refine the owner implementation")

                scope = collect_review_scope(repo)
                incremental_scope = collect_incremental_scope(repo, scope, session)
                self.assertIn(plan, scope.changed_files)
                self.assertNotIn(anchor, scope.changed_files)
                self.assertEqual(incremental_scope.changed_files, ("app.py",))

                (repo / plan).write_text("DIRTY PLAN\n")
                (repo / anchor).write_text("DIRTY ANCHOR\n")
                note = f"Verify `{anchor}`" if source == "note" else "Verify the plan"
                prompts = (
                    build_review_prompt(repo, scope, reviewer_note=note),
                    build_incremental_review_prompt(
                        repo, scope, session, incremental_scope, reviewer_note=note,
                    ),
                )
                plan_section = (
                    f"## Prior reviewed branch blueprint (@ {session.initial_head_sha[:12]}): "
                    f"{plan}\n{plan_body}"
                )
                anchor_section = (
                    f"## Baseline task blueprint (immutable Anchor 0 @ {scope.merge_base[:12]}): "
                    f"{anchor}\n"
                    "Committed Anchor-0 audit"
                )
                for mode, prompt in zip(("full", "incremental"), prompts):
                    with self.subTest(mode=mode):
                        self.assertIn(plan_section, prompt)
                        self.assertIn(anchor_section, prompt)
                        self.assertLess(prompt.index(anchor_section), prompt.index(plan_section))
                        self.assertNotIn("DIRTY PLAN", prompt)
                        self.assertNotIn("DIRTY ANCHOR", prompt)
                        self.assertIn(
                            "## Whole-Module & Blueprint Snapshot Inspection", prompt
                        )

    def test_task_blueprint_discovery_prioritizes_and_caps_snapshot_paths(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            documents = {
                "task-plan.md": (
                    "See ./docs/context.md, docs/contract.md, docs/missing.md, "
                    "docs/publishing-ledger.md and ../docs/overflow.md."
                ),
                "docs/summary.md": "Task summary",
                "docs/context.md": "See docs/overflow.md for additional detail.",
                "docs/note.md": "Note evidence",
                "docs/overflow.md": "Outside the document budget",
                "docs/contract.md": "Already loaded contract",
                "docs/publishing-ledger.md": "Publishing ledger",
                "WORKING.md": "Working ledger",
            }
            for path, body in documents.items():
                (repo / path).write_text(body)
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Record design documents")
            (repo / "docs/missing.md").write_text("Working-tree only")

            selection = resolve_task_design_blueprints(
                repo,
                ("task-plan.md", "docs/missing.md", "WORKING.md", "docs/summary.md"),
                base_sha=_git(repo, "rev-parse", "HEAD^"),
                head_sha=_git(repo, "rev-parse", "HEAD"),
                config=GuardConfig(review={**BLUEPRINT_REVIEW, "operational_ledgers": [
                    "WORKING.md", "docs/publishing-ledger.md",
                ]}),
                initial_reviewed_sha="0" * 40,
                reviewer_note="See docs/context.md, docs/note.md, then docs/overflow.md.",
                excluded_paths=("docs/contract.md",),
            )
            self.assertEqual(
                [snapshot.path for snapshot in selection.previews],
                ["task-plan.md", "docs/summary.md", "docs/context.md", "docs/note.md"],
            )
            self.assertTrue(all(
                snapshot.baseline_sha is None and not snapshot.in_merge_base
                for snapshot in selection.previews
            ))

    def test_merge_base_blueprint_and_anchor_survive_edit_or_deletion(self):
        plan = "docs/consolidation-plan.md"
        anchor = "docs/anchor-0-audit.md"
        for change in ("weaken", "delete"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
                (repo / plan).write_text(
                    f"Retire the outbox.\nSee [Anchor 0]({anchor}#findings).\n"
                )
                (repo / anchor).write_text("Original audit evidence\n")
                _git(repo, "add", plan, anchor)
                _git(repo, "commit", "-m", "Commit the original acceptance scope")
                base = _git(repo, "rev-parse", "HEAD")
                _git(repo, "update-ref", "refs/remotes/origin/main", base)

                if change == "delete":
                    (repo / plan).unlink()
                else:
                    (repo / plan).write_text(
                        "Defer outbox retirement.\n" + "Additional scope commentary.\n" * 500
                    )
                (repo / "app.py").write_text("value = 1\n")
                _git(repo, "add", "-A")
                _git(repo, "commit", "-m", "Revise acceptance scope and code")
                prompt = build_review_prompt(repo, collect_review_scope(repo))

                self.assertIn(
                    f"## Baseline task blueprint (immutable Anchor 0 @ {base[:12]}): "
                    f"{plan}\nRetire the outbox.",
                    prompt,
                )
                changes = f"### Task blueprint modifications on reviewed branch: {plan}"
                self.assertIn(changes, prompt)
                self.assertIn(
                    "[DELETED AT HEAD]" if change == "delete" else "+Defer outbox retirement.",
                    prompt,
                )
                if change == "weaken":
                    diff = prompt.split(changes + "\n```diff\n", 1)[1].split("\n```", 1)[0]
                    self.assertLessEqual(len(diff), 8_000)
                    self.assertTrue(diff.endswith("[truncated, inspect the full snapshot diff]"))
                self.assertIn(
                    f"## Baseline task blueprint (immutable Anchor 0 @ {base[:12]}): "
                    f"{anchor}\nOriginal audit evidence",
                    prompt,
                )

    def test_first_reviewed_blueprint_survives_later_rounds_and_provider_restarts(self):
        plan = "docs/consolidation-plan.md"
        no_go = ReviewOutput("NO-GO", "Outbox still live", (), "")
        for change in ("incremental", "force", "provider", "contract", "delete"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
                (repo / plan).write_text("Retire the outbox.\n")
                (repo / "app.py").write_text("value = 1\n")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "Round 1 plan and implementation")

                provider = "codex"
                with (
                    patch("the_guard.review.engine.resolve_provider_order") as providers,
                    patch("the_guard.review.engine._invoke_provider") as invoke,
                    patch("the_guard.review.engine.record_working_provider"),
                ):
                    providers.return_value = ([(provider, "/mock/codex")], {})
                    invoke.return_value = (no_go, "review-session", "")
                    first = run_architecture_review(repo)
                    first_state = load_review_session(repo)
                    self.assertEqual(first.verdict, "NO-GO")
                    self.assertEqual(first_state.initial_head_sha, first.head_sha)

                    if change == "delete":
                        (repo / plan).unlink()
                    else:
                        (repo / plan).write_text("Defer outbox retirement.\n")
                    if change == "provider":
                        providers.return_value = ([("claude", "/mock/claude")], {})
                    if change == "contract":
                        (repo / ".guard/config.json").write_text(
                            json.dumps({"review": {**BLUEPRINT_REVIEW, "project_title": "Renamed project"}})
                        )
                    (repo / "app.py").write_text("value = 2\n")
                    _git(repo, "add", "-A")
                    _git(repo, "commit", "-m", "Round 2 relaxes the original plan")
                    second = run_architecture_review(repo, force=change == "force")
                    second_prompt = invoke.call_args.args[2]
                    expected_mode = (
                        "full" if change in {"force", "provider", "contract"} else "incremental"
                    )
                    self.assertEqual(second.mode, expected_mode)
                    self.assertEqual(load_review_session(repo).initial_head_sha, first.head_sha)

                    (repo / "app.py").write_text("value = 3\n")
                    _git(repo, "add", "app.py")
                    _git(repo, "commit", "-m", "Round 3 changes only code")
                    third = run_architecture_review(repo)
                    third_prompt = invoke.call_args.args[2]
                    third_state = load_review_session(repo)
                    self.assertEqual(third.mode, "incremental")
                    self.assertEqual(third_state.initial_head_sha, first.head_sha)
                    self.assertEqual(third_state.last_head_sha, third.head_sha)
                    if change == "incremental":
                        self.assertEqual(third_state.round_count, 3)
                    if change == "delete":
                        self.assertNotIn(plan, collect_review_scope(repo).changed_files)

                for prompt in (second_prompt, third_prompt):
                    self.assertIn(
                        "## Prior reviewed branch blueprint (@ "
                        f"{first.head_sha[:12]}): {plan}\nRetire the outbox.",
                        prompt,
                    )
                    self.assertIn(
                        "[DELETED AT HEAD]" if change == "delete" else
                        "-Retire the outbox.\n+Defer outbox retirement.",
                        prompt,
                    )

    def test_later_reviewed_blueprints_survive_classification_changes_and_deletion(self):
        plan = "docs/consolidation-plan.md"
        anchor = "docs/referenced-audit.md"
        for change in ("promote", "unlink", "delete"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
                (repo / anchor).write_text("Authoritative audit evidence\n")
                _git(repo, "add", anchor)
                _git(repo, "commit", "-m", "Record the audit upstream")
                base = _git(repo, "rev-parse", "HEAD")
                _git(repo, "update-ref", "refs/remotes/origin/main", base)

                prompts = []

                def reviewer(prompt):
                    prompts.append(prompt)
                    return {"verdict": "NO-GO", "summary": "Open work", "blocking_findings": []}

                (repo / "app.py").write_text("value = 1\n")
                _git(repo, "add", "app.py")
                _git(repo, "commit", "-m", "Start work without a task blueprint")
                run_architecture_review(repo, reviewer_fn=reviewer)

                (repo / plan).write_text(f"Retire the outbox. See {anchor}.\n")
                _git(repo, "add", plan)
                _git(repo, "commit", "-m", "Commit the acceptance scope in round 2")
                second = run_architecture_review(repo, reviewer_fn=reviewer)

                if change == "promote":
                    (repo / ".guard/config.json").write_text(json.dumps({
                        "review": BLUEPRINT_REVIEW,
                        "topology": {"architecture_docs": [plan]},
                    }))
                if change == "delete":
                    (repo / plan).unlink()
                else:
                    (repo / plan).write_text("Retire the outbox.\n")
                _git(repo, "add", "-A")
                _git(repo, "commit", "-m", "Weaken the reviewed plan")
                run_architecture_review(repo, reviewer_fn=reviewer)

                (repo / "app.py").write_text("value = 2\n")
                _git(repo, "add", "app.py")
                _git(repo, "commit", "-m", "Continue with a code-only change")
                run_architecture_review(repo, force=True, reviewer_fn=reviewer)

                self.assertIn(
                    f"## Prior reviewed branch blueprint (@ {second.head_sha[:12]}): "
                    f"{plan}\nRetire the outbox.",
                    prompts[-1],
                )
                self.assertIn(
                    "[DELETED AT HEAD]" if change == "delete" else "+Retire the outbox.",
                    prompts[-1],
                )
                self.assertIn("Authoritative audit evidence", prompts[-1])
                self.assertEqual(load_review_session(repo).task_blueprint_anchors[anchor], (base,))

    def test_reviewed_blueprint_tightening_survives_reversion_and_forced_review(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            merged_plan = "docs/merged-plan.md"
            branch_plan = "docs/branch-plan.md"
            original = "Keep one owner.\n"
            (repo / merged_plan).write_text(original)
            _git(repo, "add", merged_plan)
            _git(repo, "commit", "-m", "Publish merge-base blueprint")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "update-ref", "refs/remotes/origin/main", base)

            prompts = []

            def reviewer(prompt):
                prompts.append(prompt)
                return {"verdict": "NO-GO", "summary": "Open work", "blocking_findings": []}

            (repo / merged_plan).write_text(original + "Begin consolidation.\n")
            (repo / branch_plan).write_text(original)
            (repo / "app.py").write_text("value = 1\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Start the branch")
            first = run_architecture_review(repo, reviewer_fn=reviewer)

            session_path, = (git_common_dir(repo) / "the-guard-sessions").glob("*.json")
            legacy = json.loads(session_path.read_text())
            legacy["task_blueprint_anchors"] = {merged_plan: base, branch_plan: first.head_sha}
            session_path.write_text(json.dumps(legacy))
            legacy_anchors = load_review_session(repo).task_blueprint_anchors

            commitments = {
                merged_plan: "Retire the named outbox.",
                branch_plan: "Limit the owner to one state transition.",
            }
            for path, commitment in commitments.items():
                (repo / path).write_text(original + commitment + "\n")
            _git(repo, "commit", "-am", "Tighten both reviewed blueprints")
            second = run_architecture_review(repo, reviewer_fn=reviewer)

            for path in commitments:
                (repo / path).write_text(original)
            _git(repo, "commit", "-am", "Restore the original blueprints")
            run_architecture_review(repo, force=True, reviewer_fn=reviewer)
            (repo / "app.py").write_text("value = 2\n")
            _git(repo, "commit", "-am", "Change implementation only")
            run_architecture_review(repo, reviewer_fn=reviewer)

            for prompt in prompts[-2:]:
                self.assertIn(
                    f"## Baseline task blueprint (immutable Anchor 0 @ {base[:12]}): "
                    f"{merged_plan}\n{original}",
                    prompt,
                )
                self.assertIn(
                    f"## Prior reviewed branch blueprint (@ {first.head_sha[:12]}): "
                    f"{branch_plan}\n{original}",
                    prompt,
                )
                self.assertEqual(prompt.splitlines().count(original.rstrip()), len(commitments))
                for path, commitment in commitments.items():
                    _, heading, history = prompt.partition(
                        f"### Prior reviewed branch blueprint (@ {second.head_sha[:12]}): "
                        f"{path}\n",
                    )
                    self.assertTrue(heading)
                    baseline_diff, heading, head_diff = history.partition(
                        f"### Task blueprint modifications on reviewed branch: {path}\n"
                    )
                    self.assertTrue(heading)
                    self.assertIn(f"+{commitment}", baseline_diff)
                    self.assertIn(f"-{commitment}", head_diff.split("```", 2)[1])
            self.assertEqual(legacy_anchors, {merged_plan: (base,), branch_plan: (first.head_sha,)})
            expected = {
                merged_plan: (base, first.head_sha, second.head_sha),
                branch_plan: (first.head_sha, second.head_sha),
            }
            self.assertEqual(load_review_session(repo).task_blueprint_anchors, expected)
            self.assertEqual(
                json.loads(session_path.read_text())["task_blueprint_anchors"],
                {path: list(revisions) for path, revisions in expected.items()},
            )

    def test_blueprint_references_resolve_in_the_source_revision(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            plan = "docs/specs/plan.md"
            audit = "docs/specs/audit.md"
            (repo / plan).parent.mkdir()
            (repo / plan).write_text("[Audit](audit.md)\n")
            (repo / audit).write_text("Original audit beside the plan\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Publish plan and its audit")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "update-ref", "refs/remotes/origin/main", base)

            (repo / plan).write_text("[Audit](audit.md)\nRevise the plan.\n")
            (repo / "audit.md").write_text("New audit at the repository root\n")
            (repo / "app.py").write_text("value = 1\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Add a competing audit at the root")
            scope = collect_review_scope(repo)
            context = _snapshot_context(repo, scope)
            prompt = "\n".join(context.sections)
            original_audit = f"immutable Anchor 0 @ {base[:12]}): {audit}\nOriginal audit"
            changed_plan = f"immutable Anchor 0 @ {base[:12]}): {plan}"
            self.assertIn(original_audit, prompt)
            self.assertIn("## Task design & architecture blueprint: audit.md", prompt)
            self.assertLess(prompt.index(original_audit), prompt.index(changed_plan))
            self.assertEqual(context.task_blueprint_anchors[audit], (base,))
            self.assertEqual(context.task_blueprint_anchors["audit.md"], (scope.head_sha,))
            selection = resolve_task_design_blueprints(
                repo, (plan,), base_sha=base, head_sha=scope.head_sha, config=GuardConfig(review=BLUEPRINT_REVIEW),
            )
            self.assertEqual([snapshot.path for snapshot in selection.previews], [audit, plan, "audit.md"])

    def test_recorded_blueprint_revision_requires_the_anchored_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            before_plan = _git(repo, "rev-parse", "HEAD")
            plan = "docs/plan.md"
            (repo / plan).write_text("Merge-base plan\n")
            _git(repo, "add", plan)
            _git(repo, "commit", "-m", "Publish blueprint")
            base = _git(repo, "rev-parse", "HEAD")
            with self.assertRaisesRegex(ValueError, f"Recorded task blueprint missing at {before_plan}"):
                resolve_task_design_blueprints(
                    repo, (plan,), base_sha=base, head_sha=base, config=GuardConfig(review=BLUEPRINT_REVIEW),
                    retained_anchors={plan: (base, before_plan)},
                )

    def test_persisted_blueprint_references_stay_one_hop_across_review_rounds(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            plan = "docs/plan.md"
            audit = "docs/audit.md"
            note_audit = "docs/note-audit.md"
            documents = {
                audit: "Plan audit. See [supporting audit](./supporting-audit.md).\n",
                note_audit: "Note audit. See [supporting audit](./supporting-audit.md).\n",
                "docs/supporting-audit.md": "Supporting audit. See [appendix](./appendix.md).\n",
                "docs/appendix.md": "Audit appendix.\n",
            }
            for path, body in documents.items():
                (repo / path).write_text(body)
            _git(repo, "add", "docs")
            _git(repo, "commit", "-m", "Publish the audit chain upstream")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "update-ref", "refs/remotes/origin/main", base)

            def reviewer(_):
                return {"verdict": "GO", "summary": "reviewed", "blocking_findings": []}

            (repo / plan).write_text("Consolidate the owner. See [audit](./audit.md).\n")
            (repo / "app.py").write_text("value = 1\n")
            _git(repo, "add", plan, "app.py")
            _git(repo, "commit", "-m", "Start the consolidation")
            first = run_architecture_review(repo, reviewer_fn=reviewer, reviewer_note=f"See {note_audit}")
            expected = {plan: (first.head_sha,), audit: (base,), note_audit: (base,)}
            self.assertEqual(load_review_session(repo).task_blueprint_anchors, expected)

            for round_number in (2, 3):
                with self.subTest(round_number=round_number):
                    (repo / "app.py").write_text(f"value = {round_number}\n")
                    _git(repo, "commit", "-am", f"Refine the implementation in round {round_number}")
                    run_architecture_review(repo, reviewer_fn=reviewer)
                    self.assertEqual(load_review_session(repo).task_blueprint_anchors, expected)

    def test_unchanged_retained_contracts_leave_four_inline_blueprint_slots(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            architecture = "docs/architecture.md"
            (repo / architecture).write_text("Loaded architecture contract\n")
            _git(repo, "add", architecture)
            _git(repo, "commit", "-m", "Publish the architecture contract upstream")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "update-ref", "refs/remotes/origin/main", base)

            plans = [f"docs/plan-{index}.md" for index in range(4)]
            for path in plans:
                (repo / path).write_text(f"Task blueprint in {path}\n")
            (repo / "app.py").write_text("value = 1\n")
            _git(repo, "add", "docs", "app.py")
            _git(repo, "commit", "-m", "Write four branch blueprints")

            retained = {"AGENTS.md": (base,), architecture: (base,)}
            context = _snapshot_context(repo, collect_review_scope(repo), task_blueprint_anchors=retained)
            prompt = "\n".join(context.sections)
            for path in plans:
                self.assertIn(f"## Task design & architecture blueprint: {path}\n", prompt)
            for path, revisions in retained.items():
                self.assertNotIn(f"immutable Anchor 0 @ {base[:12]}): {path}\n", prompt)
                self.assertEqual(context.task_blueprint_anchors[path], revisions)
            self.assertIn("## Root architecture & agent instructions: AGENTS.md", prompt)
            self.assertIn(f"## Architecture blueprint: {architecture}", prompt)

    def test_relative_audits_from_reviewed_snapshots_take_priority_and_survive_overflow(self):
        plan = "docs/superpowers/specs/consolidation.md"
        for audit_count in (1, 5):
            with self.subTest(audit_count=audit_count), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
                (repo / plan).parent.mkdir(parents=True)
                (repo / plan).write_text("Retire the outbox.\n")
                merged_plan = "docs/budget.md"
                (repo / merged_plan).write_text("Baseline budget\n")
                audits = [f"docs/reports/{index}-audit.md" for index in range(audit_count)]
                (repo / "docs/reports").mkdir()
                for path in audits:
                    (repo / path).write_text(f"Authoritative audit evidence in {path}\n")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "Record upstream plans and audits")
                base = _git(repo, "rev-parse", "HEAD")
                _git(repo, "update-ref", "refs/remotes/origin/main", base)

                drafts = [f"docs/{index}-draft.md" for index in range(4)]
                for path in drafts:
                    (repo / path).write_text("Branch draft\n")
                (repo / merged_plan).write_text("Revised budget\n")
                (repo / "app.py").write_text("value = 1\n")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "Start branch plans")
                initial = _git(repo, "rev-parse", "HEAD")

                links = "".join(
                    f"[Audit](../../reports/{Path(path).name}#findings)\n" for path in audits
                )
                (repo / plan).write_text(f"Retire the outbox.\n{links}")
                _git(repo, "add", plan)
                _git(repo, "commit", "-m", "Cite audits in an intermediate reviewed plan")
                intermediate = _git(repo, "rev-parse", "HEAD")
                (repo / plan).write_text("Retire the outbox atomically.\n")
                _git(repo, "add", plan)
                _git(repo, "commit", "-m", "Remove the audit links")
                scope = collect_review_scope(repo)

                head = scope.head_sha

                def assert_context(context, *, retained_revisions=()):
                    prompt = "\n".join(context.sections)
                    evidence = f"Authoritative audit evidence in {audits[0]}"
                    self.assertIn(evidence, prompt)
                    index_text = prompt.split(
                        "## Additional previously reviewed task blueprints\n", 1
                    )[1]
                    guidance, index_json = index_text.split("\n", 1)
                    self.assertIn("immutable Anchor 0", guidance)
                    self.assertIn("Other revisions record previously reviewed branch blueprints", guidance)
                    self.assertIn("Compare the recorded bodies with HEAD", guidance)
                    self.assertIn("apply the Blocking policy below", guidance)
                    index, _ = json.JSONDecoder().raw_decode(index_json)
                    self.assertEqual(
                        context.task_blueprint_anchors,
                        dict.fromkeys(audits, (base,))
                        | dict.fromkeys(drafts, (initial,))
                        | {plan: (base, intermediate, head), merged_plan: (base, initial)}
                        | {drafts[0]: (initial, *retained_revisions)},
                    )
                    if retained_revisions:
                        self.assertTrue(set(retained_revisions).issubset(index[drafts[0]]))
                    if audit_count == 1:
                        self.assertLess(prompt.index(evidence), prompt.index("Baseline budget"))
                        self.assertLess(prompt.index("Baseline budget"), prompt.index("Branch draft"))
                        self.assertEqual(set(drafts) & index.keys(), set(drafts[1:]) | (
                            {drafts[0]} if retained_revisions else set()
                        ))
                        if retained_revisions:
                            self.assertEqual(index[drafts[0]], list(retained_revisions))
                    else:
                        self.assertEqual(index[audits[-1]], [base])
                        self.assertEqual(index[plan], [base, intermediate, head])
                        self.assertEqual(set(index), {audits[-1], plan, merged_plan, *drafts})

                context = _snapshot_context(
                    repo, scope, initial_reviewed_sha=initial, prior_reviewed_sha=intermediate,
                )
                assert_context(context)
                assert_context(_snapshot_context(
                    repo, scope, initial_reviewed_sha=initial,
                    task_blueprint_anchors={drafts[0]: intermediate},
                ), retained_revisions=(intermediate,))
                assert_context(_snapshot_context(
                    repo, scope, initial_reviewed_sha=initial,
                    task_blueprint_anchors=context.task_blueprint_anchors,
                ))

    def test_blueprint_reference_resolution_uses_committed_repository_paths(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            plan = "docs/specs/plan.md"
            (repo / plan).parent.mkdir()
            documents = {
                "root-audit.md": "Repository-root evidence",
                "docs/specs/root-audit.md": "Shadowed sibling evidence",
                "docs/specs/sibling-audit.md": "Fallback sibling evidence",
                "docs/specs/foo.md": "Explicit sibling evidence",
                "docs/absolute-audit.md": "Absolute path decoy",
                "docs/escape-audit.md": "Escaping path decoy",
            }
            for path, body in documents.items():
                (repo / path).write_text(body)
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Record audit candidates")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "update-ref", "refs/remotes/origin/main", base)

            (repo / plan).write_text(
                "See root-audit.md, sibling-audit.md and [sibling](./foo.md).\n"
                "Ignore /docs/absolute-audit.md and ../../../docs/escape-audit.md.\n"
            )
            (repo / "app.py").write_text("value = 1\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Cite relative and repository-root evidence")
            (repo / "sibling-audit.md").write_text("Working-tree decoy evidence")
            context = _snapshot_context(repo, collect_review_scope(repo))
            self.assertEqual(
                context.task_blueprint_anchors,
                dict.fromkeys(
                    ("root-audit.md", "docs/specs/sibling-audit.md", "docs/specs/foo.md"), (base,)
                ) | {plan: (_git(repo, "rev-parse", "HEAD"),)},
            )
            prompt = "\n".join(context.sections)
            for path in ("root-audit.md", "docs/specs/sibling-audit.md", "docs/specs/foo.md"):
                self.assertIn(documents[path], prompt)

    def test_blueprint_symlink_diff_compares_the_committed_target_bodies(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review=BLUEPRINT_REVIEW)
            (repo / "docs/task-plan.md").symlink_to("plan-source.txt")
            source = repo / "docs/plan-source.txt"
            source.write_text("Retire the outbox.\n")
            _git(repo, "add", "docs")
            _git(repo, "commit", "-m", "Commit the original symlinked blueprint")
            base = _git(repo, "rev-parse", "HEAD")
            _git(repo, "update-ref", "refs/remotes/origin/main", base)
            source.write_text("Defer outbox retirement.\n")
            (repo / "app.py").write_text("value = 1\n")
            _git(repo, "add", "docs", "app.py")
            _git(repo, "commit", "-m", "Change the blueprint target")

            scope = collect_review_scope(repo)
            self.assertEqual(scope.reviewable_files, ("app.py", "docs/plan-source.txt"))
            prompt = build_review_prompt(repo, scope)
            self.assertIn(
                f"## Baseline task blueprint (immutable Anchor 0 @ {base[:12]}): "
                "docs/task-plan.md\nRetire the outbox.",
                prompt,
            )
            self.assertIn("-Retire the outbox.\n+Defer outbox retirement.", prompt)

            def reviewer(_):
                return {"verdict": "NO-GO", "summary": "Open work", "blocking_findings": []}

            second = run_architecture_review(repo, reviewer_fn=reviewer)
            (repo / "docs/task-plan.md").unlink()
            _git(repo, "commit", "-am", "Delete the symlinked blueprint")
            scope = collect_review_scope(repo)
            prompt = build_review_prompt(repo, scope)
            _, heading, history = prompt.partition(
                f"### Prior reviewed branch blueprint (@ {second.head_sha[:12]}): docs/task-plan.md\n"
            )
            self.assertTrue(heading)
            self.assertIn(
                "-Retire the outbox.\n+Defer outbox retirement.\n```\n"
                "### Task blueprint modifications on reviewed branch: docs/task-plan.md\n"
                "[DELETED AT HEAD]",
                history,
            )

    def test_methodology_requires_zero_based_whole_module_reconciliation(self):
        obligations = (
            "Derive the post-change minimal model from first principles",
            "copy child-table fields and secondary status columns onto a parent",
            "preserving separate state machines or control loops",
            "Approved repairs, retirements, constraints, and complexity budgets remain binding",
            "Require explicit user approval",
            "evidence of an independent lifecycle for any retained entity",
            "preserves the approved commitments and equivalent protection at the sole owner",
        )
        preset = (REPO_ROOT / "src/the_guard/presets/rules/architecture-simplicity.md").read_text()
        for configured in (False, True):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td))
                if configured:
                    details = "docs/standards/rules/architecture-simplicity.md"
                    (repo / details).write_text(preset)
                    manifest = repo / "AGENTS.md"
                    manifest.write_text(manifest.read_text().replace(
                        "```\n<!-- the-guard-policy:end -->",
                        "  - id: guard.architecture-simplicity\n"
                        "    severity: block\n"
                        "    scope: [backend]\n"
                        "    summary: Each lifecycle has one owner.\n"
                        f"    details: {details}\n"
                        "    checker: static-plus-review\n"
                        "```\n<!-- the-guard-policy:end -->",
                    ))
                    _git(repo, "add", "AGENTS.md", details)
                    _git(repo, "commit", "-m", "Install the architecture simplicity preset upstream")
                    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                (repo / "app.py").write_text("value = 1\n")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "Start implementation")
                run_architecture_review(repo, reviewer_fn=lambda _: {
                    "verdict": "GO", "summary": "reviewed", "blocking_findings": [],
                })
                session = load_review_session(repo)
                (repo / "app.py").write_text("value = 2\n")
                _git(repo, "commit", "-am", "Revise implementation")
                scope = collect_review_scope(repo)
                incremental_scope = collect_incremental_scope(repo, scope, session)
                prompts = {
                    "full": build_review_prompt(repo, scope),
                    "incremental": build_incremental_review_prompt(
                        repo, scope, session, incremental_scope,
                    ),
                }

                for mode, prompt in prompts.items():
                    with self.subTest(mode=mode):
                        heading = "## Whole-Module & Blueprint Snapshot Inspection\n"
                        self.assertIn(heading, prompt)
                        section = prompt.split(heading, 1)[1].split("\n## ", 1)[0]
                        for evidence in (
                            "`git show <target-sha>:<path>`", "`git grep <pattern> <target-sha> -- <module>`",
                            "schema and migrations", "writers", "lifecycle transitions", "readers",
                            "outside the changed lines", "immutable Anchor 0", "prior reviewed branch blueprints",
                            "Blocking policy below",
                        ):
                            self.assertIn(evidence, section)
                        policy_heading = "### guard.architecture-simplicity [backend]\n"
                        before_rule, separator, rule_and_rest = prompt.partition(policy_heading)
                        if configured:
                            self.assertEqual(separator, policy_heading)
                            self.assertIn("## Blocking policy", before_rule)
                            rule_body = preset.split("\n---\n", 1)[1].strip()
                            self.assertIn(rule_body, rule_and_rest)
                            for obligation in obligations:
                                self.assertIn(obligation, rule_body)
                        else:
                            self.assertFalse(separator)
                        outside_rule = " ".join(before_rule.split()).replace("Anchor-0", "Anchor 0").replace("`", "")
                        for obligation in (*obligations, "block surviving dual persistence"):
                            self.assertNotIn(
                                obligation.replace("Anchor-0", "Anchor 0").replace("`", ""), outside_rule,
                            )

    def test_declared_contracts_override_extension_and_test_tiers(self):
        config = GuardConfig(topology={"architecture_docs": ["docs/root.json"], "modules": [
            {"path": "packages/a", "boundary_docs": ["tests/owner.txt"]}]})
        for path in ("docs/root.json", "tests/owner.txt", "packages/a/AGENTS.md", "packages/a/CLAUDE.md"):
            self.assertTrue(is_reviewable_path(path, config), path)
            self.assertEqual(_classify_file_tier(path, config), 0, path)
        for path in ("src/a.mjs", "src/a.cjs"):
            self.assertTrue(is_reviewable_path(path, config))

    def test_rebase_invalidates_receipt_for_changed_root_or_boundary_prose(self):
        for document in ("AGENTS.md", "docs/owner.md"):
            with self.subTest(document=document), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td))
                (repo / ".guard").mkdir()
                (repo / ".guard/config.json").write_text(json.dumps({"topology": {
                    "modules": [{"path": "services/a", "boundary_docs": ["docs/owner.md"]}]}}))
                (repo / "docs/owner.md").write_text("Original owner contract\n")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "baseline contracts")
                _git(repo, "branch", "-f", "main", "HEAD")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                (repo / "services/a").mkdir(parents=True)
                (repo / "services/a/main.py").write_text("value = 1\n")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "reviewed code")
                reviewer = lambda _: {"verdict": "GO", "summary": "reviewed", "blocking_findings": []}
                run_architecture_review(repo, reviewer_fn=reviewer)
                prior = load_review_session(repo)
                old_scope = collect_review_scope(repo)
                digest = compute_review_contract_digest(load_policy(repo), load_config(repo),
                                                       repo_root=repo, revision=old_scope.head_sha,
                                                       scope=old_scope)
                _git(repo, "checkout", "main")
                path = repo / document
                path.write_text(path.read_text() + "\nChanged architectural responsibility\n")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "change owner blueprint upstream")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                _git(repo, "checkout", "topic")
                _git(repo, "rebase", "main")
                scope = collect_review_scope(repo)
                new_digest = compute_review_contract_digest(load_policy(repo), load_config(repo),
                                                           repo_root=repo, revision=scope.head_sha,
                                                           scope=scope)
                self.assertNotEqual(digest, new_digest)
                self.assertIsNone(resolve_or_carry_forward_receipt(repo, scope))
                self.assertEqual(run_architecture_review(repo, reviewer_fn=reviewer).mode, "full")
                saved = load_review_session(repo)
                self.assertTrue(saved.session_contract_digest)
                if document == "AGENTS.md":
                    self.assertNotEqual(prior.session_contract_digest, saved.session_contract_digest)
                else:
                    self.assertEqual(prior.session_contract_digest, saved.session_contract_digest)

    def test_missing_boundary_reference_does_not_bind_an_unbound_module(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/b").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("value = 1\n")
            (repo / "services/b/main.py").write_text("value = 1\n")
            (repo / ".guard").mkdir()
            (repo / ".guard/config.json").write_text(json.dumps({"topology": {"modules": [
                {"path": "services/a", "role": "governed owner"},
                {"path": "services/b", "boundary_docs": ["docs/missing.md"]}]}}))
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "mixed contracts")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            guidance = prompt.split("## Unbound Module Topology Guidance", 1)[1].split("\n", 2)[1]
            self.assertIn("services/b", guidance)
            self.assertNotIn("services/a", guidance)

    def test_deleted_gitlink_is_reviewed_as_root_change_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._init_repo(Path(tmp))
            old_sha = _git(repo, "rev-parse", "HEAD")
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{old_sha},services/example",
            )
            (repo / ".gitmodules").write_text(
                '[submodule "example"]\npath = services/example\nurl = ../example\n'
            )
            _git(repo, "add", ".gitmodules")
            _git(repo, "commit", "-m", "register service")
            _git(repo, "push", "origin", "HEAD:main")
            _git(repo, "rm", "--cached", "services/example")
            _git(repo, "rm", ".gitmodules")
            _git(repo, "commit", "-m", "remove service")
            scope = collect_review_scope(repo)
            self.assertEqual(scope.submodule_changes, ())
            self.assertIn("services/example", scope.reviewable_files)
            prompt = build_review_prompt(repo, scope)
            self.assertIn("services/example", prompt)

    def test_provider_command_enforces_process_level_readonly_flags(self) -> None:
        agy_cmd = _build_provider_cmd(
            "agy",
            "/bin/agy",
            "prompt",
            "{}",
            model="gemini-4-argon-high",
            effort="high",
        )
        self.assertIn("--mode", agy_cmd)
        self.assertEqual(agy_cmd[agy_cmd.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", agy_cmd)
        agy_settings = json.loads(agy_cmd[agy_cmd.index("--settings") + 1])
        self.assertEqual(agy_settings["toolPermission"], "proceed-in-sandbox")
        self.assertEqual(
            agy_settings["permissions"]["allow"], ["read_file(*)", "command(*)"]
        )
        self.assertEqual(
            set(agy_settings["permissions"]["deny"]),
            {
                "write_file(*)",
                "command(git commit)",
                "command(git push)",
                "command(git reset)",
                "command(git checkout)",
                "command(git switch)",
                "command(git merge)",
                "command(git rebase)",
                "command(git cherry-pick)",
                "command(git clean)",
                "command(rm)",
                "command(mv)",
                "command(cp)",
                "command(chmod)",
                "command(chown)",
            },
        )
        self.assertNotIn("--add-dir", agy_cmd)

        agy_common_dir_cmd = _build_provider_cmd(
            "agy", "/bin/agy", "prompt", "{}", common_dir=Path("/repo/.git")
        )
        agy_common_dir_settings = json.loads(
            agy_common_dir_cmd[agy_common_dir_cmd.index("--settings") + 1]
        )
        self.assertEqual(
            agy_common_dir_settings["permissions"]["allow"],
            ["read_file(*)", "read_file(/repo/.git)", "command(*)"],
        )
        self.assertEqual(
            agy_common_dir_settings["permissions"]["deny"],
            agy_settings["permissions"]["deny"],
        )
        self.assertNotIn("--add-dir", agy_common_dir_cmd)
        self.assertEqual(agy_cmd[agy_cmd.index("--model") + 1], "gemini-4-argon-high")
        self.assertEqual(agy_cmd[agy_cmd.index("--effort") + 1], "high")
        self.assertNotIn("--disable-slash-commands", agy_cmd)
        self.assertEqual(agy_cmd[agy_cmd.index("--output-format") + 1], "stream-json")
        # `agy -p` requires its prompt as the flag value and does not read stdin.
        self.assertEqual(agy_cmd[:3], ["/bin/agy", "-p", "prompt"])

        claude_cmd = _build_provider_cmd(
            "claude",
            "/bin/claude",
            "prompt",
            "{}",
            session_id="uuid-1",
            model="opus",
            effort="high",
        )
        self.assertEqual(claude_cmd[claude_cmd.index("--model") + 1], "opus")
        self.assertEqual(claude_cmd[claude_cmd.index("--effort") + 1], "high")
        self.assertEqual(claude_cmd[claude_cmd.index("--permission-mode") + 1], "plan")
        # The prompt is streamed over stdin, never argv, to stay under the OS size limit.
        self.assertEqual(claude_cmd[:2], ["/bin/claude", "-p"])
        self.assertNotIn("prompt", claude_cmd)
        self.assertIn("--disallowedTools", claude_cmd)
        disallowed = claude_cmd[claude_cmd.index("--disallowedTools") + 1]
        self.assertIn("Write", disallowed)
        self.assertIn("Task", disallowed)
        self.assertIn("Agent", disallowed)
        self.assertEqual(claude_cmd[claude_cmd.index("--session-id") + 1], "uuid-1")

        claude_resume_cmd = _build_provider_cmd(
            "claude", "/bin/claude", "inc", "{}", resume_session_id="uuid-1"
        )
        self.assertEqual(
            claude_resume_cmd[claude_resume_cmd.index("--resume") + 1], "uuid-1"
        )
        self.assertNotIn("--session-id", claude_resume_cmd)
        self.assertNotIn("inc", claude_resume_cmd)
        for cmd in (claude_cmd, claude_resume_cmd):
            self.assertIn("--strict-mcp-config", cmd)
            self.assertEqual(cmd[cmd.index("--settings") + 1], '{"disableAllHooks":true}')

        agy_resume_cmd = _build_provider_cmd(
            "agy", "/bin/agy", "inc", "{}", resume_session_id="conv-1"
        )
        self.assertEqual(
            agy_resume_cmd[agy_resume_cmd.index("--conversation") + 1], "conv-1"
        )
        self.assertEqual(agy_resume_cmd[:3], ["/bin/agy", "-p", "inc"])

        codex_cmd = _build_provider_cmd(
            "codex",
            "/bin/codex",
            "prompt",
            "{}",
            schema_file=Path("/tmp/s.json"),
            out_file=Path("/tmp/o.json"),
            model="gpt-6-astra",
            effort="high",
        )
        self.assertIn("-s", codex_cmd)
        self.assertEqual(codex_cmd[codex_cmd.index("-s") + 1], "read-only")

        codex_resume_cmd = _build_provider_cmd(
            "codex",
            "/bin/codex",
            "inc",
            "{}",
            schema_file=Path("/tmp/s.json"),
            out_file=Path("/tmp/o.json"),
            resume_session_id="thread-1",
            model="gpt-6-astra",
            effort="high",
        )
        self.assertEqual(codex_resume_cmd[:3], ["/bin/codex", "exec", "resume"])
        self.assertIn("thread-1", codex_resume_cmd)
        for cmd in (codex_cmd, codex_resume_cmd):
            self.assertIn("--ignore-rules", cmd)
            self.assertIn("--disable", cmd)
            self.assertIn("plugins", cmd)
            self.assertIn("remote_plugin", cmd)
            self.assertIn("hooks", cmd)
            self.assertIn("check_for_update_on_startup=false", cmd)
            self.assertNotIn("--ephemeral", cmd)
            self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6-astra")
            effort_index = cmd.index('model_reasoning_effort="high"')
            self.assertEqual(cmd[effort_index - 1], "-c")
            positional_index = (
                cmd.index("thread-1") if "resume" in cmd else len(cmd) - 1
            )
            self.assertLess(effort_index, positional_index)
            self.assertLess(cmd.index("-m"), positional_index)

        provider_env = _build_provider_env({"PATH": "/usr/bin", "CLAUDECODE": "1"})
        self.assertEqual(provider_env.get("THE_GUARD_REVIEW_ACTIVE"), "1")
        self.assertEqual(provider_env.get("THE_GUARD_SESSION_MODE"), "readonly")
        self.assertNotIn("CLAUDECODE", provider_env)

    def test_codex_exec_and_resume_use_one_mcp_disable_override(self) -> None:
        for session_id in (None, "thread-1"):
            with self.subTest(session_id=session_id):
                cmd = _build_provider_cmd(
                    "codex", "/bin/codex", "prompt", "{}",
                    resume_session_id=session_id,
                    disabled_mcp_servers=("plain", "local-tools", "local.tools"),
                )
                overrides = [
                    value for flag, value in zip(cmd, cmd[1:])
                    if flag == "-c" and value.startswith("mcp_servers")
                ]
                self.assertEqual(len(overrides), 1)
                self.assertEqual(tomllib.loads(overrides[0]), {"mcp_servers": {
                    "plain": {"enabled": False},
                    "local-tools": {"enabled": False},
                    "local.tools": {"enabled": False},
                }})
                position = cmd.index(session_id) if session_id else len(cmd) - 1
                self.assertLess(cmd.index(overrides[0]), position)

    def test_claude_prompt_travels_over_stdin_for_large_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/catalog").mkdir(parents=True)
            (repo / "services/catalog/service.py").write_text(
                "value = 1\n", encoding="utf-8"
            )
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "review scope")
            scope = collect_review_scope(repo)

            fake_bin = tmp / "bin"
            argv_record = tmp / "claude_argv.json"
            stdin_record = tmp / "claude_stdin.txt"
            log_dir = tmp / "logs"
            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, os, subprocess, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('2.0.0')\n"
                "    raise SystemExit(0)\n"
                "with open(os.environ['CLAUDE_ARGV_FILE'], 'w', encoding='utf-8') as f:\n"
                "    json.dump(sys.argv[1:], f)\n"
                "prompt = sys.stdin.read()\n"
                "with open(os.environ['CLAUDE_STDIN_FILE'], 'w', encoding='utf-8') as f:\n"
                "    f.write(prompt)\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'claude-stdin-session'}))\n"
                "print(json.dumps({'type': 'result', 'result': {'structured_output': {\n"
                "    'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),\n"
                "    'verdict': 'GO', 'summary': 'stdin transport ok', 'blocking_findings': []\n"
                "}}}))\n",
            )
            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(tmp / "home"),
                "THE_GUARD_REVIEW_LOG_DIR": str(log_dir),
                "CLAUDE_ARGV_FILE": str(argv_record),
                "CLAUDE_STDIN_FILE": str(stdin_record),
            }
            prompt = "review-prompt-line\n" * 70000
            self.assertGreater(len(prompt.encode("utf-8")), 1_200_000)

            output, session_id, log_path = _invoke_provider(
                "claude",
                str(fake_bin / "claude"),
                prompt,
                repo,
                env,
                scope=scope,
                mode="full",
                full_review_reason="no_prior_session",
            )

            self.assertEqual(output.verdict, "GO")
            self.assertEqual(session_id, "claude-stdin-session")
            recorded_argv = json.loads(argv_record.read_text(encoding="utf-8"))
            self.assertEqual(recorded_argv[:2], ["-p", "--permission-mode"])
            self.assertNotIn(prompt, recorded_argv)
            self.assertNotIn("review-prompt-line", " ".join(recorded_argv))
            self.assertEqual(stdin_record.read_text(encoding="utf-8"), prompt)
            meta = json.loads(
                Path(log_path.replace(".events.jsonl", ".meta.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta["mode"], "full")
            self.assertEqual(meta["full_review_reason"], "no_prior_session")

    def test_snapshot_git_commands_disable_fsmonitor(self) -> None:
        with (
            patch.dict(os.environ, {"GIT_DIR": "/other/.git", "GIT_OPTIONAL_LOCKS": "0"}, clear=True),
            patch("the_guard.review.repository.subprocess.run") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                ["git"], 0, b"0123456789abcdef\n", b""
            )
            result = review_repository._git(Path("/repo"), "rev-parse", "HEAD")

        self.assertEqual(result.stdout, b"0123456789abcdef\n")
        run.assert_called_once_with(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=Path("/repo"),
            env={"GIT_OPTIONAL_LOCKS": "0"},
            check=True,
            capture_output=True,
        )

    def test_provider_review_plan_owns_mode_and_full_review_reason(self) -> None:
        session = ReviewSessionState(
            worktree_path="/repo",
            branch="topic",
            merge_base="a" * 40,
            provider="claude",
            session_id="session-1",
            last_head_sha="b" * 40,
            last_patch_digest="c" * 40,
            last_verdict="GO",
            last_summary="",
            last_blocking_findings=(),
            last_submodule_shas={},
        )
        missing_session_id = replace(session, session_id="")
        cases = [
            (
                ("full", "resume_failed"),
                {
                    "force": False,
                    "stored_session": session,
                    "prior_session": session,
                    "inc_prompt": "incremental prompt",
                    "provider": "claude",
                    "resume_failed": True,
                },
            ),
            (
                ("full", "force"),
                {
                    "force": True,
                    "stored_session": session,
                    "prior_session": None,
                    "inc_prompt": None,
                    "provider": "claude",
                },
            ),
            (
                ("full", "no_prior_session"),
                {
                    "force": False,
                    "stored_session": None,
                    "prior_session": None,
                    "inc_prompt": None,
                    "provider": "claude",
                },
            ),
            (
                ("full", "contract_changed"),
                {
                    "force": False,
                    "stored_session": session,
                    "prior_session": None,
                    "inc_prompt": None,
                    "provider": "claude",
                },
            ),
            (
                ("full", "no_incremental_delta"),
                {
                    "force": False,
                    "stored_session": session,
                    "prior_session": session,
                    "inc_prompt": None,
                    "provider": "claude",
                },
            ),
            (
                ("full", "provider_changed"),
                {
                    "force": False,
                    "stored_session": session,
                    "prior_session": session,
                    "inc_prompt": "incremental prompt",
                    "provider": "agy",
                },
            ),
            (
                ("full", "missing_session_id"),
                {
                    "force": False,
                    "stored_session": missing_session_id,
                    "prior_session": missing_session_id,
                    "inc_prompt": "incremental prompt",
                    "provider": "claude",
                },
            ),
            (
                ("incremental", ""),
                {
                    "force": False,
                    "stored_session": session,
                    "prior_session": session,
                    "inc_prompt": "incremental prompt",
                    "provider": "claude",
                },
            ),
        ]
        for (expected_mode, expected_reason), kwargs in cases:
            with self.subTest(mode=expected_mode, reason=expected_reason):
                plan = _provider_review_plan(**kwargs)
                self.assertEqual(plan.mode, expected_mode)
                self.assertEqual(plan.full_review_reason, expected_reason)

        incremental_plan = _provider_review_plan(**cases[-1][1])
        self.assertEqual(incremental_plan.incremental_prompt, "incremental prompt")
        self.assertEqual(incremental_plan.resume_session_id, "session-1")
        failed_plan = _provider_review_plan(**cases[0][1])
        self.assertEqual(failed_plan.incremental_prompt, "")
        self.assertEqual(failed_plan.resume_session_id, "")

    def test_run_metadata_records_cold_forced_and_contract_full_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            service = repo / "services/catalog/service.py"
            service.parent.mkdir(parents=True)
            service.write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "round 1")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"
            log_dir = tmp / "logs"
            (fake_home / ".claude").mkdir(parents=True)
            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, subprocess, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('2.0.0')\n"
                "    raise SystemExit(0)\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'reason-session'}))\n"
                "print(json.dumps({'type': 'result', 'result': {'structured_output': {\n"
                "    'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),\n"
                "    'verdict': 'GO', 'summary': 'reason probe', 'blocking_findings': []\n"
                "}}}))\n",
            )
            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
                "THE_GUARD_REVIEW_LOG_DIR": str(log_dir),
            }

            def meta_of(result) -> dict:
                return json.loads(
                    Path(
                        result.log_path.replace(".events.jsonl", ".meta.json")
                    ).read_text(encoding="utf-8")
                )

            cold = run_architecture_review(repo, env=env)
            self.assertEqual((cold.mode, cold.verdict), ("full", "GO"))
            self.assertEqual(meta_of(cold)["full_review_reason"], "no_prior_session")

            forced = run_architecture_review(repo, env=env, force=True)
            self.assertEqual((forced.mode, forced.verdict), ("full", "GO"))
            self.assertEqual(meta_of(forced)["full_review_reason"], "force")

            (repo / ".guard").mkdir(exist_ok=True)
            (repo / ".guard/config.json").write_text(
                json.dumps({"review": {"project_title": "Renamed project"}}),
                encoding="utf-8",
            )
            _git(repo, "add", ".guard/config.json")
            _git(repo, "commit", "-m", "change reviewer contract")
            contracted = run_architecture_review(repo, env=env)
            self.assertEqual((contracted.mode, contracted.verdict), ("full", "GO"))
            self.assertEqual(
                meta_of(contracted)["full_review_reason"], "contract_changed"
            )

    def test_run_metadata_records_provider_changed_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            service = repo / "services/catalog/service.py"
            service.parent.mkdir(parents=True)
            service.write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "round 1")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"
            log_dir = tmp / "logs"
            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".gemini/antigravity-cli").mkdir(parents=True)
            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, subprocess, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('2.0.0')\n"
                "    raise SystemExit(0)\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'claude-session'}))\n"
                "print(json.dumps({'type': 'result', 'result': {'structured_output': {\n"
                "    'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),\n"
                "    'verdict': 'GO', 'summary': 'claude probe', 'blocking_findings': []\n"
                "}}}))\n",
            )
            _write_exec(
                fake_bin / "agy",
                "#!/usr/bin/env python3\n"
                "import json, subprocess, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('1.4.2')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "assert '-p' in args and args[args.index('-p') + 1]\n"
                "print(json.dumps({'event': 'init', 'conversation_id': 'agy-session'}))\n"
                "print(json.dumps({'event': 'result', 'result': {'structured_output': {\n"
                "    'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),\n"
                "    'verdict': 'GO', 'summary': 'agy provider probe', 'blocking_findings': []\n"
                "}}}))\n",
            )
            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
                "THE_GUARD_REVIEW_LOG_DIR": str(log_dir),
            }

            first = run_architecture_review(repo, env=env)
            self.assertEqual((first.provider, first.mode), ("claude", "full"))

            service.write_text("value = 2\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "round 2")

            switched = run_architecture_review(
                repo, env={**env, "THE_GUARD_REVIEW_PROVIDER": "agy"}
            )
            self.assertEqual((switched.provider, switched.mode), ("agy", "full"))
            meta = json.loads(
                Path(
                    switched.log_path.replace(".events.jsonl", ".meta.json")
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(meta["full_review_reason"], "provider_changed")

    def test_prune_isolated_codex_homes_evicts_only_unlocked_expired_homes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            homes_root = Path(td)
            now = 1_700_000_000
            cutoff = now - 24 * 3600
            keep = homes_root / "current"
            active = homes_root / "active"
            stale = homes_root / "stale"
            recent = homes_root / "recent"
            for home in (keep, active, stale, recent):
                home.mkdir()

            with (active / ".review.lock").open("w") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                for home in (keep, active, stale):
                    os.utime(home, (cutoff - 1, cutoff - 1))
                os.utime(recent, (cutoff, cutoff))

                with patch("time.time", return_value=now):
                    _prune_isolated_codex_homes(homes_root, keep.name)

                self.assertFalse(stale.exists())
                self.assertTrue((active / ".review.lock").is_file())
                self.assertTrue(keep.is_dir())
                self.assertTrue(recent.is_dir())

            _prune_isolated_codex_homes(homes_root, keep.name, now=now)
            self.assertFalse(active.exists())

    def test_prune_isolated_codex_homes_keeps_eleven_recent_homes_plus_current(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            homes_root = Path(td)
            now = 1_700_000_000
            keep = homes_root / "current"
            keep.mkdir()
            os.utime(keep, (now - 20, now - 20))
            for index in range(13):
                home = homes_root / f"home-{index}"
                home.mkdir()
                os.utime(home, (now - index, now - index))

            with patch("time.time", return_value=now):
                _prune_isolated_codex_homes(homes_root, keep.name)

            self.assertEqual(
                {home.name for home in homes_root.iterdir() if home.is_dir()},
                {"current", *(f"home-{index}" for index in range(11))},
            )

    def test_discover_codex_mcp_servers_from_linked_worktree_layers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            linked = tmp / "linked"
            _git(repo, "worktree", "add", "--detach", str(linked))
            nested = linked / "nested"
            for directory, config in (
                (tmp, '[mcp_servers.above-root]\ncommand = "fixture"\n'),
                (repo, '[mcp_servers.main-only]\ncommand = "fixture"\n'),
                (linked, '[mcp_servers.plain]\ncommand = "fixture"\n'
                         '[mcp_servers.local-tools]\ncommand = "fixture"\n'),
                (nested, '[mcp_servers."local.tools"]\nurl = "https://example.invalid"\n'
                         '[mcp_servers.plain]\ncommand = "fixture"\n'),
            ):
                (directory / ".codex").mkdir(parents=True, exist_ok=True)
                (directory / ".codex/config.toml").write_text(config, encoding="utf-8")

            self.assertEqual(
                review_provider.discover_codex_mcp_servers(nested),
                ("local-tools", "local.tools", "plain"),
            )
            self.assertEqual(review_provider.discover_codex_mcp_servers(repo), ("main-only",))

    def test_discover_codex_mcp_servers_ignores_unusable_config_layers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".git").mkdir()
            config = repo / ".codex/config.toml"
            config.parent.mkdir()
            for content in (None, b"[broken", b"\xff", b"mcp_servers = []"):
                with self.subTest(content=content):
                    if content is not None:
                        config.write_bytes(content)
                    self.assertEqual(review_provider.discover_codex_mcp_servers(repo), ())

    def test_prepare_isolated_codex_home_and_codex_subprocess_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src_home = tmp / "source-codex"
            common_dir = tmp / "common"
            src_home.mkdir()
            common_dir.mkdir()
            (src_home / "auth.json").write_text("{}", encoding="utf-8")
            (src_home / ".credentials.json").write_text("{}", encoding="utf-8")
            (src_home / "AGENTS.md").write_text("ambient", encoding="utf-8")
            (src_home / "hooks.json").write_text("ambient", encoding="utf-8")
            (src_home / "plugins").mkdir()
            (src_home / "marketplaces").mkdir()
            (src_home / "config.toml").write_text(
                'model = "gpt-test"\n'
                'model_catalog_json = "catalog.json"\n'
                'openai_base_url = "https://example.invalid"\n'
                '[model_providers.magpie]\n'
                'experimental_bearer_token = "secret-token-123"\n'
                'http_headers = {"X-Test" = "1"}\n'
                '[mcp_servers.foo]\ncommand = "unsafe"\n'
                '[plugins.foo]\nenabled = true\n'
                '[marketplaces.foo]\nurl = "https://example.invalid"\n'
                '[hooks.foo]\nenabled = true\n'
                '[projects.foo]\npath = "/tmp"\n',
                encoding="utf-8",
            )
            def materialize(env):
                with isolated_codex_home(common_dir, worktree_path=tmp / "worktree", branch="main", env=env) as home:
                    return home

            target = materialize({"CODEX_HOME": str(src_home)})
            self.assertEqual(target.parent, common_dir / "the-guard-codex-homes")
            other_source = tmp / "other-codex"
            other_source.mkdir()
            (other_source / "auth.json").write_text("other", encoding="utf-8")
            with isolated_codex_home(common_dir, worktree_path=tmp / "other-worktree", branch="main", env={"CODEX_HOME": str(other_source)}) as scoped:
                self.assertNotEqual(scoped, target)
                self.assertEqual((scoped / "auth.json").read_text(encoding="utf-8"), "other")
                probe_fd = os.open(scoped / ".review.lock", os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe_fd)
            self.assertEqual((target / "auth.json").read_text(encoding="utf-8"), "{}")
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertEqual((target / "config.toml").stat().st_mode & 0o777, 0o600)
            self.assertFalse((target / "auth.json").is_symlink())
            self.assertFalse((target / ".credentials.json").is_symlink())
            self.assertFalse((target / "AGENTS.md").exists())
            self.assertFalse((target / "hooks.json").exists())
            self.assertFalse((target / "plugins").exists())
            self.assertFalse((target / "marketplaces").exists())
            rendered = tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(rendered["model"], "gpt-test")
            self.assertEqual(rendered["openai_base_url"], "https://example.invalid")
            self.assertEqual(rendered["model_catalog_json"], str((src_home / "catalog.json").resolve()))
            self.assertEqual(rendered["model_providers"]["magpie"]["experimental_bearer_token"], "secret-token-123")
            self.assertEqual(rendered["model_providers"]["magpie"]["http_headers"], {"X-Test": "1"})
            self.assertFalse(any(key in rendered for key in ("mcp_servers", "plugins", "marketplaces", "hooks")))
            self.assertEqual(
                rendered["projects"],
                {str((tmp / "worktree").resolve()): {"trust_level": "trusted"}},
            )
            self.assertFalse(rendered["check_for_update_on_startup"])
            self.assertTrue(all(rendered["features"][feature] is False for feature in CODEX_DISABLED_FEATURES))
            self.assertIn("multi_agent", CODEX_DISABLED_FEATURES)
            auth_inode = (target / "auth.json").lstat().st_ino
            config_inode = (target / "config.toml").stat().st_ino
            config_bytes = (target / "config.toml").read_bytes()
            materialize({"CODEX_HOME": str(src_home)})
            self.assertEqual((target / "auth.json").lstat().st_ino, auth_inode)
            self.assertEqual((target / "config.toml").stat().st_ino, config_inode)
            self.assertEqual((target / "config.toml").read_bytes(), config_bytes)
            (target / "auth.json").unlink()
            wrong_auth = tmp / "wrong-auth.json"
            wrong_auth.write_text("{}", encoding="utf-8")
            (target / "auth.json").write_bytes(wrong_auth.read_bytes())
            wrong_inode = (target / "auth.json").stat().st_ino
            materialize({"CODEX_HOME": str(src_home)})
            self.assertNotEqual((target / "auth.json").lstat().st_ino, wrong_inode)
            (src_home / "config.toml").write_text((src_home / "config.toml").read_text(encoding="utf-8").replace('gpt-test', 'gpt-updated'), encoding="utf-8")
            materialize({"CODEX_HOME": str(src_home)})
            self.assertNotEqual((target / "config.toml").stat().st_ino, config_inode)
            self.assertEqual(tomllib.loads((target / "config.toml").read_text(encoding="utf-8"))["model"], "gpt-updated")

            (src_home / ".credentials.json").unlink()
            materialize({"CODEX_HOME": str(src_home)})
            self.assertFalse((target / ".credentials.json").exists())

            auth_only = tmp / "auth-only"
            auth_only.mkdir()
            (auth_only / "auth.json").write_text("{}", encoding="utf-8")
            self.assertTrue(materialize({"CODEX_HOME": str(auth_only)}).is_dir())
            self.assertTrue(_has_provider_credentials("codex", {"CODEX_HOME": str(auth_only), "HOME": str(tmp / "empty")}))
            corrupt = tmp / "corrupt"
            corrupt.mkdir()
            (corrupt / "config.toml").write_text("[broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                materialize({"CODEX_HOME": str(corrupt)})

            repo, _ = self._init_repo(tmp / "repo-fixture")
            (repo / ".codex").mkdir()
            (repo / ".codex/config.toml").write_text(
                '[mcp_servers."project.tools"]\ncommand = "fixture"\n', encoding="utf-8",
            )
            (repo / "services").mkdir()
            (repo / "services/example.py").write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", "services/example.py")
            _git(repo, "commit", "-m", "change")
            fake_bin = tmp / "bin"
            argv_file = tmp / "codex-argv.json"
            env_file = tmp / "codex-env.json"
            _write_exec(
                fake_bin / "codex",
                "#!/usr/bin/env python3\n"
                "import json, os, subprocess, sys\n"
                "if '--version' in sys.argv: print('codex test'); raise SystemExit(0)\n"
                "json.dump(sys.argv[1:], open(os.environ['ARGV_FILE'], 'w'))\n"
                "json.dump({'CODEX_HOME': os.environ.get('CODEX_HOME')}, open(os.environ['ENV_FILE'], 'w'))\n"
                "out = sys.argv[sys.argv.index('-o') + 1]\n"
                "json.dump({'verdict':'GO','summary':'ok','blocking_findings':[],'reviewed_head_sha':subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()}, open(out, 'w'))\n",
            )
            env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp / "empty-home"), "CODEX_HOME": str(src_home), "THE_GUARD_REVIEW_CONFIG": str(tmp / "review.json"), "ARGV_FILE": str(argv_file), "ENV_FILE": str(env_file)}
            result = run_architecture_review(repo, force=True, explicit_provider="codex", env=env)
            self.assertEqual(result.verdict, "GO")
            scope = codex_isolation_scope_key(repo, "topic", env)
            self.assertEqual(json.loads(env_file.read_text())["CODEX_HOME"], str(git_common_dir(repo) / "the-guard-codex-homes" / scope))
            argv = json.loads(argv_file.read_text())
            self.assertNotIn("secret-token-123", " ".join(argv))
            self.assertIn('mcp_servers={"project.tools"={enabled=false}}', argv)
            self.assertFalse(any("foo" in arg for arg in argv if arg.startswith("mcp_servers")))

    def test_isolated_codex_home_trusts_worktree_and_common_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            source_home = tmp / "source-codex"
            source_home.mkdir()
            (source_home / "config.toml").write_text(
                '[projects.ambient]\ntrust_level = "trusted"\n', encoding="utf-8"
            )
            repo_root = tmp / "repo"
            common_dir = repo_root / ".git"
            common_dir.mkdir(parents=True)
            worktree = repo_root / "feature"
            worktree.mkdir()
            with isolated_codex_home(
                common_dir,
                worktree_path=worktree,
                branch="topic",
                env={"CODEX_HOME": str(source_home)},
            ) as isolated_home:
                rendered = tomllib.loads(
                    (isolated_home / "config.toml").read_text(encoding="utf-8")
                )
            self.assertEqual(
                rendered["projects"],
                {
                    str(worktree.resolve()): {"trust_level": "trusted"},
                    str(repo_root.resolve()): {"trust_level": "trusted"},
                },
            )
            self.assertNotIn("ambient", rendered["projects"])

    def test_pure_documentation_diff_is_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            report = "docs/reports/anchor-0.md"
            (repo / report).parent.mkdir(parents=True)
            (repo / report).write_text("# Doc only\n", encoding="utf-8")
            plan = "docs/consolidation-plan.md"
            (repo / plan).write_text(f"See {report}.\n")
            _git(repo, "add", report, plan)
            _git(repo, "commit", "-m", "report only")
            self.assertTrue(collect_review_scope(repo).is_exempt)

            (repo / "WORKING.md").write_text("# Working\n", encoding="utf-8")
            _git(repo, "add", "WORKING.md")
            _git(repo, "commit", "-m", "docs only")

            scope = collect_review_scope(repo)
            self.assertTrue(scope.is_exempt)
            self.assertEqual(scope.reviewable_files, ())

            proc = subprocess.run(
                [sys.executable, str(GATE_CLI), "check-push", "--repo-root", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("EXEMPT", proc.stdout)

    def test_check_push_verifies_non_head_commit_with_dirty_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            path = repo / "services/catalog/app.py"
            path.parent.mkdir(parents=True)
            path.write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "reviewed candidate")
            target = _git(repo, "rev-parse", "HEAD").strip()
            result = run_architecture_review(
                repo, reviewer_fn=lambda _prompt: {
                    "verdict": "GO",
                    "summary": "Clean change",
                    "blocking_findings": [],
                },
            )
            self.assertEqual(result.verdict, "GO")
            path.write_text("value = 2\n", encoding="utf-8")
            _git(repo, "commit", "-am", "different unreviewed head")
            dirty_config = repo / ".guard/config.json"
            dirty_config.parent.mkdir(exist_ok=True)
            dirty_config.write_text("invalid dirty config\n", encoding="utf-8")

            cmd = [sys.executable, str(GATE_CLI), "review", "check-push", "--repo-root", str(repo)]
            verified = subprocess.run([*cmd, "--head", target], capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertIn("VERIFIED", verified.stdout)
            blocked = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertIn("NO-GO", blocked.stderr)

    def test_code_diff_blocks_push_until_go_receipt_and_survives_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, origin = self._init_repo(tmp)
            app_py = repo / "services/catalog/app.py"
            app_py.parent.mkdir(parents=True)
            app_py.write_text(
                "def handle_get():\n    return {'ok': True}\n", encoding="utf-8"
            )
            _git(repo, "add", "services/catalog/app.py")
            _git(repo, "commit", "-m", "feat: add handler")

            chk_blocked = subprocess.run(
                [sys.executable, str(GATE_CLI), "check-push", "--repo-root", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(chk_blocked.returncode, 2)
            self.assertIn("NO-GO", chk_blocked.stderr)

            res_nogo = run_architecture_review(
                repo,
                reviewer_fn=lambda _prompt: {
                    "verdict": "NO-GO",
                    "summary": "read path lock",
                    "blocking_findings": [
                        {
                            "rule_id": "guard.database-query-efficiency",
                            "file": "services/catalog/app.py",
                            "line": 1,
                            "issue": "lock in GET",
                            "required_fix": "remove lock",
                        }
                    ],
                },
            )
            self.assertEqual(res_nogo.verdict, "NO-GO")
            self.assertIsNone(load_valid_receipt(repo, res_nogo.patch_digest))

            res_go = run_architecture_review(
                repo,
                reviewer_fn=lambda _prompt: {
                    "verdict": "GO",
                    "summary": "Clean single-owner read path",
                    "blocking_findings": [],
                },
            )
            self.assertEqual(res_go.verdict, "GO")
            self.assertIsNotNone(load_valid_receipt(repo, res_go.patch_digest))

            chk_ok = subprocess.run(
                [sys.executable, str(GATE_CLI), "check-push", "--repo-root", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(chk_ok.returncode, 0)
            self.assertIn("VERIFIED", chk_ok.stdout)

            other_clone = tmp / "other"
            subprocess.run(
                ["git", "clone", str(origin), str(other_clone)],
                check=True,
                capture_output=True,
            )
            _git(other_clone, "checkout", "main")
            _git(other_clone, "config", "user.email", "other@example.invalid")
            _git(other_clone, "config", "user.name", "Other")
            (other_clone / "unrelated.txt").write_text(
                "main advance\n", encoding="utf-8"
            )
            _git(other_clone, "add", "unrelated.txt")
            _git(other_clone, "commit", "-m", "advance main")
            _git(other_clone, "push", "origin", "main")

            _git(repo, "fetch", "origin", "main")
            _git(repo, "rebase", "origin/main")

            rebased_scope = collect_review_scope(repo)
            self.assertEqual(rebased_scope.patch_digest, res_go.patch_digest)
            chk_after_rebase = subprocess.run(
                [sys.executable, str(GATE_CLI), "check-push", "--repo-root", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(chk_after_rebase.returncode, 0)

            app_py.write_text(
                "def handle_get():\n    return {'ok': False}\n", encoding="utf-8"
            )
            _git(repo, "add", "services/catalog/app.py")
            _git(repo, "commit", "--amend", "--no-edit")
            chk_after_edit = subprocess.run(
                [sys.executable, str(GATE_CLI), "check-push", "--repo-root", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(chk_after_edit.returncode, 2)

    def test_fake_cli_stream_json_ndjson_parsing_for_go_and_nogo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/b").mkdir(parents=True)
            (repo / "services/b/handler.py").write_text(
                "def f():\n    return 1\n", encoding="utf-8"
            )
            _git(repo, "add", "services/b/handler.py")
            _git(repo, "commit", "-m", "feat: add b handler")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"
            argv_record = tmp / "agy_argv.json"
            (fake_home / ".gemini/antigravity-cli").mkdir(parents=True)

            _write_exec(
                fake_bin / "agy",
                "#!/usr/bin/env python3\n"
                "import json, os, sys, subprocess\n"
                "if '--version' in sys.argv:\n"
                "    print('1.4.2')\n"
                "    raise SystemExit(0)\n"
                "with open(os.environ['AGY_ARGV_FILE'], 'w', encoding='utf-8') as f:\n"
                "    json.dump(sys.argv[1:], f)\n"
                "print(json.dumps({'event': 'init', 'conversation_id': 'c1'}))\n"
                "print(json.dumps({'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'view_file'}}))\n"
                "if os.environ.get('FAKE_VERDICT') == 'NO-GO':\n"
                "    inner = '```json\\n' + json.dumps({\n"
                "        'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'NO-GO',\n"
                "        'summary': 'duplicate query in handler',\n"
                "        'blocking_findings': [{\n"
                "            'rule_id': 'guard.database-query-efficiency',\n"
                "            'file': 'services/b/handler.py',\n"
                "            'line': 1,\n"
                "            'issue': 'dup read',\n"
                "            'required_fix': 'batch read',\n"
                "        }],\n"
                "    }) + '\\n```'\n"
                "    print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': inner}}))\n"
                "else:\n"
                "    print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'structured_output': {\n"
                "        'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'GO', 'summary': 'stream-json ok', 'blocking_findings': []\n"
                "    }}}))\n",
            )

            base_env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
                "AGY_ARGV_FILE": str(argv_record),
            }
            probe_environment(
                repo,
                env=base_env,
                priority_order=["agy", "codex"],
                provider_models={
                    "agy": {"model": "gemini-4-argon-high", "effort": "high"}
                },
            )

            nogo_res = run_architecture_review(
                repo,
                force=True,
                env={**base_env, "FAKE_VERDICT": "NO-GO"},
            )
            self.assertEqual(nogo_res.verdict, "NO-GO")
            self.assertEqual(nogo_res.provider, "agy")
            self.assertEqual(len(nogo_res.blocking_findings), 1)
            self.assertIsNone(load_valid_receipt(repo, nogo_res.patch_digest))

            recorded_argv = json.loads(argv_record.read_text(encoding="utf-8"))
            self.assertIn("--mode", recorded_argv)
            self.assertIn("plan", recorded_argv)
            self.assertIn("--sandbox", recorded_argv)
            self.assertEqual(
                recorded_argv[recorded_argv.index("--model") + 1], "gemini-4-argon-high"
            )
            self.assertEqual(recorded_argv[recorded_argv.index("--effort") + 1], "high")
            recorded_settings = json.loads(
                recorded_argv[recorded_argv.index("--settings") + 1]
            )
            self.assertIn(
                f"read_file({git_common_dir(repo)})",
                recorded_settings["permissions"]["allow"],
            )
            self.assertNotIn("--add-dir", recorded_argv)

            go_res = run_architecture_review(
                repo,
                force=True,
                env={**base_env, "FAKE_VERDICT": "GO"},
            )
            self.assertEqual(go_res.verdict, "GO")
            self.assertEqual(go_res.provider, "agy")
            self.assertIsNotNone(load_valid_receipt(repo, go_res.patch_digest))

    def test_streaming_cmd_heartbeat_and_stall_watchdog(self) -> None:
        large_stdin = "line-content\n" * 5000
        rc, out, _ = _run_streaming_cmd(
            "test-prov",
            [
                "python3",
                "-c",
                "import json, sys; data = sys.stdin.read(); "
                "print(json.dumps({'type': 'step', 'bytes': len(data)}))",
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "THE_GUARD_REVIEW_HEARTBEAT_SECONDS": "0.05"},
            stdin_text=large_stdin,
        )
        self.assertEqual(rc, 0)
        self.assertIn(str(len(large_stdin.encode("utf-8"))), out)

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            with self.assertRaises(RuntimeError) as ctx:
                _run_streaming_cmd(
                    "stalled-prov",
                    ["python3", "-c", "import time; time.sleep(10)"],
                    cwd=REPO_ROOT,
                    env={
                        **os.environ,
                        "THE_GUARD_REVIEW_STALL_SECONDS": "0.2",
                        "THE_GUARD_REVIEW_HEARTBEAT_SECONDS": "0.05",
                    },
                )
        self.assertIn("stalled with no output", str(ctx.exception))
        self.assertIn("[arch-review][stalled-prov] +", stderr_capture.getvalue())
        self.assertIn("events=0 | last=initializing", stderr_capture.getvalue())

        with self.assertRaises(RuntimeError) as runaway_ctx:
            _run_streaming_cmd(
                "runaway-prov",
                [
                    "python3",
                    "-c",
                    "import time; \n"
                    "while True:\n"
                    '    print(\'{"type": "tick"}\', flush=True)\n'
                    "    time.sleep(0.05)\n",
                ],
                cwd=REPO_ROOT,
                env={
                    **os.environ,
                    "THE_GUARD_REVIEW_MAX_SECONDS": "0.2",
                    "THE_GUARD_REVIEW_STALL_SECONDS": "10",
                },
            )
        self.assertIn("exceeded runaway safety limit", str(runaway_ctx.exception))

    def test_streaming_cmd_fails_closed_on_truncated_stdin(self) -> None:
        huge_prompt = "diff-line-payload-" * 30000
        with self.assertRaises(RuntimeError) as ctx:
            _run_streaming_cmd(
                "codex",
                [
                    "python3",
                    "-c",
                    "import json, os, sys; os.close(0); "
                    "print(json.dumps({'verdict': 'GO', 'summary': 'truncated', 'blocking_findings': []}))",
                ],
                cwd=REPO_ROOT,
                env=dict(os.environ),
                stdin_text=huge_prompt,
            )
        self.assertIn("stdin", str(ctx.exception))

    def test_provider_failure_detail_surfaces_stdout_quota_errors_behind_stderr_warnings(self) -> None:
        stdout = "\n".join([
            json.dumps({
                "type": "error",
                "message": "[features].multi_agent is deprecated and will be removed",
            }),
            json.dumps({
                "type": "turn.failed",
                "error": {
                    "message": "You’ve hit your usage limit. Purchase more credits to continue."
                },
            }),
        ])
        detail = _provider_failure_detail(
            stdout, "Untrusted project-local configuration is disabled"
        )
        self.assertIn("Untrusted project-local configuration is disabled", detail)
        self.assertIn("You’ve hit your usage limit", detail)
        self.assertNotIn("[features].multi_agent", detail)
        self.assertTrue(
            _is_persistent_provider_failure(RuntimeError(f"codex exited 1: {detail}"))
        )
        deprecation_only = _provider_failure_detail(
            json.dumps({
                "type": "error",
                "message": "[features].multi_agent is deprecated",
            }),
            "",
        )
        self.assertIn("[features].multi_agent", deprecation_only)
        self.assertFalse(_is_persistent_provider_failure(RuntimeError(deprecation_only)))

    def test_codex_stdout_quota_error_records_sticky_secondary_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/catalog").mkdir(parents=True)
            (repo / "services/catalog/service.py").write_text(
                "value = 1\n", encoding="utf-8"
            )
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "review scope")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".codex/auth.json").write_text("{}", encoding="utf-8")
            (fake_home / ".gemini/antigravity-cli").mkdir(parents=True)
            _write_exec(
                fake_bin / "codex",
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('codex test')\n"
                "    raise SystemExit(0)\n"
                "sys.stdin.read()\n"
                "print('Untrusted project-local configuration is disabled', file=sys.stderr)\n"
                "print(json.dumps({'type': 'error', 'message': '[features].multi_agent is deprecated'}))\n"
                "print(json.dumps({'type': 'turn.failed', 'error': {'message': 'You’ve hit your usage limit. Purchase more credits to continue.'}}))\n"
                "raise SystemExit(1)\n",
            )
            _write_exec(
                fake_bin / "agy",
                "#!/usr/bin/env python3\n"
                "import json, subprocess, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('1.4.2')\n"
                "    raise SystemExit(0)\n"
                "print(json.dumps({'event': 'init', 'conversation_id': 'agy-quota-fallback'}))\n"
                "print(json.dumps({'event': 'result', 'result': {'structured_output': {\n"
                "    'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),\n"
                "    'verdict': 'GO', 'summary': 'secondary provider', 'blocking_findings': []\n"
                "}}}))\n",
            )
            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
            }

            result = run_architecture_review(repo, force=True, env=env)
            self.assertEqual((result.verdict, result.provider), ("GO", "agy"))
            persisted = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["preferred_provider"], "agy")

    def test_colleague_environment_check_and_fallback_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "services/a/main.py")
            _git(repo, "commit", "-m", "code change")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"

            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".gemini/antigravity-cli").mkdir(parents=True)
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".codex/auth.json").write_text("{}", encoding="utf-8")

            _write_exec(
                fake_bin / "codex",
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "--version" ]; then echo \'codex 0.1\'; exit 0; fi\n'
                "echo 'codex quota exceeded' >&2; exit 1\n",
            )
            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "--version" ]; then echo \'2.0.0\'; exit 0; fi\n'
                "echo 'auth expired' >&2; exit 1\n",
            )
            _write_exec(
                fake_bin / "agy",
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "--version" ]; then echo \'1.4.2\'; exit 0; fi\n'
                'printf \'{"event":"init","conversation_id":"c1"}\\n\'\n'
                'printf \'{"event":"result","result":{"status":"SUCCESS","structured_output":{"verdict":"GO","summary":"ok via agy","blocking_findings":[],"reviewed_head_sha":"%s"}}}\\n\' "$(git rev-parse HEAD)"\n',
            )

            env = {
                "PATH": str(fake_bin) + ":/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
            }

            probed = probe_environment(repo, env=env)
            self.assertEqual(probed["priority_order"], ["codex", "claude", "agy"])
            self.assertEqual(probed["preferred_provider"], "codex")
            self.assertEqual(probed["available_providers"], ["codex", "claude", "agy"])

            result = run_architecture_review(repo, force=True, env=env)
            self.assertEqual(result.verdict, "GO")
            self.assertEqual(result.provider, "agy")

            persisted = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["preferred_provider"], "agy")

            ordered, _ = resolve_provider_order(repo, env=env)
            self.assertEqual(ordered[0][0], "agy")

            models = {
                "agy": {"model": "gemini-4-argon-high", "effort": "high"},
                "codex": {"model": "gpt-6-astra", "effort": "high"},
            }
            configured = subprocess.run(
                [
                    sys.executable,
                    str(GATE_CLI),
                    "probe",
                    "--repo-root",
                    str(repo),
                    "--priority-order",
                    "agy,codex",
                    "--provider-model",
                    "agy=gemini-4-argon-high:high",
                    "--provider-model",
                    "codex=gpt-6-astra:high",
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)
            self.assertEqual(json.loads(configured.stdout)["provider_models"], models)
            reprobed = probe_environment(repo, env=env)
            self.assertEqual(reprobed["priority_order"], ["agy", "codex"])
            self.assertEqual(reprobed["available_providers"], ["agy", "codex"])
            self.assertEqual(reprobed["preferred_provider"], "agy")
            self.assertEqual(reprobed["provider_models"], models)
            merged = probe_environment(
                repo, env=env, provider_models={"codex": {"effort": "medium"}}
            )
            self.assertEqual(merged["provider_models"]["agy"], models["agy"])
            self.assertEqual(
                merged["provider_models"]["codex"],
                {"model": "gpt-6-astra", "effort": "medium"},
            )
            probe_environment(repo, env=env, provider_models=models)
            record_working_provider(repo, "codex", str(fake_bin / "codex"), env=env)
            persisted = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["priority_order"], ["agy", "codex"])
            self.assertEqual(persisted["provider_models"], models)
            ordered, payload = resolve_provider_order(repo, env=env)
            self.assertEqual([provider for provider, _ in ordered], ["codex", "agy"])
            self.assertEqual(payload, persisted)
            for overrides in (
                {"explicit_provider": "claude"},
                {"env": {**env, "THE_GUARD_REVIEW_PROVIDER": "claude"}},
            ):
                forced, _ = resolve_provider_order(repo, **{"env": env, **overrides})
                self.assertEqual(
                    [provider for provider, _ in forced], ["claude", "codex", "agy"]
                )

            record_working_provider(repo, "claude", str(fake_bin / "claude"), env=env)
            ordered, _ = resolve_provider_order(repo, env=env)
            self.assertEqual([provider for provider, _ in ordered], ["agy", "codex"])
            record_working_provider(repo, "agy", str(fake_bin / "agy"), env=env)
            with patch("the_guard.review.engine._invoke_provider") as invoke:
                invoke.side_effect = [
                    RuntimeError("quota exceeded"),
                    (
                        ReviewOutput("GO", "ok", (), ""),
                        "thread-1",
                        "",
                    ),
                ]
                result = run_architecture_review(repo, force=True, env=env)
            self.assertEqual((result.verdict, result.provider), ("GO", "codex"))
            self.assertEqual(
                [call.args[0] for call in invoke.call_args_list], ["agy", "codex"]
            )
            for call in invoke.call_args_list:
                self.assertEqual(
                    (call.kwargs["model"], call.kwargs["effort"]),
                    tuple(models[call.args[0]][key] for key in ("model", "effort")),
                )
            with patch("the_guard.review.engine._invoke_provider") as invoke:
                invoke.return_value = (
                    ReviewOutput("GO", "ok", (), ""),
                    "thread-2",
                    "",
                )
                result = run_architecture_review(
                    repo,
                    force=True,
                    env={
                        **env,
                        "THE_GUARD_REVIEW_MODEL": "override",
                        "THE_GUARD_REVIEW_EFFORT": "low",
                    },
                )
            self.assertEqual((result.verdict, result.provider), ("GO", "codex"))
            self.assertEqual(
                (invoke.call_args.kwargs["model"], invoke.call_args.kwargs["effort"]),
                ("override", "low"),
            )

    def test_provider_configuration_normalization_and_environment_overrides(
        self,
    ) -> None:
        self.assertEqual(
            _normalize_priority_order(["agy", "unknown", "codex", "agy", None]),
            ["agy", "codex"],
        )
        self.assertEqual(_normalize_priority_order("agy,codex"), [])
        models = {
            "agy": {
                "model": " gemini-4-argon-high ",
                "effort": "high",
                "extra": "ignored",
            },
            "codex": {"model": 7},
            "unknown": {"model": "x"},
        }
        self.assertEqual(
            _normalize_provider_models(models),
            {"agy": {"model": "gemini-4-argon-high", "effort": "high"}},
        )
        self.assertEqual(_normalize_provider_models([]), {})
        payload = {"provider_models": models}
        self.assertEqual(
            resolve_provider_model_config("agy", payload, {}),
            ("gemini-4-argon-high", "high"),
        )
        self.assertEqual(resolve_provider_model_config("codex", payload, {}), ("", ""))
        self.assertEqual(
            resolve_provider_model_config(
                "agy",
                payload,
                {
                    "THE_GUARD_REVIEW_MODEL": "override",
                    "THE_GUARD_REVIEW_EFFORT": "low",
                },
            ),
            ("override", "low"),
        )
        self.assertEqual(
            resolve_provider_model_config(
                "agy", payload, {"THE_GUARD_REVIEW_MODEL": "override"}
            ),
            ("override", "high"),
        )

    def test_build_review_prompt_loads_canonical_policy_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "services/a/main.py")
            _git(repo, "commit", "-m", "code change")

            scope = collect_review_scope(repo)
            committed_policy = load_policy(repo, revision=scope.head_sha)
            # Even if the working tree has uncommitted edits to AGENTS.md, build_review_prompt
            # reads strictly from the committed revision (scope.head_sha).
            (repo / "AGENTS.md").write_text(
                "# uncommitted working tree edit\n", encoding="utf-8"
            )
            prompt = build_review_prompt(repo, scope)
            self.assertIn("Exhaustive Single-Pass Findings", prompt)
            self.assertIn("Pattern-Wide Sweep", prompt)
            self.assertIn("Preempt Transitional Stubs", prompt)
            self.assertIn("moving two state machines into one file", prompt)
            self.assertIn("changed files", prompt)
            self.assertTrue(committed_policy.rules)
            for rule in committed_policy.rules:
                self.assertIn(rule.id, prompt)
                self.assertIn(rule.summary, prompt)
                self.assertTrue(rule.detail_body)
                self.assertIn(rule.detail_body, prompt)

        expected = "abcdef0123456789" * 2 + "abcdef01"
        previous = "1234567890abcdef" * 2 + "12345678"
        for reported in (expected, expected[:7], "  " + expected[:12].upper() + "  "):
            verify_reviewed_head_sha(reported, expected)
        for reported in (
            previous,
            expected[:6],
            expected + "abcd",
            "abcdefg",
            "",
            " ",
            None,
            42,
        ):
            with self.assertRaises(RuntimeError):
                verify_reviewed_head_sha(reported, expected)
        with self.assertRaisesRegex(RuntimeError, "requires a non-empty"):
            verify_reviewed_head_sha(None, expected)
        with self.assertRaisesRegex(RuntimeError, "mismatched HEAD"):
            verify_reviewed_head_sha(previous, expected)

    def test_prompt_resolves_nested_leaf_and_top_level_contracts(self) -> None:
        for contract in (
            {"enforce_module_dependencies": True},
            {"layers": {"core": []}},
            {},
        ):
            with self.subTest(contract=contract), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td))
                (repo / "packages/host/nested").mkdir(parents=True)
                (repo / "frontend").mkdir()
                (repo / "docs/frontend.md").write_text("FRONTEND OWNER DOCUMENT")
                (repo / "packages/host/nested/main.py").write_text("value = 1\n")
                (repo / "frontend/main.py").write_text("value = 1\n")
                leaf = {"path": "packages/host/nested", "dependencies": []}
                if "layers" in contract:
                    leaf["layer"] = "core"
                elif not contract:
                    leaf["role"] = "Foundation state owner"
                topology = {**contract, "modules": [leaf,
                    {"path": "frontend", "boundary_docs": ["docs/frontend.md"], "dependencies": []},
                ]}
                (repo / ".guard").mkdir(exist_ok=True)
                (repo / ".guard/config.json").write_text(json.dumps({"topology": topology}))
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "nested and top level contracts")
                prompt = build_review_prompt(repo, collect_review_scope(repo))
                self.assertIn("FRONTEND OWNER DOCUMENT", prompt)
                self.assertNotIn("## Unbound Module Topology Guidance", prompt)
                self.assertNotIn("## Boundary reference: packages/host/", prompt)

    def test_prompt_keeps_full_topology_and_snapshot_declared_docs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/b").mkdir(parents=True)
            (repo / "docs/architecture.md").write_text("SNAPSHOT ARCHITECTURE\n" + "x" * 25_000)
            (repo / "services/a/README.md").write_text("A OWNER")
            (repo / "services/b/README.md").write_text("B OWNER")
            config = {"topology": {"layers": {"core": [], "plugin": ["core"]}, "modules": [
                {"path": "services/a", "layer": "plugin", "role": "adapter", "dependencies": ["services/b"],
                 "boundary_docs": ["docs/architecture.md", "services/a/README.md", "docs/architecture.md", "AGENTS.md"]},
                {"path": "services/b", "layer": "core", "role": "domain owner", "dependencies": [],
                 "boundary_docs": ["docs/architecture.md"]},
            ]}}
            (repo / ".guard").mkdir(exist_ok=True)
            (repo / ".guard/config.json").write_text(json.dumps(config))
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "topology contract")
            baseline = _git(repo, "rev-parse", "HEAD")
            _git(repo, "update-ref", "refs/remotes/origin/main", baseline)
            (repo / "services/a/main.py").write_text("value = 1\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "touch adapter only")
            scope = collect_review_scope(repo)
            (repo / "docs/architecture.md").write_text("DIRTY ARCHITECTURE")
            (repo / ".guard/config.json").write_text("invalid JSON")
            prompt = build_review_prompt(repo, scope)
            self.assertIn('"role": "domain owner"', prompt)
            self.assertIn('"layers":', prompt)
            self.assertIn("SNAPSHOT ARCHITECTURE", prompt)
            self.assertNotIn("DIRTY ARCHITECTURE", prompt)
            self.assertEqual(prompt.count("## Architecture blueprint: docs/architecture.md"), 1)
            self.assertEqual(prompt.count("## Root architecture & agent instructions: AGENTS.md"), 1)
            self.assertEqual(prompt.count("## Boundary reference: services/a/README.md"), 1)
            self.assertNotIn("## Boundary reference: services/b/README.md", prompt)
            self.assertIn("... [truncated]", prompt)
            self.assertNotIn("x" * 24_001, prompt)
            self.assertIn("## Architectural & Layer Boundary Integrity", prompt)
            self.assertNotIn("## Unbound Module Topology Guidance", prompt)

    def test_build_review_prompt_guides_unbound_topology_only_without_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "services/a/main.py")
            _git(repo, "commit", "-m", "unbound change")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            self.assertIn("## Unbound Module Topology Guidance", prompt)

        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "pkg/server").mkdir(parents=True)
            (repo / "pkg/server/handler.go").write_text("package server\n", encoding="utf-8")
            _git(repo, "add", "pkg/server/handler.go")
            _git(repo, "commit", "-m", "unbound external layout change")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            self.assertIn("## Unbound Module Topology Guidance", prompt)

        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / ".guard").mkdir()
            (repo / ".guard/config.json").write_text(
                '{"topology": {"exclusive_sql_owners": [{"table": "jobs", "owners": ["services/owner.py"]}]}}\n',
                encoding="utf-8",
            )
            (repo / "services/b").mkdir(parents=True)
            (repo / "services/b/main.py").write_text("y = 2\n", encoding="utf-8")
            _git(repo, "add", ".guard/config.json", "services/b/main.py")
            _git(repo, "commit", "-m", "global owner topology rule")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            self.assertNotIn("## Unbound Module Topology Guidance", prompt)
            self.assertNotIn("## Boundary reference: services/a/", prompt)

        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / ".guard").mkdir()
            (repo / ".guard/config.json").write_text(
                '{"topology": {"forbidden_imports": [{"scope_prefixes": ["services/a"], "imports": ["internal.storage"]}]}}\n',
                encoding="utf-8",
            )
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", ".guard/config.json", "services/a/main.py")
            _git(repo, "commit", "-m", "bound change")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            self.assertNotIn("## Unbound Module Topology Guidance", prompt)

        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/README.md").write_text("# boundary\n", encoding="utf-8")
            (repo / "services/a/main.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "services/a")
            _git(repo, "commit", "-m", "boundary docs")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            self.assertIn("## Boundary reference: services/a/README.md", prompt)
            self.assertNotIn("## Unbound Module Topology Guidance", prompt)

        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/README.md").write_text("# boundary\n", encoding="utf-8")
            (repo / "services/a/main.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "services/b").mkdir(parents=True)
            (repo / "services/b/main.py").write_text("y = 2\n", encoding="utf-8")
            _git(repo, "add", "services/a", "services/b")
            _git(repo, "commit", "-m", "mixed boundaries")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            self.assertIn("## Boundary reference: services/a/README.md", prompt)
            self.assertIn("## Unbound Module Topology Guidance", prompt)

        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / ".guard").mkdir()
            (repo / ".guard/config.json").write_text(
                '{"topology": {"forbidden_imports": [{"scope_prefixes": ["services/other"], "imports": ["internal.storage"]}]}}\n',
                encoding="utf-8",
            )
            (repo / "services/b").mkdir(parents=True)
            (repo / "services/b/main.py").write_text("y = 2\n", encoding="utf-8")
            _git(repo, "add", ".guard/config.json", "services/b/main.py")
            _git(repo, "commit", "-m", "unrelated topology rule")
            prompt = build_review_prompt(repo, collect_review_scope(repo))
            self.assertIn("## Unbound Module Topology Guidance", prompt)

    def test_large_diff_preserves_full_stat_and_lists_omitted_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/first.py").write_text(
                "a = " + "'x' * 50\n" * 15, encoding="utf-8"
            )
            (repo / "services/a/second.py").write_text(
                "b = " + "'y' * 50\n" * 30, encoding="utf-8"
            )
            (repo / "services/a/third.py").write_text(
                "c = " + "'z' * 50\n" * 30, encoding="utf-8"
            )
            _git(
                repo,
                "add",
                "services/a/first.py",
                "services/a/second.py",
                "services/a/third.py",
            )
            _git(repo, "commit", "-m", "multi-file change")

            scope = collect_review_scope(repo)
            rendered = _render_bounded_diff_section(
                repo,
                scope.merge_base,
                scope.head_sha,
                title="Repository",
                max_chars=500,
                paths=scope.reviewable_files,
            )
            self.assertIn("services/a/first.py", rendered)
            self.assertIn("services/a/second.py", rendered)
            self.assertIn("services/a/third.py", rendered)
            self.assertIn("Files omitted from inline preview", rendered)
            self.assertIn("- `services/a/second.py`", rendered)
            self.assertIn("- `services/a/third.py`", rendered)

    def test_policy_and_rule_files_are_reviewable_while_normal_docs_stay_exempt(
        self,
    ) -> None:
        self.assertTrue(is_reviewable_path("AGENTS.md"))
        self.assertTrue(is_reviewable_path("AGENTS.md"))
        self.assertTrue(
            is_reviewable_path("docs/standards/rules/event-driven-execution.md")
        )
        self.assertFalse(is_reviewable_path("docs/design.md"))
        self.assertFalse(is_reviewable_path("WORKING.md"))

    def test_build_review_prompt_fails_closed_when_policy_is_broken(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "AGENTS.md").write_text(
                "# broken manifest without policy block\n", encoding="utf-8"
            )
            _git(repo, "add", "services/a/main.py", "AGENTS.md")
            _git(repo, "commit", "-m", "commit broken policy")
            scope = collect_review_scope(repo)
            with self.assertRaises(PolicyError):
                build_review_prompt(repo, scope)
            with self.assertRaises(PolicyError):
                run_architecture_review(
                    repo,
                    force=True,
                    reviewer_fn=lambda _p: {
                        "verdict": "GO",
                        "summary": "should not run",
                        "blocking_findings": [],
                    },
                )
            self.assertIsNone(load_valid_receipt(repo, scope.patch_digest))

            cli_proc = subprocess.run(
                [
                    sys.executable,
                    str(GATE_CLI),
                    "review",
                    "run",
                    "--repo-root",
                    str(repo),
                    "--force",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cli_proc.returncode, 2)
            self.assertIn("policy error:", cli_proc.stderr)
            self.assertIsNone(load_valid_receipt(repo, scope.patch_digest))

    def test_production_files_prioritized_before_tests_in_bounded_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "services/a/tests").mkdir(parents=True)
            (repo / "services/z/domain").mkdir(parents=True)
            (repo / "services/a/tests/test_alpha.py").write_text(
                "def test_a():\n" + "    assert True\n" * 40, encoding="utf-8"
            )
            (repo / "services/z/domain/service.py").write_text(
                "def run_service():\n    return 'production_logic_inlined'\n",
                encoding="utf-8",
            )
            _git(
                repo,
                "add",
                "services/a/tests/test_alpha.py",
                "services/z/domain/service.py",
            )
            _git(repo, "commit", "-m", "prod and test change")

            scope = collect_review_scope(repo)
            rendered = _render_bounded_diff_section(
                repo,
                scope.merge_base,
                scope.head_sha,
                title="Repository",
                max_chars=350,
                paths=scope.reviewable_files,
            )
            self.assertIn("production_logic_inlined", rendered)
            self.assertIn("Omitted production/contracts 0, tests 1", rendered)
            self.assertIn("- `services/a/tests/test_alpha.py`", rendered)

    def test_stream_line_summarizer_extracts_tool_and_file_targets(self) -> None:
        read_ev = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {
                                "file_path": "/workspace/services/catalog/daily/readers.py"
                            },
                        }
                    ]
                },
            }
        )
        self.assertEqual(_summarize_stream_line(read_ev), "Read(daily/readers.py)")

        bash_ev = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": "git -C /workspace diff a1b2..c3d4 -- services/catalog/ports.py"
                            },
                        }
                    ]
                },
            }
        )
        self.assertEqual(
            _summarize_stream_line(bash_ev),
            "Bash(git diff a1b2..c3d4 -- services/catalog/ports.py)",
        )

        user_noise_ev = json.dumps(
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
        )
        self.assertIsNone(_summarize_stream_line(user_noise_ev))
