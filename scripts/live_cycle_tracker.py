#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.analytics.live_diagnostics import (
    classify_funnel_seam,
    fetch_diagnostics_snapshot,
    refresh_diagnostics,
    resolve_active_profile_ids,
)
from vanguard.config.runtime_config import get_runtime_config


DEFAULT_DB = _REPO_ROOT / "data" / "vanguard_universe.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _profile_health_verdict(row: dict[str, object]) -> tuple[str, str]:
    verdict = classify_funnel_seam(row)
    return (str(verdict.get("status") or "blocked"), str(verdict.get("reason") or "unknown"))


def _short_profile_label(profile_id: str) -> str:
    pid = str(profile_id or "")
    if "metagate" in pid:
        return "slot1"
    if "cde_d" in pid:
        return "slot2"
    if "cde_e" in pid:
        return "slot3"
    if "control" in pid:
        return "slot4"
    return pid


def _build_window_summary(
    con: sqlite3.Connection,
    profile_ids: list[str],
    *,
    window_minutes: int,
) -> list[dict[str, object]]:
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    if not clean_profile_ids:
        return []
    placeholders = ",".join("?" for _ in clean_profile_ids)
    rows = con.execute(
        f"""
        SELECT
            profile_id,
            COUNT(*) AS cycles,
            COALESCE(SUM(predictions_count), 0) AS predictions,
            COALESCE(SUM(economics_pass_rows), 0) AS economics_pass,
            COALESCE(SUM(economics_fail_rows), 0) AS economics_fail,
            COALESCE(SUM(skipped_meta_gate_rows), 0) AS meta_skip,
            COALESCE(SUM(live_policy_filtered_rows), 0) AS policy_skip,
            COALESCE(SUM(route_candidate_rows), 0) AS route_candidates,
            COALESCE(SUM(portfolio_approved_rows), 0) AS approvals,
            COALESCE(SUM(executed_rows), 0) AS executions
        FROM vanguard_diagnostics_cycle_funnel
        WHERE profile_id IN ({placeholders})
          AND cycle_ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
        GROUP BY profile_id
        ORDER BY profile_id
        """,
        [*clean_profile_ids, f"-{int(window_minutes)} minutes"],
    ).fetchall()
    return [dict(row) for row in rows]


