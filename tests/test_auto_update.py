"""Focused contracts for zero-touch self-update, skill symlink repair, and 6h cooldown."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from the_guard import auto_update
from the_guard.cli import build_parser, main


class AutoUpdateTests(unittest.TestCase):
    def test_parser_exposes_self_update_and_status_contracts(self) -> None:
        """CLI parser exposes checkout, home, state, gemini, quiet, and json flags."""
        parser = build_parser()
        args = parser.parse_args(
            [
                "self-update",
                "--checkout-dir",
                "/tmp/checkout",
                "--home-dir",
                "/tmp/home",
                "--state-dir",
                "/tmp/state",
                "--gemini-dir",
                "/tmp/gemini",
                "--quiet",
                "--json",
            ]
        )
        self.assertEqual(args.checkout_dir, "/tmp/checkout")
        self.assertEqual(args.home_dir, "/tmp/home")
        self.assertEqual(args.state_dir, "/tmp/state")
        self.assertEqual(args.gemini_dir, "/tmp/gemini")
        self.assertTrue(args.quiet)
        self.assertTrue(args.json)

    def test_status_surfaces_persisted_update_without_mutating(self) -> None:
        """guard status reports persisted auto_update.json metadata without modifying it."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state_dir = root_path / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "auto_update.json").write_text(
                '{"status": "up_to_date", "head_sha": "abc1234"}\n',
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["status", "--state-dir", str(state_dir), "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["auto_update"]["status"], "up_to_date")
            self.assertEqual(payload["auto_update"]["head_sha"], "abc1234")

    def test_self_update_distinguishes_same_head(self) -> None:
        """A clean main checkout with no incoming commits reports up_to_date."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            checkout = root_path / "checkout"
            subprocess.run(
                ["git", "init", "-b", "main", str(checkout)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "config", "user.name", "Test"],
                check=True,
            )
            (checkout / "README.md").write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(checkout), "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            upstream = root_path / "upstream.git"
            subprocess.run(
                ["git", "init", "--bare", str(upstream)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "add", "origin", str(upstream)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "push", "-u", "origin", "main"],
                check=True,
                capture_output=True,
            )
            state_dir = root_path / "state"
            report = auto_update.self_update(
                checkout_dir=checkout,
                home_dir=root_path / "home",
                state_dir=state_dir,
                gemini_dir=root_path / "gemini",
            )
            self.assertEqual(report["status"], "up_to_date")
            self.assertEqual(report["source_status"], "up_to_date")
            persisted = json.loads(
                (state_dir / "auto_update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "up_to_date")
            log_line = (
                (state_dir / "auto_update.log")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(json.loads(log_line)["event"], "auto_update_completed")

    def test_self_update_skips_dirty_and_non_main_checkouts(self) -> None:
        """Dirty worktrees and non-main branches are skipped without pulling or clobbering edits."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            checkout = root_path / "checkout"
            subprocess.run(
                ["git", "init", "-b", "main", str(checkout)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "config", "user.name", "Test"],
                check=True,
            )
            (checkout / "README.md").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(checkout), "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            (checkout / "README.md").write_text("dirty\n", encoding="utf-8")
            dirty_report = auto_update.self_update(
                checkout_dir=checkout,
                home_dir=root_path / "home",
                state_dir=root_path / "state1",
                gemini_dir=root_path / "gemini",
            )
            self.assertEqual(dirty_report["source_status"], "skipped_dirty_checkout")
            self.assertEqual(dirty_report["status"], "skipped_dirty_checkout")
            self.assertEqual(
                (checkout / "README.md").read_text(encoding="utf-8"), "dirty\n"
            )

            subprocess.run(
                ["git", "-C", str(checkout), "restore", "README.md"], check=True
            )
            subprocess.run(
                ["git", "-C", str(checkout), "checkout", "-b", "topic"],
                check=True,
                capture_output=True,
            )
            branch_report = auto_update.self_update(
                checkout_dir=checkout,
                home_dir=root_path / "home",
                state_dir=root_path / "state2",
                gemini_dir=root_path / "gemini",
            )
            self.assertEqual(branch_report["source_status"], "skipped_non_main_branch")
            self.assertEqual(branch_report["status"], "skipped_non_main_branch")

    def test_self_update_pulls_new_commit_syncs_skills_and_preserves_unmanaged_dirs(
        self,
    ) -> None:
        """Advancing HEAD marks updated, repairs global skill symlinks, and preserves unmanaged dirs."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            upstream = root_path / "upstream.git"
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(upstream)],
                check=True,
                capture_output=True,
            )
            producer = root_path / "producer"
            subprocess.run(
                ["git", "init", "-b", "main", str(producer)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(producer), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(producer), "config", "user.name", "Test"],
                check=True,
            )
            for name in ("the-guard", "the-guard-setup"):
                skill_dir = producer / "skills" / name
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(producer), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(producer), "commit", "-m", "v1"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(producer), "remote", "add", "origin", str(upstream)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(producer), "push", "-u", "origin", "main"],
                check=True,
                capture_output=True,
            )

            checkout = root_path / "checkout"
            subprocess.run(
                ["git", "clone", str(upstream), str(checkout)],
                check=True,
                capture_output=True,
            )
            (producer / "skills/the-guard/SKILL.md").write_text(
                "# the-guard v2\n", encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(producer), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(producer), "commit", "-m", "v2"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(producer), "push", "origin", "main"],
                check=True,
                capture_output=True,
            )
            expected_sha = subprocess.run(
                ["git", "-C", str(producer), "rev-parse", "--short", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            home = root_path / "home"
            gemini = root_path / "gemini"
            (home / ".agents/skills").mkdir(parents=True)
            (gemini / "config/skills").mkdir(parents=True)
            unmanaged = home / ".claude/skills/the-guard"
            unmanaged.mkdir(parents=True)
            (unmanaged / "custom.txt").write_text("keep", encoding="utf-8")

            report = auto_update.self_update(
                checkout_dir=checkout,
                home_dir=home,
                state_dir=root_path / "state",
                gemini_dir=gemini,
            )
            self.assertEqual(report["status"], "updated")
            self.assertEqual(report["source_status"], "updated")
            self.assertEqual(report["head_sha"], expected_sha)
            self.assertTrue((home / ".agents/skills/the-guard").is_symlink())
            self.assertTrue((home / ".agents/skills/the-guard-setup").is_symlink())
            self.assertTrue((gemini / "config/skills/the-guard").is_symlink())
            self.assertFalse(unmanaged.is_symlink())
            self.assertEqual(
                (unmanaged / "custom.txt").read_text(encoding="utf-8"), "keep"
            )

    def test_invalid_explicit_checkout_fails_closed(self) -> None:
        """An explicit non-Git --checkout-dir fails closed and persists status=failed."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state_dir = root_path / "state"
            with self.assertRaises(RuntimeError):
                auto_update.self_update(
                    checkout_dir=root_path / "missing",
                    home_dir=root_path / "home",
                    state_dir=state_dir,
                    gemini_dir=root_path / "gemini",
                )
            persisted = json.loads(
                (state_dir / "auto_update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "failed")
            self.assertIn("not a Git repository", persisted["error"])
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "self-update",
                        "--checkout-dir",
                        str(root_path / "missing"),
                        "--state-dir",
                        str(state_dir),
                    ]
                )
            self.assertEqual(code, 3)

    def test_no_source_is_reported_explicitly(self) -> None:
        """Without a local checkout or uv, status and source_status are skipped_no_source."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            with (
                mock.patch.object(
                    auto_update, "_discover_checkout", return_value=(None, False)
                ),
                mock.patch.object(auto_update.shutil, "which", return_value=None),
            ):
                report = auto_update.self_update(
                    home_dir=root_path / "home",
                    state_dir=root_path / "state",
                    gemini_dir=root_path / "gemini",
                )
            self.assertEqual(report["source_status"], "skipped_no_source")
            self.assertEqual(report["status"], "skipped_no_source")

    def test_concurrent_lock_returns_skipped_locked_immediately(self) -> None:
        """When auto_update.lock is held, self_update returns skipped_locked without blocking."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state_dir = root_path / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "auto_update.json").write_text(
                '{"status": "up_to_date", "head_sha": "1112223"}\n',
                encoding="utf-8",
            )
            lock_path = state_dir / "auto_update.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    report = auto_update.self_update(
                        home_dir=root_path / "home",
                        state_dir=state_dir,
                        gemini_dir=root_path / "gemini",
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            self.assertEqual(report["status"], "skipped_locked")
            self.assertEqual(report["source_status"], "skipped_locked")
            persisted = json.loads(
                (state_dir / "auto_update.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["status"], "up_to_date")

    def test_cooldown_suppresses_within_ttl_and_spawns_when_expired(self) -> None:
        """The 6-hour cooldown prevents duplicate spawns and triggers Popen once expired."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state_dir = root_path / "state"
            state_dir.mkdir(parents=True)
            now = time.time()
            (state_dir / "auto_update.json").write_text(
                json.dumps({"last_checked_at": now}), encoding="utf-8"
            )
            env = {"THE_GUARD_BIN": "/usr/bin/true", "THE_GUARD_AUTO_UPDATE": "1"}
            with mock.patch.object(auto_update.subprocess, "Popen") as popen:
                triggered = auto_update.maybe_trigger_background_auto_update(
                    home_dir=root_path / "home",
                    state_dir=state_dir,
                    gemini_dir=root_path / "gemini",
                    env=env,
                )
                self.assertFalse(triggered)
                popen.assert_not_called()

            expired = now - auto_update.AUTO_UPDATE_INTERVAL_SECONDS - 5
            (state_dir / "auto_update.json").write_text(
                json.dumps({"last_checked_at": expired}), encoding="utf-8"
            )
            with mock.patch.object(auto_update.subprocess, "Popen") as popen:
                triggered = auto_update.maybe_trigger_background_auto_update(
                    home_dir=root_path / "home",
                    state_dir=state_dir,
                    gemini_dir=root_path / "gemini",
                    env=env,
                )
                self.assertTrue(triggered)
                popen.assert_called_once()
                _, kwargs = popen.call_args
                self.assertTrue(kwargs["start_new_session"])
            updated = json.loads(
                (state_dir / "auto_update.json").read_text(encoding="utf-8")
            )
            self.assertGreater(updated["last_checked_at"], expired)

    def test_spawn_failure_does_not_advance_cooldown(self) -> None:
        """A failed Popen records status=failed and error without advancing last_checked_at."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state_dir = root_path / "state"
            state_dir.mkdir(parents=True)
            old = time.time() - auto_update.AUTO_UPDATE_INTERVAL_SECONDS - 10
            (state_dir / "auto_update.json").write_text(
                json.dumps({"last_checked_at": old}), encoding="utf-8"
            )
            env = {"THE_GUARD_BIN": sys.executable, "THE_GUARD_AUTO_UPDATE": "1"}
            with mock.patch.object(
                auto_update.subprocess, "Popen", side_effect=OSError("spawn failed")
            ):
                triggered = auto_update.maybe_trigger_background_auto_update(
                    home_dir=root_path / "home",
                    state_dir=state_dir,
                    gemini_dir=root_path / "gemini",
                    env=env,
                )
                self.assertFalse(triggered)
                record = json.loads(
                    (state_dir / "auto_update.json").read_text(encoding="utf-8")
                )
                self.assertEqual(record["last_checked_at"], old)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"], "spawn failed")
            log_text = (state_dir / "auto_update.log").read_text(encoding="utf-8")
            self.assertIn("auto_update_spawn_failed", log_text)

    def test_background_trigger_skips_when_active_repo_is_guard_checkout(self) -> None:
        """Background auto-update does not mutate the-guard checkout while it is the active repo."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            state_dir = root_path / "state"
            state_dir.mkdir(parents=True)
            checkout = auto_update.PACKAGE_DIR.parents[1]
            env = {"THE_GUARD_BIN": "/usr/bin/true", "THE_GUARD_AUTO_UPDATE": "1"}
            with mock.patch.object(auto_update.subprocess, "Popen") as popen:
                triggered = auto_update.maybe_trigger_background_auto_update(
                    active_repo_root=checkout,
                    home_dir=root_path / "home",
                    state_dir=state_dir,
                    gemini_dir=root_path / "gemini",
                    env=env,
                )
                self.assertFalse(triggered)
                popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
