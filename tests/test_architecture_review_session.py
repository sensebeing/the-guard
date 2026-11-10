"""Architecture review session: incremental scope, rebuttals, and carry-forward.

Contract: incremental sessions bind reviewed scope and patch IDs; invalid continuation never issues GO.
"""

from __future__ import annotations

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
from the_guard.review.session import ReviewRunLogger
from the_guard.review.prompt import _parse_numstat_entries
from the_guard.review.repository import (
    _collect_branch_delta,
    _is_same_per_file_branch_patch,
    load_valid_receipt,
    review_logs_dir,
)
from architecture_review_support import (
    ArchitectureReviewTestCase,
    _git,
    _write_exec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_CLI = REPO_ROOT / "bin/guard"


class ArchitectureReviewSessionTests(ArchitectureReviewTestCase):
    def test_conversational_incremental_review_resumes_session_and_persists_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/catalog").mkdir(parents=True)
            readers_py = repo / "services/catalog/readers.py"
            readers_py.write_text("def read_qualification_obligations():\n    return []\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/readers.py")
            _git(repo, "commit", "-m", "round 1 commit with dead shell")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/sensebeing/arch-review-agent.json"
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
                "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
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
                "            'rule_id': 'peq.code-organization',\n"
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

            readers_py.write_text("def read_completion_tail():\n    return {'tail': 1}\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/readers.py")
            _git(repo, "commit", "-m", "round 2 remove dead shell")

            env["PRIOR_HEAD"] = r1.head_sha
            r2 = run_architecture_review(repo, env=env)
            self.assertEqual(r2.verdict, "GO")
            self.assertEqual(r2.mode, "incremental")
            self.assertEqual(r2.session_id, r1.session_id)
            self.assertIsNotNone(load_valid_receipt(repo, r2.patch_digest))

            calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["resume_id"], "")
            self.assertEqual(calls[1]["resume_id"], r1.session_id)
            self.assertIn("Continue round 2", calls[1]["prompt"])
            self.assertIn("dead shell read_qualification_obligations", calls[1]["prompt"])
            self.assertIn("read_completion_tail", calls[1]["prompt"])
            self.assertNotIn("Session Batch Read Plan", calls[1]["prompt"])
            for contract_field in ("reviewed_head_sha", "prior_unresolved", "introduced_by_delta"):
                self.assertIn(contract_field, calls[1]["prompt"])
            self.assertEqual(saved_session.last_blocking_findings[0].finding_origin, "initial_full_scope")

            log_dir = review_logs_dir(repo, env)
            self.assertTrue((log_dir / "latest.json").is_file())
            self.assertTrue((log_dir / "index.jsonl").is_file())
            index_entries = [json.loads(l) for l in (log_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(index_entries), 2)
            self.assertEqual(index_entries[0]["mode"], "full")
            self.assertEqual(index_entries[1]["mode"], "incremental")

            logger = ReviewRunLogger(
                repo / ".git", worktree_path=repo, branch="topic", provider="claude",
                mode="full", merge_base=r2.merge_base, head_sha=r2.head_sha,
                patch_digest=r2.patch_digest, reviewable_file_count=1, prompt="review", env=env,
            )
            logger.finish(status="completed")
            # Quiet worktrees retain their evidence despite a much noisier peer.
            for idx in range(120):
                prefix = f"retention-{idx:03d}"
                meta = log_dir / f"{prefix}.meta.json"
                meta.write_text(json.dumps({"worktree": "quiet" if idx < 12 else "noisy"}))
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
            self.assertEqual(len(list(log_dir.glob("*.meta.json"))), 90)
            for idx in range(12):
                expected = idx >= 4
                for suffix in (".meta.json", ".prompt.md", ".events.jsonl"):
                    self.assertEqual((log_dir / f"retention-{idx:03d}{suffix}").exists(), expected)

    def test_unverified_session_resume_falls_back_to_full_review_before_issuing_go(self) -> None:
        for failure_mode in ("session", "sha", "missing"):
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                repo, _ = self._init_repo(tmp)
                (repo / "services/catalog").mkdir(parents=True)
                readers_py = repo / "services/catalog/readers.py"
                readers_py.write_text("def legacy_fn():\n    return []\n", encoding="utf-8")
                _git(repo, "add", "services/catalog/readers.py")
                _git(repo, "commit", "-m", "round 1 commit")

                fake_home = tmp / "home"
                fake_bin = fake_home / ".local/bin"
                cfg_file = fake_home / ".config/sensebeing/arch-review-agent.json"
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
                    "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
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
                    "            'rule_id': 'peq.code-organization',\n"
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

                readers_py.write_text("def active_fn():\n    return {'ok': True}\n", encoding="utf-8")
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

                calls = [json.loads(l) for l in calls_file.read_text(encoding="utf-8").splitlines() if l.strip()]
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[1]["resume_id"], "conv-round-1")
                self.assertEqual(calls[2]["resume_id"], "")
                self.assertIn("Full branch scope", calls[2]["prompt"])
                self.assertIn("legacy_fn is dead", calls[2]["prompt"])
                saved = load_review_session(repo)
                self.assertEqual(saved.session_id, "conv-fresh-full")
                self.assertEqual(saved.last_head_sha, r2.head_sha)
                self.assertEqual(saved.last_summary, "verified full review go")
                failures = [
                    json.loads(p.read_text()) for p in review_logs_dir(repo, env).glob("*.meta.json")
                    if json.loads(p.read_text())["status"] == "failed"
                ]
                self.assertEqual(len(failures), 1)
                expected_error = {"session": "continuation", "sha": "mismatched HEAD", "missing": "requires a non-empty"}
                self.assertIn(expected_error[failure_mode], failures[0]["error"])

    def test_session_driven_scope_manifest_folds_tests_and_generates_batch_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            prod_file = repo / "services/catalog/service.py"
            ui_test_file = repo / "services/client/app-ios/SampleUITests/LoginUITests.swift"
            contract_test_file = repo / "services/client/app-ios/SampleContractTests/AuthContractTests.swift"
            lock_file = repo / "services/catalog/uv.lock"
            inventory_file = repo / "services/catalog/tests/test_tier_inventory.txt"
            for f in (prod_file, ui_test_file, contract_test_file, lock_file, inventory_file):
                f.parent.mkdir(parents=True, exist_ok=True)
            prod_file.write_text("def handle_sync():\n    return 'SECRET_INLINE_DIFF_MARKER'\n", encoding="utf-8")
            ui_test_file.write_text("class LoginUITests {}\n", encoding="utf-8")
            contract_test_file.write_text("class AuthContractTests {}\n", encoding="utf-8")
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
            self.assertIn(f"git -C {repo} diff {scope.merge_base}..{scope.head_sha} -- ':(literal)services/catalog/service.py'", prompt)
            # UITests and ContractTests are grouped into Tier 2 and Test Batch 1/1
            self.assertIn("tests/fixtures/locks 2", prompt)
            self.assertIn("[Test/fixture/lock directory summary and files]", prompt)
            self.assertIn("services/client/: 2 files (+2/-0)", prompt)
            self.assertIn("Test Batch 1/1", prompt)
            # Snapshot isolation instruction is present
            self.assertIn("Git Snapshot Isolation", prompt)
            # Fail-closed when path list does not match git diff or git revision is invalid
            with self.assertRaises(RuntimeError):
                _parse_numstat_entries(repo, scope.merge_base, scope.head_sha, (*scope.reviewable_files, "nonexistent.py"))
            with self.assertRaises(RuntimeError):
                _parse_numstat_entries(repo, "0000000000000000000000000000000000000000", scope.head_sha)

    def test_conversational_rebuttal_on_unchanged_head_resumes_session_and_issues_go(self) -> None:
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
            cfg_file = fake_home / ".config/sensebeing/arch-review-agent.json"
            calls_file = tmp / "claude_rebuttal_calls.jsonl"
            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "dev@sensebeing.ai"}}', encoding="utf-8")

            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, os, sys, subprocess\n"
                "if '--version' in sys.argv:\n"
                "    print('2.1.105 (Claude Code)')\n"
                "    raise SystemExit(0)\n"
                "args = sys.argv[1:]\n"
                "prompt = args[args.index('-p') + 1] if '-p' in args else ''\n"
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
                "                'rule_id': 'peq.data-modeling',\n"
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

            calls = [json.loads(l) for l in calls_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]["resume_id"], "sess-rebuttal-1")
            self.assertIn("Evidence-Based Rebuttal Resolution", calls[1]["prompt"])
            self.assertIn("HEAD unchanged", calls[1]["prompt"])
            self.assertIn("Migration 699 already dropped the legacy table", calls[1]["prompt"])

            # Advance origin/main (changing merge_base) and merge with only a non-reviewable doc change
            # (so patch_digest changes, but zero reviewable files changed):
            # Must automatically carry forward the GO receipt without invoking the LLM.
            other_clone = tmp / "other_rebuttal"
            subprocess.run(["git", "clone", str(origin), str(other_clone)], check=True, capture_output=True)
            _git(other_clone, "checkout", "main")
            _git(other_clone, "config", "user.email", "other@example.invalid")
            _git(other_clone, "config", "user.name", "Other")
            (other_clone / "services/other").mkdir(parents=True)
            (other_clone / "services/other/upstream.py").write_text("UPSTREAM = 1\n", encoding="utf-8")
            _git(other_clone, "add", "services/other/upstream.py")
            _git(other_clone, "commit", "-m", "advance main")
            _git(other_clone, "push", "origin", "main")

            _git(repo, "fetch", "origin", "main")
            _git(repo, "merge", "--no-commit", "--no-ff", "origin/main")
            (repo / "WORKING.md").write_text("# updated working note during merge\n", encoding="utf-8")
            _git(repo, "add", "WORKING.md")
            _git(repo, "commit", "-m", "merge origin/main with doc note")
            merged_scope = collect_review_scope(repo)
            self.assertNotEqual(merged_scope.patch_digest, r2.patch_digest)
            preview_carried = resolve_or_carry_forward_receipt(repo, merged_scope, persist=False)
            self.assertIsNotNone(preview_carried)
            self.assertIsNone(load_valid_receipt(repo, merged_scope.patch_digest))
            carried = resolve_or_carry_forward_receipt(repo, merged_scope, persist=True)
            self.assertIsNotNone(carried)
            assert carried is not None
            self.assertEqual(carried["mode"], "carried-forward")
            self.assertIsNotNone(load_valid_receipt(repo, merged_scope.patch_digest))
            self.assertEqual(
                len([l for l in calls_file.read_text(encoding="utf-8").splitlines() if l.strip()]),
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
            calls_after = [json.loads(l) for l in calls_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(calls_after), 3)
            self.assertEqual(calls_after[2]["resume_id"], "sess-rebuttal-1")
            self.assertNotIn("HEAD unchanged", calls_after[2]["prompt"])
            self.assertIn("Incremental files (1): `services/catalog/service.py`", calls_after[2]["prompt"])
            self.assertNotIn("services/other/upstream.py", calls_after[2]["prompt"])

            # If session.merge_base is not an ancestor of scope.merge_base (while last_patch_digest still
            # points to a valid receipt r3.patch_digest), carry-forward must reject and run must fall back to full.
            service_py.write_text("def write_turn():\n    return 3\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "new code with non-ancestor session merge_base")
            cur_session = load_review_session(repo)
            assert cur_session is not None
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
            calls_final = [json.loads(l) for l in calls_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(calls_final), 4)
            self.assertEqual(calls_final[3]["resume_id"], "")
            self.assertIn("Different review baseline: review the full current branch scope", calls_final[3]["prompt"])

    def test_carry_forward_fails_closed_on_delta_enumeration_error_and_glob_path_diff_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, origin = self._init_repo(tmp)
            glob_dir = repo / "services/catalog/[slug]"
            glob_dir.mkdir(parents=True)
            glob_file = glob_dir / "handler.py"
            glob_file.write_text("def handle():\n    return 'branch_v1'\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/[slug]/handler.py")
            _git(repo, "commit", "-m", "add glob path handler")

            r1 = run_architecture_review(
                repo,
                reviewer_fn=lambda _p: {"verdict": "GO", "summary": "glob ok", "blocking_findings": []},
            )
            self.assertEqual(r1.verdict, "GO")

            other = tmp / "other_glob"
            subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
            _git(other, "checkout", "main")
            _git(other, "config", "user.email", "other@example.invalid")
            _git(other, "config", "user.name", "Other")
            (other / "services/other").mkdir(parents=True)
            (other / "services/other/base.py").write_text("BASE = 1\n", encoding="utf-8")
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

            def _failing_raw_diff(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                if len(cmd) >= 3 and cmd[1] == "diff" and "--raw" in cmd and f"{session.merge_base}..{session.last_head_sha}" in cmd:
                    return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: simulated corrupt pack")
                return real_git(cmd, *args, **kwargs)  # type: ignore[arg-type]

            with mock.patch("the_guard.review.runner.subprocess.run", side_effect=_failing_raw_diff):
                with self.assertRaises(RuntimeError):
                    _collect_branch_delta(repo, merged_scope, session)
                self.assertIsNone(collect_incremental_scope(repo, merged_scope, session))
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
            glob_file.write_text("def handle():\n    return 'branch_v2'\n", encoding="utf-8")
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

    def test_transient_prompt_too_long_failure_preserves_claude_as_preferred_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, _ = self._init_repo(tmp)
            (repo / "services/catalog").mkdir(parents=True)
            (repo / "services/catalog/service.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "services/catalog/service.py")
            _git(repo, "commit", "-m", "commit")

            fake_home = tmp / "home"
            fake_bin = fake_home / ".local/bin"
            cfg_file = fake_home / ".config/sensebeing/arch-review-agent.json"
            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "dev@sensebeing.ai"}}', encoding="utf-8")
            (fake_home / ".gemini/antigravity-cli").mkdir(parents=True)

            _write_exec(
                fake_bin / "claude",
                "#!/usr/bin/env python3\n"
                "import json, sys, subprocess\n"
                "if '--version' in sys.argv:\n"
                "    print('2.1.105 (Claude Code)')\n"
                "    raise SystemExit(0)\n"
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

            result = run_architecture_review(repo, env=env)
            self.assertEqual(result.verdict, "GO")
            self.assertEqual(result.provider, "agy")
            saved_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_cfg["preferred_provider"], "claude")
