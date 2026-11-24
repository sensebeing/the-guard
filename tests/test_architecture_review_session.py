"""Architecture review session: incremental scope, rebuttals, and carry-forward.

Contract: incremental sessions bind reviewed scope and patch IDs; invalid continuation never issues GO.
"""

from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

from the_guard.review.engine import (
    build_review_prompt,
    collect_incremental_scope,
    collect_review_scope,
    load_review_session,
    resolve_or_carry_forward_receipt,
    run_architecture_review,
    save_review_session,
)
from the_guard.review import session
from the_guard.review.contract import ReviewOutput, ReviewScope
from the_guard.review.documents import resolve_scope_modules
from the_guard.review.prompt import _parse_numstat_entries, build_incremental_review_prompt
from the_guard.review.provider import probe_environment, record_provider_cooldown, record_working_provider
from the_guard.review.repository import (
    _collect_branch_delta,
    _is_same_per_file_branch_patch,
    contract_change_causes,
    load_valid_receipt,
    review_logs_dir,
    compute_review_contract_digest,
)
from the_guard.review.session import ReviewResult, ReviewRunLogger
from the_guard.config import GuardConfig, load_config
from the_guard.hooks import main_push_gate
from the_guard.policy import load_policy
from architecture_review_support import (
    ArchitectureReviewTestCase,
    _git,
    _write_exec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_CLI = REPO_ROOT / "bin/guard"


class ArchitectureReviewSessionTests(ArchitectureReviewTestCase):
    def test_rule_scope_projects_global_paths_and_glob_descendants(self) -> None:
        applies = session.rule_applies_to_modules
        self.assertTrue(applies({}, set()))
        self.assertTrue(applies({"scope_prefixes": []}, set()))
        self.assertTrue(applies({"scope_prefixes": [""]}, set()))
        self.assertTrue(applies({"scope_prefixes": ["services/a/"]}, {"services/a/module"}))
        self.assertTrue(applies({"scope_prefixes": ["services/[ab]/internal/*.py"]}, {"services/a"}))
        self.assertFalse(applies({"scope_prefixes": ["services/[ab]/internal/*.py"]}, {"services/c"}))

    def test_object_only_push_carries_review_from_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = self._init_repo(root)
            review_tree = root / "review"
            _git(repo, "worktree", "add", "-b", "review", str(review_tree), "main")
            (review_tree / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
            _git(review_tree, "add", "service.py")
            _git(review_tree, "commit", "-m", "Reviewed change")
            reviewed = run_architecture_review(review_tree, reviewer_fn=lambda _: {
                "verdict": "GO", "summary": "Reviewed change", "blocking_findings": [],
            })
            base = _git(repo, "rev-parse", "HEAD")
            tree = _git(repo, "rev-parse", f"{reviewed.head_sha}^{{tree}}")
            replay = _git(repo, "commit-tree", tree, "-p", base, "-m", "Replay reviewed change")
            update = f"{replay} {replay} refs/heads/main {base}\n"
            self.assertEqual(main_push_gate(repo, update), 0)
            self.assertEqual(_git(repo, "rev-parse", "HEAD"), base)
            self.assertEqual(load_review_session(review_tree).last_head_sha, reviewed.head_sha)
            self.assertIsNone(load_review_session(repo))
            scope = collect_review_scope(repo, main_branch=base, head_ref=replay)
            self.assertEqual(resolve_or_carry_forward_receipt(repo, scope)["head_sha"], replay)
            self.assertEqual(load_review_session(review_tree).last_head_sha, reviewed.head_sha)
            self.assertIsNone(load_review_session(repo))

            (review_tree / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
            _git(review_tree, "commit", "-am", "Unreviewed change")
            tree = _git(review_tree, "rev-parse", "HEAD^{tree}")
            unreviewed = _git(repo, "commit-tree", tree, "-p", base, "-m", "Replay unreviewed change")
            with redirect_stderr(StringIO()) as stderr:
                self.assertEqual(main_push_gate(repo, update.replace(replay, unreviewed)), 2)
            self.assertIn("missing review receipt", stderr.getvalue())
            with redirect_stderr(StringIO()) as stderr:
                self.assertEqual(main_push_gate(repo, update.replace(base, "a" * 40)), 2)
            self.assertEqual(stderr.getvalue(), "NO-GO: remote target commit is not fetched locally; run git fetch and retry.\n")

    def test_object_only_push_can_reuse_review_across_an_advanced_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = "docs/plan.md"
            repo, _ = self._init_repo(root, review={"blueprint_globs": [plan]})
            review_tree = root / "review"
            _git(repo, "worktree", "add", "-b", "review", str(review_tree), "main")
            (review_tree / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
            (review_tree / plan).write_text("Keep exactly one owner.\n", encoding="utf-8")
            _git(review_tree, "add", "service.py", plan)
            _git(review_tree, "commit", "-m", "Reviewed change")
            reviewed = run_architecture_review(review_tree, reviewer_fn=lambda _: {
                "verdict": "GO", "summary": "Reviewed change", "blocking_findings": [],
            })

            (repo / "other.py").write_text("UPSTREAM = True\n", encoding="utf-8")
            _git(repo, "add", "other.py")
            _git(repo, "commit", "-m", "Advance remote baseline")
            base = _git(repo, "rev-parse", "HEAD")
            _git(review_tree, "rebase", base)
            (review_tree / "notes.md").write_text("Non-reviewable notes\n", encoding="utf-8")
            _git(review_tree, "add", "notes.md")
            _git(review_tree, "commit", "-m", "Document the replay")
            tree = _git(review_tree, "rev-parse", "HEAD^{tree}")
            replay = _git(repo, "commit-tree", tree, "-p", base, "-m", "Replay on advanced base")
            scope = collect_review_scope(repo, main_branch=base, head_ref=replay)
            self.assertNotEqual(scope.patch_digest, reviewed.patch_digest)
            self.assertNotEqual(scope.merge_base, reviewed.merge_base)
            self.assertEqual(main_push_gate(repo, f"{replay} {replay} refs/heads/main {base}\n"), 0)
            self.assertIsNone(load_valid_receipt(repo, scope.patch_digest))
            self.assertIsNone(load_review_session(repo))
            # Carry the receipt with the recipient commit actually checked out.
            _git(repo, "merge", "--ff-only", replay)
            carried = resolve_or_carry_forward_receipt(repo, scope)
            self.assertEqual(carried["merge_base"], base)
            self.assertEqual(carried["head_sha"], replay)
            self.assertEqual(load_review_session(review_tree).last_patch_digest, reviewed.patch_digest)
            recipient = load_review_session(repo)
            self.assertEqual((recipient.last_head_sha, recipient.last_patch_digest), (replay, scope.patch_digest))
            donor = load_review_session(review_tree)
            self.assertEqual(donor.task_blueprint_anchors, {plan: (reviewed.head_sha,)})
            self.assertEqual(recipient.task_blueprint_anchors, donor.task_blueprint_anchors)

    def test_incremental_blueprint_symlink_change_uses_only_physical_diff_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review={"blueprint_globs": ["docs/plan.md"]})
            source = repo / "docs/plan-source.txt"
            source.write_text("Initial owner constraint.\n")
            (repo / "docs/plan.md").symlink_to(source.name)
            _git(repo, "add", "docs")
            _git(repo, "commit", "-m", "Record blueprint")
            run_architecture_review(repo, reviewer_fn=lambda _: {
                "verdict": "GO", "summary": "Reviewed", "blocking_findings": [],
            })
            prior = load_review_session(repo)
            source.write_text("Changed owner constraint.\n" + "Additional detail.\n" * 4200)
            _git(repo, "commit", "-am", "Update blueprint target")
            scope = collect_review_scope(repo)
            delta = collect_incremental_scope(repo, scope, prior)
            self.assertEqual(delta.reviewable_files, ("docs/plan-source.txt",))
            self.assertEqual(delta.changed_blueprints, ("docs/plan.md",))
            prompt = build_incremental_review_prompt(repo, scope, prior, delta)
            self.assertIn("docs/plan.md", prompt)
            self.assertIn("docs/plan-source.txt", prompt)

    def test_object_only_push_checks_retained_blueprint_from_another_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = "docs/plan.md"
            repo, _ = self._init_repo(root, review={"blueprint_globs": [plan]})
            _git(repo, "switch", "main")
            (repo / "obsolete.py").write_text("enabled = False\n")
            _git(repo, "add", "obsolete.py")
            _git(repo, "commit", "-m", "Record obsolete owner")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            donor = root / "donor"
            _git(repo, "worktree", "add", "-b", "review", str(donor), "main")
            (donor / plan).write_text("Keep exactly one owner.\n")
            _git(donor, "add", plan)
            _git(donor, "rm", "obsolete.py")
            _git(donor, "commit", "-m", "Review binding plan")

            def reviewer(_):
                return {"verdict": "GO", "summary": "Reviewed", "blocking_findings": []}

            reviewed = run_architecture_review(donor, reviewer_fn=reviewer)
            (donor / ".guard/config.json").write_text("{}\n")
            _git(donor, "commit", "-am", "Disable new blueprint discovery")
            base = run_architecture_review(donor, reviewer_fn=reviewer).head_sha
            self.assertEqual(load_review_session(donor).task_blueprint_anchors[plan], (reviewed.head_sha,))
            self.assertIsNone(load_review_session(repo))

            _git(donor, "rm", plan)
            tree = _git(donor, "write-tree")
            base_tree = _git(repo, "rev-parse", f"{base}^{{tree}}")
            replayed_base = _git(repo, "commit-tree", base_tree, "-p", "main", "-m", "Replay the baseline")
            for remote in (base, replayed_base):
                with self.subTest(remote=remote):
                    target = _git(repo, "commit-tree", tree, "-p", remote, "-m", "Remove retained blueprint")
                    scope = collect_review_scope(repo, main_branch=remote, head_ref=target)
                    self.assertEqual(scope.reviewable_files, (plan,))
                    prompt = build_review_prompt(repo, scope)
                    self.assertIn("Keep exactly one owner", prompt)
                    self.assertIn("[deleted", prompt.lower())
                    update = f"{target} {target} refs/heads/main {remote}\n"
                    with redirect_stderr(StringIO()) as stderr:
                        self.assertEqual(main_push_gate(repo, update), 2)
                    self.assertIn("missing review receipt", stderr.getvalue())
            self.assertIsNone(load_review_session(repo))

    def test_landed_upstream_blueprints_do_not_pollute_sibling_worktree_reviews(self) -> None:
        upstream_plan = "docs/plans/upstream.md"
        sibling_plan = "docs/plans/sibling.md"

        def go(_):
            return {"verdict": "GO", "summary": "Reviewed", "blocking_findings": []}

        for landing in ("fast-forward", "tree-replay"):
            with self.subTest(landing=landing), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                repo, _ = self._init_repo(root, review={"blueprint_globs": ["docs/plans/*.md"]})
                _git(repo, "switch", "main")
                baseline = _git(repo, "rev-parse", "HEAD")
                upstream = root / "upstream"
                sibling = root / "sibling"
                _git(repo, "worktree", "add", "-b", "upstream", str(upstream), "main")
                _git(repo, "worktree", "add", "-b", "sibling", str(sibling), "main")

                (sibling / "docs/plans").mkdir(parents=True)
                (sibling / sibling_plan).write_text("Sibling binding plan.\n", encoding="utf-8")
                (sibling / "sibling.py").write_text("SIBLING = 1\n", encoding="utf-8")
                _git(sibling, "add", "docs/plans/sibling.md", "sibling.py")
                _git(sibling, "commit", "-m", "Record sibling blueprint")
                reviewed_sibling = run_architecture_review(sibling, reviewer_fn=go)
                self.assertEqual(
                    load_review_session(sibling).task_blueprint_anchors,
                    {sibling_plan: (reviewed_sibling.head_sha,)},
                )

                (upstream / "docs/plans").mkdir(parents=True)
                (upstream / upstream_plan).write_text("Upstream binding plan.\n", encoding="utf-8")
                (upstream / "upstream.py").write_text("UPSTREAM = 1\n", encoding="utf-8")
                _git(upstream, "add", "docs/plans/upstream.md", "upstream.py")
                _git(upstream, "commit", "-m", "Record upstream blueprint")
                reviewed_upstream = run_architecture_review(upstream, reviewer_fn=go)
                upstream_head = reviewed_upstream.head_sha
                if landing == "fast-forward":
                    _git(repo, "merge", "--ff-only", "upstream")
                else:
                    tree = _git(repo, "rev-parse", f"{upstream_head}^{{tree}}")
                    replay = _git(
                        repo, "commit-tree", tree, "-p", baseline, "-m", "Replay upstream blueprint"
                    )
                    _git(repo, "merge", "--ff-only", replay)
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                landed = _git(repo, "rev-parse", "HEAD")

                _git(sibling, "rebase", "main")
                scope = collect_review_scope(sibling)
                self.assertEqual(scope.merge_base, landed)
                self.assertEqual(
                    scope.task_blueprint_anchors, {sibling_plan: (reviewed_sibling.head_sha,)}
                )
                carried = resolve_or_carry_forward_receipt(sibling, scope)
                self.assertIsNotNone(carried)
                assert carried is not None
                self.assertEqual(
                    (carried["head_sha"], carried["merge_base"], carried["mode"]),
                    (scope.head_sha, landed, "carried-forward"),
                )
                reused = run_architecture_review(
                    sibling, reviewer_fn=lambda _: self.fail("reviewer ran after the upstream landing")
                )
                self.assertEqual((reused.verdict, reused.mode), ("GO", "carried-forward"))

                (sibling / ".guard/config.json").write_text("{}\n", encoding="utf-8")
                _git(sibling, "commit", "-am", "Disable new blueprint discovery")
                self.assertEqual(run_architecture_review(sibling, reviewer_fn=go).verdict, "GO")
                disabled = _git(sibling, "rev-parse", "HEAD")

                for variant in ("delete", "edit"):
                    with self.subTest(landing=landing, variant=variant):
                        if variant == "delete":
                            _git(sibling, "rm", upstream_plan)
                            _git(sibling, "commit", "-m", "Delete the upstream-retained blueprint")
                        else:
                            (sibling / upstream_plan).write_text(
                                "Upstream binding plan, edited by the sibling.\n",
                                encoding="utf-8",
                            )
                            _git(sibling, "commit", "-am", "Edit the upstream-retained blueprint")
                        scope = collect_review_scope(sibling)
                        self.assertEqual(
                            scope.task_blueprint_anchors,
                            {
                                sibling_plan: (reviewed_sibling.head_sha,),
                                upstream_plan: (upstream_head,),
                            },
                        )
                        self.assertIn(upstream_plan, scope.reviewable_files)
                        self.assertIsNone(resolve_or_carry_forward_receipt(sibling, scope))
                        prompt = build_review_prompt(sibling, scope)
                        self.assertIn("Upstream binding plan.", prompt)
                        if variant == "delete":
                            self.assertIn("[deleted", prompt.lower())
                        _git(sibling, "reset", "--hard", disabled)

    def test_cross_worktree_symlink_blueprint_overlap_keeps_sibling_history(self) -> None:
        plan_link = "docs/plan.md"
        plan_target = "docs/plan-source.md"
        plan_retarget = "docs/plan-v2.md"

        def go(_):
            return {"verdict": "GO", "summary": "Reviewed", "blocking_findings": []}

        def disable_discovery(tree: Path, message: str) -> None:
            (tree / ".guard/config.json").write_text("{}\n", encoding="utf-8")
            _git(tree, "commit", "-am", message)

        def retarget(tree: Path, body: str) -> None:
            (tree / plan_retarget).write_text(body, encoding="utf-8")
            (tree / plan_link).unlink()
            (tree / plan_link).symlink_to("plan-v2.md")

        for direction in ("target-body", "symlink-retarget"):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                repo, _ = self._init_repo(root, review={"blueprint_globs": [plan_link]})
                _git(repo, "switch", "main")
                (repo / plan_target).write_text("Plan body one.\n", encoding="utf-8")
                (repo / plan_link).symlink_to("plan-source.md")
                _git(repo, "add", "docs")
                _git(repo, "commit", "-m", "Record symlinked blueprint")
                baseline = _git(repo, "rev-parse", "HEAD")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                upstream = root / "upstream"
                sibling = root / "sibling"
                _git(repo, "worktree", "add", "-b", "upstream", str(upstream), "main")

                if direction == "target-body":
                    (upstream / plan_target).write_text("Plan body two.\n", encoding="utf-8")
                else:
                    retarget(upstream, "Plan body two.\n")
                _git(upstream, "add", "docs")
                _git(upstream, "commit", "-m", "Review the symlinked blueprint")
                reviewed = run_architecture_review(upstream, reviewer_fn=go)
                self.assertIn(plan_link, load_review_session(upstream).task_blueprint_anchors)
                disable_discovery(upstream, "Disable new blueprint discovery")
                self.assertEqual(run_architecture_review(upstream, reviewer_fn=go).verdict, "GO")
                self.assertEqual(
                    load_review_session(upstream).task_blueprint_anchors,
                    {plan_link: (baseline, reviewed.head_sha)},
                )

                # Discovery is disabled repo-wide before the sibling branches off main.
                disable_discovery(repo, "Disable new blueprint discovery")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                _git(repo, "worktree", "add", "-b", "sibling", str(sibling), "main")
                if direction == "target-body":
                    retarget(sibling, "Sibling retarget.\n")
                else:
                    (sibling / plan_target).write_text("Sibling body.\n", encoding="utf-8")
                _git(sibling, "add", "docs")
                _git(sibling, "commit", "-m", "Change the blueprint through the other alias")

                scope = collect_review_scope(sibling)
                self.assertEqual(
                    scope.task_blueprint_anchors, {plan_link: (baseline, reviewed.head_sha)}
                )
                changed_alias = plan_link if direction == "target-body" else plan_target
                self.assertIn(changed_alias, scope.reviewable_files)
                self.assertFalse(scope.is_exempt)
                self.assertIn("Plan body two", build_review_prompt(sibling, scope))
                self.assertIsNone(resolve_or_carry_forward_receipt(sibling, scope))

    def test_advanced_baseline_cannot_carry_receipt_across_changed_blueprint(self) -> None:
        plan = "docs/plan.md"
        for retain_only in (False, True):
            with self.subTest(retain_only=retain_only), tempfile.TemporaryDirectory() as td:
                repo, _ = self._init_repo(Path(td), review={"blueprint_globs": [plan]})
                _git(repo, "switch", "main")
                (repo / plan).write_text("Keep one persistence owner.\n")
                _git(repo, "add", plan)
                _git(repo, "commit", "-m", "Set binding plan")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                _git(repo, "switch", "topic")
                _git(repo, "rebase", "main")
                (repo / "app.py").write_text("value = 1\n")
                _git(repo, "add", "app.py")
                _git(repo, "commit", "-m", "Implement reviewed patch")

                def reviewer(_):
                    return {"verdict": "GO", "summary": "Reviewed", "blocking_findings": []}

                reviewed = run_architecture_review(repo, reviewer_fn=reviewer, reviewer_note=f"See {plan}")
                if retain_only:
                    (repo / ".guard/config.json").write_text("{}\n")
                    _git(repo, "commit", "-am", "Disable new blueprint discovery")
                    reviewed = run_architecture_review(repo, reviewer_fn=reviewer)
                self.assertIn(plan, load_review_session(repo).task_blueprint_anchors)

                _git(repo, "switch", "main")
                (repo / plan).write_text("Permit a second persistence owner.\n")
                _git(repo, "commit", "-am", "Change upstream blueprint")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                _git(repo, "switch", "topic")
                _git(repo, "rebase", "main")
                scope = collect_review_scope(repo)
                self.assertEqual(scope.patch_digest, reviewed.patch_digest)
                self.assertIsNone(resolve_or_carry_forward_receipt(repo, scope))
                prompts = []

                def record_review(prompt):
                    prompts.append(prompt)
                    return reviewer(prompt)

                run_architecture_review(repo, reviewer_fn=record_review)
                self.assertIn("Keep one persistence owner", prompts[0])
                self.assertIn("Permit a second persistence owner", prompts[0])

    def test_rebase_ignores_unrelated_blueprint_additions_but_not_retained_edits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = "docs/plans/task.md"
            repo, _ = self._init_repo(
                Path(td), review={"blueprint_globs": ["docs/plans/*.md"]}
            )
            _git(repo, "switch", "main")
            (repo / "docs/plans").mkdir(parents=True)
            (repo / plan).write_text("Keep one owner.\n")
            _git(repo, "add", "docs/plans")
            _git(repo, "commit", "-m", "baseline plan")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "switch", "topic")
            _git(repo, "rebase", "main")
            (repo / "app.py").write_text("value = 1\n")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "reviewed patch")
            reviewer = lambda _: {
                "verdict": "GO", "summary": "Reviewed", "blocking_findings": [],
            }
            reviewed = run_architecture_review(
                repo, reviewer_fn=reviewer, reviewer_note=f"See {plan}"
            )
            self.assertIn(plan, load_review_session(repo).task_blueprint_anchors)

            # An unrelated blueprint added on main is outside the replayed branch contract.
            _git(repo, "switch", "main")
            (repo / "docs/plans/unrelated.md").write_text("Unrelated plan.\n")
            _git(repo, "add", "docs/plans/unrelated.md")
            _git(repo, "commit", "-m", "add unrelated blueprint upstream")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "switch", "topic")
            _git(repo, "rebase", "main")
            scope = collect_review_scope(repo)
            self.assertEqual(scope.patch_digest, reviewed.patch_digest)
            self.assertNotEqual(scope.merge_base, reviewed.merge_base)
            carried = resolve_or_carry_forward_receipt(repo, scope)
            self.assertIsNotNone(carried)
            assert carried is not None
            self.assertEqual((carried["head_sha"], carried["merge_base"]), (scope.head_sha, scope.merge_base))
            self.assertIn(plan, load_review_session(repo).task_blueprint_anchors)

            # Editing a retained blueprint upstream still fails closed across the rebase.
            _git(repo, "switch", "main")
            (repo / plan).write_text("Permit a second persistence owner.\n")
            _git(repo, "commit", "-am", "edit retained blueprint upstream")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "switch", "topic")
            _git(repo, "rebase", "main")
            scope = collect_review_scope(repo)
            self.assertIsNone(resolve_or_carry_forward_receipt(repo, scope))

    def test_receipt_reuse_requires_review_of_retained_blueprint_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = "docs/plan.md"
            repo, _ = self._init_repo(root, review={"blueprint_globs": [plan]})
            base = _git(repo, "rev-parse", "main")
            (repo / "app.py").write_text("value = 1\n")
            (repo / plan).write_text("Keep one owner.\n")
            _git(repo, "add", "app.py", plan)
            _git(repo, "commit", "-m", "Record initial blueprint")

            def go(_):
                return {"verdict": "GO", "summary": "Reviewed", "blocking_findings": []}

            original = run_architecture_review(repo, reviewer_fn=go)
            linked = root / "linked"
            _git(repo, "worktree", "add", "-b", "linked", str(linked), original.head_sha)
            (linked / plan).write_text("Keep one owner and retire the old write path.\n")
            _git(linked, "commit", "-am", "Tighten blueprint")
            tightened = run_architecture_review(linked, reviewer_fn=lambda _: {
                "verdict": "NO-GO", "summary": "Retirement missing", "blocking_findings": [{
                    "rule_id": "guard.single-source-of-truth", "file": "app.py", "line": 1,
                    "issue": "The old write path remains", "required_fix": "Remove it",
                }],
            })
            self.assertEqual(tightened.verdict, "NO-GO")

            _git(linked, "revert", "--no-edit", tightened.head_sha)
            scope = collect_review_scope(linked)
            self.assertEqual(scope.patch_digest, original.patch_digest)
            self.assertIn(tightened.head_sha, scope.task_blueprint_anchors[plan])
            self.assertIn("retire the old write path", build_review_prompt(linked, scope))
            self.assertIsNone(resolve_or_carry_forward_receipt(linked, scope))
            self.assertEqual(load_review_session(linked).last_verdict, "NO-GO")

            _git(linked, "reset", "--hard", original.head_sha)
            scope = collect_review_scope(linked)
            self.assertIn(tightened.head_sha, scope.task_blueprint_anchors[plan])
            with redirect_stderr(StringIO()) as stderr:
                update = f"{original.head_sha} {original.head_sha} refs/heads/main {base}\n"
                self.assertEqual(main_push_gate(linked, update), 2)
            self.assertIn("blueprint-anchor drift", stderr.getvalue())

            fresh = run_architecture_review(linked, reviewer_fn=go)
            self.assertEqual(fresh.verdict, "GO")
            self.assertEqual(main_push_gate(linked, update), 0)

    def test_configured_blueprint_change_is_reviewable_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review={"blueprint_globs": ["docs/plan.md"]})
            (repo / "docs/plan.md").write_text("Binding plan.\n")
            (repo / "docs/notes.md").write_text("Unrelated notes.\n")
            _git(repo, "add", "docs")
            _git(repo, "commit", "-m", "Record plan and notes")
            self.assertEqual(collect_review_scope(repo).reviewable_files, ("docs/plan.md",))


    def test_save_review_session_prunes_deleted_worktree_records_below_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            common_dir = root / "git"
            active_worktree = root / "active"
            deleted_worktree = root / "deleted"
            active_worktree.mkdir()
            deleted_worktree.mkdir()
            state = session.ReviewSessionState(
                worktree_path=str(active_worktree),
                branch="topic",
                merge_base="base",
                provider="codex",
                session_id="session",
                last_head_sha="head",
                last_patch_digest="digest",
                last_verdict="GO",
                last_summary="ok",
                last_blocking_findings=(),
                last_submodule_shas={},
            )
            active = session.save_review_session(common_dir, state)
            relative = session.save_review_session(
                common_dir, replace(state, worktree_path="relative-worktree")
            )
            missing_parent = session.save_review_session(
                common_dir, replace(state, worktree_path=str(root / "absent" / "worktree"))
            )
            deleted_state = replace(state, worktree_path=str(deleted_worktree))
            deleted = session.save_review_session(common_dir, deleted_state)
            deleted_worktree.rmdir()

            current = session.save_review_session(
                common_dir, replace(deleted_state, branch="current")
            )

            self.assertEqual(
                set(current.parent.glob("*.json")), {current, active, relative, missing_parent}
            )
            self.assertFalse(deleted.exists())

    def test_contract_digest_change_invalidates_incremental_session_and_carry_forward(self) -> None:
        """Changing repository config prevents carry-forward and forces a full fresh review."""
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            service = repo / "services/catalog/service.py"
            service.parent.mkdir(parents=True, exist_ok=True)
            service.write_text("VALUE = 1\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "initial review scope")
            reviewer = lambda _prompt: {"verdict": "GO", "summary": "ok", "blocking_findings": []}
            first = run_architecture_review(repo, reviewer_fn=reviewer)
            self.assertEqual(first.mode, "full")
            config_path = repo / ".guard/config.json"
            config_path.parent.mkdir(exist_ok=True)
            config_path.write_text('{"main_branch":"main","review":{"project_title":"changed"}}\n', encoding="utf-8")
            service.write_text("VALUE = 2\n", encoding="utf-8")
            _git(repo, "add", ".guard/config.json", "services/catalog/service.py")
            _git(repo, "commit", "-m", "change review contract")
            scope = collect_review_scope(repo)
            self.assertIsNone(resolve_or_carry_forward_receipt(repo, scope))
            second = run_architecture_review(repo, reviewer_fn=reviewer)
            self.assertEqual(second.mode, "full")
            saved = load_review_session(repo)
            self.assertIsNotNone(saved)
            self.assertTrue(saved.contract_digest)

    def _start_module_review(
        self, tmp: Path, *, with_downstream: bool = False
    ) -> tuple[Path, dict[str, str], ReviewResult]:
        repo, _ = self._init_repo(tmp)
        _git(repo, "switch", "main")
        (repo / ".guard").mkdir()
        (repo / ".guard/config.json").write_text(json.dumps({"topology": {
            "modules": [
                {"path": "services/a", "role": "feature owner", "dependencies": ["services/b"]},
                {"path": "services/b", "role": "dependency owner"},
                {
                    "path": "services/c", "role": "other owner",
                    "dependencies": ["services/a"] if with_downstream else [],
                },
            ],
            "retired_tables": [{"tables": ["c_only"], "scope_prefixes": ["services/c"]}],
        }}))
        for module in ("a", "b", "c"):
            directory = repo / "services" / module
            directory.mkdir(parents=True)
            (directory / "main.py").write_text("value = 1\n")
            (directory / "CLAUDE.md").write_text(f"{module} original boundary\n")
        (repo / "services/a/CLAUDE.md").unlink()
        (repo / "services/a/CLAUDE.md").symlink_to("../../docs/a-owner.md")
        (repo / "docs/a-owner.md").write_text("a original boundary\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "declare baseline modules")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        _git(repo, "switch", "topic")
        _git(repo, "rebase", "main")
        (repo / "services/a/main.py").write_text("value = 2\n")
        _git(repo, "commit", "-am", "round 1")

        fake_home = tmp / "home"
        (fake_home / ".claude").mkdir(parents=True)
        fake_bin = fake_home / ".local/bin"
        _write_exec(
            fake_bin / "claude",
            "#!/usr/bin/env python3\n"
            "import json, subprocess, sys\n"
            "if '--version' in sys.argv:\n"
            "    print('2.0.0')\n"
            "    raise SystemExit(0)\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'module-session'}))\n"
            "print(json.dumps({'type': 'result', 'result': {'structured_output': {\n"
            "    'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),\n"
            "    'verdict': 'GO', 'summary': 'module review', 'blocking_findings': []\n"
            "}}}))\n",
        )
        env = {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(fake_home),
            "THE_GUARD_REVIEW_CONFIG": str(fake_home / ".config/the-guard/review-agent.json"),
            "THE_GUARD_REVIEW_LOG_DIR": str(tmp / "logs"),
        }
        first = run_architecture_review(repo, env=env)
        self.assertEqual((first.mode, first.verdict, first.session_id), ("full", "GO", "module-session"))
        return repo, env, first

    def test_boundary_fix_resumes_review_and_replaces_receipt_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, first = self._start_module_review(Path(td))
            prior = load_review_session(repo)
            old_receipt = load_valid_receipt(repo, first.patch_digest)
            self.assertEqual(old_receipt["contract_digest"], prior.contract_digest)

            document = repo / "services/a/CLAUDE.md"
            document.unlink()
            document.write_text("a revised boundary\n")
            _git(repo, "commit", "-am", "clarify owner during review")
            self.assertIsNone(resolve_or_carry_forward_receipt(repo, collect_review_scope(repo)))

            second = run_architecture_review(repo, env=env)
            self.assertEqual((second.mode, second.provider), ("incremental", "claude"))
            prompt = Path(second.log_path.replace(".events.jsonl", ".prompt.md")).read_text()
            self.assertIn("## Updated Module Scope & Boundary Context", prompt)
            self.assertIn("`services/a/CLAUDE.md`", prompt)
            self.assertIn("See the verified incremental scope", prompt)
            self.assertNotIn("## Boundary reference: services/a/CLAUDE.md", prompt)
            self.assertEqual(prompt.count("+a revised boundary"), 1)
            saved = load_review_session(repo)
            self.assertEqual(saved.round_count, 2)
            self.assertTrue(saved.session_contract_digest)
            self.assertEqual(prior.session_contract_digest, saved.session_contract_digest)
            self.assertNotEqual(prior.contract_digest, saved.contract_digest)
            receipt = load_valid_receipt(repo, second.patch_digest)
            self.assertEqual(receipt["contract_digest"], saved.contract_digest)

    def test_owned_config_edits_continue_but_neighbour_edits_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, _ = self._start_module_review(Path(td))
            path = repo / ".guard/config.json"
            config = json.loads(path.read_text())
            config["topology"]["modules"][0]["role"] = "updated feature owner"
            config["topology"]["retired_tables"].append(
                {"tables": ["obsolete"], "scope_prefixes": ["services/a"]},
            )
            path.write_text(json.dumps(config, indent=2))
            _git(repo, "commit", "-am", "Update the touched owner contract")
            result = run_architecture_review(repo, env=env)
            self.assertEqual(result.mode, "incremental")
            config["topology"]["modules"][1]["role"] = "updated neighbour"
            path.write_text(json.dumps(config, indent=2))
            _git(repo, "commit", "-am", "Update a neighbour contract")
            with redirect_stderr(StringIO()) as stderr:
                result = run_architecture_review(repo, env=env)
            self.assertEqual(result.mode, "full")
            self.assertIn(".guard/config.json", stderr.getvalue())
            meta = json.loads(Path(result.log_path.replace(".events.jsonl", ".meta.json")).read_text())
            self.assertEqual(meta["contract_change_causes"], [".guard/config.json"])
            entries = (Path(result.log_path).parent / "index.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(entries[-1])["contract_change_causes"], [".guard/config.json"])

    def _add_codex_provider(self, repo: Path, env: dict[str, str]) -> Path:
        home = Path(env["HOME"])
        (home / ".codex").mkdir()
        (home / ".codex/auth.json").write_text("{}")
        _write_exec(home / ".local/bin/codex", "#!/bin/sh\nexit 0\n")
        probe_environment(repo, env=env)
        return Path(env["THE_GUARD_REVIEW_CONFIG"])

    def test_active_session_provider_affinity_leaves_global_preference_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, first = self._start_module_review(Path(td))
            cfg_file = self._add_codex_provider(repo, env)
            before = cfg_file.read_bytes()
            self.assertEqual(json.loads(before)["preferred_provider"], "codex")
            (repo / "services/a/main.py").write_text("value = 3\n")
            _git(repo, "commit", "-am", "round 2")
            result = run_architecture_review(repo, env=env)
            self.assertEqual((result.mode, result.provider), ("incremental", "claude"))
            self.assertEqual(result.session_id, first.session_id)
            self.assertEqual(cfg_file.read_bytes(), before)

    def test_successful_session_continuation_clears_provider_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, first = self._start_module_review(Path(td))
            cfg_file = self._add_codex_provider(repo, env)
            record_provider_cooldown(repo, "claude", reason="quota exceeded", env=env)
            self.assertIn("claude", json.loads(cfg_file.read_bytes())["provider_cooldowns"])
            (repo / "services/a/main.py").write_text("value = 3\n")
            _git(repo, "commit", "-am", "round 2")
            result = run_architecture_review(repo, env=env)
            self.assertEqual((result.mode, result.provider), ("incremental", "claude"))
            self.assertEqual(result.session_id, first.session_id)
            saved = json.loads(cfg_file.read_bytes())
            self.assertEqual(saved["preferred_provider"], "codex")
            self.assertNotIn("provider_cooldowns", saved)

    def test_finishing_old_fallback_session_does_not_overwrite_recovered_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, env, _ = self._start_module_review(tmp)
            codex = tmp / "home/.local/bin/codex"
            _write_exec(codex, "#!/bin/sh\nexit 0\n")
            env = {**env, "OPENAI_API_KEY": "test-credentials"}
            cfg_file = Path(env["THE_GUARD_REVIEW_CONFIG"])
            original = json.loads(cfg_file.read_text())
            self.assertEqual(original["priority_order"][0], "codex")
            self.assertEqual(original["preferred_provider"], "claude")
            (repo / "services/a/main.py").write_text("value = 3\n")
            _git(repo, "commit", "-am", "round 2")

            def finish_after_recovery(*args, **kwargs):
                self.assertEqual(args[0], "claude")
                self.assertEqual(kwargs["resume_session_id"], "module-session")
                record_working_provider(repo, "codex", str(codex), env)
                return ReviewOutput("GO", "ok", (), ""), "module-session", ""

            with mock.patch("the_guard.review.engine._invoke_provider", side_effect=finish_after_recovery):
                result = run_architecture_review(repo, env=env)
            self.assertEqual((result.mode, result.provider), ("incremental", "claude"))
            self.assertEqual(json.loads(cfg_file.read_text())["preferred_provider"], "codex")

    def test_run_index_rotates_atomically_and_retains_four_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {"THE_GUARD_REVIEW_LOG_DIR": str(root / "logs")}

            def finish_run() -> Path:
                logger = ReviewRunLogger(
                    root, worktree_path=root, branch="topic", provider="codex", mode="full",
                    merge_base="a" * 40, head_sha="b" * 40, patch_digest="c" * 64,
                    reviewable_file_count=1, prompt="Snapshot", env=env,
                )
                logger.finish(status="completed")
                return logger.meta_path

            first = finish_run()
            index = first.parent / "index.jsonl"
            backup = index.with_name("index.jsonl.1")
            backup.write_text("old backup")
            old_index = b"x" * (2 * 1024 * 1024)
            index.write_bytes(old_index)
            with mock.patch.object(session, "MAX_RETAINED_RUN_LOGS", 4):
                recent = [finish_run() for _ in range(5)]
            self.assertEqual(backup.read_bytes(), old_index)
            self.assertEqual(len(index.read_text().splitlines()), 5)
            self.assertEqual(set(first.parent.glob("*.meta.json")), set(recent[-4:]))

    def test_transient_primary_retries_before_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, _ = self._start_module_review(Path(td))
            cfg_file = self._add_codex_provider(repo, env)
            before = cfg_file.read_bytes()
            self.assertEqual(json.loads(before)["preferred_provider"], "codex")
            sleep = mock.Mock()
            with (
                mock.patch("the_guard.review.engine._invoke_provider", side_effect=[
                    RuntimeError("429 at capacity"), RuntimeError("503 overloaded"),
                    RuntimeError("stream disconnected"), (ReviewOutput("GO", "ok", (), ""), "", ""),
                ]) as invoke,
                redirect_stderr(StringIO()) as stderr,
            ):
                result = run_architecture_review(repo, env=env, force=True, _sleep_fn=sleep)
            self.assertEqual(result.provider, "claude")
            self.assertEqual([call.args[0] for call in invoke.call_args_list],
                             ["codex", "codex", "codex", "claude"])
            self.assertEqual(sleep.call_args_list, [mock.call(10.0), mock.call(20.0)])
            self.assertIn("--force", stderr.getvalue())
            self.assertEqual(cfg_file.read_bytes(), before)

    def test_newly_touched_declared_module_resumes_review_with_its_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, _ = self._start_module_review(Path(td))
            (repo / "services/c/main.py").write_text("value = 2\n")
            _git(repo, "commit", "-am", "extend fix to another owner")

            second = run_architecture_review(repo, env=env)
            self.assertEqual(second.mode, "incremental")
            prompt = Path(second.log_path.replace(".events.jsonl", ".prompt.md")).read_text()
            update = prompt.split("## Updated Module Scope & Boundary Context", 1)[1].split("\n## ", 1)[0]
            self.assertIn("`services/c`", update)
            self.assertNotIn("`services/a`", update)
            self.assertIn('"role": "other owner"', update)
            self.assertIn('"c_only"', update)
            self.assertIn("## Boundary reference: services/c/CLAUDE.md\nc original boundary", prompt)

    def test_missing_historical_submodule_checkout_falls_back_to_full_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, _ = self._start_module_review(Path(td))
            _git(repo, "switch", "main")
            path = "services/library"
            _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-b", "main", str(repo.parent / "origin.git"), path)
            _git(repo, "commit", "-m", "register library submodule")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "switch", "topic")
            _git(repo, "rebase", "main")
            library = repo / path
            _git(library, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "advance library")
            _git(repo, "commit", "-am", "review library update")
            first = run_architecture_review(repo, env=env, force=True)
            self.assertEqual(first.verdict, "GO")

            _git(repo, "rm", "-f", path)
            (repo / "docs/a-owner.md").write_text("a revised boundary\n")
            _git(repo, "commit", "-am", "remove the library and clarify the boundary")
            second = run_architecture_review(repo, env=env)
            self.assertEqual((second.verdict, second.mode), ("GO", "full"))

    def test_rebased_boundary_target_change_has_bounded_diff_with_or_without_branch_delta(self) -> None:
        for branch_delta in (True, False):
            with self.subTest(branch_delta=branch_delta), tempfile.TemporaryDirectory() as td:
                repo, env, first = self._start_module_review(Path(td))
                _git(repo, "switch", "main")
                (repo / "docs/a-owner.md").write_text("a upstream boundary\n" + "more detail\n" * 1_000)
                _git(repo, "commit", "-am", "advance the resolved boundary document upstream")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                _git(repo, "switch", "topic")
                _git(repo, "rebase", "main")
                rebased = collect_review_scope(repo)
                self.assertEqual(rebased.patch_digest, first.patch_digest)
                self.assertIsNone(resolve_or_carry_forward_receipt(repo, rebased))
                if branch_delta:
                    (repo / "services/a/main.py").write_text("value = 3\n")
                    _git(repo, "commit", "-am", "round 2")

                second = run_architecture_review(repo, env=env)
                meta = json.loads(Path(second.log_path.replace(".events.jsonl", ".meta.json")).read_text())
                self.assertEqual((second.mode, meta["full_review_reason"]), ("incremental", ""))
                self.assertEqual(second.session_id, first.session_id)
                prompt = Path(second.log_path.replace(".events.jsonl", ".prompt.md")).read_text()
                update = prompt.split("## Updated Module Scope & Boundary Context", 1)[1].split("\n## ", 1)[0]
                self.assertIn("`services/a/CLAUDE.md`", update)
                diff = update.split("```diff\n", 1)[1].split("\n```", 1)[0]
                self.assertIn("-a original boundary\n+a upstream boundary", diff)
                self.assertIn("[truncated", diff)
                self.assertLessEqual(len(diff), 8_000)
                if not branch_delta:
                    self.assertIn("Rebased branch patch unchanged", prompt)
                    self.assertNotIn("## Verified incremental scope", prompt)
                    receipt = load_valid_receipt(repo, first.patch_digest)
                    self.assertEqual(receipt["head_sha"], second.head_sha)

    def test_same_patch_contract_mismatch_without_inspectable_delta_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, _ = self._start_module_review(Path(td))
            config_path = repo / ".guard/config.json"
            config = json.loads(config_path.read_text())
            config["review"] = {"blueprint_globs": ["docs/plan.md"]}
            config_path.write_text(json.dumps(config))
            (repo / "docs/plan.md").write_text("# Branch plan\n")
            _git(repo, "add", ".guard/config.json", "docs/plan.md")
            _git(repo, "commit", "-m", "bind a branch blueprint")
            first = run_architecture_review(repo, env=env)
            prior = load_review_session(repo)
            assert prior is not None
            self.assertIn("docs/plan.md", prior.task_blueprint_anchors)
            save_review_session(repo, replace(prior, contract_digest="different-receipt-contract"))
            _git(repo, "switch", "main")
            (repo / "services/c/CLAUDE.md").write_text("unrelated change\n")
            _git(repo, "commit", "-am", "update unrelated owner")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "switch", "topic")
            _git(repo, "rebase", "main")
            rebased = collect_review_scope(repo)
            self.assertEqual(rebased.patch_digest, first.patch_digest)
            with self.assertRaisesRegex(RuntimeError, "No inspectable boundary or blueprint delta"):
                build_incremental_review_prompt(repo, rebased, prior, None)
            second = run_architecture_review(repo, env=env)
            meta = json.loads(Path(second.log_path.replace(".events.jsonl", ".meta.json")).read_text())
            self.assertEqual((second.mode, meta["full_review_reason"]), ("full", "contract_changed"))

    def test_contract_change_causes_exclude_untouched_module_documents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _, first = self._start_module_review(Path(td))
            _git(repo, "switch", "main")
            (repo / "docs/a-owner.md").write_text("touched owner change\n")
            (repo / "services/c/CLAUDE.md").write_text("unrelated change\n")
            _git(repo, "commit", "-am", "update boundary documents")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "switch", "topic")
            _git(repo, "rebase", "main")
            scope = collect_review_scope(repo)
            config = load_config(repo, revision=scope.head_sha)
            policy = load_policy(repo, revision=scope.head_sha, config=config)
            self.assertEqual(
                contract_change_causes(repo, scope, first.head_sha, config, policy),
                ("docs/a-owner.md",),
            )

    def test_root_contract_and_legacy_session_changes_force_full_review(self) -> None:
        for change in ("branch config", "root instructions", "policy detail", "legacy session", "version"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                repo, env, _ = self._start_module_review(Path(td))
                if change in {"legacy session", "version"}:
                    session_path = next((repo / ".git/the-guard-sessions").glob("*.json"))
                    payload = json.loads(session_path.read_text())
                    if change == "legacy session":
                        payload.pop("session_contract_digest", None)
                    else:
                        payload["session_contract_digest"] = "old-version-digest"
                    session_path.write_text(json.dumps(payload))
                document, content = {
                    "branch config": (".guard/config.json", "\n"),
                    "root instructions": ("AGENTS.md", "\nClarify architecture responsibilities.\n"),
                    "policy detail": ("docs/standards/rules/single-source-of-truth.md", "\nOne writer.\n"),
                    "legacy session": ("docs/a-owner.md", "\nClarify ownership.\n"),
                    "version": ("README.md", "\nOperational note.\n"),
                }[change]
                path = repo / document
                if change == "branch config":
                    config = json.loads(path.read_text())
                    config["review"] = {"project_title": "New project title"}
                    path.write_text(json.dumps(config))
                else:
                    path.write_text(path.read_text() + content)
                (repo / "services/a/main.py").write_text("value = 3\n")
                _git(repo, "commit", "-am", "review contract change")

                second = run_architecture_review(repo, env=env)
                meta = json.loads(Path(second.log_path.replace(".events.jsonl", ".meta.json")).read_text())
                self.assertEqual((second.mode, meta["full_review_reason"]), ("full", "contract_changed"))
                self.assertEqual(
                    meta["contract_change_causes"],
                    ["contract_version_or_legacy" if change == "version" else document],
                )
                if change == "branch config":
                    reviewed = load_review_session(repo)
                    path.write_text(path.read_text() + "\n")
                    (repo / "services/a/main.py").write_text("value = 4\n")
                    _git(repo, "commit", "-am", "edit the already reviewed configuration")
                    third = run_architecture_review(repo, env=env)
                    meta = json.loads(Path(third.log_path.replace(".events.jsonl", ".meta.json")).read_text())
                    self.assertEqual((third.mode, meta["full_review_reason"]), ("incremental", ""))
                    saved = load_review_session(repo)
                    self.assertEqual(saved.contract_digest, reviewed.contract_digest)
                    self.assertEqual(saved.session_contract_digest, reviewed.session_contract_digest)

    def test_rebase_only_invalidates_for_related_module_declarations(self) -> None:
        for module, mode, reason in (
            ("services/a", "full", "contract_changed"),
            ("services/b", "full", "contract_changed"),
            ("services/c", "incremental", ""),
            ("services/new", "full", "contract_changed"),
            ("remove downstream", "full", "contract_changed"),
        ):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as td:
                repo, env, _ = self._start_module_review(
                    Path(td), with_downstream=module == "remove downstream"
                )
                _git(repo, "switch", "main")
                path = repo / ".guard/config.json"
                config = json.loads(path.read_text())
                modules = config["topology"]["modules"]
                if module == "services/new":
                    modules.append({"path": module, "role": "new caller", "dependencies": ["services/a"]})
                    (repo / module).mkdir()
                    (repo / module / "main.py").write_text("value = 1\n")
                    _git(repo, "add", module)
                elif module == "remove downstream":
                    modules[:] = [item for item in modules if item["path"] != "services/c"]
                    _git(repo, "rm", "-r", "services/c")
                else:
                    next(item for item in modules if item["path"] == module)["role"] = "updated owner"
                if module == "services/c":
                    # Force a receipt mismatch so this also exercises projection of the historical config.
                    (repo / "docs/a-owner.md").write_text("a updated boundary\n")
                path.write_text(json.dumps(config))
                _git(repo, "commit", "-am", "change a module declaration upstream")
                _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
                _git(repo, "switch", "topic")
                _git(repo, "rebase", "main")
                (repo / "services/a/main.py").write_text("value = 3\n")
                _git(repo, "commit", "-am", "round 2")

                second = run_architecture_review(repo, env=env)
                meta = json.loads(Path(second.log_path.replace(".events.jsonl", ".meta.json")).read_text())
                self.assertEqual((second.mode, meta["full_review_reason"]), (mode, reason))

    def test_unrelated_module_contract_changes_allow_incremental_rebase_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, env, first = self._start_module_review(Path(td))
            prior = load_review_session(repo)
            assert prior is not None

            # An unrelated module's boundary document lands on main and the branch rebases.
            _git(repo, "switch", "main")
            (repo / "services/c/CLAUDE.md").write_text("c boundary tightened\n")
            config_path = repo / ".guard/config.json"
            config = json.loads(config_path.read_text())
            config["topology"].update(
                retired_tables=[{"tables": ["retired_elsewhere"], "scope_prefixes": ["services/c"]}],
                forbidden_patterns=[{"literal_or_regex": "never_here", "scope_prefixes": ["services/c*"]}],
                forbidden_imports=[{"imports": ["elsewhere"], "scope_prefixes": ["services/c"]}],
            )
            config_path.write_text(json.dumps(config))
            _git(repo, "commit", "-am", "advance unrelated module contract")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "switch", "topic")
            _git(repo, "rebase", "main")
            rebased = collect_review_scope(repo)
            self.assertEqual(rebased.patch_digest, first.patch_digest)
            self.assertNotEqual(rebased.merge_base, first.merge_base)
            self.assertEqual(
                compute_review_contract_digest(
                    load_policy(repo, revision=rebased.head_sha),
                    load_config(repo, revision=rebased.head_sha),
                    repo_root=repo,
                    revision=rebased.head_sha,
                    scope=rebased,
                ),
                prior.contract_digest,
            )
            self.assertIsNotNone(resolve_or_carry_forward_receipt(repo, rebased))
            prompt = build_review_prompt(repo, rebased)
            self.assertNotIn("retired_elsewhere", prompt)
            self.assertNotIn("never_here", prompt)
            self.assertNotIn('"elsewhere"', prompt)

            (repo / "services/a/main.py").write_text("value = 3\n")
            _git(repo, "commit", "-am", "round 2")
            second = run_architecture_review(repo, env=env)
            self.assertEqual((second.mode, second.verdict), ("incremental", "GO"))
            meta = json.loads(Path(second.log_path.replace(".events.jsonl", ".meta.json")).read_text())
            self.assertEqual(meta["resume_session_id"], first.session_id)

    def test_contract_digest_changes_when_repository_config_changes(self) -> None:
        """A policy/config change must produce a new digest and invalidate old continuation evidence."""
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            first = compute_review_contract_digest(load_policy(repo), load_config(repo))
            (repo / ".guard").mkdir(exist_ok=True)
            (repo / ".guard/config.json").write_text('{"main_branch":"main","review":{"project_title":"changed"}}\n', encoding="utf-8")
            second = compute_review_contract_digest(load_policy(repo), load_config(repo))
            self.assertNotEqual(first, second)

    def test_contract_digest_changes_with_review_contract_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            policy, config = load_policy(repo), load_config(repo)
            for digest in (compute_review_contract_digest, session.compute_session_contract_digest):
                with self.subTest(digest=digest.__name__):
                    baseline = digest(policy, config)
                    with mock.patch.object(session, "REVIEW_CONTRACT_VERSION", "future"):
                        self.assertNotEqual(baseline, digest(policy, config))

    def test_contract_digest_ignores_runtime_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = self._init_repo(root)
            policy, config = load_policy(repo), load_config(repo)
            runtime = root / "installed" / "the_guard"
            review = runtime / "review"
            review.mkdir(parents=True)
            for name in ("config.py", "policy.py", "git_helpers.py"):
                (runtime / name).write_text("# Packaged module\n", encoding="utf-8")
            module = review / "different_name.py"
            for digest in (compute_review_contract_digest, session.compute_session_contract_digest):
                with self.subTest(digest=digest.__name__):
                    baseline = digest(policy, config)
                    module.write_text("# Patched runtime\n", encoding="utf-8")
                    with mock.patch.object(session, "__file__", str(review / "session.py")):
                        self.assertEqual(baseline, digest(policy, config))
                        module.write_text("# Another release\n", encoding="utf-8")
                        self.assertEqual(baseline, digest(policy, config))

    def test_scoped_contract_digest_changes_with_committed_repository_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td), review={})
            config_path = repo / ".guard/config.json"
            config_data = {"topology": {"modules": [{
                "path": "app", "boundary_docs": ["app/README.md"],
            }]}}
            config_path.write_text(json.dumps(config_data), encoding="utf-8")
            app = repo / "app"
            app.mkdir()
            boundary = app / "README.md"
            boundary.write_text("Application owns the source data.\n", encoding="utf-8")
            (app / "main.py").write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "Add reviewed application")

            def digest() -> str:
                scope = collect_review_scope(repo)
                config = load_config(repo, revision=scope.head_sha)
                policy = load_policy(repo, revision=scope.head_sha, config=config)
                return compute_review_contract_digest(
                    policy, config, repo_root=repo, revision=scope.head_sha, scope=scope,
                )

            previous = digest()
            policy_path = repo / "docs/standards/rules/single-source-of-truth.md"
            updates = (
                (policy_path, policy_path.read_text() + "\nKeep data ownership explicit.\n"),
                (config_path, json.dumps({**config_data, "review": {"project_title": "App"}})),
                (boundary, "Application owns source data and validation.\n"),
            )
            for path, content in updates:
                with self.subTest(path=path.relative_to(repo)):
                    path.write_text(content, encoding="utf-8")
                    _git(repo, "commit", "-am", f"Update {path.name}")
                    current = digest()
                    self.assertNotEqual(previous, current)
                    previous = current

    def test_scoped_contract_digest_covers_only_touched_and_one_hop_modules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            for module in ("a", "b", "c", "d"):
                (repo / f"services/{module}").mkdir(parents=True)
                (repo / f"services/{module}/main.py").write_text("value = 1\n")
                (repo / f"services/{module}/CLAUDE.md").write_text(f"{module} owner\n")
            (repo / "docs/a-owner.md").write_text("a explicit owner\n")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "module baseline")
            base = _git(repo, "rev-parse", "HEAD")
            (repo / "services/a/main.py").write_text("value = 2\n")
            _git(repo, "commit", "-am", "reviewed change")
            head = _git(repo, "rev-parse", "HEAD")
            scope = ReviewScope(
                base_ref="main",
                merge_base=base,
                head_sha=head,
                changed_files=("services/a/main.py",),
                reviewable_files=("services/a/main.py",),
                submodule_changes=(),
                patch_digest="patch",
            )
            modules = [
                {
                    "path": "services/a",
                    "dependencies": ["services/b"],
                    "boundary_docs": ["docs/a-owner.md"],
                },
                {"path": "services/b", "layer": "domain", "role": "upstream owner"},
                {"path": "services/c", "dependencies": ["services/a"], "layer": "workflow"},
                {"path": "services/d", "layer": "surface", "role": "unrelated owner"},
            ]
            config = GuardConfig(topology={"modules": modules})
            policy = load_policy(repo, revision=head)

            def digest(config_value: GuardConfig = config, revision: str = head) -> str:
                return compute_review_contract_digest(
                    policy, config_value, repo_root=repo, revision=revision, scope=scope
                )

            def variant(overrides: dict[str, dict]) -> GuardConfig:
                return GuardConfig(topology={"modules": [
                    {**module, **overrides.get(module["path"], {})}
                    for module in modules
                ]})

            touched, upstream, downstream = resolve_scope_modules(
                repo, scope, config, revision=head
            )
            self.assertEqual(touched, {"services/a"})
            self.assertEqual(upstream, {"services/b"})
            self.assertEqual(downstream, {"services/c"})
            baseline = digest()

            # Unrelated module declarations and one-hop boundary prose stay outside the contract.
            self.assertEqual(
                baseline, digest(variant({"services/d": {"layer": "foundation"}}))
            )
            self.assertEqual(
                baseline,
                digest(variant({"services/d": {"dependencies": ["services/b"]}})),
            )
            (repo / "services/b/CLAUDE.md").write_text("b owner tightened\n")
            _git(repo, "commit", "-am", "upstream module prose")
            upstream_doc = _git(repo, "rev-parse", "HEAD")
            self.assertEqual(baseline, digest(revision=upstream_doc))
            (repo / "services/c/CLAUDE.md").write_text("c owner tightened\n")
            _git(repo, "commit", "-am", "downstream module prose")
            downstream_doc = _git(repo, "rev-parse", "HEAD")
            self.assertEqual(baseline, digest(revision=downstream_doc))
            (repo / "services/d/CLAUDE.md").write_text("d owner tightened\n")
            _git(repo, "commit", "-am", "unrelated module prose")
            unrelated_doc = _git(repo, "rev-parse", "HEAD")
            self.assertEqual(baseline, digest(revision=unrelated_doc))

            # Touched prose and one-hop structural contract changes invalidate.
            (repo / "services/a/CLAUDE.md").write_text("a owner tightened\n")
            _git(repo, "commit", "-am", "touched module prose")
            touched_doc = _git(repo, "rev-parse", "HEAD")
            self.assertNotEqual(baseline, digest(revision=touched_doc))
            (repo / "docs/a-owner.md").write_text("a explicit owner tightened\n")
            _git(repo, "commit", "-am", "touched explicit boundary document")
            touched_explicit_doc = _git(repo, "rev-parse", "HEAD")
            self.assertNotEqual(baseline, digest(revision=touched_explicit_doc))
            self.assertNotEqual(
                baseline, digest(variant({"services/a": {"dependencies": []}}))
            )
            for upstream_change in (
                {"layer": "workflow"},
                {"role": "tighter owner"},
                {"dependencies": ["services/d"]},
                {"retired": True},
            ):
                with self.subTest(upstream_change=upstream_change):
                    self.assertNotEqual(
                        baseline, digest(variant({"services/b": upstream_change}))
                    )
            for downstream_change in (
                {"layer": "surface"},
                {"role": "downstream owner"},
                {"dependencies": ["services/a", "services/d"]},
                {"retired": True},
            ):
                with self.subTest(downstream_change=downstream_change):
                    self.assertNotEqual(
                        baseline, digest(variant({"services/c": downstream_change}))
                    )

            # A scope touching no declared module keeps the empty scoped projection.
            unbound_scope = replace(
                scope,
                changed_files=("tests/test_x.py",),
                reviewable_files=("tests/test_x.py",),
            )

            def unbound_digest(config_value: GuardConfig = config) -> str:
                return compute_review_contract_digest(
                    policy,
                    config_value,
                    repo_root=repo,
                    revision=head,
                    scope=unbound_scope,
                )

            self.assertEqual(
                unbound_digest(), unbound_digest(variant({"services/d": {"layer": "foundation"}}))
            )
            self.assertEqual(
                unbound_digest(),
                unbound_digest(variant({"services/d": {"dependencies": ["services/b"]}})),
            )
            config_scope = replace(
                unbound_scope,
                changed_files=(".guard/config.json", "tests/test_x.py"),
                reviewable_files=(".guard/config.json", "tests/test_x.py"),
            )
            self.assertNotEqual(
                compute_review_contract_digest(
                    policy, config, repo_root=repo, revision=head, scope=config_scope
                ),
                compute_review_contract_digest(
                    policy,
                    variant({"services/d": {"layer": "foundation"}}),
                    repo_root=repo,
                    revision=head,
                    scope=config_scope,
                ),
            )

            # Standalone callers keep the complete-config behavior.
            full_baseline = compute_review_contract_digest(
                policy, config, repo_root=repo, revision=head
            )
            self.assertNotEqual(
                full_baseline,
                compute_review_contract_digest(
                    policy, config, repo_root=repo, revision=unrelated_doc
                ),
            )

    def test_scope_module_resolution_follows_boundary_document_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "docs/owner.md").write_text("Owner contract.\n")
            (repo / "services/a").mkdir(parents=True)
            (repo / "services/a/main.py").write_text("value = 1\n")
            (repo / "services/a/CLAUDE.md").symlink_to("../../docs/owner.md")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "symlinked boundary document")
            head = _git(repo, "rev-parse", "HEAD")
            scope = ReviewScope(
                base_ref="main",
                merge_base=head,
                head_sha=head,
                changed_files=("docs/owner.md",),
                reviewable_files=("docs/owner.md",),
                submodule_changes=(),
                patch_digest="patch",
            )
            config = GuardConfig(topology={"modules": [{"path": "services/a"}]})
            self.assertEqual(
                resolve_scope_modules(repo, scope, config, revision=head),
                ({"services/a"}, set(), set()),
            )

    def test_conversational_incremental_review_resumes_session_and_persists_logs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/catalog").mkdir(parents=True)
            readers_py = repo / "services/catalog/readers.py"
            readers_py.write_text(
                "def read_qualification_obligations():\n    return []\n",
                encoding="utf-8",
            )
            _git(repo, "add", "services/catalog/readers.py")
            _git(repo, "commit", "-m", "round 1 commit with dead shell")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"
            calls_file = tmp / "claude_calls.jsonl"
            (fake_home / ".claude").mkdir(parents=True)

            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, os, sys, subprocess\n"
                "if '--version' in sys.argv:\n"
                "    print('2.0.0')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "prompt = sys.stdin.read()\n"
                "resume_id = args[args.index('--resume') + 1] if '--resume' in args else ''\n"
                "session_id = args[args.index('--session-id') + 1] if '--session-id' in args else (resume_id or 'sid-default')\n"
                "with open(os.environ['CLAUDE_CALLS_FILE'], 'a', encoding='utf-8') as f:\n"
                "    f.write(json.dumps({'args': args, 'resume_id': resume_id, 'session_id': session_id, 'prompt': prompt}) + '\\n')\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session_id}))\n"
                "if not resume_id:\n"
                "    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': {\n"
                "        'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'NO-GO',\n"
                "        'summary': 'dead shell remained',\n"
                "        'blocking_findings': [{\n"
                "            'rule_id': 'guard.single-source-of-truth',\n"
                "            'file': 'services/catalog/readers.py',\n"
                "            'line': 1,\n"
                "            'issue': 'dead shell read_qualification_obligations',\n"
                "            'required_fix': 'remove dead shell function',\n"
                "        }],\n"
                "    }}))\n"
                "else:\n"
                "    print(json.dumps({'type': 'result', 'session_id': session_id, 'structured_output': {\n"
                "        'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'GO',\n"
                "        'summary': os.environ['PRIOR_HEAD'][:10] + ' problems fixed',\n"
                "        'blocking_findings': [],\n"
                "    }}))\n",
            )

            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
                "CLAUDE_CALLS_FILE": str(calls_file),
            }

            r1 = run_architecture_review(repo, env=env)
            self.assertEqual(r1.verdict, "NO-GO")
            self.assertEqual(r1.mode, "full")
            self.assertTrue(r1.session_id)
            self.assertTrue(Path(r1.log_path).is_file())

            saved_session = load_review_session(repo)
            self.assertIsNotNone(saved_session)
            assert saved_session is not None
            self.assertEqual(saved_session.session_id, r1.session_id)
            self.assertEqual(saved_session.round_count, 1)
            self.assertEqual(len(saved_session.last_blocking_findings), 1)
            session_file = next((repo / ".git/the-guard-sessions").glob("*.json"))
            legacy_payload = json.loads(session_file.read_text())
            self.assertEqual(legacy_payload.pop("initial_head_sha"), r1.head_sha)
            session_file.write_text(json.dumps(legacy_payload))
            self.assertEqual(load_review_session(repo).initial_head_sha, r1.head_sha)

            readers_py.write_text(
                "def read_completion_tail():\n    return {'tail': 1}\n",
                encoding="utf-8",
            )
            _git(repo, "add", "services/catalog/readers.py")
            _git(repo, "commit", "-m", "round 2 remove dead shell")

            env["PRIOR_HEAD"] = r1.head_sha
            r2 = run_architecture_review(repo, env=env)
            self.assertEqual(r2.verdict, "GO")
            self.assertEqual(r2.mode, "incremental")
            self.assertEqual(r2.session_id, r1.session_id)
            self.assertIsNotNone(load_valid_receipt(repo, r2.patch_digest))

            calls = [
                json.loads(line)
                for line in calls_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["resume_id"], "")
            self.assertEqual(calls[1]["resume_id"], r1.session_id)
            self.assertIn("Continue round 2", calls[1]["prompt"])
            self.assertIn(
                "dead shell read_qualification_obligations", calls[1]["prompt"]
            )
            self.assertIn("read_completion_tail", calls[1]["prompt"])
            self.assertNotIn("Session Batch Read Plan", calls[1]["prompt"])
            for contract_field in (
                "reviewed_head_sha",
                "prior_unresolved",
                "introduced_by_delta",
            ):
                self.assertIn(contract_field, calls[1]["prompt"])
            self.assertEqual(
                saved_session.last_blocking_findings[0].finding_origin,
                "initial_full_scope",
            )

            log_dir = review_logs_dir(repo, env)
            self.assertTrue((log_dir / "latest.json").is_file())
            self.assertTrue((log_dir / "index.jsonl").is_file())
            index_entries = [
                json.loads(l)
                for l in (log_dir / "index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(index_entries), 2)
            self.assertEqual(index_entries[0]["mode"], "full")
            self.assertEqual(index_entries[1]["mode"], "incremental")

            logger = ReviewRunLogger(
                repo / ".git",
                worktree_path=repo,
                branch="topic",
                provider="claude",
                mode="full",
                merge_base=r2.merge_base,
                head_sha=r2.head_sha,
                patch_digest=r2.patch_digest,
                reviewable_file_count=1,
                prompt="review",
                env=env,
            )
            logger.finish(status="completed")
            # Quiet worktrees retain their evidence despite a much noisier peer.
            for idx in range(120):
                prefix = f"retention-{idx:03d}"
                meta = log_dir / f"{prefix}.meta.json"
                meta.write_text(
                    json.dumps({"worktree": "quiet" if idx < 12 else "noisy"})
                )
                os.utime(meta, (1000 + idx, 1000 + idx))
                for suffix in (".prompt.md", ".events.jsonl"):
                    (log_dir / f"{prefix}{suffix}").write_text("evidence")
            deleted_worktree = str(tmp / "deleted-wt")
            orphan_latest = log_dir / "latest-deleted.json"
            orphan_latest.write_text(json.dumps({"worktree": deleted_worktree}))
            active_latest = log_dir / "latest-active.json"
            active_latest.write_text(json.dumps({"worktree": str(repo)}))
            for idx in range(12):
                meta = log_dir / f"deleted-{idx}.meta.json"
                meta.write_text(json.dumps({"worktree": deleted_worktree}))
                os.utime(meta, (1 + idx, 1 + idx))
            logger._prune_old_runs()
            self.assertFalse(orphan_latest.exists())
            self.assertTrue(active_latest.exists())
            self.assertEqual(list(log_dir.glob("deleted-*.meta.json")), [])
            self.assertEqual(len(list(log_dir.glob("*.meta.json"))), 30)
            for idx in range(12):
                expected = idx >= 8
                for suffix in (".meta.json", ".prompt.md", ".events.jsonl"):
                    self.assertEqual(
                        (log_dir / f"retention-{idx:03d}{suffix}").exists(), expected
                    )

    def test_unverified_session_resume_falls_back_to_full_review_before_issuing_go(
        self,
    ) -> None:
        for failure_mode in ("session", "sha", "missing"):
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                repo, _ = self._init_repo(tmp)
                (repo / "services/catalog").mkdir(parents=True)
                readers_py = repo / "services/catalog/readers.py"
                readers_py.write_text(
                    "def legacy_fn():\n    return []\n", encoding="utf-8"
                )
                _git(repo, "add", "services/catalog/readers.py")
                _git(repo, "commit", "-m", "round 1 commit")

                fake_home = tmp / "home"
                fake_bin = fake_home / ".local/bin"
                cfg_file = fake_home / ".config/the-guard/review-agent.json"
                calls_file = tmp / "agy_calls.jsonl"
                (fake_home / ".gemini/antigravity-cli").mkdir(parents=True)

                _write_exec(
                    fake_bin / "agy",
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys, subprocess\n"
                    "if '--version' in sys.argv:\n"
                    "    print('1.4.2')\n"
                    "    raise SystemExit(0)\n"
                    "args = sys.argv[1:]\n"
                    "prompt = json.loads(sys.stdin.read())['message']['content']\n"
                    "resume_id = args[args.index('--conversation') + 1] if '--conversation' in args else ''\n"
                    "calls_path = os.environ['AGY_CALLS_FILE']\n"
                    "call_idx = len(open(calls_path, encoding='utf-8').read().splitlines()) if os.path.exists(calls_path) else 0\n"
                    "with open(calls_path, 'a', encoding='utf-8') as f:\n"
                    "    f.write(json.dumps({'call_idx': call_idx, 'resume_id': resume_id, 'prompt': prompt}) + '\\n')\n"
                    "if call_idx == 0:\n"
                    "    print(json.dumps({'event': 'init', 'conversation_id': 'conv-round-1'}))\n"
                    "    print(json.dumps({'event': 'result', 'result': {'structured_output': {\n"
                    "        'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'NO-GO',\n"
                    "        'summary': 'legacy shell',\n"
                    "        'blocking_findings': [{\n"
                    "            'rule_id': 'guard.single-source-of-truth',\n"
                    "            'file': 'services/catalog/readers.py',\n"
                    "            'line': 1,\n"
                    "            'issue': 'legacy_fn is dead',\n"
                    "            'required_fix': 'delete legacy_fn',\n"
                    "        }],\n"
                    "    }}}))\n"
                    "elif resume_id:\n"
                    "    mode = os.environ['FAILURE_MODE']\n"
                    "    sid = 'conv-brand-new-unverified' if mode == 'session' else resume_id\n"
                    "    print(json.dumps({'event': 'init', 'conversation_id': sid}))\n"
                    "    payload = {'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'GO', 'summary': 'blind incremental go', 'blocking_findings': []}\n"
                    "    if mode == 'sha':\n"
                    "        payload['reviewed_head_sha'] = os.environ['STALE_HEAD']\n"
                    "    if mode == 'missing':\n"
                    "        del payload['reviewed_head_sha']\n"
                    "    print(json.dumps({'event': 'result', 'result': {'structured_output': payload}}))\n"
                    "else:\n"
                    "    print(json.dumps({'event': 'init', 'conversation_id': 'conv-fresh-full'}))\n"
                    "    print(json.dumps({'event': 'result', 'result': {'structured_output': {\n"
                    "        'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'GO', 'summary': 'verified full review go', 'blocking_findings': []\n"
                    "    }}}))\n",
                )

                env = {
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "HOME": str(fake_home),
                    "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
                    "AGY_CALLS_FILE": str(calls_file),
                }

                r1 = run_architecture_review(repo, env=env)
                self.assertEqual(r1.verdict, "NO-GO")
                self.assertEqual(r1.session_id, "conv-round-1")

                readers_py.write_text(
                    "def active_fn():\n    return {'ok': True}\n", encoding="utf-8"
                )
                _git(repo, "add", "services/catalog/readers.py")
                _git(repo, "commit", "-m", "round 2 replace legacy_fn")

                env["STALE_HEAD"] = r1.head_sha
                env["FAILURE_MODE"] = failure_mode
                r2 = run_architecture_review(repo, env=env)
                self.assertEqual(r2.verdict, "GO")
                self.assertEqual(r2.mode, "full")
                self.assertEqual(r2.session_id, "conv-fresh-full")
                self.assertEqual(r2.summary, "verified full review go")
                self.assertIsNotNone(load_valid_receipt(repo, r2.patch_digest))

                calls = [
                    json.loads(l)
                    for l in calls_file.read_text(encoding="utf-8").splitlines()
                    if l.strip()
                ]
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[1]["resume_id"], "conv-round-1")
                self.assertEqual(calls[2]["resume_id"], "")
                self.assertIn("Full branch scope", calls[2]["prompt"])
                self.assertIn("legacy_fn is dead", calls[2]["prompt"])
                saved = load_review_session(repo)
                self.assertEqual(saved.session_id, "conv-fresh-full")
                self.assertEqual(saved.last_head_sha, r2.head_sha)
                self.assertEqual(saved.initial_head_sha, r1.head_sha)
                self.assertEqual(saved.last_summary, "verified full review go")
                failures = [
                    json.loads(p.read_text())
                    for p in review_logs_dir(repo, env).glob("*.meta.json")
                    if json.loads(p.read_text())["status"] == "failed"
                ]
                self.assertEqual(len(failures), 1)
                expected_error = {
                    "session": "continuation",
                    "sha": "mismatched HEAD",
                    "missing": "requires a non-empty",
                }
                self.assertIn(expected_error[failure_mode], failures[0]["error"])

    def test_session_driven_scope_manifest_folds_tests_and_generates_batch_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            prod_file = repo / "services/catalog/service.py"
            ui_test_file = (
                repo / "services/client/app-ios/SampleUITests/LoginUITests.swift"
            )
            contract_test_file = (
                repo
                / "services/client/app-ios/SampleContractTests/AuthContractTests.swift"
            )
            lock_file = repo / "services/catalog/uv.lock"
            inventory_file = repo / "services/catalog/tests/test_tier_inventory.txt"
            for f in (
                prod_file,
                ui_test_file,
                contract_test_file,
                lock_file,
                inventory_file,
            ):
                f.parent.mkdir(parents=True, exist_ok=True)
            prod_file.write_text(
                "def handle_sync():\n    return 'SECRET_INLINE_DIFF_MARKER'\n",
                encoding="utf-8",
            )
            ui_test_file.write_text("class LoginUITests {}\n", encoding="utf-8")
            contract_test_file.write_text(
                "class AuthContractTests {}\n", encoding="utf-8"
            )
            lock_file.write_text("version = 1\n", encoding="utf-8")
            inventory_file.write_text("tests/unit/test_a.py\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "add production and test/lock files")

            scope = collect_review_scope(repo)
            prompt = build_review_prompt(repo, scope)

            # Raw diff body must NOT be stuffed into Turn 1 prompt
            self.assertNotIn("SECRET_INLINE_DIFF_MARKER", prompt)
            # Production file is listed in core production manifest and batch git diff command
            self.assertIn("services/catalog/service.py", prompt)
            self.assertIn("Session Batch Read Plan", prompt)
            self.assertIn(
                f"git -C {repo} diff {scope.merge_base}..{scope.head_sha} -- ':(literal)services/catalog/service.py'",
                prompt,
            )
            # UITests and ContractTests are grouped into Tier 2 and Test Batch 1/1
            self.assertIn("tests/fixtures/locks 2", prompt)
            self.assertIn("[Test/fixture/lock directory summary and files]", prompt)
            self.assertIn("services/client/: 2 files (+2/-0)", prompt)
            self.assertIn("Test Batch 1/1", prompt)
            # Snapshot isolation instruction is present
            self.assertIn("Git Snapshot Isolation", prompt)
            # Fail-closed when path list does not match git diff or git revision is invalid
            with self.assertRaises(RuntimeError):
                _parse_numstat_entries(
                    repo,
                    scope.merge_base,
                    scope.head_sha,
                    (*scope.reviewable_files, "nonexistent.py"),
                )
            with self.assertRaises(RuntimeError):
                _parse_numstat_entries(
                    repo, "0000000000000000000000000000000000000000", scope.head_sha
                )

    def test_conversational_rebuttal_on_unchanged_head_resumes_session_and_issues_go(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, origin = self._init_repo(tmp)
            (repo / "services/catalog").mkdir(parents=True)
            service_py = repo / "services/catalog/service.py"
            service_py.write_text("def write_turn():\n    return 1\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "round 1 commit")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"
            calls_file = tmp / "claude_rebuttal_calls.jsonl"
            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".claude.json").write_text(
                '{"oauthAccount": {"emailAddress": "dev@example.invalid"}}',
                encoding="utf-8",
            )

            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, os, sys, subprocess\n"
                "if '--version' in sys.argv:\n"
                "    print('2.1.105 (Claude Code)')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "prompt = sys.stdin.read()\n"
                "resume_id = args[args.index('--resume') + 1] if '--resume' in args else ''\n"
                "calls_path = os.environ['CLAUDE_CALLS_FILE']\n"
                "with open(calls_path, 'a', encoding='utf-8') as f:\n"
                "    f.write(json.dumps({'resume_id': resume_id, 'prompt': prompt}) + '\\n')\n"
                "if not resume_id:\n"
                "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-rebuttal-1'}))\n"
                "    print(json.dumps({\n"
                "        'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 'sess-rebuttal-1',\n"
                "        'structured_output': {\n"
                "            'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'NO-GO',\n"
                "            'summary': 'suspected missing migration',\n"
                "            'blocking_findings': [{\n"
                "                'rule_id': 'guard.single-source-of-truth',\n"
                "                'file': 'services/catalog/service.py',\n"
                "                'line': 1,\n"
                "                'issue': 'thought legacy table still existed',\n"
                "                'required_fix': 'drop legacy table',\n"
                "            }],\n"
                "        }\n"
                "    }))\n"
                "else:\n"
                "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': resume_id}))\n"
                "    print(json.dumps({\n"
                "        'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': resume_id,\n"
                "        'structured_output': {\n"
                "            'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'GO',\n"
                "            'summary': 'verified rebuttal evidence, legacy table already dropped in migration 699',\n"
                "            'blocking_findings': [],\n"
                "        }\n"
                "    }))\n",
            )

            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
                "CLAUDE_CALLS_FILE": str(calls_file),
            }

            r1 = run_architecture_review(repo, env=env)
            self.assertEqual(r1.verdict, "NO-GO")
            self.assertEqual(r1.mode, "full")

            # Without changing HEAD, developer submits a conversational rebuttal (--note)
            r2 = run_architecture_review(
                repo,
                env=env,
                reviewer_note="Migration 699 already dropped the legacy table; see migrations/699_drop.sql.",
            )
            self.assertEqual(r2.verdict, "GO")
            self.assertEqual(r2.mode, "incremental")
            self.assertEqual(r2.session_id, "sess-rebuttal-1")
            self.assertIsNotNone(load_valid_receipt(repo, r2.patch_digest))

            calls = [
                json.loads(l)
                for l in calls_file.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]["resume_id"], "sess-rebuttal-1")
            self.assertIn("Evidence-Based Rebuttal Resolution", calls[1]["prompt"])
            self.assertIn("HEAD unchanged", calls[1]["prompt"])
            self.assertIn(
                "Migration 699 already dropped the legacy table", calls[1]["prompt"]
            )

            # Advance origin/main (changing merge_base) and merge with only a non-reviewable doc change
            # (so patch_digest changes, but zero reviewable files changed):
            # Must automatically carry forward the GO receipt without invoking the LLM.
            other_clone = tmp / "other_rebuttal"
            subprocess.run(
                ["git", "clone", str(origin), str(other_clone)],
                check=True,
                capture_output=True,
            )
            _git(other_clone, "checkout", "main")
            _git(other_clone, "config", "user.email", "other@example.invalid")
            _git(other_clone, "config", "user.name", "Other")
            (other_clone / "services/other").mkdir(parents=True)
            (other_clone / "services/other/upstream.py").write_text(
                "UPSTREAM = 1\n", encoding="utf-8"
            )
            _git(other_clone, "add", "services/other/upstream.py")
            _git(other_clone, "commit", "-m", "advance main")
            _git(other_clone, "push", "origin", "main")

            _git(repo, "fetch", "origin", "main")
            _git(repo, "merge", "--no-commit", "--no-ff", "origin/main")
            (repo / "WORKING.md").write_text(
                "# updated working note during merge\n", encoding="utf-8"
            )
            _git(repo, "add", "WORKING.md")
            _git(repo, "commit", "-m", "merge origin/main with doc note")
            merged_scope = collect_review_scope(repo)
            self.assertNotEqual(merged_scope.patch_digest, r2.patch_digest)
            preview_carried = resolve_or_carry_forward_receipt(
                repo, merged_scope, persist=False
            )
            self.assertIsNotNone(preview_carried)
            self.assertIsNone(load_valid_receipt(repo, merged_scope.patch_digest))
            carried = resolve_or_carry_forward_receipt(repo, merged_scope, persist=True)
            self.assertIsNotNone(carried)
            assert carried is not None
            self.assertEqual(carried["mode"], "carried-forward")
            self.assertIsNotNone(load_valid_receipt(repo, merged_scope.patch_digest))
            self.assertEqual(load_review_session(repo).initial_head_sha, r1.head_sha)
            self.assertEqual(
                len(
                    [
                        l
                        for l in calls_file.read_text(encoding="utf-8").splitlines()
                        if l.strip()
                    ]
                ),
                2,
            )

            # Now commit a new code change after merging origin/main and pass --note:
            # Cross-merge-base incremental scope resumes the session for only the modified branch file
            # and must NOT claim "HEAD unchanged".
            service_py.write_text("def write_turn():\n    return 2\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "new code after merge")

            r3 = run_architecture_review(
                repo,
                env=env,
                reviewer_note="Note attached alongside new code after merge.",
            )
            self.assertEqual(r3.mode, "incremental")
            calls_after = [
                json.loads(l)
                for l in calls_file.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(len(calls_after), 3)
            self.assertEqual(calls_after[2]["resume_id"], "sess-rebuttal-1")
            self.assertNotIn("HEAD unchanged", calls_after[2]["prompt"])
            self.assertIn(
                "Incremental files (1): `services/catalog/service.py`",
                calls_after[2]["prompt"],
            )
            self.assertNotIn("services/other/upstream.py", calls_after[2]["prompt"])

            # If session.merge_base is not an ancestor of scope.merge_base (while last_patch_digest still
            # points to a valid receipt r3.patch_digest), carry-forward must reject and run must fall back to full.
            service_py.write_text("def write_turn():\n    return 3\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "new code with non-ancestor session merge_base")
            cur_session = load_review_session(repo)
            assert cur_session is not None
            self.assertEqual(cur_session.initial_head_sha, r1.head_sha)
            save_review_session(
                repo,
                type(cur_session)(
                    **{
                        **cur_session.__dict__,
                        "merge_base": "0" * 40,
                        "last_patch_digest": r3.patch_digest,
                    }
                ),
            )
            scope_r4 = collect_review_scope(repo)
            self.assertIsNone(resolve_or_carry_forward_receipt(repo, scope_r4))
            r4 = run_architecture_review(
                repo,
                env=env,
                force=False,
                reviewer_note="Note with non-ancestor merge_base.",
            )
            self.assertEqual(r4.mode, "full")
            self.assertEqual(load_review_session(repo).initial_head_sha, r4.head_sha)
            calls_final = [
                json.loads(l)
                for l in calls_file.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(len(calls_final), 4)
            self.assertEqual(calls_final[3]["resume_id"], "")
            self.assertIn(
                "Different review baseline: review the full current branch scope",
                calls_final[3]["prompt"],
            )

    def test_carry_forward_fails_closed_on_delta_enumeration_error_and_glob_path_diff_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, origin = self._init_repo(tmp)
            glob_dir = repo / "services/catalog/[slug]"
            glob_dir.mkdir(parents=True)
            glob_file = glob_dir / "handler.py"
            glob_file.write_text(
                "def handle():\n    return 'branch_v1'\n", encoding="utf-8"
            )
            _git(repo, "add", "services/catalog/[slug]/handler.py")
            _git(repo, "commit", "-m", "add glob path handler")

            r1 = run_architecture_review(
                repo,
                reviewer_fn=lambda _p: {
                    "verdict": "GO",
                    "summary": "glob ok",
                    "blocking_findings": [],
                },
            )
            self.assertEqual(r1.verdict, "GO")

            other = tmp / "other_glob"
            subprocess.run(
                ["git", "clone", str(origin), str(other)],
                check=True,
                capture_output=True,
            )
            _git(other, "checkout", "main")
            _git(other, "config", "user.email", "other@example.invalid")
            _git(other, "config", "user.name", "Other")
            (other / "services/other").mkdir(parents=True)
            (other / "services/other/base.py").write_text(
                "BASE = 1\n", encoding="utf-8"
            )
            _git(other, "add", "services/other/base.py")
            _git(other, "commit", "-m", "advance main")
            _git(other, "push", "origin", "main")

            _git(repo, "fetch", "origin", "main")
            _git(repo, "merge", "--no-commit", "--no-ff", "origin/main")
            (repo / "WORKING.md").write_text("# merge note\n", encoding="utf-8")
            _git(repo, "add", "WORKING.md")
            _git(repo, "commit", "-m", "merge main with doc note")

            merged_scope = collect_review_scope(repo)
            session = load_review_session(repo)
            assert session is not None

            # 1) Simulate `git diff --raw` failure during _collect_branch_delta:
            # Must raise RuntimeError in _collect_branch_delta, and resolve_or_carry_forward_receipt
            # must fail closed (return None, no receipt written, check-push exits 2).
            real_git = subprocess.run

            def _failing_raw_diff(
                cmd: list[str], *args: object, **kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                if (
                    len(cmd) >= 6
                    and cmd[:4] == ["git", "-c", "core.fsmonitor=false", "diff"]
                    and "--raw" in cmd
                    and f"{session.merge_base}..{session.last_head_sha}" in cmd
                ):
                    return subprocess.CompletedProcess(
                        cmd, 128, b"", b"fatal: simulated corrupt pack"
                    )
                return real_git(cmd, *args, **kwargs)  # type: ignore[arg-type]

            with mock.patch(
                "the_guard.review.runner.subprocess.run", side_effect=_failing_raw_diff
            ):
                with self.assertRaises(RuntimeError):
                    _collect_branch_delta(repo, merged_scope, session)
                self.assertIsNone(
                    collect_incremental_scope(repo, merged_scope, session)
                )
                self.assertIsNone(resolve_or_carry_forward_receipt(repo, merged_scope))
            self.assertIsNone(load_valid_receipt(repo, merged_scope.patch_digest))

            chk_proc = subprocess.run(
                [sys.executable, str(GATE_CLI), "check-push", "--repo-root", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            # Without the simulated error, check-push succeeds in-memory without writing receipt to disk:
            self.assertEqual(chk_proc.returncode, 0)
            self.assertIn("VERIFIED", chk_proc.stdout)
            self.assertIsNone(load_valid_receipt(repo, merged_scope.patch_digest))

            # 2) Modify the glob-path file after merge so its blob differs from session.last_head_sha:
            # Even if per-file diff is normal or if per-file diff fails, the file must be treated as changed
            # and resolve_or_carry_forward_receipt must refuse to carry forward.
            glob_file.write_text(
                "def handle():\n    return 'branch_v2'\n", encoding="utf-8"
            )
            _git(repo, "add", "services/catalog/[slug]/handler.py")
            _git(repo, "commit", "-m", "modify glob path handler")
            glob_scope = collect_review_scope(repo)
            delta = _collect_branch_delta(repo, glob_scope, session)
            self.assertIsNotNone(delta)
            assert delta is not None
            self.assertIn("services/catalog/[slug]/handler.py", delta.reviewable_files)
            self.assertIsNone(resolve_or_carry_forward_receipt(repo, glob_scope))

            # Even if both per-file diffs returned empty bytes, _is_same_per_file_branch_patch must return False (fail-closed)
            self.assertFalse(
                _is_same_per_file_branch_patch(
                    repo,
                    old_range=f"{session.merge_base}..{session.last_head_sha}",
                    new_range=f"{glob_scope.merge_base}..{glob_scope.head_sha}",
                    path="services/catalog/[slug]/nonexistent.py",
                )
            )

    def test_transient_prompt_too_long_failure_preserves_claude_as_preferred_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/catalog").mkdir(parents=True)
            (repo / "services/catalog/service.py").write_text(
                "x = 1\n", encoding="utf-8"
            )
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "commit")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/the-guard/review-agent.json"
            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".claude.json").write_text(
                '{"oauthAccount": {"emailAddress": "dev@example.invalid"}}',
                encoding="utf-8",
            )
            (fake_home / ".gemini/antigravity-cli").mkdir(parents=True)

            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, sys, subprocess\n"
                "if '--version' in sys.argv:\n"
                "    print('2.1.105 (Claude Code)')\n"
                "    raise SystemExit(0)\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type': 'result', 'is_error': True, 'result': 'Prompt is too long'}))\n"
                "raise SystemExit(1)\n",
            )
            _write_exec(
                fake_bin / "agy",
                "#!/usr/bin/env python3\n"
                "import json, sys, subprocess\n"
                "if '--version' in sys.argv:\n"
                "    print('1.4.2')\n"
                "    raise SystemExit(0)\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'event': 'init', 'conversation_id': 'agy-1'}))\n"
                "print(json.dumps({'event': 'result', 'result': {'structured_output': {\n"
                "    'reviewed_head_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'verdict': 'GO', 'summary': 'fallback ok', 'blocking_findings': []\n"
                "}}}))\n",
            )

            env = {
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(fake_home),
                "THE_GUARD_REVIEW_CONFIG": str(cfg_file),
            }

            probe_environment(repo, env=env, priority_order=["claude", "agy"])
            before = cfg_file.read_bytes()
            result = run_architecture_review(repo, env=env)
            self.assertEqual(result.verdict, "GO")
            self.assertEqual(result.provider, "agy")
            self.assertEqual(cfg_file.read_bytes(), before)

    def test_run_metadata_records_incremental_and_resume_failure_reasons(self) -> None:
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
                "import json, os, subprocess, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('2.0.0')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "sys.stdin.read()\n"
                "if '--resume' in args and os.environ.get('FAIL_RESUME') == '1':\n"
                "    print(json.dumps({'type': 'error', 'message': 'resume transport exploded'}))\n"
                "    raise SystemExit(1)\n"
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

            first = run_architecture_review(repo, env=env)
            self.assertEqual(first.mode, "full")

            service.write_text("value = 2\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "round 2")
            incremental = run_architecture_review(repo, env=env)
            self.assertEqual(incremental.mode, "incremental")
            self.assertEqual(meta_of(incremental)["full_review_reason"], "")

            service.write_text("value = 3\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "round 3")
            fallback = run_architecture_review(repo, env={**env, "FAIL_RESUME": "1"})
            self.assertEqual(fallback.mode, "full")
            self.assertEqual(meta_of(fallback)["full_review_reason"], "resume_failed")
            failed = [
                payload
                for payload in (
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in log_dir.glob("*.meta.json")
                )
                if payload["status"] == "failed"
            ]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["mode"], "incremental")
            self.assertEqual(failed[0]["full_review_reason"], "")
