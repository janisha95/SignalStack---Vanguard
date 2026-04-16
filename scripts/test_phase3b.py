#!/usr/bin/env python3
"""
Phase 3b acceptance tests — Trade Journal + Slippage Logging.
Run: python3 scripts/test_phase3b.py

Tests 1-8 per CC_PHASE_3B_TRADE_JOURNAL.md §3.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.execution.trade_journal import (
    JournalStateError,
    VALID_STATUSES,
    ensure_table,
    insert_approval_row,
    update_filled,
    update_rejected_by_broker,
    update_submitted,
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


# ---------------------------------------------------------------------------
# Test 1 — Dry-run cycle populates journal
# ---------------------------------------------------------------------------
def test1() -> None:
    print("\n--- Test 1: Dry-run cycle populates journal ---")
    db = _make_db()
    try:
        ensure_table(db)
        cycle_ts = "2026-04-05T14:00:00Z"
        candidates = [
            {"symbol": "EURUSD", "asset_class": "forex", "side": "LONG",
             "entry_price": 1.08500, "stop_price": 1.08200, "tp_price": 1.09200,
             "shares_or_lots": 0.1},
            {"symbol": "BTCUSD", "asset_class": "crypto", "side": "SHORT",
             "entry_price": 84000.0, "stop_price": 85000.0, "tp_price": 82000.0,
             "shares_or_lots": 0.01},
            {"symbol": "AAPL", "asset_class": "equity", "side": "LONG",
             "entry_price": 185.50, "stop_price": 183.00, "tp_price": 192.00,
             "shares_or_lots": 10},
        ]
        policy_decision = {
            "decision": "APPROVED",
            "reject_reason": None,
            "approved_qty": 0.1,
            "approved_sl": 1.08200,
            "approved_tp": 1.09200,
            "approved_side": "LONG",
            "original_qty": 0.1,
            "sizing_method": "gft_standard_v1",
            "notes": ["edge_ok", "session_ok"],
        }

        for cand in candidates:
            insert_approval_row(
                db_path=db, profile_id="gft_10k",
                candidate=cand, policy_decision=policy_decision,
                cycle_ts_utc=cycle_ts, status="FORWARD_TRACKED",
            )

        rows = _fetchall(
            db,
            "SELECT * FROM vanguard_trade_journal WHERE approved_cycle_ts_utc = ?",
            (cycle_ts,),
        )

        ok = len(rows) == 3
        detail = f"Inserted {len(rows)} rows (expected 3)\n"
        for r in rows:
            detail += (
                f"  trade_id={r['trade_id'][:8]}... symbol={r['symbol']} "
                f"side={r['side']} status={r['status']} "
                f"expected_entry={r['expected_entry']} "
                f"expected_sl={r['expected_sl']} expected_tp={r['expected_tp']}\n"
            )
            if r["status"] != "FORWARD_TRACKED":
                ok = False
                detail += f"  ERROR: status={r['status']} expected FORWARD_TRACKED\n"
            if not r["expected_entry"]:
                ok = False
                detail += f"  ERROR: expected_entry is null for {r['symbol']}\n"
            if not r["approval_reasoning_json"]:
                ok = False
                detail += "  ERROR: approval_reasoning_json is null\n"
        _check(1, "dry-run cycle populates journal", ok, detail)
    except Exception:
        _check(1, "dry-run cycle populates journal", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 2 — Fill populates fill + slippage
# ---------------------------------------------------------------------------
def test2() -> None:
    print("\n--- Test 2: Fill populates fill + slippage ---")
    db = _make_db()
    try:
        ensure_table(db)
        trade_id = insert_approval_row(
            db_path=db, profile_id="gft_10k",
            candidate={"symbol": "EURUSD", "asset_class": "forex", "side": "LONG",
                        "entry_price": 1.08500, "stop_price": 1.08200,
                        "tp_price": 1.09200, "shares_or_lots": 0.1},
            policy_decision={"decision": "APPROVED", "sizing_method": "gft_10k_v1"},
            cycle_ts_utc="2026-04-05T14:00:00Z", status="PENDING_FILL",
        )
        update_submitted(db, trade_id, "ORD-12345", "2026-04-05T14:00:05Z")
        update_filled(
            db_path=db, trade_id=trade_id,
            broker_position_id="POS-99999", fill_price=1.08512, fill_qty=0.1,
            filled_at_utc="2026-04-05T14:00:08Z",
            expected_entry=1.08500, side="LONG",
        )

        row = _fetchone(db, "SELECT * FROM vanguard_trade_journal WHERE trade_id = ?", (trade_id,))
        ok = (
            row["status"] == "OPEN"
            and row["fill_price"] is not None
            and row["slippage_price"] is not None
            and row["broker_order_id"] == "ORD-12345"
        )
        detail = (
            f"trade_id={trade_id[:8]}...\n"
            f"  status={row['status']} (expected OPEN)\n"
            f"  fill_price={row['fill_price']} (expected 1.08512)\n"
            f"  slippage_price={row['slippage_price']}\n"
            f"  slippage_bps={row['slippage_bps']}\n"
            f"  broker_order_id={row['broker_order_id']}\n"
            f"  broker_position_id={row['broker_position_id']}\n"
        )
        _check(2, "fill populates fill + slippage, status=OPEN", ok, detail)
    except Exception:
        _check(2, "fill populates fill + slippage, status=OPEN", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 3 — Slippage sign LONG
# ---------------------------------------------------------------------------
def test3() -> None:
    print("\n--- Test 3: Slippage sign correctness (LONG) ---")
    db = _make_db()
    try:
        ensure_table(db)
        tid = insert_approval_row(
            db_path=db, profile_id="gft_10k",
            candidate={"symbol": "EURUSD", "asset_class": "forex", "side": "LONG",
                        "entry_price": 100.00, "stop_price": 99.00, "tp_price": 102.00,
                        "shares_or_lots": 1.0},
            policy_decision={}, cycle_ts_utc="2026-04-05T14:00:00Z",
        )
        update_filled(
            db_path=db, trade_id=tid,
            broker_position_id="POS-1", fill_price=100.05, fill_qty=1.0,
            filled_at_utc="2026-04-05T14:00:05Z",
            expected_entry=100.00, side="LONG",
        )
        row = _fetchone(
            db, "SELECT slippage_price, slippage_bps FROM vanguard_trade_journal WHERE trade_id=?",
            (tid,),
        )
        slip = row["slippage_price"]
        bps  = row["slippage_bps"]
        expected_slip = 100.05 - 100.00  # +0.05 (worse for long)
        ok = abs(slip - expected_slip) < 1e-9
        detail = (
            f"expected_entry=100.00, fill_price=100.05 → LONG\n"
            f"  slippage_price={slip} (expected +0.05)\n"
            f"  slippage_bps={bps} (expected +5.00)\n"
            f"  sign_ok={slip > 0} (positive = worse for long ✓)\n"
        )
        _check(3, "slippage sign LONG: fill > expected → positive slippage", ok, detail)
    except Exception:
        _check(3, "slippage sign LONG", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 4 — Slippage sign SHORT
# ---------------------------------------------------------------------------
def test4() -> None:
    print("\n--- Test 4: Slippage sign correctness (SHORT) ---")
    db = _make_db()
    try:
        ensure_table(db)

        # SHORT fill at 100.05 → BETTER (higher sell price)
        tid1 = insert_approval_row(
            db_path=db, profile_id="gft_10k",
            candidate={"symbol": "GBPUSD", "asset_class": "forex", "side": "SHORT",
                        "entry_price": 100.00, "stop_price": 101.00, "tp_price": 99.00,
                        "shares_or_lots": 1.0},
            policy_decision={}, cycle_ts_utc="2026-04-05T14:00:00Z",
        )
        update_filled(
            db_path=db, trade_id=tid1,
            broker_position_id="POS-2", fill_price=100.05, fill_qty=1.0,
            filled_at_utc="2026-04-05T14:00:05Z",
            expected_entry=100.00, side="SHORT",
        )

        # SHORT fill at 99.95 → WORSE (lower sell price)
        tid2 = insert_approval_row(
            db_path=db, profile_id="gft_10k",
            candidate={"symbol": "GBPUSD", "asset_class": "forex", "side": "SHORT",
                        "entry_price": 100.00, "stop_price": 101.00, "tp_price": 99.00,
                        "shares_or_lots": 1.0},
            policy_decision={}, cycle_ts_utc="2026-04-05T14:00:00Z",
        )
        update_filled(
            db_path=db, trade_id=tid2,
            broker_position_id="POS-3", fill_price=99.95, fill_qty=1.0,
            filled_at_utc="2026-04-05T14:00:05Z",
            expected_entry=100.00, side="SHORT",
        )

        r1 = _fetchone(db, "SELECT slippage_price FROM vanguard_trade_journal WHERE trade_id=?", (tid1,))
        r2 = _fetchone(db, "SELECT slippage_price FROM vanguard_trade_journal WHERE trade_id=?", (tid2,))
        slip1 = r1["slippage_price"]  # expected - fill = 100.00 - 100.05 = -0.05 (better)
        slip2 = r2["slippage_price"]  # expected - fill = 100.00 - 99.95 = +0.05 (worse)

        ok = abs(slip1 - (-0.05)) < 1e-9 and abs(slip2 - 0.05) < 1e-9
        detail = (
            "SHORT fill at 100.05 (higher → better for short seller):\n"
            f"  slippage_price={slip1} (expected -0.05, negative=better ✓)\n"
            "SHORT fill at 99.95 (lower → worse for short seller):\n"
            f"  slippage_price={slip2} (expected +0.05, positive=worse ✓)\n"
        )
        _check(4, "slippage sign SHORT: correct sign for both better/worse", ok, detail)
    except Exception:
        _check(4, "slippage sign SHORT", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 5 — State precondition enforced
# ---------------------------------------------------------------------------
def test5() -> None:
    print("\n--- Test 5: State precondition enforced ---")
    db = _make_db()
    try:
        ensure_table(db)
        tid = insert_approval_row(
            db_path=db, profile_id="gft_10k",
            candidate={"symbol": "EURUSD", "asset_class": "forex", "side": "LONG",
                        "entry_price": 1.085, "stop_price": 1.082, "tp_price": 1.092,
                        "shares_or_lots": 0.1},
            policy_decision={}, cycle_ts_utc="2026-04-05T14:00:00Z",
        )
        # Force row to CLOSED (simulate slice 3d)
        con = sqlite3.connect(db)
        con.execute("UPDATE vanguard_trade_journal SET status='CLOSED' WHERE trade_id=?", (tid,))
        con.commit()
        con.close()

        raised = False
        try:
            update_filled(
                db_path=db, trade_id=tid,
                broker_position_id="POS-BAD", fill_price=1.08512, fill_qty=0.1,
                filled_at_utc="2026-04-05T14:01:00Z",
                expected_entry=1.085, side="LONG",
            )
        except JournalStateError as e:
            raised = True
            exc_msg = str(e)

        row_after = _fetchone(db, "SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (tid,))
        ok = raised and row_after["status"] == "CLOSED"
        detail = (
            f"JournalStateError raised: {raised}\n"
            f"Row status after failed call: {row_after['status']} (unchanged ✓)\n"
        )
        if raised:
            detail += f"Exception message: {exc_msg}\n"
        _check(5, "state precondition: update_filled on CLOSED raises JournalStateError", ok, detail)
    except Exception:
        _check(5, "state precondition enforced", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 6 — No duplicate trade_ids
# ---------------------------------------------------------------------------
def test6() -> None:
    print("\n--- Test 6: No duplicate trade_ids ---")
    db = _make_db()
    try:
        ensure_table(db)
        for i in range(30):
            insert_approval_row(
                db_path=db, profile_id="gft_10k",
                candidate={"symbol": f"SYM{i:03d}", "asset_class": "forex", "side": "LONG",
                            "entry_price": 1.00, "stop_price": 0.99, "tp_price": 1.03,
                            "shares_or_lots": 0.1},
                policy_decision={}, cycle_ts_utc="2026-04-05T14:00:00Z",
                status="FORWARD_TRACKED",
            )

        dups = _fetchall(
            db,
            "SELECT trade_id, COUNT(*) AS c FROM vanguard_trade_journal GROUP BY trade_id HAVING c > 1",
        )
        con = sqlite3.connect(db)
        total = con.execute("SELECT COUNT(*) FROM vanguard_trade_journal").fetchone()[0]
        con.close()

        ok = len(dups) == 0 and total == 30
        detail = (
            f"Inserted: 30 rows\n"
            f"Total in DB: {total}\n"
            f"Duplicate trade_ids: {len(dups)} (expected 0)\n"
        )
        _check(6, "no duplicate trade_ids across 30 inserts", ok, detail)
    except Exception:
        _check(6, "no duplicate trade_ids", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 7 — slippage_bps calculation
# ---------------------------------------------------------------------------
def test7() -> None:
    print("\n--- Test 7: slippage_bps calculation ---")
    db = _make_db()
    try:
        ensure_table(db)
        expected_entry = 1.08500
        fill_price     = 1.08512

        tid = insert_approval_row(
            db_path=db, profile_id="gft_10k",
            candidate={"symbol": "EURUSD", "asset_class": "forex", "side": "LONG",
                        "entry_price": expected_entry, "stop_price": 1.082, "tp_price": 1.092,
                        "shares_or_lots": 0.1},
            policy_decision={}, cycle_ts_utc="2026-04-05T14:00:00Z",
        )
        update_filled(
            db_path=db, trade_id=tid,
            broker_position_id="POS-7", fill_price=fill_price, fill_qty=0.1,
            filled_at_utc="2026-04-05T14:00:05Z",
            expected_entry=expected_entry, side="LONG",
        )
        row = _fetchone(
            db,
            "SELECT slippage_price, slippage_bps FROM vanguard_trade_journal WHERE trade_id=?",
            (tid,),
        )
        slip_price = row["slippage_price"]
        slip_bps   = row["slippage_bps"]
        expected_slip = fill_price - expected_entry
        expected_bps  = expected_slip / expected_entry * 10000.0

        ok = abs(slip_bps - expected_bps) < 0.001
        detail = (
            f"expected_entry={expected_entry}, fill_price={fill_price}\n"
            f"  slippage_price={slip_price:.6f} (expected {expected_slip:.6f})\n"
            f"  slippage_bps={slip_bps:.4f} (expected {expected_bps:.4f})\n"
            f"  formula check: {slip_price:.6f} / {expected_entry} * 10000 = {slip_bps:.4f}\n"
        )
        _check(7, "slippage_bps = slippage_price / expected_entry * 10000", ok, detail)
    except Exception:
        _check(7, "slippage_bps calculation", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 8 — Reasoning JSON round-trip
# ---------------------------------------------------------------------------
def test8() -> None:
    print("\n--- Test 8: Reasoning JSON round-trip ---")
    db = _make_db()
    try:
        ensure_table(db)
        policy_decision = {
            "decision": "APPROVED",
            "reject_reason": None,
            "approved_qty": 0.1,
            "approved_sl": 1.08200,
            "approved_tp": 1.09200,
            "approved_side": "LONG",
            "original_qty": 0.1,
            "sizing_method": "gft_10k_v1",
            "notes": ["edge_ok", "session_ok", "drawdown_ok"],
        }
        tid = insert_approval_row(
            db_path=db, profile_id="gft_10k",
            candidate={"symbol": "EURUSD", "asset_class": "forex", "side": "LONG",
                        "entry_price": 1.085, "stop_price": 1.082, "tp_price": 1.092,
                        "shares_or_lots": 0.1},
            policy_decision=policy_decision,
            cycle_ts_utc="2026-04-05T14:00:00Z", status="FORWARD_TRACKED",
        )
        row = _fetchone(
            db,
            "SELECT approval_reasoning_json FROM vanguard_trade_journal WHERE trade_id=?",
            (tid,),
        )
        raw_json = row["approval_reasoning_json"]
        parsed = json.loads(raw_json)

        ok = (
            parsed.get("decision") == "APPROVED"
            and parsed.get("sizing_method") == "gft_10k_v1"
            and isinstance(parsed.get("notes"), list)
            and len(parsed["notes"]) == 3
        )
        detail = f"approval_reasoning_json (parsed):\n  {json.dumps(parsed, indent=2)}\n"
        _check(8, "reasoning JSON round-trip parses with PolicyDecision fields", ok, detail)
    except Exception:
        _check(8, "reasoning JSON round-trip", False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 60)
    print("Phase 3b Acceptance Tests — Trade Journal")
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
