"""Review orchestration: scope, cached evidence, provider execution and session continuation."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import load_config
from ..policy import load_policy
from .prompt import (
    _sanitize_reviewer_note,
    build_incremental_review_prompt,
    build_review_prompt,
)
from .provider import (
    record_working_provider,
    resolve_provider_model_config,
    resolve_provider_order,
)
from .repository import (
    _is_unchanged_rebuttal_round,
    collect_incremental_scope,
    collect_review_scope,
    current_branch_name,
    compute_review_contract_digest,
    git_common_dir,
    load_review_session,
    resolve_or_carry_forward_receipt,
    save_receipt,
    save_review_session,
)
from .runner import (
    _invoke_provider,
    _is_persistent_provider_failure,
    _parse_findings,
)
from .session import (
    contract_digest_matches,
    UTC,
    ReviewResult,
    ReviewRunLogger,
    ReviewSessionState,
)


def run_architecture_review(
    repo_root: Path,
    *,
    main_branch: str = "main",
    explicit_provider: str | None = None,
    reprobe: bool = False,
    force: bool = False,
    reviewer_note: str = "",
    env: dict[str, str] | None = None,
    reviewer_fn: Callable[[str], dict[str, Any]] | None = None,
) -> ReviewResult:
    """Run independent architecture review; `reviewer_fn` is a Python-only unit-test seam."""
    eff_env = dict(os.environ if env is None else env)
    eff_note = _sanitize_reviewer_note(reviewer_note)
    scope = collect_review_scope(repo_root, main_branch=main_branch)

    if scope.is_exempt:
        return ReviewResult(
            verdict="GO",
            summary="EXEMPT: documentation and non-code configuration only.",
            blocking_findings=(),
            provider="exempt",
            patch_digest=scope.patch_digest,
            head_sha=scope.head_sha,
            merge_base=scope.merge_base,
            mode="exempt",
        )

    policy = load_policy(repo_root, revision=scope.head_sha)
    contract_config = load_config(repo_root, revision=scope.head_sha)
    current_contract_digest = compute_review_contract_digest(policy, contract_config)
    branch = current_branch_name(repo_root)
    prior_session = None if force else load_review_session(repo_root, branch)
    contract_changed = prior_session is not None and not contract_digest_matches(
        prior_session.contract_digest, policy, contract_config
    )
    if contract_changed:
        prior_session = None

    if not force and not contract_changed:
        existing = resolve_or_carry_forward_receipt(repo_root, scope)
        if existing is not None:
            return ReviewResult(
                verdict="GO",
                summary=str(
                    existing.get("summary")
                    or "Reused verified architecture review receipt."
                ),
                blocking_findings=(),
                provider=f"reused:{existing.get('provider', 'unknown')}",
                patch_digest=scope.patch_digest,
                head_sha=scope.head_sha,
                merge_base=scope.merge_base,
                mode=str(existing.get("mode") or "reused"),
                session_id=str(existing.get("session_id") or ""),
                log_path=str(existing.get("log_path") or ""),
            )

    inc_scope = collect_incremental_scope(repo_root, scope, prior_session)
    prev_head_sha = prior_session.last_head_sha if prior_session is not None else None
    full_prompt_cache: str | None = None

    def _get_full_prompt() -> str:
        nonlocal full_prompt_cache
        if full_prompt_cache is None:
            full_prompt_cache = build_review_prompt(
                repo_root,
                scope,
                prior_session=prior_session,
                incremental_scope=inc_scope,
                reviewer_note=eff_note,
                policy=policy,
            )
        return full_prompt_cache

    can_incremental = prior_session is not None and (
        inc_scope is not None
        or _is_unchanged_rebuttal_round(scope, prior_session, eff_note)
    )
    inc_prompt = (
        build_incremental_review_prompt(
            repo_root,
            scope,
            prior_session,
            inc_scope,
            reviewer_note=eff_note,
            policy=policy,
        )
        if can_incremental and prior_session is not None
        else None
    )

    resolved_session_id = ""
    log_path = ""
    effective_mode = "full"

    if reviewer_fn is not None:
        full_prompt = _get_full_prompt()
        run_logger = ReviewRunLogger(
            git_common_dir(repo_root),
            worktree_path=repo_root,
            branch=branch,
            provider="test-fn",
            mode="full",
            merge_base=scope.merge_base,
            head_sha=scope.head_sha,
            patch_digest=scope.patch_digest,
            reviewable_file_count=len(scope.reviewable_files),
            prompt=full_prompt,
            prev_head_sha=prev_head_sha,
            reviewer_note=eff_note,
            env=eff_env,
        )
        payload = reviewer_fn(full_prompt)
        chosen_provider = "test-fn"
        log_path = str(
            run_logger.finish(
                status="completed",
                session_id="",
                event_count=1,
                verdict=str(payload.get("verdict") or ""),
                summary=str(payload.get("summary") or ""),
                blocking_findings=_parse_findings(payload),
            )
        )
    else:
        candidates, config_payload = resolve_provider_order(
            repo_root, explicit_provider=explicit_provider, reprobe=reprobe, env=eff_env
        )
        if not candidates:
            raise RuntimeError(
                "No available architecture review CLI; check provider installation and credentials."
            )
        errors: list[str] = []
        payload = None
        chosen_provider = ""
        primary_failed_persistently = False
        for idx, (provider, binary_path) in enumerate(candidates):
            model, effort = resolve_provider_model_config(
                provider, config_payload, eff_env
            )
            can_resume = (
                inc_prompt is not None
                and prior_session is not None
                and prior_session.provider == provider
                and bool(prior_session.session_id)
            )
            if can_resume and prior_session is not None and inc_prompt is not None:
                try:
                    payload, resolved_session_id, log_path = _invoke_provider(
                        provider,
                        binary_path,
                        inc_prompt,
                        repo_root,
                        eff_env,
                        scope=scope,
                        mode="incremental",
                        resume_session_id=prior_session.session_id,
                        prev_head_sha=prev_head_sha,
                        reviewer_note=eff_note,
                        model=model,
                        effort=effort,
                    )
                    chosen_provider = provider
                    effective_mode = "incremental"
                    if idx == 0 or primary_failed_persistently:
                        record_working_provider(
                            repo_root, provider, binary_path, eff_env
                        )
                    break
                except Exception as resume_exc:
                    sys.stderr.write(
                        f"[arch-review][{provider}] incremental session resume ({prior_session.session_id}) failed ({resume_exc}); falling back to full session...\n"
                    )
                    sys.stderr.flush()

            try:
                payload, resolved_session_id, log_path = _invoke_provider(
                    provider,
                    binary_path,
                    _get_full_prompt(),
                    repo_root,
                    eff_env,
                    scope=scope,
                    mode="full",
                    prev_head_sha=prev_head_sha,
                    reviewer_note=eff_note,
                    model=model,
                    effort=effort,
                )
                chosen_provider = provider
                effective_mode = "full"
                if idx == 0 or primary_failed_persistently:
                    record_working_provider(repo_root, provider, binary_path, eff_env)
                break
            except Exception as exc:
                if idx == 0 and _is_persistent_provider_failure(exc):
                    primary_failed_persistently = True
                sys.stderr.write(f"[arch-review][{provider}] failed: {exc}\n")
                sys.stderr.flush()
                errors.append(f"{provider}: {exc}")
        if payload is None:
            raise RuntimeError("All review providers failed: " + " | ".join(errors))

    findings = _parse_findings(payload)
    verdict = "NO-GO" if findings or payload.get("verdict") != "GO" else "GO"
    result = ReviewResult(
        verdict=verdict,
        summary=str(payload.get("summary") or ""),
        blocking_findings=findings,
        provider=chosen_provider,
        patch_digest=scope.patch_digest,
        head_sha=scope.head_sha,
        merge_base=scope.merge_base,
        mode=effective_mode,
        session_id=resolved_session_id,
        log_path=log_path,
    )
    save_review_session(
        repo_root,
        ReviewSessionState(
            worktree_path=str(repo_root.resolve()),
            branch=branch,
            merge_base=scope.merge_base,
            provider=chosen_provider,
            session_id=resolved_session_id,
            last_head_sha=scope.head_sha,
            last_patch_digest=scope.patch_digest,
            last_verdict=result.verdict,
            last_summary=result.summary,
            last_blocking_findings=findings,
            last_submodule_shas={
                sub.path: sub.new_sha for sub in scope.submodule_changes
            },
            contract_digest=current_contract_digest,
            round_count=(prior_session.round_count + 1)
            if (prior_session and effective_mode == "incremental")
            else 1,
            updated_at=datetime.now(UTC).isoformat(),
        ),
    )
    if result.verdict == "GO":
        save_receipt(repo_root, result)
    return result
