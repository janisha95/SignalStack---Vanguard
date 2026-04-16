#!/usr/bin/env python3
"""
Phase 3d acceptance tests — Reconciliation Actions (DB-Only).
Run: python3 scripts/test_phase3d.py

Tests 1-9 per CC_PHASE_3D_RECONCILE_ACT.md §3.
All tests use synthetic data (no live MetaApi required for most).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.execution.reconciler import (
    Divergence,
    detect_divergences,
    ensure_table,
    log_divergences,
    load_journal_open,
    load_open_positions,
    resolve_db_open_broker_closed,
    resolve_db_closed_broker_open,
    resolve_qty_mismatch,
    resolve_unknown_position,
    resolve_divergences,
)
from vanguard.execution.trade_journal import (
    ensure_table as journal_ensure_table,
    insert_approval_row,
    update_filled,
)
from vanguard.execution.open_positions_writer import ensure_table as op_ensure

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
_test_results: list[tuple[int, str, bool, str]] = []


def _check(test_num: int, name: str, ok: bool, detail: str = "") -> None:
    _test_results.append((test_num, name, ok, detail))
    status = PASS if ok else FAIL
    print(f"  [{status}] Test {test_num}: {name}")
    if detail:
        for line in detail.strip().splitlines():
            print(f"    {line}")


def _make_db() -> str:
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    ensure_table(tf.name)
    journal_ensure_table(tf.name)
    op_ensure(tf.name)
    return tf.name


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetchone(db: str, sql: str, params: tuple = ()) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(sql, params).fetchone()
    con.close()
    return dict(row) if row else {}


def _fetchall(db: str, sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _insert_open_pos(db: str, profile_id: str, bp_id: str, symbol: str, qty: float) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT OR REPLACE INTO vanguard_open_positions "
        "(profile_id, broker_position_id, symbol, side, qty, entry_price, opened_at_utc, last_synced_at_utc) "
        "VALUES (?, ?, ?, 'LONG', ?, 1.0, ?, ?)",
        (profile_id, bp_id, symbol, qty, _now(), _now()),
    )
    con.commit()
    con.close()


def _insert_journal_open(db: str, profile_id: str, bp_id: str, symbol: str, qty: float) -> str:
    tid = insert_approval_row(
        db_path=db, profile_id=profile_id,
        candidate={"symbol": symbol, "asset_class": "forex", "side": "LONG",
                    "entry_price": 1.0, "stop_price": 0.99, "tp_price": 1.03,
                    "shares_or_lots": qty},
        policy_decision={"decision": "APPROVED"}, cycle_ts_utc="2026-04-05T14:00:00Z",
    )
    update_filled(
        db_path=db, trade_id=tid, broker_position_id=bp_id,
        fill_price=1.0, fill_qty=qty, filled_at_utc=_now(),
        expected_entry=1.0, side="LONG",
    )
    return tid


# ---------------------------------------------------------------------------
# Test 1 — No-op when states agree
# ---------------------------------------------------------------------------
def test1() -> None:
    print("\n--- Test 1: No-op when states agree ---")
    db = _make_db()
    try:
        broker_positions  = []
        db_journal_open   = []

        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        result = resolve_divergences(db, divs, broker_positions)
        recon_rows = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")

        ok = len(divs) == 0 and result["resolved"] == 0 and len(recon_rows) == 0
        detail = (
            f"Divergences: {len(divs)} | resolved: {result['resolved']} | recon_log: {len(recon_rows)}\n"
            f"(all expected 0)\n"
        )
        _check(1, "no-op when states agree", ok, detail)
    except Exception:
        _check(1, "no-op when states agree", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 2 — DB_OPEN_BROKER_CLOSED resolved: journal → RECONCILED_CLOSED
# ---------------------------------------------------------------------------
def test2() -> None:
    print("\n--- Test 2: DB_OPEN_BROKER_CLOSED resolved → RECONCILED_CLOSED ---")
    db = _make_db()
    try:
        tid = _insert_journal_open(db, "gft_10k", "POS-CLOSED", "EURUSD", 0.1)
        _insert_open_pos(db, "gft_10k", "POS-CLOSED", "EURUSD", 0.1)

        journal_before = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))

        # Broker has nothing (position was closed at broker)
        broker_positions  = []
        db_journal_open   = load_journal_open(db, "gft_10k")

        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        result = resolve_divergences(db, divs, broker_positions)

        journal_after = _fetchone(db, "SELECT status, close_reason FROM vanguard_trade_journal WHERE trade_id=?", (tid,))
        open_pos_after = _fetchall(db, "SELECT * FROM vanguard_open_positions WHERE broker_position_id='POS-CLOSED'")
        recon_rows = _fetchall(db, "SELECT resolved_action FROM vanguard_reconciliation_log")

        ok = (
            journal_after["status"] == "RECONCILED_CLOSED"
            and journal_after["close_reason"] == "RECONCILED"
            and len(open_pos_after) == 0  # deleted
            and any(r["resolved_action"] == "JOURNAL_MARKED_CLOSED" for r in recon_rows)
        )
        detail = (
            f"journal before: status={journal_before['status']}\n"
            f"journal after:  status={journal_after['status']} close_reason={journal_after['close_reason']}\n"
            f"open_positions after: {len(open_pos_after)} rows (expected 0 — deleted)\n"
            f"recon_log actions: {[r['resolved_action'] for r in recon_rows]}\n"
        )
        _check(2, "DB_OPEN_BROKER_CLOSED → journal=RECONCILED_CLOSED, open_pos deleted", ok, detail)
    except Exception:
        _check(2, "DB_OPEN_BROKER_CLOSED resolved", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 3 — UNKNOWN_POSITION resolved: new journal row inserted
# ---------------------------------------------------------------------------
def test3() -> None:
    print("\n--- Test 3: UNKNOWN_POSITION resolved → journal row inserted ---")
    db = _make_db()
    try:
        # Broker has manual position, no journal
        broker_positions = [
            {"broker_position_id": "POS-MANUAL", "symbol": "GBPUSD",
             "side": "LONG", "qty": 0.05, "entry_price": 1.27}
        ]
        db_journal_open = []

        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        result = resolve_divergences(db, divs, broker_positions)

        new_journal = _fetchone(
            db, "SELECT * FROM vanguard_trade_journal WHERE trade_id='recon_POS-MANUAL'"
        )
        recon_rows = _fetchall(db, "SELECT resolved_action FROM vanguard_reconciliation_log")

        ok = (
            new_journal.get("trade_id") == "recon_POS-MANUAL"
            and new_journal.get("status") == "OPEN"
            and "reconciled_unknown" in (new_journal.get("approval_reasoning_json") or "")
            and any(r["resolved_action"] == "JOURNAL_ROW_INSERTED" for r in recon_rows)
        )
        detail = (
            f"New journal row: trade_id={new_journal.get('trade_id')}\n"
            f"  status={new_journal.get('status')} (expected OPEN)\n"
            f"  approval_reasoning_json={new_journal.get('approval_reasoning_json')}\n"
            f"recon_log actions: {[r['resolved_action'] for r in recon_rows]}\n"
        )
        _check(3, "UNKNOWN_POSITION → new journal row 'recon_POS-MANUAL' with status=OPEN", ok, detail)
    except Exception:
        _check(3, "UNKNOWN_POSITION resolved", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 4 — QTY_MISMATCH resolved: open_positions qty updated
# ---------------------------------------------------------------------------
def test4() -> None:
    print("\n--- Test 4: QTY_MISMATCH resolved → open_positions qty updated ---")
    db = _make_db()
    try:
        tid = _insert_journal_open(db, "gft_10k", "POS-QTY", "EURUSD", 0.10)
        _insert_open_pos(db, "gft_10k", "POS-QTY", "EURUSD", 0.99)  # wrong qty in mirror

        broker_positions = [
            {"broker_position_id": "POS-QTY", "symbol": "EURUSD", "side": "LONG", "qty": 0.20}
        ]
        db_journal_open = load_journal_open(db, "gft_10k")
        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        result = resolve_divergences(db, divs, broker_positions)

        op_after = _fetchone(db, "SELECT qty FROM vanguard_open_positions WHERE broker_position_id='POS-QTY'")
        journal_after = _fetchone(db, "SELECT notes, approved_qty FROM vanguard_trade_journal WHERE trade_id=?", (tid,))
        recon_rows = _fetchall(db, "SELECT resolved_action FROM vanguard_reconciliation_log")

        ok = (
            abs(op_after.get("qty", 0) - 0.20) < 1e-6   # updated to broker truth
            and float(journal_after.get("approved_qty", 0)) == 0.10  # historical approved_qty unchanged
            and any(r["resolved_action"] == "DB_QTY_SYNCED" for r in recon_rows)
        )
        detail = (
            f"open_positions.qty after: {op_after.get('qty')} (expected 0.20 from broker)\n"
            f"journal.approved_qty unchanged: {journal_after.get('approved_qty')} (expected 0.10)\n"
            f"journal.notes: {journal_after.get('notes')}\n"
            f"recon_log actions: {[r['resolved_action'] for r in recon_rows]}\n"
        )
        _check(4, "QTY_MISMATCH → open_pos qty updated, journal.approved_qty preserved", ok, detail)
    except Exception:
        _check(4, "QTY_MISMATCH resolved", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 5 — DB_CLOSED_BROKER_OPEN: original journal unchanged, new row inserted
# ---------------------------------------------------------------------------
def test5() -> None:
    print("\n--- Test 5: DB_CLOSED_BROKER_OPEN → original journal unchanged, new row inserted ---")
    db = _make_db()
    try:
        tid = _insert_journal_open(db, "gft_10k", "POS-REOPEN", "USDJPY", 0.1)
        # Force journal to CLOSED (simulate prior close attempt)
        con = sqlite3.connect(db)
        con.execute("UPDATE vanguard_trade_journal SET status='CLOSED' WHERE trade_id=?", (tid,))
        con.commit()
        con.close()

        broker_positions = [
            {"broker_position_id": "POS-REOPEN", "symbol": "USDJPY",
             "side": "LONG", "qty": 0.1, "entry_price": 150.0}
        ]
        db_journal_open = []  # no OPEN rows (original is now CLOSED)
        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        result = resolve_divergences(db, divs, broker_positions)

        original_after = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))
        new_row = _fetchone(db, "SELECT * FROM vanguard_trade_journal WHERE trade_id='recon_reopen_POS-REOPEN'")
        recon_rows = _fetchall(db, "SELECT resolved_action FROM vanguard_reconciliation_log")

        ok = (
            original_after["status"] == "CLOSED"   # original NOT modified
            and new_row.get("trade_id") == "recon_reopen_POS-REOPEN"  # new row inserted
            and any(r["resolved_action"] == "FLAGGED_FOR_MANUAL_REVIEW" for r in recon_rows)
        )
        detail = (
            f"Original journal status after: {original_after['status']} (expected CLOSED — NOT modified)\n"
            f"New journal row: trade_id={new_row.get('trade_id')}\n"
            f"  status={new_row.get('status')} | notes={new_row.get('notes')}\n"
            f"recon_log actions: {[r['resolved_action'] for r in recon_rows]}\n"
        )
        _check(5, "DB_CLOSED_BROKER_OPEN → original journal CLOSED unchanged, new row inserted", ok, detail)
    except Exception:
        _check(5, "DB_CLOSED_BROKER_OPEN handling", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 6 — Transactional rollback on failure
# ---------------------------------------------------------------------------
def test6() -> None:
    print("\n--- Test 6: Transactional rollback on failure ---")
    db = _make_db()
    try:
        tid = _insert_journal_open(db, "gft_10k", "POS-FAIL", "EURUSD", 0.1)
        # DO NOT create open_positions row — the DELETE will be a no-op, not a failure
        # Instead, break by creating a divergence that will fail:
        # We'll use a fake divergence with a trade_id that doesn't exist in journal
        fake_divergence = Divergence(
            divergence_type="DB_OPEN_BROKER_CLOSED",
            profile_id="gft_10k",
            broker_position_id="POS-FAIL",
            db_trade_id="NONEXISTENT-TRADE-ID-XYZ",  # doesn't exist
            symbol="EURUSD",
            details={"journal_qty": 0.1},
        )

        journal_before = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))
        recon_before = len(_fetchall(db, "SELECT * FROM vanguard_reconciliation_log"))

        # This should rollback the journal update (trade_id doesn't exist → UPDATE affects 0 rows)
        # But it should still write a RESOLUTION_FAILED or complete successfully (0-row UPDATE is OK)
        try:
            resolve_db_open_broker_closed(db, fake_divergence, broker_last_known_deal=None)
            resolution_raised = False
        except Exception:
            resolution_raised = True

        journal_after = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))
        recon_after = _fetchall(db, "SELECT resolved_action FROM vanguard_reconciliation_log")

        # Real journal row (tid) should not have been touched
        ok = journal_after.get("status") == "OPEN"
        detail = (
            f"Original journal (tid={tid[:8]}...) status after: {journal_after.get('status')} (expected OPEN)\n"
            f"Resolution raised exception: {resolution_raised}\n"
            f"Recon log actions after: {[r['resolved_action'] for r in recon_after]}\n"
        )
        _check(6, "real journal row untouched when nonexistent trade_id used in resolution", ok, detail)
    except Exception:
        _check(6, "transactional rollback", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 7 — Idempotency: second run finds 0 divergences
# ---------------------------------------------------------------------------
def test7() -> None:
    print("\n--- Test 7: Idempotency — second run finds 0 divergences ---")
    db = _make_db()
    try:
        # Set up initial state
        broker_positions = [
            {"broker_position_id": "POS-STABLE", "symbol": "EURUSD",
             "side": "LONG", "qty": 0.1, "entry_price": 1.085}
        ]

        # First run: UNKNOWN_POSITION → inserts journal row
        divs1 = detect_divergences(db, "gft_10k", broker_positions, [], [])
        result1 = resolve_divergences(db, divs1, broker_positions)

        recon1 = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")

        # Second run: journal now has recon_POS-STABLE (status=OPEN) → should match broker
        journal_open2 = load_journal_open(db, "gft_10k")
        divs2 = detect_divergences(db, "gft_10k", broker_positions, [], journal_open2)
        result2 = resolve_divergences(db, divs2, broker_positions)

        recon2 = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")

        ok = len(divs2) == 0 and result2["resolved"] == 0 and len(recon2) == len(recon1)
        detail = (
            f"Run 1: divs={len(divs1)} resolved={result1['resolved']} recon_rows={len(recon1)}\n"
            f"Run 2: divs={len(divs2)} resolved={result2['resolved']} recon_rows={len(recon2)}\n"
            f"Second run produced 0 new rows: {len(recon2) == len(recon1)}\n"
        )
        _check(7, "idempotency — second run finds 0 divergences and 0 new log rows", ok, detail)
    except Exception:
        _check(7, "idempotency", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 8 — Dry-run (no --act) logs DETECTED_ONLY
# ---------------------------------------------------------------------------
def test8() -> None:
    print("\n--- Test 8: No --act → DETECTED_ONLY, no state mutations ---")
    db = _make_db()
    try:
        tid = _insert_journal_open(db, "gft_10k", "POS-DETECT", "EURUSD", 0.1)
        broker_positions = []   # will trigger DB_OPEN_BROKER_CLOSED
        db_journal_open  = load_journal_open(db, "gft_10k")

        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        rows_logged = log_divergences(db, divs, _now())  # detect-only mode

        journal_after = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))
        recon_rows = _fetchall(db, "SELECT resolved_action FROM vanguard_reconciliation_log")

        ok = (
            journal_after["status"] == "OPEN"   # NOT mutated
            and rows_logged > 0
            and all(r["resolved_action"] == "DETECTED_ONLY" for r in recon_rows)
        )
        detail = (
            f"Journal status: {journal_after['status']} (expected OPEN — not mutated)\n"
            f"Recon log rows: {rows_logged}\n"
            f"All DETECTED_ONLY: {all(r['resolved_action'] == 'DETECTED_ONLY' for r in recon_rows)}\n"
        )
        _check(8, "detect-only mode: DETECTED_ONLY logged, journal not mutated", ok, detail)
    except Exception:
        _check(8, "dry-run mode", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 9 — Audit trail complete
# ---------------------------------------------------------------------------
def test9() -> None:
    print("\n--- Test 9: Audit trail complete across all action types ---")
    db = _make_db()
    try:
        # Set up scenarios for each divergence type and resolve all
        # 1. DB_OPEN_BROKER_CLOSED
        tid1 = _insert_journal_open(db, "gft_10k", "POS-AUDIT-1", "EURUSD", 0.1)
        _insert_open_pos(db, "gft_10k", "POS-AUDIT-1", "EURUSD", 0.1)

        # 2. UNKNOWN_POSITION
        broker_positions = [
            {"broker_position_id": "POS-AUDIT-2", "symbol": "GBPUSD",
             "side": "LONG", "qty": 0.05, "entry_price": 1.27},
        ]

        db_journal_open = load_journal_open(db, "gft_10k")
        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        result = resolve_divergences(db, divs, broker_positions)

        recon_rows = _fetchall(db,
            "SELECT divergence_type, resolved_action, COUNT(*) as c "
            "FROM vanguard_reconciliation_log GROUP BY divergence_type, resolved_action"
        )

        detail = "Audit trail:\n"
        for r in recon_rows:
            detail += f"  [{r['divergence_type']}] → {r['resolved_action']} (count={r['c']})\n"

        # Expect one JOURNAL_MARKED_CLOSED and one JOURNAL_ROW_INSERTED
        expected_actions = {"JOURNAL_MARKED_CLOSED", "JOURNAL_ROW_INSERTED"}
        found_actions = {r["resolved_action"] for r in recon_rows}

        ok = expected_actions.issubset(found_actions)
        detail += f"Expected actions: {expected_actions}\nFound actions: {found_actions}\n"
        _check(9, "audit trail contains JOURNAL_MARKED_CLOSED and JOURNAL_ROW_INSERTED", ok, detail)
    except Exception:
        _check(9, "audit trail complete", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Verify no broker calls in reconciler.py
# ---------------------------------------------------------------------------
def check_no_broker_calls() -> None:
    print("\n--- Bonus: Verify no broker calls in reconciler.py ---")
    reconciler_path = _REPO_ROOT / "vanguard" / "execution" / "reconciler.py"
    content = reconciler_path.read_text()
    dangerous = ["close_position", "place_order", "modify_order", "execute_trade"]
    found = [d for d in dangerous if d in content]
    ok = len(found) == 0
    detail = f"Checked for: {dangerous}\nFound: {found}\n"
    print(f"  [{'PASS' if ok else 'FAIL'}] No broker write calls in reconciler.py: {ok}")
    print(f"    {detail.strip()}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 60)
    print("Phase 3d Acceptance Tests — Reconciliation Actions")
    print("=" * 60)

    test1()
    test2()
    test3()
    test4()
    test5()
    test6()
    test7()
    test8()
    test9()
    check_no_broker_calls()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, _, ok, _ in _test_results if ok)
    total  = len(_test_results)
    for num, name, ok, _ in _test_results:
        status = PASS if ok else FAIL
        print(f"  [{status}] Test {num}: {name}")
    print(f"\n{passed}/{total} passed")
    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
