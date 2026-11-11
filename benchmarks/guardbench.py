"""Reproducible, dependency-free GuardBench architectural benchmark."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

THE_GUARD_SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, THE_GUARD_SRC)

from the_guard.git_helpers import changed_files
from the_guard.init import initialize
from the_guard.policy import load_policy
from the_guard.review.engine import run_architecture_review
from the_guard.topology import run_topology_guard


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write_file(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def build_guardbench_repo(bench_root: Path) -> Path:
    repo = bench_root / "guardbench-repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)

    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Benchmark Runner")
    git(repo, "config", "user.email", "bench@example.invalid")

    initialize(repo, preset="full")

    # Module owner contracts
    write_file(
        repo,
        "services/orders/README.md",
        """# Orders Service (Canonical Owner)

## Responsibilities
- Sole owner of the `orders` SQL table and all order lifecycle state transitions (`PENDING`, `PAID`, `SHIPPED`, `REFUNDED`).
- Sole owner of refund eligibility calculation (`OrderProjection.can_refund`).
- Exposes read projections via `services.orders.api.get_order_projection` and `services.orders.api.get_order_projections_batch`.

## Non-Responsibilities
- Never formats UI badges or billing invoices.
""",
    )
    write_file(
        repo,
        "services/orders/internal/storage.py",
        """\"\"\"Orders storage owner.\"\"\"

async def fetch_order_row(db, order_id: str) -> dict:
    return await db.fetch_one("SELECT id, status, paid_cents, disputed FROM orders WHERE id = :id", {"id": order_id})

async def fetch_order_rows_batch(db, order_ids: list[str]) -> list[dict]:
    return await db.fetch_all("SELECT id, status, paid_cents, disputed FROM orders WHERE id = ANY(:ids)", {"ids": order_ids})
""",
    )
    write_file(
        repo,
        "services/orders/api.py",
        """\"\"\"Public Orders Service API.\"\"\"
from dataclasses import dataclass
from services.orders.internal.storage import fetch_order_row, fetch_order_rows_batch


@dataclass(frozen=True)
class OrderProjection:
    order_id: str
    status: str
    paid_cents: int
    can_refund: bool

def _to_projection(row: dict) -> OrderProjection:
    can_refund = row["status"] == "PAID" and row["paid_cents"] > 0 and not row.get("disputed", False)
    return OrderProjection(
        order_id=row["id"],
        status=row["status"],
        paid_cents=row["paid_cents"],
        can_refund=can_refund,
    )

async def get_order_projection(db, order_id: str) -> OrderProjection:
    row = await fetch_order_row(db, order_id)
    return _to_projection(row)

async def get_order_projections_batch(db, order_ids: list[str]) -> list[OrderProjection]:
    rows = await fetch_order_rows_batch(db, order_ids)
    return [_to_projection(r) for r in rows]
""",
    )

    write_file(
        repo,
        "services/billing/README.md",
        """# Billing Service

## Responsibilities
- Sole owner of the `invoices` SQL table.
- Consumes `services.orders.api` projections for order status and refund eligibility.
- Migrating away from retired table `legacy_invoices`: any edit to billing files must reduce `legacy_invoices` references and never keep dual-read fallback branches.
""",
    )
    write_file(
        repo,
        "services/billing/invoices.py",
        """\"\"\"Billing invoice repository.\"\"\"

LEGACY_EXPORT_SQL = "SELECT id FROM legacy_invoices WHERE archived = 0"

async def get_invoice(db, invoice_id: str) -> dict:
    return await db.fetch_one("SELECT id, order_id, amount_cents FROM invoices WHERE id = :id", {"id": invoice_id})
""",
    )

    write_file(
        repo,
        "ui/checkout/README.md",
        """# Checkout UI Surface

## Responsibilities
- Renders `OrderProjection` returned by `services.orders.api`.
- Must never recalculate order state machines or refund eligibility locally, and must never import `services.orders.internal`.
""",
    )
    write_file(
        repo,
        "ui/checkout/view.py",
        """\"\"\"Checkout UI view model.\"\"\"
from services.orders.api import OrderProjection

def render_order_card(order: OrderProjection) -> dict:
    return {
        "order_id": order.order_id,
        "status_label": order.status,
        "show_refund_button": order.can_refund,
    }
""",
    )

    write_file(
        repo,
        "agents/tools/README.md",
        """# Agent Tools Module