def _dominant_blockers(row: dict[str, object]) -> str:
    parts: list[tuple[str, int]] = [
        ("meta_gate", int(row.get("meta_skip") or 0)),
        ("economics", int(row.get("economics_fail") or 0)),
        ("policy", int(row.get("policy_skip") or 0)),
    ]
    nonzero = [(label, count) for label, count in parts if count > 0]
    if not nonzero:
        return "none"
    nonzero.sort(key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{label}={count}" for label, count in nonzero[:3])


def _print_slot_summary(snapshot: dict[str, object]) -> None:
    rows = snapshot.get("slot_funnels") or []
    if not rows:
        print("slots: none")
        return
    cycle_ts = max((str(row.get("cycle_ts_utc") or "") for row in rows), default="")
    if cycle_ts:
        print(f"cycle: {cycle_ts}")
    print("slots:")
    for row in rows:
        verdict, reason = _profile_health_verdict(row)
        print(
            "  "
            f"{_short_profile_label(str(row.get('profile_id') or ''))} "
            f"verdict={verdict} "
            f"pred={row.get('predictions_count')} "
            f"econ_pass={row.get('economics_pass_rows')} "
            f"meta_skip={row.get('skipped_meta_gate_rows')} "
            f"policy_skip={row.get('live_policy_filtered_rows')} "
            f"route={row.get('route_candidate_rows')} "
            f"risk_approved={row.get('portfolio_approved_rows')} "
            f"exec={row.get('executed_rows')} "
            f"why={reason}"
        )


def _print_window_summary(window_rows: list[dict[str, object]], *, window_minutes: int) -> None:
    if not window_rows:
        return
    print(f"window: last {int(window_minutes)}m")
    for row in window_rows:
        print(
            "  "
            f"{_short_profile_label(str(row.get('profile_id') or ''))} "
            f"cycles={row.get('cycles')} "
            f"pred={row.get('predictions')} "
            f"econ_pass={row.get('economics_pass')} "
            f"route={row.get('route_candidates')} "
            f"risk_approved={row.get('approvals')} "
            f"exec={row.get('executions')} "
            f"blockers={_dominant_blockers(row)}"
        )


def _print_account_truth(snapshot: dict[str, object]) -> None:
    rows = snapshot.get("account_truth") or []
    if not rows:
        print("account_truth: none")
        return
    flagged = [
        row for row in rows
        if bool(row.get("cached_fallback"))
        or str(row.get("truth_state") or "") != "OK"
        or str(row.get("source_status") or "") != "OK"
    ]
    if not flagged:
        return
    fallback_count = sum(1 for row in flagged if bool(row.get("cached_fallback")))
    bad_count = len(flagged)
    print(
        "account_truth:"
        f" flagged={bad_count} fallback={fallback_count}"
    )
    for row in flagged:
        print(
            "  "
            f"{_short_profile_label(str(row.get('profile_id') or ''))} "
            f"state={row.get('truth_state')} "
            f"fallback={bool(row.get('cached_fallback'))} "
            f"bal={row.get('balance')} eq={row.get('equity_snapshot')} "
            f"open={row.get('open_positions_count')} ts={row.get('received_ts_utc') or '-'}"
        )


def _print_service_verdicts(snapshot: dict[str, object]) -> None:
    rows = snapshot.get("service_verdicts") or []
    if not rows:
        return
    degraded: list[dict[str, object]] = []
    for row in rows:
        freshness_state = str(row.get("freshness_state") or "")
        if freshness_state in {"OK", "LOG_ONLY", "PROCESS_ONLY"}:
            continue
        degraded.append(row)
    if not degraded:
        return
    print("service_verdicts:")
    for row in degraded:
        freshness_state = str(row.get("freshness_state") or "")
        print(
            "  "
            f"! {row.get('service_name')}: state={freshness_state} "
            f"alive={bool(row.get('process_alive'))} required={bool(row.get('required_for_ready'))} "
            f"freshness_budget={row.get('freshness_seconds')}s "
            f"log_age={row.get('log_age_seconds')} "
            f"last_success={row.get('last_success_ts_utc') or '-'}"
        )


def _print_active_issues(snapshot: dict[str, object], limit: int) -> None:
    issues: list[str] = []

    slot_rows = snapshot.get("slot_funnels") or []
    for row in slot_rows:
        profile_id = str(row.get("profile_id") or "")
        _, reason = _profile_health_verdict(row)
        if reason != "pipeline is flowing":
            issues.append(f"{_short_profile_label(profile_id)}: {reason}")

    account_rows = snapshot.get("account_truth") or []
    fallback_count = sum(1 for row in account_rows if bool(row.get("cached_fallback")))
    if fallback_count:
        issues.append(f"account_truth: {fallback_count} fallback snapshots active")

    service_rows = snapshot.get("service_verdicts") or []
    degraded_services = [
        str(row.get("service_name") or "")
        for row in service_rows
        if str(row.get("freshness_state") or "") not in {"OK", "LOG_ONLY", "PROCESS_ONLY"}
    ]
    if degraded_services:
        issues.append(f"services: degraded={','.join(sorted(degraded_services))}")

    active_anomalies = snapshot.get("active_anomalies") or []
    for row in active_anomalies:
        family = str(row.get("family") or "")
        scope_type = str(row.get("scope_type") or "")
        scope_id = str(row.get("scope_id") or "")
        summary = str(row.get("summary") or "").strip()
        if not summary:
            continue
        if family not in {
            "stage_contract_drift",
            "selection_handoff_drift",
            "exit_policy_drift",
            "execution_attempt_drift",
            "cycle_error",
        }:
            continue
        if scope_type == "profile":
            issues.append(f"{_short_profile_label(scope_id)}: {summary}")
        elif scope_type == "stage":
            issues.append(f"stage:{scope_id}: {summary}")
        elif scope_type == "trade":
            issues.append(f"trade:{scope_id}: {summary}")
        else:
            issues.append(summary)

    if not issues:
        return
    deduped: list[str] = []
    seen: set[str] = set()
    for item in issues:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)

    print(f"issues: {len(deduped)}")
    for item in deduped[:limit]:
        print(f"  {item}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Operator-grade cycle diagnostics for the live stack.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--profile-id", action="append", default=[])
    parser.add_argument("--history-limit", type=int, default=10)
    parser.add_argument("--window-minutes", type=int, default=120)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        runtime_cfg = get_runtime_config()
    profile_ids = [str(pid).strip() for pid in (args.profile_id or []) if str(pid).strip()]
    if not profile_ids:
        profile_ids = resolve_active_profile_ids(runtime_cfg) or ["ftmo_demo_100k"]

    with _connect(Path(args.db_path)) as con:
        refresh_diagnostics(con, runtime_cfg, profile_ids)
        snapshot = fetch_diagnostics_snapshot(
            con,
            profile_ids,
            active_only=True,
            anomaly_limit=max(10, int(args.history_limit)),
        )
        window_rows = _build_window_summary(
            con,
            profile_ids,
            window_minutes=max(1, int(args.window_minutes)),
        )

    if args.json:
        payload = dict(snapshot)
        payload["window_summary"] = window_rows
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0

    _print_slot_summary(snapshot)
    _print_window_summary(window_rows, window_minutes=max(1, int(args.window_minutes)))
    _print_account_truth(snapshot)
    _print_service_verdicts(snapshot)
    _print_active_issues(snapshot, limit=int(args.history_limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
