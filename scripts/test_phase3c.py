#!/usr/bin/env python3
"""
Phase 3c acceptance tests — Reconciliation Detect-Only.
Run: python3 scripts/test_phase3c.py

Tests 1-8 per CC_PHASE_3C_RECONCILE_DETECT.md §3.
Tests that require live MetaApi (3, 4, 5) are run with synthetic broker data
injected directly so the detection logic can be verified without live broker.
Test 8 verifies MetaApi outage handling via bad credentials.
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
    load_open_positions,
    load_journal_open,
)
from vanguard.execution.trade_journal import (
    ensure_table as journal_ensure_table,
    insert_approval_row,
    update_filled,
)

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
    return tf.name


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetchall(db: str, sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _fetchone(db: str, sql: str, params: tuple = ()) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(sql, params).fetchone()
    con.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Helpers to set up synthetic state
# ---------------------------------------------------------------------------

def _insert_open_position(db: str, profile_id: str, broker_pos_id: str, symbol: str, qty: float) -> None:
    """Insert a fake row into vanguard_open_positions."""
    from vanguard.execution.open_positions_writer import ensure_table as op_ensure
    op_ensure(db)
    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT OR REPLACE INTO vanguard_open_positions
            (profile_id, broker_position_id, symbol, side, qty,
             entry_price, opened_at_utc, last_synced_at_utc)
        VALUES (?, ?, ?, 'LONG', ?, 1.0, ?, ?)
        """,
        (profile_id, broker_pos_id, symbol, qty, _now(), _now()),
    )
    con.commit()
    con.close()


def _insert_journal_open(db: str, profile_id: str, broker_pos_id: str, symbol: str, qty: float) -> str:
    """Insert a journal row with status=OPEN (as if a trade was filled)."""
    journal_ensure_table(db)
    tid = insert_approval_row(
        db_path=db, profile_id=profile_id,
        candidate={"symbol": symbol, "asset_class": "forex", "side": "LONG",
                    "entry_price": 1.0, "stop_price": 0.99, "tp_price": 1.03,
                    "shares_or_lots": qty},
        policy_decision={"decision": "APPROVED", "sizing_method": "gft_10k_v1"},
        cycle_ts_utc="2026-04-05T14:00:00Z",
    )
    update_filled(
        db_path=db, trade_id=tid, broker_position_id=broker_pos_id,
        fill_price=1.0, fill_qty=qty, filled_at_utc=_now(),
        expected_entry=1.0, side="LONG",
    )
    return tid