## Responsibilities
- Exposes flat action-discriminated tool schemas (`guard.tool-authoring`).
- Business recovery and settlement must rely on domain obligations (`OrderProjection`), never process metadata such as `prompt_version` or `build_sha` (`guard.process-adjunct-not-truth`).
""",
    )
    write_file(
        repo,
        "agents/tools/order_tool.py",
        """\"\"\"Order management tool for coding/support agents.\"\"\"

TOOL_SCHEMA = {
    "name": "order_action",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "refund"]},
            "order_id": {"type": "string"},
        },
        "required": ["action", "order_id"],
    },
}
""",
    )

    # Configure .guard/config.json with real topology rules
    guard_config = {
        "manifest_file": "AGENTS.md",
        "detail_dir": "docs/standards/rules",
        "main_branch": "main",
        "module_doc_roots": ["services", "ui", "agents"],
        "topology": {
            "forbidden_imports": [
                {"from": "ui/", "to": "services.orders.internal"},
                {"from": "services/billing/", "to": "services.orders.internal"},
            ],
            "exclusive_sql_owners": [
                {
                    "table": "orders",
                    "owners": ["services/orders/internal/storage.py"],
                    "rule_id": "guard.single-source-of-truth",
                    "message": "Only services/orders/internal/storage.py may query or mutate the orders table.",
                }
            ],
            "retired_tables": [
                {
                    "scope_prefixes": ["services/billing/"],
                    "tables": ["legacy_invoices"],
                    "rule_id": "guard.data-modeling",
                }
            ],
        },
    }
    write_file(repo, ".guard/config.json", json.dumps(guard_config, indent=2) + "\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "feat: initial multi-service baseline")

    # Case 1: UI duplicates backend refund state machine (Semantic SSOT violation - no import violation, pure syntax clean!)
    git(repo, "switch", "-c", "case-1-duplicate-state-machine", "main")
    write_file(
        repo,
        "ui/checkout/view.py",
        """\"\"\"Checkout UI view model.\"\"\"
from services.orders.api import OrderProjection

def render_order_card(order: OrderProjection, is_vip_override: bool = False) -> dict:
    # Recompute refund eligibility rules in UI for faster badge rendering
    can_refund_ui = (order.status in ("PAID", "SETTLED") and order.paid_cents >= 100) or is_vip_override
    return {
        "order_id": order.order_id,
        "status_label": order.status,
        "show_refund_button": can_refund_ui,
    }
""",
    )
    git(repo, "commit", "-am", "feat(ui): compute refund badge eligibility in checkout view")

    # Case 2: Cross-service SQL ownership & forbidden internal import
    git(repo, "switch", "-c", "case-2-cross-service-sql-and-import", "main")
    write_file(
        repo,
        "services/billing/reconcile.py",
        """\"\"\"Billing reconciliation.\"\"\"
from services.orders.internal.storage import fetch_order_row

async def reconcile_order_payment(db, order_id: str) -> dict:
    direct_row = await db.fetch_one("SELECT id, status, paid_cents FROM orders WHERE id = :id", {"id": order_id})
    backup_row = await fetch_order_row(db, order_id)
    return direct_row or backup_row
""",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feat(billing): add order payment reconciliation query")

    # Case 3: Touching file with retired table without reducing references + keeping dual-read fallback
    git(repo, "switch", "-c", "case-3-retired-table-dual-read", "main")
    write_file(
        repo,
        "services/billing/invoices.py",
        """\"\"\"Billing invoice repository.\"\"\"

LEGACY_EXPORT_SQL = "SELECT id FROM legacy_invoices WHERE archived = 0"

async def get_invoice(db, invoice_id: str) -> dict:
    row = await db.fetch_one("SELECT id, order_id, amount_cents FROM invoices WHERE id = :id", {"id": invoice_id})
    if row is not None:
        return row
    # Dual-read fallback to legacy_invoices in case migration hasn't synced
    return await db.fetch_one("SELECT id, order_id, amount_cents FROM legacy_invoices WHERE id = :id", {"id": invoice_id})
""",
    )
    git(repo, "commit", "-am", "fix(billing): fallback to legacy_invoices when invoice is missing")

    # Case 4: Symptom patch with local retry loop + silent fallback masking upstream failure
    git(repo, "switch", "-c", "case-4-retry-loop-silent-fallback", "main")
    write_file(
        repo,
        "services/billing/summary.py",
        """\"\"\"Billing customer summary.\"\"\"
import asyncio
from services.orders.api import get_order_projection

