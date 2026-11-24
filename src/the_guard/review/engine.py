"""Review orchestration: scope, cached evidence, provider execution and session continuation."""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any

from ..config import GuardConfig, load_config
from ..git_helpers import clean_git_env
from ..policy import Policy, load_policy
from .contract import ReviewOutput, ReviewScope, validate_review_output
from .documents import resolve_module_neighbours, resolve_scope_modules
from .prompt import (
    _PriorModuleContextUnavailable,
    _sanitize_reviewer_note,
    _snapshot_context,
    build_incremental_review_prompt,
    build_review_prompt,
)
from .provider import (
    record_working_provider,
    resolve_provider_model_config,
    resolve_provider_order,
)
from .repository import (
    _git,
    _is_unchanged_rebuttal_round,
    collect_incremental_scope,
    collect_review_scope,
    contract_change_causes,
    current_branch_name,
    compute_review_contract_digest,
    git_common_dir,
    load_review_session,
    previous_review_scope,
    resolve_or_carry_forward_receipt,
    review_anchor_session,
    save_receipt,
    save_review_session,
)
from .runner import (
    _invoke_provider,
    _is_persistent_provider_failure,
    _is_transient_provider_failure,
)
from .session import (
    UTC,
    ReviewResult,
    ReviewRunLogger,
    ReviewSessionState,
    _scoped_config_payload,
    compute_session_contract_digest,
    config_changes_outside_modules,
)


@dataclass(frozen=True)
class _ProviderReviewPlan:
    """One provider attempt's mode decision with everything its invocation needs."""

    mode: str
    full_review_reason: str
    incremental_prompt: str = ""
    resume_session_id: str = ""


def _provider_review_plan(
    *,
    force: bool,
    stored_session: ReviewSessionState | None,
    prior_session: ReviewSessionState | None,
    inc_prompt: str | None,
    provider: str,
    resume_failed: bool = False,
) -> _ProviderReviewPlan:
    """Decide one provider's mode and own the full-review-reason vocabulary.

    ``mode == "incremental"`` exactly when the stored session can resume for this
    provider and an incremental prompt was built; that plan records an empty
    ``full_review_reason``. Every other plan is a full run whose objective cause is
    ``force``, ``no_prior_session``, ``contract_changed``, ``no_incremental_delta``,
    ``provider_changed``, ``missing_session_id`` or ``resume_failed``.
    """
    if resume_failed:
        return _ProviderReviewPlan("full", "resume_failed")
    if force:
        return _ProviderReviewPlan("full", "force")
    if stored_session is None:
        return _ProviderReviewPlan("full", "no_prior_session")
    if prior_session is None:
        return _ProviderReviewPlan("full", "contract_changed")
    if inc_prompt is None:
        return _ProviderReviewPlan("full", "no_incremental_delta")
    if prior_session.provider != provider:
        return _ProviderReviewPlan("full", "provider_changed")
    if not prior_session.session_id:
        return _ProviderReviewPlan("full", "missing_session_id")
    return _ProviderReviewPlan(
        "incremental",
        "",
        incremental_prompt=inc_prompt,
        resume_session_id=prior_session.session_id,
    )


def _session_contract_changed(
    repo_root: Path,
    scope: ReviewScope,
    config: GuardConfig,
    prior_session: ReviewSessionState | None,
    *,
    session_contract_digest: str,
    receipt_contract_changed: bool,
) -> bool:
    if prior_session is None:
        return False
    if not prior_session.session_contract_digest:
        return receipt_contract_changed
    if prior_session.session_contract_digest != session_contract_digest:
        return True
    changed_branch_config = False
    if ".guard/config.json" in scope.changed_files or (
        receipt_contract_changed and prior_session.merge_base == scope.merge_base
    ):
        config_diff = _git(
            repo_root, "diff", "--quiet", "--no-ext-diff", "--no-textconv",
            f"{prior_session.last_head_sha}..{scope.head_sha}", "--", ".guard/config.json",
            check=False,
        )
        if config_diff.returncode not in (0, 1):
            return True
        changed_branch_config = config_diff.returncode == 1
    if not changed_branch_config and (
        prior_session.merge_base == scope.merge_base or not receipt_contract_changed
    ):
        return False

    try:
        if changed_branch_config:
            prior_scope, prior_config = previous_review_scope(repo_root, prior_session)
            prior_touched, _, _ = resolve_scope_modules(repo_root, prior_scope, prior_config)
        else:
            prior_config = load_config(
                repo_root, revision=prior_session.last_head_sha, allow_historical=True
            )
    except (OSError, RuntimeError, ValueError, CalledProcessError):
        # An unavailable historical declaration cannot establish safe continuation.
        return True
    touched, upstream, downstream = resolve_scope_modules(
        repo_root, scope, config, revision=scope.head_sha
    )
    if changed_branch_config:
        return config_changes_outside_modules(prior_config, config, touched | prior_touched)
    prior_declarations = {m["path"]: m for m in prior_config.topology.get("modules", [])}
    prior_upstream, prior_downstream = resolve_module_neighbours(touched, prior_declarations)
    modules = touched, upstream | prior_upstream, downstream | prior_downstream
    return _scoped_config_payload(prior_config, scope, *modules) != _scoped_config_payload(
        config, scope, *modules
    )


