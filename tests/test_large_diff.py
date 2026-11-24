"""Regression coverage for snapshot review and static checks on mass deletions."""

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from architecture_review_support import ArchitectureReviewTestCase, _git
from the_guard import git_helpers
from the_guard.config import load_config
from the_guard.review.documents import resolve_task_design_blueprints
from the_guard.review.prompt import (
    _render_scope_manifest_section, build_incremental_review_prompt, build_review_prompt,
)
from the_guard.review.engine import run_architecture_review
from the_guard.review.repository import (
    _collect_branch_delta,
    _session_landed_in_merge_base,
    collect_review_scope,
    collect_incremental_scope,
    load_review_session,
)
from the_guard.review.session import ReviewSessionState
from the_guard.topology import check_contract_drift, run_topology_guard
from the_guard.topology.imports import extract_imports


class LargeDiffTests(ArchitectureReviewTestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo, _ = self._init_repo(
            Path(temp.name), review={"blueprint_globs": ["docs/plans/*.md"]}
        )

    def write(self, path, body):
        target = self.repo / path
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_bytes(body)

    def commit(self, message):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", message)
        return _git(self.repo, "rev-parse", "HEAD")

    def record_git(self, commands):
        real_run = subprocess.run

        def record(args, *rest, **kwargs):
            commands.append(args)
            return real_run(args, *rest, **kwargs)

        return patch("subprocess.run", new=record)

    def test_review_prompt_batches_snapshot_reads_and_bounds_git_output(self):
        self.write("service/app.py", b"value = 1\n")
        self.commit("Add a file to review")
        prompt = build_review_prompt(self.repo, collect_review_scope(self.repo))
        batch_plan = prompt.split("## Session Batch Read Plan\n", 1)[1].split("\n## ", 1)[0]

        assert "parallel read-only tool calls in the same turn" in batch_plan
        assert "combine bounded `git show <target-sha>:<path>` or `git grep`" in batch_plan
        assert "Scope `git ls-tree` to touched module directories" in batch_plan
        assert "pass `-n` to `git log`" in batch_plan
        assert "Bound `git diff` by the batch plan or with `-- <paths>`" in batch_plan
        assert "Read every listed production/contract/configuration batch at full depth" in batch_plan

    def test_full_budget_keeps_owner_body_and_indexes_secondary_only_once(self):
        self.write(".guard/config.json", json.dumps({"topology": {"modules": [
            {"path": "service", "role": "owner", "boundary_docs": ["docs/large.md"]},
        ]}}).encode())
        self.write("service/AGENTS.md", b"# Owner\n" + b"A" * 42_000 + b"\nOWNER END\n")
        self.write("docs/large.md", b"# Reference\n" + b"B" * 33_000 + b"\n## Late section\n")
        self.commit("Declare large boundary documents")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.write("service/app.py", b"value = 1\n")
        head = self.commit("Review the owner")
        scope = collect_review_scope(self.repo)
        full = build_review_prompt(self.repo, scope)
        assert "OWNER END" in full
        assert "B" * 100 not in full
        assert "3: ## Late section" in full
        assert f"git show {head}:docs/large.md" in full

        run_architecture_review(self.repo, reviewer_fn=lambda _: {
            "verdict": "GO", "summary": "Reviewed owner", "blocking_findings": [],
        })
        session = load_review_session(self.repo)
        self.write("service/app.py", b"value = 2\n")
        self.commit("Refine the owner")
        scope = collect_review_scope(self.repo)
        delta = collect_incremental_scope(self.repo, scope, session)
        incremental = build_incremental_review_prompt(self.repo, scope, session, delta)
        assert "## Blocking policy" not in incremental
        assert "## Boundary reference" not in incremental
        assert "## Declared topology" not in incremental
        assert "## Session Context Continuity Note" in incremental
        assert len(incremental) < len(full) * 0.2

    def test_snapshot_budget_indexes_documents_after_primary_bodies(self):
        self.write("ARCHITECTURE.md", b"# Architecture\n" + b"P" * 42_000)
        modules = [{"path": f"service{index}", "role": "owner"} for index in range(5)]
        self.write(".guard/config.json", json.dumps({"topology": {"modules": modules}}).encode())
        for module in modules:
            path = module["path"]
            self.write(f"{path}/AGENTS.md", f"# {path}\n".encode() + b"A" * 42_000 + b"\nOWNER END\n")
            self.write(f"{path}/README.md", b"# Reference\n" + b"R" * 20_000)
        self.commit("Declare owners and reference documents")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        for module in modules:
            self.write(f"{module['path']}/app.py", b"value = 1\n")
        head = self.commit("Review all owners")
        prompt = build_review_prompt(self.repo, collect_review_scope(self.repo))
        documents = prompt.split("## Root architecture & agent instructions", 1)[1].split("## Blocking policy", 1)[0]
        assert len(documents) <= 180_000
        assert documents.count("OWNER END") == 3
        assert "P" * 42_000 in documents
        assert f"git show {head}:service4/AGENTS.md" in documents
        assert f"git show {head}:service0/README.md" in documents

    def test_batch_reader_reuses_tree_across_preloads(self):
        reader = git_helpers.BaseFileReader(self.repo, "HEAD", exact=True)
        commands = []
        with self.record_git(commands):
            reader.preload(["README.md", "absent.py"])
            reader.preload(["AGENTS.md"])
            assert reader.read("README.md") == "# base\n"
            assert reader.read("absent.py") is None
            assert "# Root" in reader.read("AGENTS.md")
        assert len([cmd for cmd in commands if "ls-tree" in cmd and "-r" in cmd]) == 1

    def test_identical_snapshot_text_needs_no_line_diff(self):
        body = "def unchanged():\n    return 42\n" * 1_000
        with patch("difflib.SequenceMatcher", side_effect=AssertionError("unchanged file was diffed")):
            assert git_helpers.compute_changed_lines(body, body) == set()

    def test_topology_does_not_read_deleted_baseline_blobs(self):
        deleted = [f"src/removal/{i}.py" for i in range(160)]
        for path in deleted:
            self.write(path, b"value = 1\n")
        self.write("src/live.py", b"value = 2\n")
        base = self.commit("baseline sources")
        for path in deleted:
            (self.repo / path).unlink()
        self.write("src/live.py", b"value = 3\n")
        head = self.commit("delete obsolete sources")
        commands = []
        batches = []
        read_batch = git_helpers.BaseFileReader._batch_contents

        def record_batch(reader, oids):
            batches.extend(oids)
            return read_batch(reader, oids)

        with self.record_git(commands), patch.object(
            git_helpers.BaseFileReader, "_batch_contents", new=record_batch
        ):
            assert run_topology_guard(self.repo, [*deleted, "src/live.py"], base, revision=head) == []
        assert len([cmd for cmd in commands if "ls-tree" in cmd]) < 10
        assert _git(self.repo, "rev-parse", f"{base}:{deleted[0]}") not in batches
        assert _git(self.repo, "rev-parse", f"{base}:src/live.py") in batches

    def test_stream_patch_id_matches_binary_and_empty_reference(self):
        self.write("image.bin", bytes(range(256)))
        self.write("code.py", b"x = 2\n")
        commit = self.commit("binary and text")
        for revision_range in (f"HEAD^..{commit}", f"{commit}..{commit}"):
            args = ["--binary", "--full-index", "--find-renames", revision_range]
            diff = subprocess.check_output(["git", "diff", *args], cwd=self.repo)
            reference = subprocess.check_output(
                ["git", "patch-id", "--stable"], cwd=self.repo, input=diff
            ).decode("ascii").split()
            assert git_helpers.stream_patch_id(self.repo, args) == (
                reference[0] if reference else "0" * 40
            )
        with self.assertRaises(subprocess.CalledProcessError):
            git_helpers.stream_patch_id(self.repo, ["missing-ref..HEAD"])
        assert git_helpers.stream_patch_id(self.repo, ["missing-ref..HEAD"], check=False) == "0" * 40

    def test_submodule_patch_digest_distinguishes_empty_and_unparseable_diff(self):
        origin = self.repo.parent / "dependency"
        origin.mkdir()
        _git(origin, "init", "-b", "main")
        _git(origin, "config", "user.name", "Test")
        _git(origin, "config", "user.email", "test@example.invalid")
        (origin / "data.bin").write_bytes(b"\0before")
        _git(origin, "add", "data.bin")
        _git(origin, "commit", "-m", "submodule baseline")
        path = "vendor/dependency"
        _git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", str(origin), path)
        submodule = self.repo / path
        _git(submodule, "config", "user.name", "Test")
        _git(submodule, "config", "user.email", "test@example.invalid")
        base = self.commit("register submodule")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        old_sub = _git(submodule, "rev-parse", "HEAD")
        driver = self.repo.parent / "diff-driver"
        driver.write_text("#!/bin/sh\nprintf 'opaque external diff\\n'\n")
        driver.chmod(0o755)
        _git(submodule, "config", "diff.external", str(driver))
        for changed in (False, True):
            if changed:
                (submodule / "data.bin").write_bytes(b"\0after")
                _git(submodule, "add", "data.bin")
            _git(submodule, "commit", "--allow-empty", "-m", "new submodule revision")
            new_sub = _git(submodule, "rev-parse", "HEAD")
            head = self.commit("update submodule revision")
            root_patch = git_helpers.stream_patch_id(self.repo, ["--binary", "--full-index", "--find-renames", f"{base}..{head}"])
            sub_patch = "0" * 40 if changed else f"{old_sub}->{new_sub}"
            expected = hashlib.sha256(f"root:{root_patch}\nsub:{path}:{sub_patch}\n".encode()).hexdigest()
            assert collect_review_scope(self.repo).patch_digest == expected

    def test_tree_entries_map_uses_one_query_at_both_sizes(self):
        path = "docs/literal[1].md"
        self.write(path, b"# plan\n")
        self.write("docs/literal1.md", b"# another plan\n")
        self.commit("documents")
        expected = {
            path: ("100644", _git(self.repo, "rev-parse", f"HEAD:{path}")),
            "docs": ("040000", _git(self.repo, "rev-parse", "HEAD:docs")),
        }
        for count in (2, 129):
            commands = []
            with self.record_git(commands):
                assert git_helpers.tree_entries_map(
                    self.repo, [path, "docs", *(f"absent/{i}" for i in range(count - 2))], "HEAD"
                ) == expected
            assert len(commands) == 1
            assert ("-r" in commands[0]) == (count > 128)

    def test_replayed_deletions_share_one_tree_lookup(self):
        paths = [f"src/old/{i}.py" for i in range(160)]
        for path in paths:
            self.write(path, b"value = 1\n")
        base = self.commit("baseline sources")
        _git(self.repo, "branch", "-f", "main", base)
        _git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        for path in paths:
            (self.repo / path).unlink()
        reviewed = collect_review_scope(self.repo, head_ref=self.commit("reviewed mass deletion"))
        session = ReviewSessionState(
            str(self.repo), "topic", base, "codex", "session", reviewed.head_sha,
            reviewed.patch_digest, "GO", "", (), {},
        )
        _git(self.repo, "switch", "main")
        for path in paths:
            (self.repo / path).unlink()
        landed = self.commit("land the same deletion as a different commit")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", landed)
        _git(self.repo, "switch", "-c", "replayed")
        self.write("src/live.py", b"value = 2\n")
        scope = collect_review_scope(self.repo, head_ref=self.commit("add live code"))
        commands = []
        with self.record_git(commands):
            assert _session_landed_in_merge_base(self.repo, scope, session, None)
            delta = _collect_branch_delta(self.repo, scope, session)
        assert delta.changed_files == ("src/live.py",)
        assert len([cmd for cmd in commands if "ls-tree" in cmd]) < 15

    def test_contract_drift_skips_inert_binary_blobs(self):
        self.write(".guard/config.json", b'{"topology": {"enforce_module_dependencies": true, "modules": [{"path": "src"}]}}')
        self.write("src/live.py", b"value = 1\n")
        paths = ["src/clip.MP4", "src/logo.SVG", "src/archive.ZIP", "src/font.WOFF2"]
        for path in paths:
            self.write(path, path.encode() + b"\0")
        base = self.commit("tracked assets")
        for path in paths:
            (self.repo / path).unlink()
        head = self.commit("drop assets")
        batches = []
        read_batch = git_helpers.BaseFileReader._batch_contents

        def record_batch(reader, oids):
            batches.extend(oids)
            return read_batch(reader, oids)

        with patch.object(git_helpers.BaseFileReader, "_batch_contents", new=record_batch):
            assert check_contract_drift(self.repo, paths, base=base, revision=head) == []
        assert not set(batches).intersection(_git(self.repo, "rev-parse", f"{base}:{p}") for p in paths)

    def test_contract_drift_reuses_imports_without_weakening_retirement_checks(self):
        config_path = ".guard/config.json"
        archived = {"path": "services/archived", "retired": True}
        legacy = {"path": "services/legacy", "retired": True}
        self.write(config_path, json.dumps({"topology": {"modules": [archived, legacy]}}).encode())
        self.write("services/archived/index.ts", b"export const value = 1;\n")
        self.write("tools/unchanged.ts", b'import {old} from "services/legacy";\n')
        base = self.commit("retirement declarations")
        (self.repo / "services/archived/index.ts").unlink()
        self.write(config_path, json.dumps({"topology": {"modules": [legacy]}}).encode())
        self.write("tools/new.ts", b'import {value} from "services/legacy";\n')
        head = self.commit("remove isolated retirement and add caller")
        parsed_paths = []

        def record_parse(path, text, roots):
            parsed_paths.append(path)
            return extract_imports(path, text, roots)

        with (
            patch("the_guard.topology.audit.extract_imports", new=record_parse),
            patch("the_guard.topology.configured.extract_imports", new=record_parse),
        ):
            findings = check_contract_drift(self.repo, [config_path], base=base, revision=head)
        assert {finding.path for finding in findings} == {"tools/new.ts"}
        assert parsed_paths.count("tools/unchanged.ts") == 1
        assert parsed_paths.count("tools/new.ts") == 1

    def test_removing_retirement_rechecks_unchanged_callers_when_alias_becomes_unique(self):
        config_path = ".guard/config.json"
        archived = {"path": "packages/legacy", "retired": True}
        legacy = {"path": "services/legacy", "retired": True}
        self.write(config_path, json.dumps({"topology": {"modules": [archived, legacy]}}).encode())
        self.write("tools/unchanged.ts", b'import {value} from "legacy/api";\n')
        base = self.commit("ambiguous alias")
        self.write(config_path, json.dumps({"topology": {"modules": [legacy]}}).encode())
        head = self.commit("remove colliding retirement")
        findings = check_contract_drift(self.repo, [config_path], base=base, revision=head)
        assert {finding.path for finding in findings} == {"tools/unchanged.ts"}

    def test_small_manifest_keeps_exact_text(self):
        base = _git(self.repo, "rev-parse", "HEAD")
        self.write("src/live.py", b"value = 1\n")
        head = self.commit("source change")
        manifest = _render_scope_manifest_section(self.repo, base, head, title="Full branch scope")
        assert manifest == (
            "\n## Full branch scope: 1 changed files (production/contracts 1, tests/fixtures/locks 0)\n"
            "```text\n+1 -0 src/live.py [tier=0]\n```\n### Session Batch Read Plan\n"
            f"Batch 1/1 (+1/-0): `git -C {self.repo} diff {base}..{head} -- ':(literal)src/live.py'`"
        )

    def test_small_omitted_blueprint_index_keeps_exact_json(self):
        paths = [f"docs/plans/task_{i}.md" for i in range(5)]
        for path in paths:
            self.write(path, b"# task\n")
        head = self.commit("add plans")
        prompt = build_review_prompt(self.repo, collect_review_scope(self.repo))
        section = prompt.split("## Additional previously reviewed task blueprints\n", 1)[1]
        inventory = section[section.index("{"):].split("\n## ", 1)[0].rstrip()
        self.assertEqual(inventory, json.dumps({paths[-1]: (head,)}, indent=2))

    def test_bulk_prompt_groups_deletions_keeps_pins_and_bounds_commands(self):
        deleted = [f"docs/plans/retired_{i:03d}.md" for i in range(160)]
        for path in deleted:
            self.write(path, b"# original plan\n")
        pinned = deleted[-1]
        self.write("docs/plans/related.md", b"# unchanged one-hop anchor\n")
        self.write(deleted[0], b"# original plan\nSee docs/plans/related.md\n")
        base = self.commit("committed plans")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        for path in deleted:
            (self.repo / path).unlink()
        nested = "/".join(["d" * 198] * 3)
        added = [f"src/{nested}/{'f' * 200}{i:03d}.py" for i in range(70)]
        for path in added:
            self.write(path, b"value = 42\n")
        head = self.commit("retire plans and add sources")
        scope = collect_review_scope(self.repo)
        session = ReviewSessionState(
            str(self.repo), "topic", base, "codex", "session", base, "previous", "GO", "", (), {},
            task_blueprint_anchors={pinned: (base,)},
        )
        commands = []
        with self.record_git(commands):
            prompt = build_review_prompt(self.repo, scope, prior_session=session)
        items = re.findall(r"`(git -C [^`]+)`", prompt)
        assert items and max(map(len, items)) <= 8_000
        batched_paths = {
            arg.removeprefix(":(literal)") for command in items for arg in shlex.split(command)
            if arg.startswith(":(literal)src/")
        }
        assert batched_paths == set(added)
        assert all(f"+1 -0 {path} [tier=0]" in prompt for path in added)
        assert "docs/plans/: 160 deleted files (-161 lines)" in prompt
        assert "# unchanged one-hop anchor" in prompt
        assert f'"{pinned}": [' in prompt
        assert deleted[80] not in prompt
        inventory = next(cmd for cmd in items if "--diff-filter=D" in cmd and "--name-only" in cmd)
        actual = subprocess.check_output(shlex.split(inventory), text=True).splitlines()
        assert set(actual) == set(deleted)
        assert len([cmd for cmd in commands if "show" in cmd]) < 30
        assert f"{base}..{head}" in prompt

    def test_batched_blueprints_preserve_bytes_and_validate_utf8(self):
        self.write("docs/plans/active.md", b"# line\r\nnext\r")
        base = _git(self.repo, "rev-parse", "HEAD")
        head = self.commit("line endings")
        from_config = git_helpers.read_repository_file(self.repo, "docs/plans/active.md", revision=head)
        config = load_config(self.repo, revision=head)
        kwargs = {"base_sha": base, "config": config}
        result = resolve_task_design_blueprints(self.repo, ["docs/plans/active.md"], head_sha=head, **kwargs)
        assert result.previews[0].head_body == from_config == "# line\r\nnext\r"
        self.write("docs/plans/active.md", b"# invalid\xff")
        head = self.commit("invalid UTF-8")
        with self.assertRaises(ValueError):
            resolve_task_design_blueprints(self.repo, ["docs/plans/active.md"], head_sha=head, **kwargs)