async def load_customer_refundable_total(db, order_ids: list[str]) -> int:
    total = 0
    for oid in order_ids:
        proj = None
        for attempt in range(3):
            try:
                proj = await get_order_projection(db, oid)
                break
            except Exception:
                await asyncio.sleep(0.05 * (attempt + 1))
        if proj is None:
            # Silently ignore failed order lookups and pretend 0
            continue
        if proj.can_refund:
            total += proj.paid_cents
    return total
""",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fix(billing): add retry loop and safe default for order projection lookup")

    # Case 5: Read-only endpoint acquiring write lock + duplicate awaited reads on same path
    git(repo, "switch", "-c", "case-5-read-lock-and-duplicate-read", "main")
    write_file(
        repo,
        "services/orders/api.py",
        """\"\"\"Public Orders Service API.\"\"\"
from dataclasses import dataclass
from services.orders.internal.storage import fetch_order_row, fetch_order_rows_batch

@dataclass(frozen=True)
class OrderProjection:
    order_id: str
    status: str
    paid_cents: int
    can_refund: bool

def _to_projection(row: dict) -> OrderProjection:
    can_refund = row["status"] == "PAID" and row["paid_cents"] > 0 and not row.get("disputed", False)
    return OrderProjection(
        order_id=row["id"],
        status=row["status"],
        paid_cents=row["paid_cents"],
        can_refund=can_refund,
    )

async def get_order_projection(db, order_id: str, write_lock=None) -> OrderProjection:
    if write_lock is not None:
        async with write_lock:
            first_row = await fetch_order_row(db, order_id)
            second_row = await fetch_order_row(db, order_id)
            return _to_projection(second_row or first_row)
    row = await fetch_order_row(db, order_id)
    return _to_projection(row)

async def get_order_projections_batch(db, order_ids: list[str]) -> list[OrderProjection]:
    rows = await fetch_order_rows_batch(db, order_ids)
    return [_to_projection(r) for r in rows]
""",
    )
    git(repo, "commit", "-am", "fix(orders): synchronize read projection with lock and double check")

    # Case 6: Nested envelope tool schema + open dict + process adjunct truth
    git(repo, "switch", "-c", "case-6-tool-envelope-and-process-adjunct", "main")
    write_file(
        repo,
        "agents/tools/order_tool.py",
        """\"\"\"Order management tool for coding/support agents.\"\"\"

TOOL_SCHEMA = {
    "name": "order_action",
    "parameters": {
        "type": "object",
        "properties": {
            "envelope": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "extra_context": {"type": "object", "additionalProperties": True},
                    "worker_secret_token": {"type": "string"},
                },
            }
        },
    },
}

def should_resume_refund_settlement(order_status: str, prompt_version: str, build_sha: str) -> bool:
    # Only resume settlement if the order was created by prompt_version v2.4 on the current build_sha
    if prompt_version != "v2.4" or not build_sha.startswith("prod-"):
        return False
    return order_status == "PAID"
""",
    )
    git(repo, "commit", "-am", "feat(tools): add envelope metadata and prompt_version settlement gate")

    # Case 7: Clean architectural change (Deletes retired table reference & uses batch owner API)
    git(repo, "switch", "-c", "case-7-clean-architecture-feature", "main")
    write_file(
        repo,
        "services/billing/invoices.py",
        """\"\"\"Billing invoice repository.\"\"\"
from services.orders.api import get_order_projections_batch

async def get_invoice(db, invoice_id: str) -> dict:
    return await db.fetch_one("SELECT id, order_id, amount_cents FROM invoices WHERE id = :id", {"id": invoice_id})

async def list_refundable_invoices(db, invoice_ids: list[str]) -> list[dict]:
    invoices = await db.fetch_all(
        "SELECT id, order_id, amount_cents FROM invoices WHERE id = ANY(:ids)",
        {"ids": invoice_ids},
    )
    order_ids = [inv["order_id"] for inv in invoices]
    projections = {
        proj.order_id: proj
        for proj in await get_order_projections_batch(db, order_ids)
    }
    return [
        inv
        for inv in invoices
        if (proj := projections.get(inv["order_id"])) is not None and proj.can_refund
    ]
