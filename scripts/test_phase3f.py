"""
test_phase3f.py — Phase 3f acceptance tests: Auto-close at max_holding_minutes.

All tests use in-memory SQLite and mock MetaApiClient. No real broker calls.
Tests 1-11 match the acceptance criteria in CC_PHASE_3F_AUTO_CLOSE.md §3.

Run: python3 scripts/test_phase3f.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.execution.auto_close import AutoCloseManager, _holding_minutes
from vanguard.execution.trade_journal import (
    ensure_table as journal_ensure_table,
    insert_approval_row,
    update_filled,
    update_journal_closed_from_auto,
    VALID_STATUSES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PASS  = "\033[92mPASS\033[0m"
FAIL  = "\033[91mFAIL\033[0m"
WARN  = "\033[93mWARN\033[0m"

results: list[tuple[int, str, str]] = []


def report(n: int, name: str, ok: bool, detail: str = "") -> None:
    tag = PASS if ok else FAIL
    print(f"  [{tag}] Test {n}: {name}")
    if detail:
        print(f"         {detail}")
    results.append((n, name, "PASS" if ok else "FAIL"))


def _make_db() -> str:
    """Create a temp SQLite DB with journal table. Returns path string."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    journal_ensure_table(tmp.name)
    return tmp.name


def _ts(minutes_ago: float = 0.0, offset_minutes: float = 0.0) -> str:
    """Return ISO UTC timestamp N minutes in the past (or future for negative N)."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago - offset_minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_position(
    broker_position_id: str = "pos_001",
    symbol: str = "USDJPY.x",
    side: str = "LONG",
    minutes_held: float = 0.0,
    entry_price: float = 100.0,
) -> dict:
    return {
        "broker_position_id": broker_position_id,
        "symbol":             symbol,
        "side":               side,
        "qty":                0.01,
        "entry_price":        entry_price,
        "current_sl":         99.0 if side == "LONG" else 101.0,
        "current_tp":         102.0 if side == "LONG" else 98.0,
        "opened_at_utc":      _ts(minutes_ago=minutes_held),
        "unrealized_pnl":     0.0,
    }


def _policy(auto_close: bool = True, max_minutes: int = 240) -> dict:
    return {
        "position_limits": {
            "max_open_positions": 2,
            "block_duplicate_symbols": True,
            "max_holding_minutes": max_minutes,
            "auto_close_after_max_holding": auto_close,
        }
    }


def _mock_client(
    positions: list[dict] | None = None,
    close_result: dict | None = None,
    close_raises: Exception | None = None,
) -> Any:
    """Build a mock MetaApiClient."""
    client = MagicMock()
    client.get_open_positions = AsyncMock(return_value=positions if positions is not None else [])
    if close_raises is not None:
        client.close_position = AsyncMock(side_effect=close_raises)
    else:
        default_close = {"status": "closed", "position_id": "pos_001",
                         "close_price": 101.5, "close_time": _ts(), "realized_pnl": None}
        client.close_position = AsyncMock(return_value=close_result or default_close)
    return client


def _insert_open_journal_row(db: str, broker_position_id: str, entry_price: float = 100.0,
                              sl: float = 99.0, tp: float = 102.0, side: str = "LONG") -> str:
    """Insert an OPEN journal row (insert then fill)."""
    trade_id = insert_approval_row(
        db_path=db,
        profile_id="gft_10k_qa",
        candidate={
            "symbol": "USDJPY.x",
            "asset_class": "forex",
            "side": side,
            "entry_price": entry_price,
            "expected_sl": sl,
            "expected_tp": tp,
            "approved_qty": 0.01,
        },
        policy_decision={"reason": "test"},
        cycle_ts_utc=_ts(),
        status="PENDING_FILL",
    )
    update_filled(
        db_path=db,
        trade_id=trade_id,
        broker_position_id=broker_position_id,
        fill_price=entry_price,
        fill_qty=0.01,
        filled_at_utc=_ts(),
        expected_entry=entry_price,
        side=side,
    )
    return trade_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_1_below_threshold_no_close() -> None:
    """Test 1: Position held 2min, threshold=240min → no close."""
    db = _make_db()
    telegram_msgs = []

    pos = _open_position(minutes_held=2.0)
    client = _mock_client(positions=[pos])

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=240),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    close_called = client.close_position.called
    ok = (not close_called) and (len(results_ac) == 0)
    initiated = [m for m in telegram_msgs if "INITIATED" in m]
    ok = ok and (len(initiated) == 0)
    report(1, "Below threshold: no close", ok,
           f"close_called={close_called} results={results_ac} telegram={telegram_msgs}")


async def test_2_above_threshold_close_fires() -> None:
    """Test 2: Position held 5min, threshold=3min → close fires, journal updated."""
    db = _make_db()
    trade_id = _insert_open_journal_row(db, "pos_001")
    telegram_msgs = []

    pos = _open_position(broker_position_id="pos_001", minutes_held=5.0)
    close_r = {"status": "closed", "position_id": "pos_001",
                "close_price": 101.5, "close_time": _ts(), "realized_pnl": None}
    client = _mock_client(positions=[pos], close_result=close_r)

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    # Verify close called
    close_called = client.close_position.called

    # Verify Telegram INITIATED + SUCCESS
    initiated_msgs = [m for m in telegram_msgs if "INITIATED" in m]
    success_msgs   = [m for m in telegram_msgs if "SUCCESS" in m]

    # Verify journal row updated to CLOSED
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT status, close_reason FROM vanguard_trade_journal WHERE trade_id=?",
        (trade_id,)
    ).fetchone()
    con.close()

    journal_closed = row is not None and row["status"] == "CLOSED"
    journal_reason = row["close_reason"] if row else None

    ok = (
        close_called
        and len(results_ac) == 1
        and results_ac[0][1]["status"] == "CLOSED"
        and len(initiated_msgs) >= 1
        and len(success_msgs) >= 1
        and journal_closed
        and journal_reason == "AUTO_CLOSED_MAX_HOLD"
    )
    report(2, "Above threshold: close fires + journal updated", ok,
           f"close={close_called} results={[(r[0], r[1].get('status')) for r in results_ac]} "
           f"telegram_initiated={len(initiated_msgs)} telegram_success={len(success_msgs)} "
           f"journal_status={row['status'] if row else None} reason={journal_reason}")


async def test_3_policy_disabled_no_close() -> None:
    """Test 3: auto_close_after_max_holding=False → no close even if over threshold."""
    db = _make_db()
    telegram_msgs = []

    pos = _open_position(minutes_held=10.0)
    client = _mock_client(positions=[pos])

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=False, max_minutes=3),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    ok = (not client.close_position.called) and (len(results_ac) == 0) and (len(telegram_msgs) == 0)
    report(3, "Policy disabled: no close", ok,
           f"close_called={client.close_position.called} results={results_ac}")


async def test_4_already_closed_skipped() -> None:
    """Test 4: Position over threshold but no longer in broker positions → SKIPPED_ALREADY_CLOSED."""
    db = _make_db()
    telegram_msgs = []

    pos = _open_position(broker_position_id="pos_gone", minutes_held=5.0)
    # get_open_positions returns [] (position already closed)
    client = _mock_client(positions=[])

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    # close_position should NOT have been called (already gone)
    ok = (not client.close_position.called)
    if results_ac:
        ok = ok and results_ac[0][1]["status"] == "SKIPPED_ALREADY_CLOSED"
    initiated_msgs = [m for m in telegram_msgs if "INITIATED" in m]
    # No INITIATED should be sent before we detect it's gone
    ok = ok and len(initiated_msgs) == 0
    report(4, "Pre-close sanity: already closed → SKIPPED_ALREADY_CLOSED", ok,
           f"close_called={client.close_position.called} results={[(r[0], r[1].get('status')) for r in results_ac]} "
           f"telegram={telegram_msgs}")


async def test_5_close_failure_sends_failed_alert() -> None:
    """Test 5: close_position() raises → FAILED Telegram sent, journal NOT updated, failures=1."""
    db = _make_db()
    trade_id = _insert_open_journal_row(db, "pos_fail")
    telegram_msgs = []

    pos = _open_position(broker_position_id="pos_fail", minutes_held=5.0)
    client = _mock_client(positions=[pos], close_raises=RuntimeError("broker rejected"))

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    initiated_msgs = [m for m in telegram_msgs if "INITIATED" in m]
    failed_msgs    = [m for m in telegram_msgs if "FAILED" in m]

    # Journal should still be OPEN
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (trade_id,)).fetchone()
    con.close()

    journal_still_open = row is not None and row["status"] == "OPEN"

    ok = (
        len(initiated_msgs) >= 1
        and len(failed_msgs) >= 1
        and journal_still_open
        and mgr._consecutive_failures == 1
    )
    report(5, "Close failure: FAILED alert sent, journal stays OPEN, failures=1", ok,
           f"initiated={len(initiated_msgs)} failed={len(failed_msgs)} "
           f"journal_status={row['status'] if row else None} "
           f"consecutive_failures={mgr._consecutive_failures}")


async def test_6_circuit_breaker_trips_after_3_failures() -> None:
    """Test 6: 3 consecutive failures → breaker trips + CIRCUIT_BREAKER_TRIPPED Telegram."""
    db = _make_db()
    telegram_msgs = []

    def make_pos(i: int) -> dict:
        return _open_position(broker_position_id=f"pos_{i}", minutes_held=5.0)

    positions = [make_pos(i) for i in range(4)]
    client = _mock_client(
        positions=positions,
        close_raises=RuntimeError("forced failure"),
    )

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    # Run 3 iterations to accumulate failures (1 close attempt per iteration for first 3 positions)
    for i in range(3):
        pos_list = [positions[i]]
        await mgr.check_and_close(
            profile={"id": "gft_10k_qa"},
            policy=_policy(auto_close=True, max_minutes=3),
            broker_positions=pos_list,
            now_utc=_ts(),
        )

    breaker_tripped = mgr._breaker_tripped
    breaker_msgs = [m for m in telegram_msgs if "CIRCUIT_BREAKER_TRIPPED" in m]

    # 4th position should be skipped (breaker tripped)
    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=[positions[3]],
        now_utc=_ts(),
    )

    ok = (
        breaker_tripped
        and len(breaker_msgs) >= 1
        and len(results_ac) == 0  # breaker prevents 4th close attempt
    )

    # Test breaker reset
    import tempfile, os
    Path("/tmp/autoclose_breaker_reset").touch()
    # Simulate daemon checking the flag
    reset_file = Path("/tmp/autoclose_breaker_reset")
    if reset_file.exists():
        reset_file.unlink()
        mgr.reset_breaker()

    breaker_cleared = not mgr._breaker_tripped and mgr._consecutive_failures == 0

    ok = ok and breaker_cleared
    report(6, "Circuit breaker trips at 3 failures, reset script works", ok,
           f"breaker_tripped={breaker_tripped} breaker_msgs={len(breaker_msgs)} "
           f"4th_skipped={len(results_ac)==0} breaker_cleared={breaker_cleared}")


async def test_7_rate_limit_enforced() -> None:
    """Test 7: 5 positions all over threshold simultaneously → at most 3 closes in first pass."""
    db = _make_db()
    telegram_msgs = []

    positions = [
        _open_position(broker_position_id=f"pos_{i}", minutes_held=5.0)
        for i in range(5)
    ]
    # get_open_positions returns all positions (sanity check passes for all)
    client = _mock_client(positions=positions)

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=positions,
        now_utc=_ts(),
    )

    n_closed = len([r for r in results_ac if r[1].get("status") == "CLOSED"])
    ok = n_closed <= AutoCloseManager._RATE_LIMIT_MAX
    report(7, f"Rate limit: at most {AutoCloseManager._RATE_LIMIT_MAX} closes per pass", ok,
           f"n_closed={n_closed} (max={AutoCloseManager._RATE_LIMIT_MAX}) results={[(r[0], r[1].get('status')) for r in results_ac]}")


async def test_8_in_flight_lock_prevents_double_close() -> None:
    """Test 8: In-flight lock prevents re-issuing close for same position."""
    db = _make_db()
    close_call_count = 0

    async def slow_close(position_id, reason="AUTO_CLOSE"):
        nonlocal close_call_count
        close_call_count += 1
        await asyncio.sleep(0.05)  # simulate slow close
        return {"status": "closed", "position_id": position_id,
                "close_price": 101.5, "close_time": _ts(), "realized_pnl": None}

    pos = _open_position(broker_position_id="pos_lock", minutes_held=5.0)
    client = MagicMock()
    client.get_open_positions = AsyncMock(return_value=[pos])
    client.close_position = slow_close

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: None,
        metaapi_clients={"gft_10k_qa": client},
    )

    # Manually add to in-flight to simulate a close in progress
    mgr._in_flight.add(("gft_10k_qa", "pos_lock"))

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    ok = close_call_count == 0 and len(results_ac) == 0
    report(8, "In-flight lock prevents double-close", ok,
           f"close_calls={close_call_count} results={results_ac}")


async def test_9_realized_rrr_correctness() -> None:
    """Test 9: realized_rrr computed correctly for LONG and SHORT positions."""
    db = _make_db()

    # LONG: entry=100, sl=99, tp=102, close=101.5
    # realized_rrr = |101.5-100| / |99-100| = 1.5/1 = 1.5, sign=+1 (profitable)
    trade_id_long = _insert_open_journal_row(
        db, "pos_long", entry_price=100.0, sl=99.0, tp=102.0, side="LONG"
    )
    update_journal_closed_from_auto(
        db_path=db,
        broker_position_id="pos_long",
        close_price=101.5,
        closed_at_utc=_ts(),
        close_reason="AUTO_CLOSED_MAX_HOLD",
        realized_pnl=None,
        holding_minutes=5,
    )

    # SHORT: entry=100, sl=101, tp=98, close=98.5
    # realized_rrr = |98.5-100| / |101-100| = 1.5/1 = 1.5, sign=+1 (profitable)
    trade_id_short = _insert_open_journal_row(
        db, "pos_short", entry_price=100.0, sl=101.0, tp=98.0, side="SHORT"
    )
    update_journal_closed_from_auto(
        db_path=db,
        broker_position_id="pos_short",
        close_price=98.5,
        closed_at_utc=_ts(),
        close_reason="AUTO_CLOSED_MAX_HOLD",
        realized_pnl=None,
        holding_minutes=5,
    )

    # LONG losing: close=98.5 (below entry 100, sl at 99 → close is below sl → worse than sl)
    trade_id_long_loss = _insert_open_journal_row(
        db, "pos_long_loss", entry_price=100.0, sl=99.0, tp=102.0, side="LONG"
    )
    update_journal_closed_from_auto(
        db_path=db,
        broker_position_id="pos_long_loss",
        close_price=98.5,
        closed_at_utc=_ts(),
        close_reason="AUTO_CLOSED_MAX_HOLD",
        realized_pnl=None,
        holding_minutes=5,
    )

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = {
        r["trade_id"]: r
        for r in con.execute(
            "SELECT trade_id, realized_rrr, status FROM vanguard_trade_journal"
        ).fetchall()
    }
    con.close()

    rrr_long       = rows[trade_id_long]["realized_rrr"]
    rrr_short      = rows[trade_id_short]["realized_rrr"]
    rrr_long_loss  = rows[trade_id_long_loss]["realized_rrr"]

    ok_long      = rrr_long is not None and abs(rrr_long - 1.5) < 0.001
    ok_short     = rrr_short is not None and abs(rrr_short - 1.5) < 0.001
    ok_long_loss = rrr_long_loss is not None and rrr_long_loss < 0  # loss → negative rrr

    ok = ok_long and ok_short and ok_long_loss
    report(9, "realized_rrr correctness: LONG=1.5, SHORT=1.5, LONG_loss<0", ok,
           f"rrr_long={rrr_long:.4f} rrr_short={rrr_short:.4f} rrr_long_loss={rrr_long_loss:.4f}")


async def test_10_telegram_failure_doesnt_block_close() -> None:
    """Test 10: Telegram raises → close still proceeds, journal still updates."""
    db = _make_db()
    trade_id = _insert_open_journal_row(db, "pos_tg_fail")

    def broken_telegram(msg):
        raise ConnectionError("Telegram is down")

    pos = _open_position(broker_position_id="pos_tg_fail", minutes_held=5.0)
    close_r = {"status": "closed", "position_id": "pos_tg_fail",
                "close_price": 101.5, "close_time": _ts(), "realized_pnl": None}
    client = _mock_client(positions=[pos], close_result=close_r)

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=broken_telegram,
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    close_called = client.close_position.called

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT status FROM vanguard_trade_journal WHERE trade_id=?", (trade_id,)).fetchone()
    con.close()

    ok = (
        close_called
        and len(results_ac) == 1
        and results_ac[0][1]["status"] == "CLOSED"
        and row is not None
        and row["status"] == "CLOSED"
    )
    report(10, "Telegram failure doesn't block close or journal update", ok,
           f"close_called={close_called} result_status={results_ac[0][1].get('status') if results_ac else None} "
           f"journal_status={row['status'] if row else None}")


async def test_11_recon_row_auto_closes() -> None:
    """Test 11: recon_* journal row (OPEN) still auto-closes and updates to CLOSED."""
    db = _make_db()

    # Simulate a reconciler-created row (broker_position_id=recon_pos_001, status=OPEN)
    # We do this by inserting with a special trade_id pattern
    recon_trade_id = "recon_pos_011_reconciled"
    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT INTO vanguard_trade_journal (
            trade_id, profile_id, symbol, asset_class, side,
            approved_cycle_ts_utc, approved_qty, expected_entry,
            expected_sl, expected_tp, approval_reasoning_json,
            broker_position_id, fill_price, fill_qty, filled_at_utc,
            status, last_synced_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """,
        (
            recon_trade_id, "gft_10k_qa", "EURUSD.x", "forex", "LONG",
            _ts(), 0.01, 105.0, 104.0, 107.0, "{}",
            "recon_pos_011", 105.0, 0.01, _ts(),
            _ts(),
        ),
    )
    con.commit()
    con.close()

    telegram_msgs = []
    pos = _open_position(broker_position_id="recon_pos_011", symbol="EURUSD.x",
                          minutes_held=5.0, entry_price=105.0)
    close_r = {"status": "closed", "position_id": "recon_pos_011",
                "close_price": 106.0, "close_time": _ts(), "realized_pnl": None}
    client = _mock_client(positions=[pos], close_result=close_r)

    mgr = AutoCloseManager(
        config={},
        db_path=db,
        telegram_fn=lambda m: telegram_msgs.append(m),
        metaapi_clients={"gft_10k_qa": client},
    )

    results_ac = await mgr.check_and_close(
        profile={"id": "gft_10k_qa"},
        policy=_policy(auto_close=True, max_minutes=3),
        broker_positions=[pos],
        now_utc=_ts(),
    )

    con2 = sqlite3.connect(db)
    con2.row_factory = sqlite3.Row
    row = con2.execute(
        "SELECT status, close_reason FROM vanguard_trade_journal WHERE trade_id=?",
        (recon_trade_id,)
    ).fetchone()
    con2.close()

    ok = (
        client.close_position.called
        and len(results_ac) == 1
        and results_ac[0][1]["status"] == "CLOSED"
        and row is not None
        and row["status"] == "CLOSED"
    )
    report(11, "Recon row (recon_*) auto-closes and journal updates to CLOSED", ok,
           f"close_called={client.close_position.called} result={results_ac[0][1].get('status') if results_ac else None} "
           f"journal_status={row['status'] if row else None} reason={row['close_reason'] if row else None}")


