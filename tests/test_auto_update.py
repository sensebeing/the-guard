"""Focused contracts for zero-touch self-update, skill symlink repair, and 6h cooldown."""

from __future__ import annotations

import contextlib
import fcntl
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from the_guard import __version__, auto_update
from the_guard.cli import build_parser, main
from the_guard.review import cli as review_cli


class AutoUpdateTests(unittest.TestCase):
    def test_status_reads_legacy_metadata_without_migrating_until_self_update(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home, package, state_metadata, _ = self._make_checkout(Path(root).resolve())
            checkout = package.parents[1]
            (checkout / "dirty").touch()
            legacy = home / ".guard"
            legacy.mkdir()
            metadata = '{"installed_sha":"reviewed"}\n'
            (legacy / "auto_update.json").write_text(metadata, encoding="utf-8")
            for name in ("auto_update.log", "auto_update.lock"):
                (legacy / name).touch()
            override = home / "elsewhere"
            self.assertEqual(auto_update.state_dir_for(home, override, {}), override)
            self.assertEqual(auto_update.state_dir_for(home, env={"THE_GUARD_STATE_DIR": str(override)}), override)
            self.assertTrue(legacy.exists())
            state = auto_update.state_dir_for(home, env={})
            self.assertEqual(state, home / ".config/the-guard")
            status = auto_update.guard_status(version="test", home_dir=home, env={})
            self.assertEqual(status["state_dir"], str(state))
            self.assertEqual(status["auto_update"], json.loads(metadata))
            self.assertEqual((legacy / "auto_update.json").read_text(), metadata)
            self.assertFalse(state_metadata.exists())
            self.assertFalse((state / "auto_update.lock").exists())
            auto_update.self_update(checkout_dir=checkout, home_dir=home, state_dir=override, env={})
            self.assertTrue(legacy.exists())
            with (legacy / "auto_update.lock").open("a+") as old_updater:
                fcntl.flock(old_updater.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = auto_update.self_update(checkout_dir=checkout, home_dir=home, env={})
                self.assertEqual((locked["status"], locked["source_status"]), ("skipped_locked", "skipped_locked"))
                self.assertEqual((legacy / "auto_update.json").read_text(), metadata)
                self.assertTrue((legacy / "auto_update.lock").exists())
                self.assertFalse(state_metadata.exists())
            report = auto_update.self_update(checkout_dir=checkout, home_dir=home, env={})
            self.assertEqual(report["status"], "skipped_dirty_checkout")
            self.assertEqual(report["installed_sha"], "reviewed")
            self.assertTrue(legacy.exists())
            self.assertFalse(state_metadata.exists())

            (checkout / "dirty").unlink()
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "fetch", "origin"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "branch", "--set-upstream-to=origin/main"], check=True, capture_output=True)

            sync_skills = auto_update.sync_global_skills

            def sync_with_lock(*args, **kwargs):
                with (legacy / "auto_update.lock").open("r+") as old_updater:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(old_updater.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual((legacy / "auto_update.json").read_text(), metadata)
                return sync_skills(*args, **kwargs)

            with mock.patch.object(auto_update, "sync_global_skills", side_effect=sync_with_lock):
                report = auto_update.self_update(checkout_dir=checkout, home_dir=home, env={})
            self.assertEqual(report["status"], "up_to_date")
            self.assertEqual(json.loads(state_metadata.read_text()), report)
            self.assertFalse(legacy.exists())
            auto_update.self_update(checkout_dir=checkout, home_dir=home, env={})
            self.assertFalse(legacy.exists())

    def test_background_scheduler_leaves_legacy_state_for_the_child_updater(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            legacy = home / ".guard"
            legacy.mkdir()
            metadata = legacy / "auto_update.json"
            metadata.write_text('{"installed_sha":"legacy","last_checked_at":1}')
            env = {
                "THE_GUARD_BIN": sys.executable,
                "THE_GUARD_AUTO_UPDATE": "1",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            }
            with mock.patch.object(auto_update.subprocess, "Popen") as popen:
                self.assertTrue(auto_update.maybe_trigger_background_auto_update(
                    home_dir=home, env={**env, "GIT_DIR": str(home / "other/.git")},
                ))
            self.assertEqual(popen.call_args.kwargs.get("env"), env)
            self.assertNotIn("--state-dir", popen.call_args.args[0])
            self.assertTrue(metadata.is_file())
            self.assertFalse((home / ".config/the-guard/auto_update.json").exists())

    def test_auto_update_log_rotates_one_backup(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            log = state / "auto_update.log"
            backup = state / "auto_update.log.1"
            log.write_bytes(b"x" * (512 * 1024))
            backup.write_text("old backup", encoding="utf-8")
            auto_update._append_log(state, "new record")
            self.assertEqual(json.loads(log.read_text())["event"], "new record")
            self.assertEqual(backup.read_bytes(), b"x" * (512 * 1024))
            self.assertEqual(sorted(p.name for p in state.glob("auto_update.log*")),
                             ["auto_update.log", "auto_update.log.1"])

    @staticmethod
    def _commit_checkout(checkout: Path, message: str) -> str:
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "-c", "user.name=Test", "-c",
             "user.email=test@example.invalid", "commit", "-m", message],
            check=True,
            capture_output=True,
        )
        return subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
        ).strip()

    def _make_checkout(self, root: Path) -> tuple[Path, Path, Path, str]:
        home = root / "home"
        checkout = home / "the-guard"
        package = checkout / "src/the_guard"
        package.mkdir(parents=True)
        (checkout / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        (package / "__init__.py").touch()
        (package / "cli.py").write_text(
            "import sys\nraise SystemExit(2 if sys.argv[1] in {'review', 'status'} else 3)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
        sha = self._commit_checkout(checkout, "guard checkout")
        state = home / ".config/the-guard"
        state.mkdir(parents=True)
        return home, package, state / "auto_update.json", sha

    @staticmethod
    def _make_stale_install(root: Path, package: Path) -> Path:
        install = root / "consumer/.venv/lib/python3.12/site-packages/the_guard"
        shutil.copytree(package, install, ignore=shutil.ignore_patterns("__pycache__"))
        (install / "cli.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        return install

    def test_direct_review_delegates_and_schedules_background_update(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            consumer = Path(root).resolve()
            home, package, metadata, baseline = self._make_checkout(consumer)
            install = self._make_stale_install(consumer, package)
            (package / "new_module.py").touch()
            self._commit_checkout(home / "the-guard", "advance checkout")
            metadata.write_text(json.dumps({"installed_sha": baseline[:7]}), encoding="utf-8")
            env = {"THE_GUARD_AUTO_UPDATE": "1", "PATH": os.environ["PATH"]}
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(auto_update, "PACKAGE_DIR", install),
                mock.patch.object(auto_update, "maybe_trigger_background_auto_update") as trigger,
            ):
                self.assertEqual(review_cli.main(["--repo-root", str(consumer)]), 2)
            trigger.assert_called_once()
            self.assertEqual(trigger.call_args.kwargs["active_repo_root"], consumer)

    def test_review_schedules_once_for_direct_and_root_entry_points(self) -> None:
        root = Path.cwd()
        argv = ["check-push", f"--repo-root={root}"]
        with (
            mock.patch.dict(os.environ, {"THE_GUARD_REVIEW_ACTIVE": "1"}),
            mock.patch("the_guard.cli.maybe_delegate_to_checkout", return_value=None) as root_delegate,
            mock.patch.object(review_cli, "maybe_delegate_to_checkout", return_value=None) as review_delegate,
            mock.patch("the_guard.cli.maybe_trigger_background_auto_update") as root_trigger,
            mock.patch.object(review_cli, "maybe_trigger_background_auto_update") as review_trigger,
        ):
            self.assertEqual(main(["review", *argv]), 0)
            root_delegate.assert_called_once()
            root_trigger.assert_called_once_with(active_repo_root=root)
            review_delegate.assert_not_called()
            review_trigger.assert_not_called()

            self.assertEqual(review_cli.main(argv), 0)
            review_delegate.assert_called_once_with(["review", *argv], active_repo_root=root)
            review_trigger.assert_called_once_with(active_repo_root=root)

    def test_review_cli_can_run_as_a_module(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "the_guard.review.cli", "--help"],
            env={**os.environ, "THE_GUARD_AUTO_UPDATE": "0", "PYTHONPATH": str(auto_update.PACKAGE_DIR.parent)},
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("usage: guard review run", result.stdout)

    def test_stale_site_packages_delegates_at_globally_installed_sha(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            home, package, metadata, sha = self._make_checkout(root_path)
            install = self._make_stale_install(root_path, package)
            env = {"THE_GUARD_AUTO_UPDATE": "1", "PATH": os.environ["PATH"]}
            with (
                mock.patch.object(auto_update, "PACKAGE_DIR", install),
                mock.patch.object(auto_update, "maybe_trigger_background_auto_update") as trigger,
            ):
                metadata.write_text(json.dumps({"source_sha": sha[:7]}), encoding="utf-8")
                self.assertIsNone(auto_update.maybe_delegate_to_checkout(["status"], home_dir=home, env=env))
                for installed_sha in (sha, sha[:7]):
                    with self.subTest(installed_sha=installed_sha):
                        metadata.write_text(json.dumps({"installed_sha": installed_sha}), encoding="utf-8")
                        self.assertEqual(
                            auto_update.maybe_delegate_to_checkout(["status"], home_dir=home, env=env),
                            2,
                        )
                trigger.reset_mock()
                shutil.copyfile(package / "cli.py", install / "cli.py")
                self.assertIsNone(auto_update.maybe_delegate_to_checkout(["status"], home_dir=home, env=env))
                trigger.assert_not_called()

    def test_activate_newer_checkout_package_reloads_bootstrap_modules_once(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            home, package, metadata, sha = self._make_checkout(root_path)
            install = self._make_stale_install(root_path, package)
            metadata.write_text(json.dumps({"installed_sha": sha[:7]}), encoding="utf-8")
            package_path = [str(install)]
            with (
                mock.patch.dict(os.environ, {"THE_GUARD_AUTO_UPDATE": "1", "PATH": os.environ["PATH"]}, clear=True),
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(auto_update, "PACKAGE_DIR", install),
                mock.patch.object(auto_update, "maybe_trigger_background_auto_update") as trigger,
                mock.patch.object(importlib, "reload", return_value=auto_update) as reload,
            ):
                (package / "untracked.py").touch()
                self.assertFalse(auto_update.maybe_activate_newer_checkout_package(package_path))
                self.assertEqual(package_path, [str(install)])
                trigger.assert_not_called()
                (package / "untracked.py").unlink()
                self.assertTrue(auto_update.maybe_activate_newer_checkout_package(package_path))
                self.assertEqual(package_path, [str(package.resolve()), str(install)])
                self.assertFalse(auto_update.maybe_activate_newer_checkout_package(package_path))
                self.assertEqual(reload.call_args_list, [
                    mock.call(sys.modules["the_guard.git_helpers"]),
                    mock.call(auto_update),
                ])
                trigger.assert_not_called()

    def test_bootstrap_uses_checkout_modules_with_hook_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            home, package, metadata, _ = self._make_checkout(root_path)
            install = root_path / "consumer/.venv/lib/python3.12/site-packages/the_guard"
            shutil.copytree(auto_update.PACKAGE_DIR, install, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(install, package, dirs_exist_ok=True)
            marker = package / "config.py"
            marker.write_text(
                marker.read_text(encoding="utf-8").replace(f'VERSION = "{__version__}"', 'VERSION = "9.8.7"'),
                encoding="utf-8",
            )
            sha = self._commit_checkout(home / "the-guard", "new package version")
            original_metadata = json.dumps({"installed_sha": sha[:7]})
            metadata.write_text(original_metadata, encoding="utf-8")
            unrelated = root_path / "unrelated"
            subprocess.run(["git", "init", str(unrelated)], check=True, capture_output=True)
            result = subprocess.check_output(
                [sys.executable, "-c", "import the_guard; "
                 "from the_guard import auto_update, config, git_helpers, policy; "
                 "print(the_guard.__version__); "
                 "print(*(module.__file__ for module in (auto_update, config, git_helpers, policy)), sep='\\n')"],
                cwd=root_path,
                env={
                    "HOME": str(home), "PYTHONPATH": str(install.parent), "PATH": os.environ["PATH"],
                    "THE_GUARD_BIN": "/usr/bin/true", "GIT_DIR": str(unrelated / ".git"),
                },
                text=True,
            )
            self.assertEqual(result.splitlines(), [
                "9.8.7", *(str(package.resolve() / filename) for filename in (
                    "auto_update.py", "config.py", "git_helpers.py", "policy.py",
                )),
            ])
            self.assertEqual(metadata.read_text(encoding="utf-8"), original_metadata)
            self.assertEqual(list(metadata.parent.iterdir()), [metadata])

    def test_review_import_reloads_checkout_updater_then_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            consumer = Path(root)
            home, package, metadata, _ = self._make_checkout(consumer)
            install = self._make_stale_install(consumer, auto_update.PACKAGE_DIR)
            shutil.copytree(install, package, dirs_exist_ok=True)
            updater = package / "auto_update.py"
            with updater.open("a", encoding="utf-8") as handle:
                handle.write("\nCHECKOUT_ONLY_SYMBOL = 'checkout updater'\n")
            review = package / "review/cli.py"
            with review.open("a", encoding="utf-8") as handle:
                handle.write("\nfrom ..auto_update import CHECKOUT_ONLY_SYMBOL\n")
                handle.write("assert CHECKOUT_ONLY_SYMBOL == 'checkout updater'\n")
            (package / "cli.py").write_text(
                "import os\nimport sys\n"
                "assert os.environ['THE_GUARD_DELEGATED'] == '1'\n"
                "assert sys.argv[1:] == ['review', '--repo-root', '.']\n"
                "print('delegated review')\n",
                encoding="utf-8",
            )
            sha = self._commit_checkout(home / "the-guard", "checkout updater API")
            metadata.write_text(
                json.dumps({"installed_sha": sha[:7], "last_checked_at": time.time()}),
                encoding="utf-8",
            )
            result = subprocess.check_output(
                [sys.executable, "-c", 'from the_guard.review.cli import main; raise SystemExit(main(["--repo-root", "."]))'],
                cwd=consumer,
                env={"HOME": str(home), "PYTHONPATH": str(install.parent), "PATH": os.environ["PATH"],
                     "THE_GUARD_BIN": "/usr/bin/true"},
                text=True,
            )
            self.assertEqual(result.strip(), "delegated review")

    def test_site_packages_uses_vcs_commit_without_global_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            home, package, metadata, installed_sha = self._make_checkout(root_path)
            install = self._make_stale_install(root_path, package)
            direct_url = install.parent / "the_guard-0.1.0.dist-info/direct_url.json"
            direct_url.parent.mkdir()
            direct_url.write_text(
                json.dumps({"vcs_info": {"vcs": "git", "commit_id": installed_sha}}),
                encoding="utf-8",
            )
            env = {"THE_GUARD_AUTO_UPDATE": "1", "PATH": os.environ["PATH"]}
            with (
                mock.patch.object(auto_update, "PACKAGE_DIR", install),
                mock.patch.object(auto_update, "maybe_trigger_background_auto_update") as trigger,
            ):
                metadata.write_text(json.dumps({"installed_sha": installed_sha}), encoding="utf-8")
                self.assertIsNone(auto_update.maybe_delegate_to_checkout(["status"], home_dir=home, env=env))
                trigger.assert_not_called()
                metadata.unlink()
                (package / "new_module.py").touch()
                self._commit_checkout(home / "the-guard", "advance checkout")
                self.assertEqual(auto_update.maybe_delegate_to_checkout(["status"], home_dir=home, env=env), 2)
                trigger.assert_not_called()
                self.assertEqual(auto_update.maybe_delegate_to_checkout(["review"], home_dir=home, env=env), 2)
                trigger.assert_called_once()

    def test_self_update_reinstalls_uv_tool_when_head_already_pulled(self) -> None:
        """A stale installed commit is repaired even when git pull has no new commit."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            home = root_path / "home"
            checkout = home / "the-guard"
            state = root_path / "state"
            gemini = root_path / "gemini"
            (checkout / "src/the_guard").mkdir(parents=True)
            (checkout / "src/the_guard/__init__.py").write_text("__version__ = '1'\n", encoding="utf-8")
            (checkout / "src/the_guard/runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
            (checkout / "pyproject.toml").write_text("[project]\nname='the-guard'\nversion='1'\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"], check=True, capture_output=True)
            baseline = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
            (checkout / "README.md").write_text("newer checkout\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "advance"], check=True, capture_output=True)
            state = root_path / "state"
            state.mkdir()
            (state / "auto_update.json").write_text(json.dumps({"source_sha": baseline}), encoding="utf-8")
            origin = root_path / "origin.git"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(origin)], check=True)
            subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)
            guard_bin = home / ".local/bin/guard"
            guard_bin.parent.mkdir(parents=True)
            guard_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            guard_bin.chmod(0o755)
            uv_log = root_path / "uv.log"
            fake_uv = root_path / "uv"
            fake_uv.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{uv_log}'\n", encoding="utf-8")
            fake_uv.chmod(0o755)
            env = {"PATH": f"{root_path}:{os.environ.get('PATH', '')}"}
            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(auto_update, "PACKAGE_DIR", root_path / "installed/src/the_guard"):
                report = auto_update.self_update(home_dir=home, state_dir=state, gemini_dir=gemini, env=env)
            self.assertEqual(report["cli_status"], "reinstalled")
            self.assertTrue(uv_log.read_text(encoding="utf-8").startswith("tool install --force"))
            self.assertEqual(report["installed_sha"], report["head_sha"])

    def test_maybe_delegate_to_checkout_runs_newer_clean_main_and_skips_dirty_or_delegated(self) -> None:
        """A newer clean checkout delegates once, while recursion and dirty trees are rejected."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            home = root_path / "home"
            checkout = home / "the-guard"
            (checkout / "src/the_guard").mkdir(parents=True)
            (checkout / "src/the_guard/__init__.py").write_text("", encoding="utf-8")
            (checkout / "src/the_guard/cli.py").write_text("print('{\\\"delegated\\\": true}')\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"], check=True, capture_output=True)
            baseline = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
            (checkout / "README.md").write_text("newer checkout\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(checkout), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "advance"], check=True, capture_output=True)
            state = root_path / "state"
            state.mkdir()
            (state / "auto_update.json").write_text(json.dumps({"source_sha": baseline}), encoding="utf-8")
            consumer = root_path / "consumer"
            subprocess.run(["git", "init", str(consumer)], check=True, capture_output=True)
            env = {"THE_GUARD_AUTO_UPDATE": "1", "PATH": os.environ.get("PATH", "")}
            with mock.patch.object(auto_update, "PACKAGE_DIR", root_path / "installed/src/the_guard"), mock.patch.object(auto_update, "maybe_trigger_background_auto_update", return_value=False):
                code = auto_update.maybe_delegate_to_checkout(["status", "--json"], active_repo_root=consumer, home_dir=home, state_dir=state, gemini_dir=root_path / "gemini", env=env)
            self.assertEqual(code, 0)
            self.assertIsNone(auto_update.maybe_delegate_to_checkout(["status"], active_repo_root=consumer, home_dir=home, env={**env, "THE_GUARD_DELEGATED": "1"}))
            (checkout / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            self.assertIsNone(auto_update.maybe_delegate_to_checkout(["status"], active_repo_root=consumer, home_dir=home, env=env))

    def test_repository_identity_ignores_hook_git_dir(self) -> None:
        """A linked worktree must not delegate or update during snapshot-bound self-inspection."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            repo = root_path / "repo"
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            (repo / "marker").write_text("main\n", encoding="utf-8")
            self._commit_checkout(repo, "init")
            linked = root_path / "linked"
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feature", str(linked)], check=True, capture_output=True)
            unrelated = root_path / "unrelated"
            subprocess.run(["git", "init", str(unrelated)], check=True, capture_output=True)
            for hook_env in ({}, {"GIT_DIR": str(unrelated / ".git")}):
                with self.subTest(hook_env=hook_env), mock.patch.dict(os.environ, hook_env):
                    self.assertTrue(auto_update._is_same_git_repo(repo, linked))
                    self.assertFalse(auto_update._is_same_git_repo(repo, unrelated))

    def test_checkout_selection_and_activation_ignore_hook_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            home, package, metadata, baseline = self._make_checkout(root_path)
            install = self._make_stale_install(root_path, package)
            metadata.write_text(json.dumps({"installed_sha": baseline[:7]}), encoding="utf-8")
            unrelated = root_path / "unrelated"
            subprocess.run(["git", "init", "-b", "topic", str(unrelated)], check=True, capture_output=True)
            env = {"THE_GUARD_AUTO_UPDATE": "1", "PATH": os.environ["PATH"]}
            for hook_env in ({}, {"GIT_DIR": str(unrelated / ".git")}):
                package_path = [str(install)]
                with (
                    self.subTest(hook_env=hook_env),
                    mock.patch.dict(os.environ, {**env, **hook_env}, clear=True),
                    mock.patch.object(Path, "home", return_value=home),
                    mock.patch.object(auto_update, "PACKAGE_DIR", install),
                    mock.patch.object(importlib, "reload", return_value=auto_update),
                ):
                    self.assertEqual(auto_update._newer_checkout_package(
                        active_repo_root=unrelated, env=os.environ,
                    ), package)
                    self.assertTrue(auto_update.maybe_activate_newer_checkout_package(package_path))
                    self.assertEqual(package_path, [str(package), str(install)])

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

    def test_cli_only_schedules_background_update_after_check_or_review(self) -> None:
        """Only completed check, check-push, or review commands trigger background auto-update."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            subprocess.run(
                ["git", "init", "-b", "main", str(root_path)],
                check=True,
                capture_output=True,
            )
            with (
                contextlib.redirect_stdout(io.StringIO()),
                mock.patch("the_guard.cli.maybe_trigger_background_auto_update") as trigger,
            ):
                self.assertEqual(
                    main(["init", "--repo-root", str(root_path)]),
                    0,
                )
                self.assertEqual(
                    main(["distill", "--repo-root", str(root_path), "--json"]),
                    0,
                )
                trigger.assert_not_called()
                self.assertEqual(
                    main(["check", "--policy-only", "--repo-root", str(root_path)]),
                    0,
                )
                trigger.assert_called_once_with(active_repo_root=root_path.resolve())

    def test_hook_command_triggers_background_auto_update(self) -> None:
        """Hook-driven workflows schedule the same background update as checks."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            with (
                mock.patch("the_guard.cli.edit_guard", return_value=0),
                mock.patch("the_guard.cli.maybe_trigger_background_auto_update") as trigger,
                mock.patch("sys.stdin", io.StringIO("{}")),
            ):
                self.assertEqual(main(["hook", "edit-guard", "--repo-root", str(root_path)]), 0)
            trigger.assert_called_once_with(active_repo_root=root_path.resolve())


if __name__ == "__main__":
    unittest.main()