""",
    )
    git(repo, "commit", "-am", "feat(billing): remove legacy_invoices and batch-filter refundable invoices via orders API")

    git(repo, "switch", "main")
    return repo

@dataclass(frozen=True)
class GuardBenchCase:
    branch: str
    category: str
    title: str
    expected_static_rule_ids: tuple[str, ...]
    expected_review_rule_ids: tuple[str, ...]


CASES: tuple[GuardBenchCase, ...] = (
    GuardBenchCase("case-1-duplicate-state-machine", "corrupt", "UI duplicates backend refund state", (), ("guard.no-silent-fallback", "guard.single-source-of-truth")),
    GuardBenchCase("case-2-cross-service-sql-and-import", "corrupt", "Cross-service SQL and internal import", ("guard.service-boundaries", "guard.single-source-of-truth"), ("guard.database-query-efficiency", "guard.service-boundaries", "guard.single-source-of-truth")),
    GuardBenchCase("case-3-retired-table-dual-read", "corrupt", "Retired table dual-read fallback", ("guard.data-modeling",), ("guard.data-modeling", "guard.single-source-of-truth")),
    GuardBenchCase("case-4-retry-loop-silent-fallback", "corrupt", "Retry loop, silent fallback and N+1", ("guard.database-query-efficiency",), ("guard.data-modeling", "guard.database-query-efficiency", "guard.no-silent-fallback")),
    GuardBenchCase("case-5-read-lock-and-duplicate-read", "corrupt", "Write lock on read path and duplicate read", ("guard.database-query-efficiency",), ("guard.architecture-simplicity", "guard.database-query-efficiency")),
    GuardBenchCase("case-6-tool-envelope-and-process-adjunct", "corrupt", "Nested tool envelope and process metadata truth", (), ("guard.process-adjunct-not-truth", "guard.tool-authoring")),
    GuardBenchCase("case-7-clean-architecture-feature", "clean", "Clean owner API and retired table cutover", (), ()),
)


def validate_policy(repo_dir: Path) -> None:
    load_policy(repo_dir)


def evaluate_deterministic_checks(repo_dir: Path) -> dict[str, Any]:
    results: dict[str, Any] = {"cases": [], "passed": True}
    for case in CASES:
        git(repo_dir, "switch", case.branch)
        validate_policy(repo_dir)
        findings = run_topology_guard(repo_dir, changed_files(repo_dir, "main"), base="main")
        observed = tuple(sorted({finding.rule_id for finding in findings}))
        expected = tuple(sorted(case.expected_static_rule_ids))
        matched = observed == expected
        results["cases"].append({"branch": case.branch, "category": case.category, "title": case.title, "expected_static_rule_ids": list(expected), "observed_static_rule_ids": list(observed), "expected_review_rule_ids": list(case.expected_review_rule_ids), "passed": matched})
        results["passed"] = results["passed"] and matched
    git(repo_dir, "switch", "main")
    return results


def run_review(repo_dir: Path, provider: str) -> dict[str, Any]:
    reviews = []
    for case in CASES:
        git(repo_dir, "switch", case.branch)
        try:
            outcome = run_architecture_review(
                repo_dir, main_branch="main", explicit_provider=provider, force=True
            )
            observed = tuple(sorted({finding.rule_id for finding in outcome.blocking_findings}))
            expected_verdict = "GO" if case.category == "clean" else "NOGO"
            reviews.append({"branch": case.branch, "verdict": outcome.verdict, "expected_verdict": expected_verdict, "provider": outcome.provider, "expected_review_rule_ids": list(case.expected_review_rule_ids), "observed_review_rule_ids": list(observed), "passed": outcome.verdict == expected_verdict})
        except (OSError, RuntimeError, ValueError) as exc:
            reviews.append({"branch": case.branch, "verdict": "ERROR", "expected_verdict": "GO" if case.category == "clean" else "NOGO", "expected_review_rule_ids": list(case.expected_review_rule_ids), "observed_review_rule_ids": [], "passed": False, "error": str(exc)})
    git(repo_dir, "switch", "main")
    return {"provider": provider, "cases": reviews}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the reproducible GuardBench benchmark")
    parser.add_argument("--repo-dir", type=Path)
    parser.add_argument("--with-review", action="store_true")
    parser.add_argument("--provider", choices=("codex", "claude", "agy"), default="codex")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    temp_dir = None
    if args.repo_dir:
        repo = build_guardbench_repo(args.repo_dir)
        result = evaluate_deterministic_checks(repo)
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="guardbench-")
        repo = build_guardbench_repo(Path(temp_dir.name))
        result = evaluate_deterministic_checks(repo)
    if args.with_review:
        result["review"] = run_review(repo, args.provider)
    if temp_dir is not None:
        temp_dir.cleanup()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("GuardBench deterministic checks")
        for case in result["cases"]:
            status = "PASS" if case["passed"] else "FAIL"
            print(f"{status:4} {case['branch']}: {', '.join(case['observed_static_rule_ids']) or 'no findings'}")
        print("PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
