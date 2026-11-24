"""Saved review receipts and diagnostics shared by both push gates."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from architecture_review_support import ArchitectureReviewTestCase, _git
from the_guard.auto_update import compute_package_tree_digest
from the_guard.config import load_config
from the_guard.hooks import main_push_gate
from the_guard.policy import load_policy
from the_guard.review import session
from the_guard.review.cli import main as review_main
from the_guard.review.contract import ReviewScope
from the_guard.review.engine import run_architecture_review
from the_guard.review.repository import (
    collect_review_scope,
    diagnose_missing_receipt,
    load_valid_receipt,
    save_receipt,
)


class ReceiptDiagnosticsTests(ArchitectureReviewTestCase):
    def test_unreadable_verifier_tree_is_not_runtime_drift(self) -> None:
        scope = ReviewScope(
            base_ref="main", merge_base="a" * 40, head_sha="b" * 40,
            changed_files=("app.py",), reviewable_files=("app.py",),
            submodule_changes=(), patch_digest="c" * 64,
        )
        receipt = {
            "contract_digest": "1" * 64,
            "guard_tree_digest": "2" * 64,
            "guard_sha": "old-sha",
        }
        with (
            patch("the_guard.review.repository.load_valid_receipt", return_value=receipt),
            patch.object(session, "compute_package_tree_digest", side_effect=OSError("unreadable")),
            patch.object(session, "_installed_vcs_commit", return_value="current-sha"),
        ):
            message = diagnose_missing_receipt(Path("."), scope, contract_digest="3" * 64)

        self.assertIn("review contract changed", message)

    def test_push_gates_explain_why_a_receipt_cannot_be_used(self) -> None:
        with TemporaryDirectory() as tmp:
            repo, _ = self._init_repo(Path(tmp), review={"blueprint_globs": ["plan.md"]})
            (repo / "plan.md").write_text("Keep one owner.\n", encoding="utf-8")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            _git(repo, "add", "app.py", "plan.md")
            _git(repo, "commit", "-m", "Add reviewed behavior and blueprint")
            # Retain the blueprint without creating a GO session that could carry receipts forward.
            run_architecture_review(repo, reviewer_fn=lambda _: {
                "verdict": "NO-GO", "summary": "Needs changes", "blocking_findings": [{
                    "rule_id": "guard.single-source-of-truth", "file": "app.py", "line": 1,
                    "issue": "Missing owner", "required_fix": "Assign one owner",
                }],
            })
            scope = collect_review_scope(repo)
            config = load_config(repo, revision=scope.head_sha)
            policy = load_policy(repo, revision=scope.head_sha, config=config)
            contract = session.compute_review_contract_digest(
                policy, config, repo_root=repo, revision=scope.head_sha, scope=scope,
            )
            result = session.ReviewResult(
                verdict="GO", summary="Reviewed", blocking_findings=(), provider="test",
                patch_digest=scope.patch_digest, head_sha=scope.head_sha, merge_base=scope.merge_base,
            )
            receipt_path = save_receipt(
                repo, result, contract_digest=contract, task_blueprint_anchors=scope.task_blueprint_anchors,
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            package_dir = Path(session.__file__).resolve().parents[1]
            self.assertEqual(receipt["guard_package_dir"], str(package_dir))
            self.assertEqual(receipt["guard_tree_digest"], compute_package_tree_digest(package_dir))
            self.assertIsInstance(receipt["guard_sha"], str)
            self.assertTrue(scope.task_blueprint_anchors)

            legacy_runtime = {key: value for key, value in receipt.items() if key != "review_contract_version"}
            legacy = {key: value for key, value in legacy_runtime.items() if not key.startswith("guard_")}
            old_contract = "1" * 64
            old_tree = "2" * 64
            old_path = "/old/the_guard"
            different_sha = ("b" if receipt["guard_sha"].startswith("a") else "a") * 40
            update = f"refs/heads/topic {scope.head_sha} refs/heads/main {scope.merge_base}\n"
            cases = (
                ("legacy accepted", legacy, 0, ()),
                ("same version and contract", {
                    **receipt, "review_contract_version": "v1",
                    "guard_tree_digest": old_tree, "guard_sha": different_sha,
                }, 0, ()),
                ("absent", None, 2, ("missing review receipt for pushed range", scope.patch_digest[:12])),
                ("same version and changed contract", {
                    **receipt, "review_contract_version": "v1", "contract_digest": old_contract,
                    "guard_tree_digest": old_tree, "guard_sha": different_sha,
                }, 2, (
                    "review contract changed", old_contract[:12], contract[:12],
                    "Policy, .guard/config.json, or boundary docs changed. Re-run guard review.",
                )),
                ("explicit version drift", {
                    **receipt, "review_contract_version": "v0", "contract_digest": old_contract,
                    "guard_package_dir": old_path,
                }, 2, (
                    "runtime mismatch", "review_contract_version=v0", "review_contract_version=v1",
                    old_path, str(package_dir), "guard self-update", "guard review",
                )),
                ("legacy runtime tree drift", {
                    **legacy_runtime, "contract_digest": old_contract, "guard_tree_digest": old_tree,
                    "guard_package_dir": old_path,
                }, 2, (
                    "runtime mismatch", old_path, str(package_dir), old_contract[:12],
                    contract[:12], "guard self-update", "guard review",
                )),
                ("legacy runtime SHA drift", {
                    **legacy_runtime, "contract_digest": old_contract, "guard_sha": different_sha,
                }, 2, ("runtime mismatch", different_sha, receipt["guard_sha"], "guard self-update")),
                ("legacy matching runtime", {**legacy_runtime, "contract_digest": old_contract}, 2, (
                    "review contract changed", old_contract[:12], contract[:12], "guard review",
                )),
                ("legacy matching SHA prefix", {
                    **legacy_runtime, "contract_digest": old_contract, "guard_sha": receipt["guard_sha"][:5],
                }, 2, ("review contract changed",)),
                ("legacy contract drift", {**legacy, "contract_digest": old_contract}, 2, (
                    "review contract changed", old_contract[:12], contract[:12], "guard review",
                )),
                ("blueprint drift", {**receipt, "task_blueprint_anchors": {}}, 2, (
                    "blueprint-anchor drift", "guard review",
                )),
                ("head drift", {**receipt, "head_sha": "a" * 40}, 2, (
                    "commit/baseline drift", "guard review",
                )),
                ("baseline drift", {**receipt, "merge_base": "b" * 40}, 2, (
                    "commit/baseline drift", "guard review",
                )),
            )
            for name, payload, exit_code, expected in cases:
                with self.subTest(case=name):
                    if payload is None:
                        receipt_path.unlink()
                    else:
                        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(load_valid_receipt(repo, scope.patch_digest), payload)
                    with redirect_stderr(StringIO()) as hook_stderr:
                        self.assertEqual(main_push_gate(repo, update), exit_code)
                    with redirect_stderr(StringIO()) as cli_stderr, redirect_stdout(StringIO()) as cli_stdout:
                        self.assertEqual(
                            review_main(["check-push", "--repo-root", str(repo)], _from_root_cli=True),
                            exit_code,
                        )
                    self.assertEqual(cli_stderr.getvalue(), hook_stderr.getvalue())
                    for part in expected:
                        self.assertIn(part, cli_stderr.getvalue())
                    if exit_code == 0:
                        self.assertIn("VERIFIED", cli_stdout.getvalue())
                    if "review contract changed" in expected:
                        self.assertNotIn("runtime mismatch", cli_stderr.getvalue())
            self.assertEqual(receipt.get("review_contract_version"), "v1")

    def test_runtime_sha_uses_only_matching_update_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            package = Path(tmp) / "site-packages/the_guard"
            package.mkdir(parents=True)
            digest = compute_package_tree_digest(package)
            status = {
                "installed_tree_digest": "stale", "installed_sha": "stale-install",
                "source_tree_digest": digest, "source_sha": "abc1234",
            }
            with patch.object(session, "__file__", str(package / "review/session.py")), patch(
                "the_guard.review.session.load_auto_update_status", return_value=status,
            ):
                self.assertEqual(session.active_guard_runtime(), {
                    "guard_package_dir": str(package.resolve()),
                    "guard_tree_digest": digest,
                    "guard_sha": "abc1234",
                })
                status["source_tree_digest"] = "stale"
                self.assertEqual(session.active_guard_runtime()["guard_sha"], "")
