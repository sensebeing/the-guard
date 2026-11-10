"""Architecture review gate: provider/streaming/scope contracts.

Contract: code requires matching patch receipts, readonly providers, complete input and bounded liveness.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from the_guard.review.engine import (
    build_review_prompt,
    collect_review_scope,
    resolve_provider_order,
    run_architecture_review,
)
from the_guard.review.prompt import _render_bounded_diff_section
from the_guard.review.provider import (
    _normalize_priority_order,
    _normalize_provider_models,
    probe_environment,
    record_working_provider,
    resolve_provider_model_config,
)
from the_guard.review.repository import (
    git_common_dir,
    is_reviewable_path,
    load_valid_receipt,
)
from the_guard.review.runner import (
    _build_provider_cmd,
    _build_provider_env,
    _run_streaming_cmd,
    _summarize_stream_line,
    _verify_reviewed_head_sha,
)
from the_guard.policy import PolicyError, load_policy
from architecture_review_support import (
    ArchitectureReviewTestCase,
    _git,
    _write_exec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_CLI = REPO_ROOT / "bin/guard"


class ArchitectureReviewGateTests(ArchitectureReviewTestCase):
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

        agy_resume_cmd = _build_provider_cmd(
            "agy", "/bin/agy", "inc", "{}", resume_session_id="conv-1"
        )
        self.assertEqual(
            agy_resume_cmd[agy_resume_cmd.index("--conversation") + 1], "conv-1"
        )

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

    def test_pure_documentation_diff_is_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self._init_repo(Path(td))
            (repo / "docs").mkdir(exist_ok=True)
            (repo / "docs/design.md").write_text("# Doc only\n", encoding="utf-8")
            (repo / "WORKING.md").write_text("# Working\n", encoding="utf-8")
            _git(repo, "add", "docs/design.md", "WORKING.md")
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
                        {"verdict": "GO", "summary": "ok", "blocking_findings": []},
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
                    {"verdict": "GO", "summary": "ok", "blocking_findings": []},
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
            _verify_reviewed_head_sha({"reviewed_head_sha": reported}, expected)
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
                _verify_reviewed_head_sha({"reviewed_head_sha": reported}, expected)
        with self.assertRaisesRegex(RuntimeError, "requires a non-empty"):
            _verify_reviewed_head_sha({}, expected)
        _verify_reviewed_head_sha(
            {
                "reviewed_head_sha": expected,
                "summary": previous[:10] + " problems fixed",
            },
            expected,
        )
        with self.assertRaisesRegex(RuntimeError, "mismatched HEAD"):
            _verify_reviewed_head_sha(
                {"reviewed_head_sha": previous, "summary": expected}, expected
            )

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