# ---------------------------------------------------------------------------
# Additional unit tests for helper functions
# ---------------------------------------------------------------------------

async def test_holding_minutes_helper() -> None:
    """Quick unit test: _holding_minutes accuracy."""
    now_dt  = datetime.now(timezone.utc)
    open_dt = now_dt - timedelta(minutes=7)
    now_s   = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    open_s  = open_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    held    = _holding_minutes(open_s, now_s)
    ok      = 6.9 < held < 7.1
    report(0, "_holding_minutes helper (7min)", ok, f"held={held:.3f}min")


async def test_get_policy_for_profile_from_daemon() -> None:
    """Quick test: daemon's _get_policy_for_profile reads policy_id from bridge_cfg."""
    from vanguard.execution.lifecycle_daemon import LifecycleDaemon

    config = {
        "execution": {
            "bridges": {
                "metaapi_gft_10k_qa": {
                    "account_id": "x",
                    "api_token": "y",
                    "policy_id": "gft_10k_v1",
                }
            }
        },
        "policy_templates": {
            "gft_10k_v1": {
                "position_limits": {
                    "max_holding_minutes": 240,
                    "auto_close_after_max_holding": True,
                }
            }
        },
        "database": {
            "source_db_path": "/tmp/x.db",
            "shadow_db_path": "/tmp/x.db",
        },
        "profiles": [],
    }

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name

    try:
        daemon = LifecycleDaemon(config=config, db_path=db, interval_s=60)
    except Exception:
        # MetaApi SDK may not be importable; that's OK for this config test
        report(0, "daemon._get_policy_for_profile (config test)", False, traceback.format_exc())
        return

    profile = {"id": "gft_10k_qa", "bridge_key": "metaapi_gft_10k_qa", "bridge_cfg": {"account_id": "x", "api_token": "y", "policy_id": "gft_10k_v1"}}
    policy  = daemon._get_policy_for_profile(profile)
    ok = policy is not None and policy.get("position_limits", {}).get("max_holding_minutes") == 240
    report(0, "daemon._get_policy_for_profile reads policy from bridge_cfg", ok,
           f"policy_position_limits={policy.get('position_limits') if policy else None}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def _main() -> None:
    print("\n" + "=" * 60)
    print(" Phase 3f — Auto-Close Acceptance Tests")
    print("=" * 60)

    # Extra helper tests (numbered 0 = bonus, not in §3)
    print("\n[Bonus] Helper unit tests:")
    await test_holding_minutes_helper()
    await test_get_policy_for_profile_from_daemon()

    print("\n[§3] Acceptance tests:")
    tests = [
        test_1_below_threshold_no_close,
        test_2_above_threshold_close_fires,
        test_3_policy_disabled_no_close,
        test_4_already_closed_skipped,
        test_5_close_failure_sends_failed_alert,
        test_6_circuit_breaker_trips_after_3_failures,
        test_7_rate_limit_enforced,
        test_8_in_flight_lock_prevents_double_close,
        test_9_realized_rrr_correctness,
        test_10_telegram_failure_doesnt_block_close,
        test_11_recon_row_auto_closes,
    ]

    for test_fn in tests:
        try:
            await test_fn()
        except Exception as exc:
            n = int(test_fn.__name__.split("_")[1])
            name = test_fn.__name__
            print(f"  [{FAIL}] Test {n}: {name}")
            print(f"         EXCEPTION: {exc}")
            traceback.print_exc()
            results.append((n, name, "FAIL"))

    print("\n" + "=" * 60)
    passed = [r for r in results if r[2] == "PASS"]
    failed = [r for r in results if r[2] == "FAIL"]
    print(f" Results: {len(passed)}/{len(results)} passed")
    if failed:
        print(f" FAILED: {[r[1] for r in failed]}")
    print("=" * 60 + "\n")
    return len(failed) == 0


if __name__ == "__main__":
    ok = asyncio.run(_main())
    sys.exit(0 if ok else 1)
