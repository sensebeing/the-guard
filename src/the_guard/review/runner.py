"""Streaming provider invocation: process supervision, event summarising, and verdict extraction."""

from __future__ import annotations

from .environment import review_environment

import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import subprocess
import sys
import time
from typing import Any
import uuid

from .contract import (
    CLAUDE_DISALLOWED_TOOLS,
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_MAX_RUNAWAY_SECONDS,
    DEFAULT_STALL_TIMEOUT_SECONDS,
    POST_EOF_WAIT_TIMEOUT_SECONDS,
    REVIEW_OUTPUT_SCHEMA,
    ReviewScope,
    ReviewTimeoutError,
    build_agy_readonly_settings,
)
from .repository import current_branch_name, git_common_dir
from .session import BlockingFinding, ReviewRunLogger


def _extract_verdict_json(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if not text:
        raise ValueError("Empty output from reviewer agent")

    def _try_candidate(obj: Any) -> dict[str, Any] | None:
        if isinstance(obj, dict):
            if "verdict" in obj and "summary" in obj and "blocking_findings" in obj:
                return obj
            for key in ("structured_output", "result", "response", "output", "content", "text", "message"):
                if key in obj:
                    nested = _try_candidate(obj[key])
                    if nested is not None:
                        return nested
        elif isinstance(obj, list):
            for item in reversed(obj):
                nested = _try_candidate(item)
                if nested is not None:
                    return nested
        elif isinstance(obj, str):
            s = obj.strip()
            if not s:
                return None
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
            if fenced:
                try:
                    return _try_candidate(json.loads(fenced.group(1)))
                except json.JSONDecodeError:
                    pass
            start, end = s.find("{"), s.rfind("}")
            if start != -1 and end > start:
                try:
                    return _try_candidate(json.loads(s[start : end + 1]))
                except json.JSONDecodeError:
                    pass
        return None

    for candidate_str in (*reversed(text.splitlines()), text):
        candidate_str = candidate_str.strip()
        if not candidate_str:
            continue
        try:
            found = _try_candidate(json.loads(candidate_str))
        except json.JSONDecodeError:
            found = _try_candidate(candidate_str)
        if found is not None:
            return found

    raise ValueError(f"Could not extract structured review JSON from output: {text[:400]}")


def _compact_command(cmd_str: str) -> str:
    compact = re.sub(r"\s+", " ", cmd_str).strip()
    compact = re.sub(r"^git\s+-C\s+\S+\s+", "git ", compact)
    return (compact[:45] + "...") if len(compact) > 48 else compact


def _compact_tool_target(inp: dict[str, Any]) -> str:
    raw_path = str(inp.get("file_path") or inp.get("path") or "").strip()
    short_path = "/".join(PurePosixPath(raw_path).parts[-2:]) if raw_path else ""
    pattern = str(inp.get("pattern") or "").strip()
    if pattern and short_path:
        return f"{pattern[:20]} in {short_path}"[:48]
    if short_path:
        return short_path[:48]
    if "command" in inp:
        return _compact_command(str(inp.get("command") or ""))
    if pattern:
        return pattern[:40]
    return ""


def _summarize_event_dict(ev: dict[str, Any]) -> str | None:
    if "event" in ev:
        ename = str(ev.get("event"))
        if ename == "step_update" and isinstance(ev.get("step_update"), dict):
            su = ev["step_update"]
            stype = su.get("step_type", "")
            tname = su.get("tool_name", "")
            base = f"step:{stype}" + (f":{tname}" if tname else "")
            target = _compact_tool_target(su)
            return f"{base}({target})" if target else base
        return ename

    if "type" in ev:
        etype = str(ev.get("type"))
        if etype == "assistant" and isinstance(ev.get("message"), dict):
            content = ev["message"].get("content")
            if isinstance(content, list):
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tname = str(block.get("name") or "tool")
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        target = _compact_tool_target(inp)
                        return f"{tname}({target})" if target else f"tool:{tname}"
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip():
                        return "drafting-verdict"
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "thinking":
                        return "thinking"
            return "assistant"

        if etype in ("item.started", "item.completed") and isinstance(ev.get("item"), dict):
            item = ev["item"]
            itype = str(item.get("type") or "")
            if itype == "command_execution":
                target = _compact_command(str(item.get("command") or ""))
                return f"Bash({target})" if target else "Bash"
            if itype == "agent_message":
                return "drafting-verdict"
            if itype == "reasoning":
                return "thinking"
            return f"{etype}:{itype}" if itype else etype

        if etype in {
            "user", "attachment", "file-history-snapshot", "last-prompt",
            "mode", "permission-mode", "atis-latch", "rate_limit_event",
        }:
            return None
        if etype == "system":
            sub = str(ev.get("subtype") or "")
            return "init" if sub == "init" else None
        return etype
    return None


def _summarize_stream_line(line: str) -> str | None:
    s = line.strip()
    if not s.startswith("{"):
        return None
    try:
        ev = json.loads(s)
    except json.JSONDecodeError:
        return None
    return _summarize_event_dict(ev) if isinstance(ev, dict) else None


def _extract_session_id_from_stream(stdout_text: str) -> str:
    found = ""
    for line in stdout_text.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
            val = ev.get(key)
            if isinstance(val, str) and val.strip():
                found = val.strip()
                break
    return found


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _register_process_streams(sel: selectors.BaseSelector, proc: subprocess.Popen[bytes], has_stdin: bool) -> None:
    if has_stdin and proc.stdin is not None:
        os.set_blocking(proc.stdin.fileno(), False)
        sel.register(proc.stdin, selectors.EVENT_WRITE, data="stdin")
    if proc.stdout is not None:
        os.set_blocking(proc.stdout.fileno(), False)
        sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    if proc.stderr is not None:
        os.set_blocking(proc.stderr.fileno(), False)
        sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")


def _close_unregistered_stdin(sel: selectors.BaseSelector, key: selectors.SelectorKey, proc: subprocess.Popen[bytes]) -> None:
    sel.unregister(key.fileobj)
    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()


def _pump_stdin_chunk(
    sel: selectors.BaseSelector,
    key: selectors.SelectorKey,
    proc: subprocess.Popen[bytes],
    stdin_buf: memoryview,
    stdin_offset: int,
    provider: str,
) -> tuple[int, bool, str | None]:
    try:
        written = os.write(key.fd, stdin_buf[stdin_offset : stdin_offset + 16384])
    except BlockingIOError:
        return stdin_offset, False, None
    except OSError as exc:
        _close_unregistered_stdin(sel, key, proc)
        return stdin_offset, False, f"{provider} stdin write failed at {stdin_offset}/{len(stdin_buf)} bytes: {exc}"
    if written <= 0:
        _close_unregistered_stdin(sel, key, proc)
        return stdin_offset, False, f"{provider} closed stdin early at {stdin_offset}/{len(stdin_buf)} bytes"
    new_offset = stdin_offset + written
    if new_offset >= len(stdin_buf):
        _close_unregistered_stdin(sel, key, proc)
    return new_offset, True, None


def _drain_stdout_lines(buffer: str) -> tuple[str, int, str | None]:
    added = 0
    last_desc: str | None = None
    while "\n" in buffer:
        line, buffer = buffer.split("\n", 1)
        s = line.strip()
        if s.startswith("{"):
            try:
                ev = json.loads(s)
            except json.JSONDecodeError:
                ev = None
            if isinstance(ev, dict):
                added += 1
                desc = _summarize_event_dict(ev)
                if desc:
                    last_desc = desc
    return buffer, added, last_desc


def _wait_for_exit_bounded(proc: subprocess.Popen[bytes], provider: str) -> int:
    try:
        return proc.wait(timeout=POST_EOF_WAIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_process(proc)
        raise ReviewTimeoutError(
            f"{provider} did not exit within {int(POST_EOF_WAIT_TIMEOUT_SECONDS)}s after closing output streams"
        ) from exc


def _run_streaming_cmd(
    provider: str,
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin_text: str | None = None,
    run_logger: ReviewRunLogger | None = None,
) -> tuple[int, str, str]:
    stall_limit = float(review_environment(env).get("ALTER_ARCH_REVIEW_STALL_SECONDS") or DEFAULT_STALL_TIMEOUT_SECONDS)
    heartbeat_sec = float(review_environment(env).get("ALTER_ARCH_REVIEW_HEARTBEAT_SECONDS") or DEFAULT_HEARTBEAT_SECONDS)
    max_runaway_limit = float(review_environment(env).get("ALTER_ARCH_REVIEW_MAX_SECONDS") or DEFAULT_MAX_RUNAWAY_SECONDS)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    sel = selectors.DefaultSelector()
    stdin_buf = memoryview(stdin_text.encode("utf-8")) if stdin_text is not None else memoryview(b"")
    stdin_offset = 0
    stdin_error: str | None = None
    _register_process_streams(sel, proc, has_stdin=stdin_text is not None)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_line_buf = ""
    start_ts = time.monotonic()
    last_activity_ts = start_ts
    last_heartbeat_ts = start_ts
    event_count = 0
    last_event_desc = "initializing"

    log_suffix = f" (mode={run_logger.mode}, log={run_logger.events_path})" if run_logger is not None else ""
    sys.stderr.write(f"[arch-review][{provider}] started independent architecture review{log_suffix}...\n")
    sys.stderr.flush()

    try:
        while sel.get_map():
            events = sel.select(timeout=1.0)
            now = time.monotonic()
            for key, _ in events:
                if key.data == "stdin":
                    stdin_offset, progressed, err = _pump_stdin_chunk(sel, key, proc, stdin_buf, stdin_offset, provider)
                    if progressed:
                        last_activity_ts = now
                    if err is not None and stdin_error is None:
                        stdin_error = err
                    continue

                try:
                    chunk = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                last_activity_ts = now
                decoded_chunk = chunk.decode("utf-8", errors="replace")
                if key.data == "stdout":
                    stdout_chunks.append(chunk)
                    if run_logger is not None:
                        run_logger.append_stream_chunk("stdout", decoded_chunk)
                    stdout_line_buf += decoded_chunk
                    stdout_line_buf, added, desc = _drain_stdout_lines(stdout_line_buf)
                    event_count += added
                    if desc is not None:
                        last_event_desc = desc
                else:
                    stderr_chunks.append(chunk)
                    if run_logger is not None:
                        run_logger.append_stream_chunk("stderr", decoded_chunk)

            if now - last_heartbeat_ts >= heartbeat_sec:
                elapsed = int(now - start_ts)
                sys.stderr.write(
                    f"[arch-review][{provider}] +{elapsed}s | events={event_count} | last={last_event_desc}\n"
                )
                sys.stderr.flush()
                last_heartbeat_ts = now

            if now - last_activity_ts > stall_limit:
                _terminate_process(proc)
                raise ReviewTimeoutError(f"{provider} stalled with no output for {int(stall_limit)}s")

            if now - start_ts > max_runaway_limit:
                _terminate_process(proc)
                raise ReviewTimeoutError(f"{provider} exceeded runaway safety limit of {int(max_runaway_limit)}s")

        if stdout_line_buf.strip() and stdout_line_buf.strip().startswith("{"):
            try:
                if isinstance(json.loads(stdout_line_buf.strip()), dict):
                    event_count += 1
            except json.JSONDecodeError:
                pass
        if run_logger is not None:
            run_logger.event_count = event_count
        rc = _wait_for_exit_bounded(proc, provider)
    finally:
        sel.close()
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
        _terminate_process(proc)

    stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if stdin_error is not None or stdin_offset < len(stdin_buf):
        reason = stdin_error or f"{provider} closed stdin before receiving full review prompt ({stdin_offset}/{len(stdin_buf)} bytes)"
        detail = (stderr_text or stdout_text).strip()[:200]
        raise RuntimeError(f"{reason} (output: {detail})" if detail else reason)
    return rc, stdout_text, stderr_text


def _build_provider_cmd(
    provider: str,
    binary_path: str,
    prompt: str,
    schema_str: str,
    schema_file: Path | None = None,
    out_file: Path | None = None,
    *,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    model: str = "",
    effort: str = "",
    common_dir: Path | None = None,
) -> list[str]:
    if provider == "agy":
        cmd = [
            binary_path, "-p", prompt,
            "--mode", "plan",
            "--sandbox",
            "--settings", build_agy_readonly_settings(common_dir),
            "--output-format", "stream-json",
            "--json-schema", schema_str,
        ]
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
        if resume_session_id:
            cmd.extend(["--conversation", resume_session_id])
        return cmd
    if provider == "claude":
        cmd = [
            binary_path, "-p", prompt,
            "--permission-mode", "plan",
            "--disallowedTools", CLAUDE_DISALLOWED_TOOLS,
            "--disable-slash-commands",
            "--output-format", "stream-json",
            "--verbose",
            "--json-schema", schema_str,
        ]
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", effort])
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        elif session_id:
            cmd.extend(["--session-id", session_id])
        return cmd
    if provider == "codex":
        options: list[str] = []
        if model:
            options.extend(["-m", model])
        if effort:
            options.extend(["-c", f'model_reasoning_effort="{effort}"'])
        if resume_session_id:
            return [
                binary_path, "exec", "resume",
                "-c", 'sandbox_mode="read-only"',
                "--json",
                "--output-schema", str(schema_file or ""),
                "-o", str(out_file or ""),
                *options,
                resume_session_id,
                "-",
            ]
        return [
            binary_path, "exec",
            "-s", "read-only",
            "--json",
            "--output-schema", str(schema_file or ""),
            "-o", str(out_file or ""),
            *options,
            "-",
        ]
    raise ValueError(f"Unsupported provider: {provider}")


def _verify_session_continuation(provider: str, resume_session_id: str | None, observed_session_id: str) -> None:
    if not resume_session_id:
        return
    if not observed_session_id or observed_session_id != resume_session_id:
        raise RuntimeError(
            f"{provider} did not confirm session continuation "
            f"(requested={resume_session_id}, observed={observed_session_id or 'none'})"
        )


def _is_persistent_provider_failure(exc: Exception) -> bool:
    if isinstance(exc, ReviewTimeoutError):
        return False
    msg = str(exc).lower()
    if "prompt is too long" in msg or "too_few_groups" in msg or "context length" in msg:
        return False
    persistent_markers = (
        "quota exceeded", "insufficient_quota", "auth expired", "authentication failed",
        "not logged in", "login required", "invalid api key", "unauthorized", "account suspended",
    )
    return any(marker in msg for marker in persistent_markers)


def _verify_reviewed_head_sha(verdict_payload: dict[str, Any], expected_head_sha: str) -> None:
    reported = verdict_payload.get("reviewed_head_sha")
    if not isinstance(reported, str) or not reported.strip():
        raise RuntimeError("reviewer output requires a non-empty reviewed_head_sha string")
    reported = reported.strip().lower()
    expected = expected_head_sha.lower()
    if (
        not 7 <= len(reported) <= len(expected)
        or re.fullmatch(r"[0-9a-f]+", reported) is None
        or not expected.startswith(reported)
    ):
        raise RuntimeError(
            f"reviewer audited mismatched HEAD snapshot (expected={expected_head_sha[:12]}, reported={reported[:12]})"
        )


def _build_provider_env(env: dict[str, str]) -> dict[str, str]:
    child_env = {
        **env,
        "ALTER_ARCH_REVIEW_ACTIVE": "1",
        "GUARD_ARCH_REVIEW_ACTIVE": "1",
        "GUARD_WORKTREE_SESSION_MODE": "readonly",
        "ALTER_WORKTREE_SESSION_MODE": "readonly",
    }
    child_env.pop("CLAUDECODE", None)
    return child_env


def _invoke_provider(
    provider: str,
    binary_path: str,
    prompt: str,
    repo_root: Path,
    env: dict[str, str],
    *,
    scope: ReviewScope,
    mode: str = "full",
    resume_session_id: str | None = None,
    prev_head_sha: str | None = None,
    reviewer_note: str = "",
    model: str = "",
    effort: str = "",
) -> tuple[dict[str, Any], str, str]:
    schema_str = json.dumps(REVIEW_OUTPUT_SCHEMA, separators=(",", ":"))
    child_env = _build_provider_env(env)

    common_dir = git_common_dir(repo_root)
    branch = current_branch_name(repo_root)
    run_logger = ReviewRunLogger(
        common_dir,
        worktree_path=repo_root,
        branch=branch,
        provider=provider,
        mode=mode,
        merge_base=scope.merge_base,
        head_sha=scope.head_sha,
        patch_digest=scope.patch_digest,
        reviewable_file_count=len(scope.reviewable_files),
        prompt=prompt,
        resume_session_id=resume_session_id,
        prev_head_sha=prev_head_sha,
        reviewer_note=reviewer_note,
        env=env,
    )
    initial_session_id = str(uuid.uuid4()) if (provider == "claude" and not resume_session_id) else ""

    if provider in ("agy", "claude"):
        cmd = _build_provider_cmd(
            provider, binary_path, prompt, schema_str,
            session_id=initial_session_id or None, resume_session_id=resume_session_id,
            model=model, effort=effort, common_dir=common_dir,
        )
        try:
            rc, out, err = _run_streaming_cmd(provider, cmd, cwd=repo_root, env=child_env, run_logger=run_logger)
            if rc != 0:
                raise RuntimeError(f"{provider} exited {rc}: {(err or out)[:300]}")
            verdict_payload = _extract_verdict_json(out)
            _verify_reviewed_head_sha(verdict_payload, scope.head_sha)
            observed_session_id = _extract_session_id_from_stream(out)
            _verify_session_continuation(provider, resume_session_id, observed_session_id)
            log_path = run_logger.finish(
                status="completed",
                session_id=observed_session_id,
                verdict=str(verdict_payload.get("verdict") or ""),
                summary=str(verdict_payload.get("summary") or ""),
                blocking_findings=_parse_findings(verdict_payload),
            )
            return verdict_payload, observed_session_id, str(log_path)
        except Exception as exc:
            run_logger.finish(status="failed", error=str(exc))
            raise

    if provider == "codex":
        schema_file = common_dir / "arch-review-schema.tmp.json"
        out_file = common_dir / f"arch-review-out-{os.getpid()}.tmp.json"
        schema_file.write_text(schema_str, encoding="utf-8")
        try:
            cmd = _build_provider_cmd(
                provider, binary_path, prompt, schema_str,
                schema_file=schema_file, out_file=out_file, resume_session_id=resume_session_id,
                model=model, effort=effort,
            )
            rc, out, err = _run_streaming_cmd(
                provider, cmd, cwd=repo_root, env=child_env, stdin_text=prompt, run_logger=run_logger
            )
            if rc != 0:
                raise RuntimeError(f"codex exited {rc}: {(err or out)[:300]}")
            raw = out_file.read_text(encoding="utf-8") if out_file.is_file() else out
            verdict_payload = _extract_verdict_json(raw)
            _verify_reviewed_head_sha(verdict_payload, scope.head_sha)
            observed_session_id = _extract_session_id_from_stream(out)
            _verify_session_continuation(provider, resume_session_id, observed_session_id)
            log_path = run_logger.finish(
                status="completed",
                session_id=observed_session_id,
                verdict=str(verdict_payload.get("verdict") or ""),
                summary=str(verdict_payload.get("summary") or ""),
                blocking_findings=_parse_findings(verdict_payload),
            )
            return verdict_payload, observed_session_id, str(log_path)
        except Exception as exc:
            run_logger.finish(status="failed", error=str(exc))
            raise
        finally:
            out_file.unlink(missing_ok=True)

    raise ValueError(f"Unsupported provider: {provider}")


def _parse_findings(payload: dict[str, Any]) -> tuple[BlockingFinding, ...]:
    raw = payload.get("blocking_findings")
    if not isinstance(raw, list):
        raise ValueError("Reviewer blocking_findings must be an array")
    findings = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Every blocking finding must be an object")
        for key in ("rule_id", "file", "issue", "required_fix"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"Blocking finding requires nonempty {key}")
        line = item.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ValueError("Blocking finding line must be a positive integer")
        origin = item.get("finding_origin", "initial_full_scope")
        if origin not in {"initial_full_scope", "prior_unresolved", "introduced_by_delta"}:
            raise ValueError("Invalid finding_origin")
        findings.append(BlockingFinding(
            rule_id=item["rule_id"], file=item["file"], line=line,
            issue=item["issue"], required_fix=item["required_fix"], finding_origin=origin,
        ))
    return tuple(findings)