def _run_with_transient_retries(
    invoke: Callable[[], tuple[ReviewOutput, str, str]],
    *,
    provider: str,
    env: dict[str, str],
    sleep: Callable[[float], None],
) -> tuple[ReviewOutput, str, str]:
    try:
        retries = min(3, max(0, int(env.get("THE_GUARD_REVIEW_TRANSIENT_RETRIES", "2"))))
    except ValueError:
        retries = 2
    try:
        backoff = max(0.0, float(env.get("THE_GUARD_REVIEW_RETRY_BACKOFF_SECONDS", "10.0")))
    except ValueError:
        backoff = 10.0
    if not math.isfinite(backoff):
        backoff = 10.0
    for attempt in range(retries):
        try:
            return invoke()
        except Exception as exc:
            if not _is_transient_provider_failure(exc):
                raise
            delay = backoff * 2**attempt
            print(
                f"[arch-review][{provider}] transient failure ({exc}); retry {attempt + 1}/{retries} in {delay:g}s",
                file=sys.stderr,
            )
            sleep(delay)
    return invoke()


def run_architecture_review(
    repo_root: Path,
    *,
    scope: ReviewScope | None = None,
    config: GuardConfig | None = None,
    policy: Policy | None = None,
    main_branch: str = "main",
    explicit_provider: str | None = None,
    reprobe: bool = False,
    force: bool = False,
    reviewer_note: str = "",
    env: dict[str, str] | None = None,
    reviewer_fn: Callable[[str], dict[str, Any]] | None = None,
    _sleep_fn: Callable[[float], None] = time.sleep,
) -> ReviewResult:
    """Run independent architecture review; `reviewer_fn` is a Python-only unit-test seam."""
    eff_env = clean_git_env(os.environ if env is None else env)
    eff_note = _sanitize_reviewer_note(reviewer_note)
    scope = (
        scope
        if scope is not None
        else collect_review_scope(repo_root, main_branch=main_branch)
    )

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

    contract_config = (
        config
        if config is not None
        else load_config(repo_root, revision=scope.head_sha)
    )
    policy = (
        policy
        if policy is not None
        else load_policy(repo_root, revision=scope.head_sha, config=contract_config)
    )
    allowed_rule_ids = tuple(rule.id for rule in policy.rules)
    current_contract_digest = compute_review_contract_digest(
        policy, contract_config, repo_root=repo_root, revision=scope.head_sha, scope=scope
    )
    current_session_contract_digest = compute_session_contract_digest(
        policy, contract_config, repo_root=repo_root, revision=scope.head_sha
    )
    branch = current_branch_name(repo_root)
    stored_session = load_review_session(repo_root, branch)
    prior_session = stored_session
    receipt_contract_changed = prior_session is not None and (
        not prior_session.contract_digest or prior_session.contract_digest != current_contract_digest
    )
    session_contract_changed = _session_contract_changed(
        repo_root, scope, contract_config, prior_session,
        session_contract_digest=current_session_contract_digest,
        receipt_contract_changed=receipt_contract_changed,
    )
    change_causes = ()
    if session_contract_changed and prior_session is not None:
        if not force:
            change_causes = contract_change_causes(
                repo_root, scope, prior_session.last_head_sha, contract_config, policy,
            )
            print("[arch-review] review contract changed: " + ", ".join(change_causes), file=sys.stderr)
        prior_session = None

    if force or not (receipt_contract_changed or session_contract_changed):
        existing = resolve_or_carry_forward_receipt(
            repo_root,
            scope,
            config=contract_config,
            policy=policy,
            contract_digest=current_contract_digest,
            persist=not force,
        )
        if existing is not None and not force:
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
    else:
        existing = None

    inc_scope = collect_incremental_scope(
        repo_root, scope, prior_session, config=contract_config
    )
    can_incremental = prior_session is not None and (
        inc_scope is not None or _is_unchanged_rebuttal_round(scope, prior_session, eff_note)
    )
    if force:
        if existing is not None or (can_incremental and prior_session.session_id):
            print("[arch-review] --force bypasses reusable review evidence; starting a full review.", file=sys.stderr)
        prior_session = None
        inc_scope = None
        can_incremental = False

    anchor_session = review_anchor_session(
        repo_root, scope, stored_session, config=contract_config
    )
    initial_reviewed_sha = anchor_session.initial_head_sha if anchor_session else ""
    snapshot_context = _snapshot_context(
        repo_root,
        scope,
        config=contract_config,
        reviewer_note=eff_note,
        initial_reviewed_sha=initial_reviewed_sha,
        prior_reviewed_sha=(
            prior_session.last_head_sha if prior_session else anchor_session.last_head_sha if anchor_session else ""
        ),
        task_blueprint_anchors=anchor_session.task_blueprint_anchors if anchor_session else None,
    )
    full_prompt_cache: str | None = None

    def _get_full_prompt() -> str:
        nonlocal full_prompt_cache
        if full_prompt_cache is None:
            full_prompt_cache = build_review_prompt(
                repo_root,
                scope,
                prior_session=prior_session,
                incremental_scope=inc_scope,
                snapshot_context=snapshot_context,
                reviewer_note=eff_note,
                policy=policy,
                config=contract_config,
            )
        return full_prompt_cache

    inc_prompt = None
    if can_incremental and prior_session is not None:
        try:
            inc_prompt = build_incremental_review_prompt(
                repo_root,
                scope,
                prior_session,
                inc_scope,
                snapshot_context=snapshot_context,
                receipt_contract_changed=receipt_contract_changed,
                reviewer_note=eff_note,
                policy=policy,
                config=contract_config,
            )
        except _PriorModuleContextUnavailable:
            prior_session = None
            inc_scope = None

    prev_head_sha = prior_session.last_head_sha if prior_session is not None else None
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
            contract_change_causes=change_causes,
            env=eff_env,
        )
        payload = validate_review_output(
            {"reviewed_head_sha": scope.head_sha, **reviewer_fn(full_prompt)},
            expected_head_sha=scope.head_sha,
            allow_implicit_origin=True,
            allowed_rule_ids=allowed_rule_ids,
        )
        chosen_provider = "test-fn"
        log_path = str(
            run_logger.finish(
                status="completed",
                session_id="",
                event_count=1,
                verdict=payload.verdict,
                summary=payload.summary,
                blocking_findings=payload.blocking_findings,
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
        primary_provider = candidates[0][0]
        forced_provider = explicit_provider or eff_env.get("THE_GUARD_REVIEW_PROVIDER", "").strip()
        if not forced_provider and prior_session and prior_session.session_id and inc_prompt is not None:
            candidates = sorted(candidates, key=lambda candidate: candidate[0] != prior_session.provider)
        for provider, binary_path in candidates:
            model, effort = resolve_provider_model_config(
                provider, config_payload, eff_env
            )
            plan_inputs = {
                "force": force,
                "stored_session": stored_session,
                "prior_session": prior_session,
                "inc_prompt": inc_prompt,
                "provider": provider,
            }
            for resume_failed in (False, True):
                plan = _provider_review_plan(**plan_inputs, resume_failed=resume_failed)
                prompt = plan.incremental_prompt if plan.mode == "incremental" else _get_full_prompt()
                try:
                    payload, resolved_session_id, log_path = _run_with_transient_retries(
                        lambda: _invoke_provider(
                            provider,
                            binary_path,
                            prompt,
                            repo_root,
                            eff_env,
                            scope=scope,
                            mode=plan.mode,
                            full_review_reason=plan.full_review_reason,
                            resume_session_id=plan.resume_session_id or None,
                            prev_head_sha=prev_head_sha,
                            reviewer_note=eff_note,
                            model=model,
                            effort=effort,
                            allowed_rule_ids=allowed_rule_ids,
                            contract_change_causes=change_causes,
                        ),
                        provider=provider, env=eff_env, sleep=_sleep_fn,
                    )
                except Exception as exc:
                    persistent = _is_persistent_provider_failure(exc)
                    if plan.mode == "incremental" and not (persistent or _is_transient_provider_failure(exc)):
                        print(
                            f"[arch-review][{provider}] incremental session resume ({plan.resume_session_id}) "
                            f"failed ({exc}); falling back to full session...",
                            file=sys.stderr,
                        )
                        continue
                    if provider == primary_provider and persistent:
                        primary_failed_persistently = True
                    print(f"[arch-review][{provider}] failed: {exc}", file=sys.stderr)
                    errors.append(f"{provider}: {exc}")
                    break

                chosen_provider = provider
                effective_mode = plan.mode
                if provider == primary_provider or primary_failed_persistently:
                    record_working_provider(repo_root, provider, binary_path, eff_env)
                break
            if payload is not None:
                break
        if payload is None:
            raise RuntimeError("All review providers failed: " + " | ".join(errors))

    result = ReviewResult(
        verdict=payload.verdict,
        summary=payload.summary,
        blocking_findings=payload.blocking_findings,
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
            last_blocking_findings=result.blocking_findings,
            last_submodule_shas={
                sub.path: sub.new_sha for sub in scope.submodule_changes
            },
            contract_digest=current_contract_digest,
            round_count=(prior_session.round_count + 1)
            if (prior_session and effective_mode == "incremental")
            else 1,
            updated_at=datetime.now(UTC).isoformat(),
            initial_head_sha=initial_reviewed_sha or scope.head_sha,
            task_blueprint_anchors=snapshot_context.task_blueprint_anchors,
            session_contract_digest=current_session_contract_digest,
        ),
    )
    if result.verdict == "GO":
        save_receipt(
            repo_root, result, contract_digest=current_contract_digest,
            task_blueprint_anchors=snapshot_context.task_blueprint_anchors,
        )
    return result