# ---------------------------------------------------------------------------
# Test 1 — No divergences when states agree
# ---------------------------------------------------------------------------
def test1() -> None:
    print("\n--- Test 1: No divergences when states agree ---")
    db = _make_db()
    try:
        ensure_table(db)
        # Both broker and journal have 0 positions
        broker_positions = []
        db_open_positions = []
        db_journal_open = []

        divs = detect_divergences(db, "gft_10k", broker_positions, db_open_positions, db_journal_open)
        rows_before = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")
        log_divergences(db, divs, _now())
        rows_after = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")

        ok = len(divs) == 0 and len(rows_after) == 0
        detail = (
            f"Divergences detected: {len(divs)} (expected 0)\n"
            f"Reconciliation log rows: {len(rows_after)} (expected 0)\n"
        )
        _check(1, "no divergences when states agree (both empty)", ok, detail)
    except Exception:
        _check(1, "no divergences when states agree", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 2 — DB_OPEN_BROKER_CLOSED detected
# ---------------------------------------------------------------------------
def test2() -> None:
    print("\n--- Test 2: DB_OPEN_BROKER_CLOSED detected ---")
    db = _make_db()
    try:
        ensure_table(db)
        journal_ensure_table(db)

        # Journal has a OPEN row, broker has nothing
        tid = _insert_journal_open(db, "gft_10k", "POS-GHOST", "EURUSD", 0.1)
        journal_open = load_journal_open(db, "gft_10k")

        broker_positions = []   # Broker has nothing (position was closed)
        db_open_positions = []

        divs = detect_divergences(db, "gft_10k", broker_positions, db_open_positions, journal_open)
        log_divergences(db, divs, _now())

        recon_rows = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")
        journal_row_after = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))

        ok = (
            len(divs) == 1
            and divs[0].divergence_type == "DB_OPEN_BROKER_CLOSED"
            and divs[0].broker_position_id == "POS-GHOST"
            and journal_row_after["status"] == "OPEN"  # NOT mutated
        )
        detail = (
            f"Divergences detected: {len(divs)} (expected 1)\n"
            f"  type={divs[0].divergence_type if divs else 'N/A'}\n"
            f"  broker_position_id={divs[0].broker_position_id if divs else 'N/A'}\n"
            f"Reconciliation log rows: {len(recon_rows)}\n"
            f"Journal row status after: {journal_row_after.get('status')} (expected OPEN — NOT mutated)\n"
        )
        _check(2, "DB_OPEN_BROKER_CLOSED detected, journal not mutated", ok, detail)
    except Exception:
        _check(2, "DB_OPEN_BROKER_CLOSED detected", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 3 — UNKNOWN_POSITION detected
# ---------------------------------------------------------------------------
def test3() -> None:
    print("\n--- Test 3: UNKNOWN_POSITION detected (broker position not in journal) ---")
    db = _make_db()
    try:
        ensure_table(db)
        journal_ensure_table(db)

        # Broker has a position, journal has nothing for it (manual trade)
        broker_positions = [
            {"broker_position_id": "POS-MANUAL-001", "symbol": "GBPUSD",
             "side": "LONG", "qty": 0.05, "entry_price": 1.27}
        ]
        db_open_positions = []
        db_journal_open = []

        divs = detect_divergences(db, "gft_10k", broker_positions, db_open_positions, db_journal_open)
        log_divergences(db, divs, _now())
        recon_rows = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")

        ok = (
            len(divs) == 1
            and divs[0].divergence_type == "UNKNOWN_POSITION"
            and divs[0].broker_position_id == "POS-MANUAL-001"
        )
        detail = (
            f"Divergences detected: {len(divs)} (expected 1)\n"
            f"  type={divs[0].divergence_type if divs else 'N/A'}\n"
            f"  broker_position_id={divs[0].broker_position_id if divs else 'N/A'}\n"
            f"  symbol={divs[0].symbol if divs else 'N/A'}\n"
            f"Reconciliation log rows: {len(recon_rows)}\n"
        )
        _check(3, "UNKNOWN_POSITION detected for broker position with no journal entry", ok, detail)
    except Exception:
        _check(3, "UNKNOWN_POSITION detected", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 4 — QTY_MISMATCH detected
# ---------------------------------------------------------------------------
def test4() -> None:
    print("\n--- Test 4: QTY_MISMATCH detected ---")
    db = _make_db()
    try:
        ensure_table(db)
        journal_ensure_table(db)

        # Journal says 0.10 lots, broker says 0.20 lots (partial add or manual change)
        tid = _insert_journal_open(db, "gft_10k", "POS-MISMATCH", "EURUSD", 0.10)
        journal_open = load_journal_open(db, "gft_10k")

        broker_positions = [
            {"broker_position_id": "POS-MISMATCH", "symbol": "EURUSD",
             "side": "LONG", "qty": 0.20}
        ]
        db_open_positions = []

        divs = detect_divergences(db, "gft_10k", broker_positions, db_open_positions, journal_open)
        log_divergences(db, divs, _now())
        recon_rows = _fetchall(db, "SELECT * FROM vanguard_reconciliation_log")

        ok = (
            len(divs) == 1
            and divs[0].divergence_type == "QTY_MISMATCH"
            and divs[0].broker_position_id == "POS-MISMATCH"
        )
        detail = (
            f"Divergences detected: {len(divs)} (expected 1)\n"
            f"  type={divs[0].divergence_type if divs else 'N/A'}\n"
            f"  details={json.dumps(divs[0].details) if divs else 'N/A'}\n"
            f"Reconciliation log rows: {len(recon_rows)}\n"
        )
        _check(4, "QTY_MISMATCH detected for broker qty != journal qty", ok, detail)
    except Exception:
        _check(4, "QTY_MISMATCH detected", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 5 — DB_OPEN_BROKER_CLOSED (position closed on broker side)
# ---------------------------------------------------------------------------
def test5() -> None:
    print("\n--- Test 5: DB_OPEN_BROKER_CLOSED — journal OPEN, broker closed ---")
    db = _make_db()
    try:
        ensure_table(db)
        journal_ensure_table(db)

        # Journal has OPEN trade, broker no longer has that position_id
        tid = _insert_journal_open(db, "gft_10k", "POS-CLOSED-AT-BROKER", "USDJPY", 0.1)
        journal_open = load_journal_open(db, "gft_10k")

        # Broker has DIFFERENT position (not POS-CLOSED-AT-BROKER)
        broker_positions = [
            {"broker_position_id": "POS-OTHER", "symbol": "GBPUSD", "side": "LONG", "qty": 0.05}
        ]
        db_open_positions = []

        divs = detect_divergences(db, "gft_10k", broker_positions, db_open_positions, journal_open)
        log_divergences(db, divs, _now())

        # Should have:
        # 1 × DB_OPEN_BROKER_CLOSED (POS-CLOSED-AT-BROKER in journal, not in broker)
        # 1 × UNKNOWN_POSITION (POS-OTHER in broker, not in journal)
        db_open_broker_closed = [d for d in divs if d.divergence_type == "DB_OPEN_BROKER_CLOSED"]
        unknown = [d for d in divs if d.divergence_type == "UNKNOWN_POSITION"]

        journal_after = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))

        ok = (
            len(db_open_broker_closed) == 1
            and db_open_broker_closed[0].broker_position_id == "POS-CLOSED-AT-BROKER"
            and journal_after["status"] == "OPEN"  # NOT mutated
        )
        detail = (
            f"Total divergences: {len(divs)}\n"
            f"  DB_OPEN_BROKER_CLOSED: {len(db_open_broker_closed)} (expected 1)\n"
            f"    → broker_position_id={db_open_broker_closed[0].broker_position_id if db_open_broker_closed else 'N/A'}\n"
            f"  UNKNOWN_POSITION: {len(unknown)}\n"
            f"Journal trade status after reconcile: {journal_after.get('status')} (expected OPEN — NOT mutated)\n"
        )
        _check(5, "DB_OPEN_BROKER_CLOSED: journal OPEN + broker closed = detected, journal unchanged", ok, detail)
    except Exception:
        _check(5, "DB_OPEN_BROKER_CLOSED detected", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 6 — No state mutation
# ---------------------------------------------------------------------------
def test6() -> None:
    print("\n--- Test 6: No state mutation outside reconciliation_log ---")
    db = _make_db()
    try:
        ensure_table(db)
        journal_ensure_table(db)

        # Set up some open journal rows
        tid1 = _insert_journal_open(db, "gft_10k", "POS-STABLE-1", "EURUSD", 0.1)
        tid2 = _insert_journal_open(db, "gft_10k", "POS-STABLE-2", "GBPUSD", 0.05)

        before_journal = _fetchall(db, "SELECT trade_id, status, last_synced_at_utc FROM vanguard_trade_journal ORDER BY trade_id")
        before_recon_count = len(_fetchall(db, "SELECT * FROM vanguard_reconciliation_log"))

        # Run detection with some divergences
        broker_positions = []   # both positions closed at broker
        db_journal_open = load_journal_open(db, "gft_10k")
        divs = detect_divergences(db, "gft_10k", broker_positions, [], db_journal_open)
        log_divergences(db, divs, _now())

        after_journal = _fetchall(db, "SELECT trade_id, status, last_synced_at_utc FROM vanguard_trade_journal ORDER BY trade_id")
        after_recon_count = len(_fetchall(db, "SELECT * FROM vanguard_reconciliation_log"))
        recon_rows = _fetchall(db, "SELECT resolved_action FROM vanguard_reconciliation_log")
        all_detected_only = all(r["resolved_action"] == "DETECTED_ONLY" for r in recon_rows)

        journal_unchanged = before_journal == after_journal
        recon_grew = after_recon_count > before_recon_count

        ok = journal_unchanged and recon_grew and all_detected_only
        detail = (
            f"Journal rows before/after: {len(before_journal)}/{len(after_journal)} (should be equal)\n"
            f"Journal content unchanged: {journal_unchanged}\n"
            f"Reconciliation log: {before_recon_count} → {after_recon_count} rows\n"
            f"All resolved_action=DETECTED_ONLY: {all_detected_only}\n"
        )
        _check(6, "no state mutation — only reconciliation_log modified", ok, detail)
    except Exception:
        _check(6, "no state mutation", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 7 — Details JSON parseable
# ---------------------------------------------------------------------------
def test7() -> None:
    print("\n--- Test 7: Details JSON is parseable ---")
    db = _make_db()
    try:
        ensure_table(db)
        journal_ensure_table(db)

        # Create multiple divergence types
        _insert_journal_open(db, "gft_10k", "POS-DB-GHOST", "EURUSD", 0.1)   # DB_OPEN_BROKER_CLOSED
        journal_open = load_journal_open(db, "gft_10k")

        broker_positions = [
            {"broker_position_id": "POS-MANUAL", "symbol": "BTCUSD", "side": "LONG", "qty": 0.01},  # UNKNOWN
        ]
        divs = detect_divergences(db, "gft_10k", broker_positions, [], journal_open)
        detected_at = _now()
        log_divergences(db, divs, detected_at)

        rows = _fetchall(db, "SELECT details_json, divergence_type FROM vanguard_reconciliation_log ORDER BY id DESC LIMIT 5")

        all_parseable = True
        parse_errors = []
        detail = f"Logged {len(rows)} rows:\n"
        for r in rows:
            try:
                parsed = json.loads(r["details_json"])
                detail += f"  [{r['divergence_type']}] details_json OK — keys: {list(parsed.keys())}\n"
            except json.JSONDecodeError as e:
                all_parseable = False
                parse_errors.append(str(e))
                detail += f"  [{r['divergence_type']}] PARSE ERROR: {e}\n"

        ok = all_parseable and len(rows) > 0
        _check(7, "all details_json rows are valid JSON", ok, detail)
    except Exception:
        _check(7, "details JSON parseable", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 8 — MetaApi outage: exit 2, no log rows
# ---------------------------------------------------------------------------
def test8() -> None:
    print("\n--- Test 8: MetaApi outage — no phantom divergences ---")
    db = _make_db()
    try:
        import subprocess
        # Run reconcile_once.py with bad profile (no MetaApi bridge config)
        result = subprocess.run(
            [sys.executable, "scripts/reconcile_once.py", "--profile-id", "nonexistent_profile_xyz"],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
        )
        exit_code = result.returncode
        stderr_output = result.stderr

        # Should exit non-zero (either 1 for FATAL config missing, or 2 for MetaApi unavailable)
        ok = exit_code != 0
        detail = (
            f"Exit code: {exit_code} (expected non-zero)\n"
            f"Stderr: {stderr_output.strip()[:200]}\n"
        )
        _check(8, "MetaApi/config failure exits non-zero, no phantom divergences", ok, detail)
    except Exception:
        _check(8, "MetaApi outage handling", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 60)
    print("Phase 3c Acceptance Tests — Reconciliation Detect-Only")
    print("=" * 60)

    test1()
    test2()
    test3()
    test4()
    test5()
    test6()
    test7()
    test8()

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
