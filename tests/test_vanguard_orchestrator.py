"""
test_vanguard_orchestrator.py — 14 tests for V7 VanguardOrchestrator.

Tests:
  1.  Session start/stop lifecycle
  2.  is_within_session() ET time detection
  3.  _wait_for_next_bar() sleep calculation
  4.  run_cycle() handles 0 V5 candidates gracefully
  5.  run_cycle() handles V5 no_features gracefully
  6.  run_cycle() handles all V6 rejected (0 approved)
  7.  execute() off mode → FORWARD_TRACKED to execution_log
  8.  execute() paper mode → calls signalstack_adapter
  9.  _wind_down() triggers EOD flatten for intraday accounts
  10. _wind_down() skips flatten for swing accounts (must_close_eod=False)
  11. 3 consecutive failures trigger session abort
  12. Session log written to vanguard_session_log
  13. Telegram alerts sent (mocked)
  14. Config-driven timing (not hardcoded)

Location: ~/SS/Vanguard/tests/test_vanguard_orchestrator.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from stages.vanguard_orchestrator import (
    VanguardOrchestrator,
    _ensure_execution_log,
    _get_approved_rows,
    _write_execution_row,
    _parse_hhmm,
    DEFAULT_SESSION_START,
    DEFAULT_SESSION_END,
    DEFAULT_EOD_FLATTEN,
    MAX_CONSECUTIVE_FAILS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_et(hour: int, minute: int, weekday_offset: int = 0) -> datetime:
    """Create a fake ET datetime. Base is 2026-03-02 (Monday)."""
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    base = datetime(2026, 3, 2, hour, minute, 0, tzinfo=ET)
    return base + timedelta(days=weekday_offset)


def _make_utc(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 3, 2, hour, minute, second, tzinfo=timezone.utc)


@pytest.fixture
def tmp_db(tmp_path):
    """Temporary SQLite DB with required tables pre-created."""
    db_path = str(tmp_path / "test_vanguard_universe.db")
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS account_profiles (
                id TEXT PRIMARY KEY, name TEXT, prop_firm TEXT, account_type TEXT,
                account_size REAL, daily_loss_limit REAL, max_drawdown REAL,
                max_positions INTEGER, must_close_eod INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_tradeable_portfolio (
                cycle_ts_utc TEXT, account_id TEXT, symbol TEXT, direction TEXT,
                status TEXT, shares_or_lots REAL DEFAULT 100,
                entry_price REAL, stop_loss REAL, take_profit REAL,
                stop_price REAL, tp_price REAL,
                tier TEXT, risk_dollars REAL, risk_pct REAL,
                rejection_reason TEXT, feature_scores TEXT
            )
        """)
        con.commit()
    _ensure_execution_log(db_path)
    return db_path


@pytest.fixture(autouse=True)
def _disable_real_telegram(monkeypatch):
    """Prevent orchestrator tests from hitting the live Telegram transport."""
    monkeypatch.setattr(VanguardOrchestrator, "_init_telegram", lambda self: None)


# ---------------------------------------------------------------------------
# Test 1: Session start/stop lifecycle
# ---------------------------------------------------------------------------


def test_session_lifecycle(tmp_db):
    """Startup initialises DB tables and session log. Wind-down writes 'complete'."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._startup()

    assert orch._session_date == "2026-03-02"
    assert orch._session_start_ts is not None

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT status FROM vanguard_session_log WHERE date = '2026-03-02'"
        ).fetchone()
    assert row is not None
    assert row[0] == "running"

    orch._wind_down()

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT status FROM vanguard_session_log WHERE date = '2026-03-02'"
        ).fetchone()
    assert row[0] == "complete"


# ---------------------------------------------------------------------------
# Test 2: is_within_session() ET time detection
# ---------------------------------------------------------------------------


def test_is_within_session_in_window(tmp_db):
    """Global session is active when any enabled asset-class session is open."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    assert orch.is_within_session(_make_et(10, 0)) is True
    assert orch.is_within_session(_make_et(9, 34)) is True      # forex/crypto active
    assert orch.is_within_session(_make_et(15, 25)) is True     # forex/crypto active
    assert orch.is_within_session(_make_et(16, 0)) is True      # forex/crypto active


def test_is_within_session_weekend(tmp_db):
    """Weekend handling remains active when at least one asset class is open."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    saturday = _make_et(10, 0, weekday_offset=5)   # Mon + 5 = Sat
    assert orch.is_within_session(saturday) is True


# ---------------------------------------------------------------------------
# Test 3: _wait_for_next_bar() sleep calculation
# ---------------------------------------------------------------------------


def test_wait_for_next_bar_duration(tmp_db):
    """
    At 10:02:30 UTC, next 5m bar = 10:05, + 10s buffer = 10:05:10.
    Expected total sleep ≈ 160s.
    """
    utc_now = datetime(2026, 3, 2, 10, 2, 30, tzinfo=timezone.utc)

    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: utc_now,
    )

    slept: list[float] = []
    # Calls in order: deadline calc(0.0), while-cond(0.0), min-calc(0.0), while-cond-exit(10000.0)
    monotonic_calls = iter([0.0, 0.0, 0.0, 10000.0])

    with patch("time.sleep", side_effect=lambda s: slept.append(s)), \
         patch("time.monotonic", side_effect=monotonic_calls):
        orch._wait_for_next_bar()

    total = sum(slept)
    # min(5.0, 160.0 - 0.0) = 5.0 → one sleep of 5s (loop exits on next check)
    # total sleep ≤ 5s but the *requested* sleep duration must be ≤ actual gap
    # Verify the single sleep chunk is ≤ 5.0 and > 0
    assert len(slept) == 1
    assert 0 < slept[0] <= 5.0


def test_get_approved_rows_scopes_to_requested_cycle(tmp_db):
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            """
            INSERT INTO vanguard_tradeable_portfolio
            (cycle_ts_utc, account_id, symbol, direction, status, shares_or_lots, entry_price)
            VALUES
            ('2026-03-02T15:00:00Z', 'ftmo_demo_100k', 'BTCUSD', 'LONG', 'APPROVED', 1, 100.0),
            ('2026-03-02T15:05:00Z', 'ftmo_demo_100k', 'ETHUSD', 'LONG', 'APPROVED', 1, 200.0)
            """
        )
        con.commit()

    rows = _get_approved_rows(tmp_db, "2026-03-02T15:00:00Z")

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSD"
    assert rows[0]["cycle_ts_utc"] == "2026-03-02T15:00:00Z"


# ---------------------------------------------------------------------------
# Test 4: run_cycle() handles 0 V5 candidates (rows=0) gracefully
# ---------------------------------------------------------------------------


def test_run_cycle_zero_v5_candidates(tmp_db):
    """V5 returns rows=0 → run_cycle returns no_candidates, V6 not called."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    v2_mock = MagicMock(return_value=[{"symbol": "AAPL", "asset_class": "equity", "status": "ACTIVE"}])
    v3_mock = MagicMock(return_value=[])
    v4b_mock = MagicMock(return_value={"status": "ok", "rows": 1})
    v5_mock = MagicMock(return_value={"status": "no_candidates", "rows": 0})
    v6_mock = MagicMock(return_value={"status": "ok", "rows": 0})

    with patch("stages.vanguard_prefilter.run", v2_mock), \
         patch("stages.vanguard_factor_engine.run", v3_mock), \
         patch("stages.vanguard_scorer.run", v4b_mock), \
         patch("stages.vanguard_selection_tradeability.run", v5_mock), \
         patch("stages.vanguard_risk_filters.run", v6_mock):
        result = orch.run_cycle()

    assert result["status"] == "no_candidates"
    assert result["approved"] == 0
    v6_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: run_cycle() handles V5 no_features gracefully
# ---------------------------------------------------------------------------


def test_run_cycle_no_features(tmp_db):
    """V5 returns status=no_features → run_cycle skips V6."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    v2_mock = MagicMock(return_value=[{"symbol": "AAPL", "asset_class": "equity", "status": "ACTIVE"}])
    v3_mock = MagicMock(return_value=[])
    v4b_mock = MagicMock(return_value={"status": "ok", "rows": 1})
    v5_mock = MagicMock(return_value={"status": "no_features", "rows": 0})
    v6_mock = MagicMock(return_value={"status": "ok", "rows": 0})

    with patch("stages.vanguard_prefilter.run", v2_mock), \
         patch("stages.vanguard_factor_engine.run", v3_mock), \
         patch("stages.vanguard_scorer.run", v4b_mock), \
         patch("stages.vanguard_selection_tradeability.run", v5_mock), \
         patch("stages.vanguard_risk_filters.run", v6_mock):
        result = orch.run_cycle()

    assert result["status"] == "no_candidates"
    v6_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: run_cycle() handles all V6 rejected (0 APPROVED rows)
# ---------------------------------------------------------------------------


def test_run_cycle_all_v6_rejected(tmp_db):
    """V5 returns 3 candidates, V6 writes rows but none are APPROVED."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    v2_mock = MagicMock(return_value=[{"symbol": "AAPL", "asset_class": "equity", "status": "ACTIVE"}])
    v3_mock = MagicMock(return_value=[{"symbol": "AAPL"}])
    v4b_mock = MagicMock(return_value={"status": "ok", "rows": 1})
    v5_mock = MagicMock(return_value={"status": "ok", "rows": 3})
    v6_mock = MagicMock(return_value={"status": "ok", "rows": 3})

    with patch("stages.vanguard_prefilter.run", v2_mock), \
         patch("stages.vanguard_factor_engine.run", v3_mock), \
         patch("stages.vanguard_scorer.run", v4b_mock), \
         patch("stages.vanguard_selection_tradeability.run", v5_mock), \
         patch.object(orch, "_refresh_active_context_snapshots", return_value={}), \
         patch.object(orch, "_log_context_state_diagnostics", return_value=None), \
         patch.object(orch, "_log_v6_diagnostics", return_value=None), \
         patch.object(orch, "_build_shortlist_telegram", return_value=""), \
         patch.object(orch, "_build_operator_diagnostics_telegram", return_value=""), \
         patch("stages.vanguard_risk_filters.run", v6_mock):
        result = orch.run_cycle()

    assert result["status"] == "ok"
    assert result["approved"] == 0   # nothing was written to DB as APPROVED


# ---------------------------------------------------------------------------
# Test 7: execute() off mode → FORWARD_TRACKED in execution_log
# ---------------------------------------------------------------------------


def test_execute_off_writes_forward_tracked(tmp_db):
    """Off mode writes FORWARD_TRACKED — no real order sent."""
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'test_acct','NVDA','LONG','APPROVED',
                       100, 450.00, 440.00, 470.00, 'vanguard')"""
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._execute_approved()

    with sqlite3.connect(tmp_db) as con:
        rows = con.execute(
            "SELECT symbol, status FROM execution_log WHERE symbol='NVDA'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "FORWARD_TRACKED"


# ---------------------------------------------------------------------------
# Test 8: execute() paper mode → calls SignalStackAdapter.send_order()
# ---------------------------------------------------------------------------


def test_execute_paper_normalizes_to_manual_forward_track(tmp_db):
    """Legacy paper mode normalizes to manual and does not route SignalStack."""
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'paper_acct','MSFT','LONG','APPROVED',
                       50, 420.00, 410.00, 440.00, 'vanguard')"""
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="paper", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._execute_approved()

    assert orch.execution_mode == "manual"

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT status FROM execution_log WHERE symbol='MSFT'"
        ).fetchone()
    assert row[0] == "FORWARD_TRACKED"


# ---------------------------------------------------------------------------
# Test 9: mt5_local forex execution preserves fractional lots
# ---------------------------------------------------------------------------


def test_execute_off_preserves_fractional_lots_for_mt5_local_forex(tmp_db):
    """MT5-local forex rows must not truncate fractional lots to zero."""
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'ftmo_demo_100k','USDNOK','LONG','APPROVED',
                       0.37, 9.3857, 9.37907, 9.39896, 'vanguard')"""
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    with patch.object(
        orch,
        "_get_account_profile",
        return_value={"execution_bridge": "mt5_local_ftmo_demo_100k"},
    ):
        orch._execute_approved()

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT symbol, status FROM execution_log WHERE symbol='USDNOK'"
        ).fetchone()
    assert row is not None
    assert row[1] == "FORWARD_TRACKED"


def test_execute_live_skips_during_scheduled_flatten_window(tmp_db):
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'ftmo_demo_100k','AUDUSD','SHORT','APPROVED',
                       1.5, 0.7172, 0.7180, 0.7160, 'vanguard')"""
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="live", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(16, 50),
        _now_utc_fn=lambda: _make_utc(20, 50),
    )
    with patch.object(orch, "_scheduled_flatten_window_active", return_value=True):
        orch._execute_approved()

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT status, signalstack_response, notes FROM execution_log WHERE symbol='AUDUSD'"
        ).fetchone()
    assert row is not None
    assert row[0] == "FORWARD_TRACKED"
    assert "scheduled_flatten_window" in (row[1] or "")


# ---------------------------------------------------------------------------
# Test 10: _wind_down() triggers EOD flatten for intraday accounts
# ---------------------------------------------------------------------------


def test_wind_down_flattens_intraday(tmp_db):
    """_wind_down triggers EOD flatten for must_close_eod=1 account at 16:00 ET."""
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            "INSERT INTO account_profiles VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("intraday_x", "Intraday X", "ttp", "intraday", 50000, 1000, 2000, 5, 1, 1),
        )
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'intraday_x','SPY','LONG','APPROVED',
                       100, 550.0, 540.0, 560.0, 'eod_test')"""
        )
        con.commit()

    et_time = _make_et(16, 0)
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: et_time,
        _now_utc_fn=lambda: _make_utc(21, 0),
        eod_flatten_time="15:50",
        no_new_positions_after="15:45",
    )
    orch._accounts         = [{"id": "intraday_x", "must_close_eod": 1}]
    orch._session_date     = "2026-03-02"
    orch._session_start_ts = _make_utc(14, 35)
    orch._ensure_session_log()
    orch._wind_down()

    with sqlite3.connect(tmp_db) as con:
        rows = con.execute(
            "SELECT symbol, status, notes FROM execution_log WHERE symbol='SPY'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] in ("FORWARD_TRACKED", "FILLED")
    assert "flatten" in (rows[0][2] or "").lower()


# ---------------------------------------------------------------------------
# Test 11: _wind_down() skips flatten for swing accounts
# ---------------------------------------------------------------------------


def test_wind_down_skips_swing_account(tmp_db):
    """_wind_down does NOT flatten positions for must_close_eod=0 account."""
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            "INSERT INTO account_profiles VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("swing_x", "Swing X", "ttp", "swing", 20000, 500, 1000, 5, 0, 1),
        )
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'swing_x','AAPL','LONG','APPROVED',
                       50, 200.0, 190.0, 220.0, 'swing_test')"""
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(16, 0),
        _now_utc_fn=lambda: _make_utc(21, 0),
        eod_flatten_time="15:50",
    )
    orch._accounts         = [{"id": "swing_x", "must_close_eod": 0}]
    orch._session_date     = "2026-03-02"
    orch._session_start_ts = _make_utc(14, 35)
    orch._ensure_session_log()
    orch._wind_down()

    with sqlite3.connect(tmp_db) as con:
        rows = con.execute(
            "SELECT symbol FROM execution_log WHERE symbol='AAPL'"
        ).fetchall()
    assert len(rows) == 0   # no flatten for swing account


# ---------------------------------------------------------------------------
# Test 12: 3 consecutive failures trigger session abort
# ---------------------------------------------------------------------------


def test_consecutive_failures_abort(tmp_db):
    """MAX_CONSECUTIVE_FAILS consecutive cycle errors abort the session."""
    call_count = {"n": 0}

    def failing_cycle(*args, **kwargs):
        call_count["n"] += 1
        raise RuntimeError("simulated failure")

    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._session_date     = "2026-03-02"
    orch._session_start_ts = _make_utc(14, 35)
    orch._ensure_session_log()

    with patch.object(orch, "run_cycle", side_effect=failing_cycle), \
         patch.object(orch, "_wait_for_next_bar"):
        orch._main_loop()

    assert call_count["n"] == MAX_CONSECUTIVE_FAILS

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT status FROM vanguard_session_log WHERE date='2026-03-02'"
        ).fetchone()
    assert row is not None
    assert row[0] == "aborted"


# ---------------------------------------------------------------------------
# Test 12: Session log written to vanguard_session_log
# ---------------------------------------------------------------------------


def test_session_log_upsert(tmp_db):
    """_upsert_session_log writes and correctly updates the session log table."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._session_date     = "2026-03-02"
    orch._session_start_ts = _make_utc(14, 35)
    orch._total_cycles     = 5
    orch._failed_cycles    = 1
    orch._trades_placed    = 3
    orch._forward_tracked  = 10

    orch._ensure_session_log()
    orch._upsert_session_log(status="running")

    with sqlite3.connect(tmp_db) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM vanguard_session_log WHERE date='2026-03-02'"
        ).fetchone()
    r = dict(row)
    assert r["total_cycles"]    == 5
    assert r["failed_cycles"]   == 1
    assert r["trades_placed"]   == 3
    assert r["forward_tracked"] == 10
    assert r["execution_mode"]  == "manual"
    assert r["status"]          == "running"

    # Verify upsert updates existing row
    orch._total_cycles = 10
    orch._upsert_session_log(status="complete")

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT total_cycles, status FROM vanguard_session_log WHERE date='2026-03-02'"
        ).fetchone()
    assert row[0] == 10
    assert row[1] == "complete"


# ---------------------------------------------------------------------------
# Test 13: Telegram alerts sent (mocked)
# ---------------------------------------------------------------------------


def test_telegram_startup_and_windown_alerts(tmp_db):
    """Startup and wind-down each send at least one Telegram message."""
    mock_tg = MagicMock()
    mock_tg.send.return_value = True

    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._telegram = mock_tg

    orch._startup()
    assert mock_tg.send.call_count >= 1
    assert "SESSION START" in mock_tg.send.call_args_list[0][0][0]

    mock_tg.reset_mock()
    orch._wind_down()
    assert mock_tg.send.call_count >= 1
    assert "SESSION END" in mock_tg.send.call_args_list[0][0][0]


def test_format_stage_row_shows_thesis_session_and_exit_by(tmp_db):
    orch = VanguardOrchestrator(
        execution_mode="manual", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    row = {
        "symbol": "EURUSD",
        "asset_class": "forex",
        "direction": "LONG",
        "rank": 1,
        "predicted_move_pips": 12.34,
        "after_cost_pips": 11.11,
        "thesis_state": "NEW",
        "session_label": "London",
        "model_horizon_hint": "90–120m",
        "exit_by_window": "12:30 ET — 13:00 ET",
        "timeout_policy": {
            "timeout_minutes": 30,
            "dedupe_minutes": 120,
            "timeout_label": "12:45 ET",
        },
        "portfolio": {
            "status": "APPROVED",
            "risk_dollars": 50.0,
            "risk_pct": 0.005,
            "stop_price": 1.08,
            "tp_price": 1.095,
            "lot_size": 0.1,
        },
    }

    msg = orch._format_v5_stage_telegram_row(row=row, price=1.089, stage="approved")

    assert "Selection Cadence" not in msg
    assert "Tier" not in msg
    assert "Thesis NEW" in msg
    assert "Session London" in msg
    assert "Model Horizon 90–120m" in msg
    assert "Exit by 12:30 ET — 13:00 ET" in msg
    assert "Timeout Guide 30m (dedupe 120) → 12:45 ET" in msg


def test_build_timeout_policy_header_lists_session_guidance(tmp_db):
    orch = VanguardOrchestrator(
        execution_mode="manual", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )

    header = orch._build_timeout_policy_header("2026-04-15T13:00:00Z")

    assert "Session Policy:" in header
    assert "London 30m (dedupe 120)" in header
    assert "NY 60m (dedupe 120)" in header
    assert "Asian 120m (dedupe 90)" in header


def test_load_thesis_display_state_uses_open_and_recent_chain_labels(tmp_db):
    with sqlite3.connect(tmp_db) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
                profile_id TEXT,
                ticket TEXT,
                symbol TEXT,
                direction TEXT,
                source_status TEXT,
                received_ts_utc TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
                trade_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                side TEXT NOT NULL,
                approved_cycle_ts_utc TEXT NOT NULL,
                approved_qty REAL NOT NULL,
                expected_entry REAL NOT NULL,
                expected_sl REAL NOT NULL,
                expected_tp REAL NOT NULL,
                approval_reasoning_json TEXT NOT NULL,
                status TEXT NOT NULL,
                filled_at_utc TEXT,
                closed_at_utc TEXT,
                last_synced_at_utc TEXT NOT NULL
            )
        """)
        con.execute(
            """
            INSERT INTO vanguard_context_positions_latest
            (profile_id, ticket, symbol, direction, source_status, received_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("gft_5k", "1001", "EURUSD", "LONG", "OK", "2026-03-02T14:59:00Z"),
        )
        con.executemany(
            """
            INSERT INTO vanguard_trade_journal (
                trade_id, profile_id, symbol, asset_class, side, approved_cycle_ts_utc,
                approved_qty, expected_entry, expected_sl, expected_tp,
                approval_reasoning_json, status, filled_at_utc, closed_at_utc, last_synced_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "t1", "gft_5k", "AUDCAD", "forex", "SHORT", "2026-03-02T14:55:00Z",
                    1.0, 1.0, 0.9, 1.1, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T14:55:00Z",
                ),
                (
                    "t2", "gft_5k", "AUDCAD", "forex", "SHORT", "2026-03-02T14:50:00Z",
                    1.0, 1.0, 0.9, 1.1, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T14:50:00Z",
                ),
                (
                    "t3", "gft_5k", "AUDCAD", "forex", "SHORT", "2026-03-02T11:00:00Z",
                    1.0, 1.0, 0.9, 1.1, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T11:00:00Z",
                ),
            ],
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="manual", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )

    state = orch._load_thesis_display_state(
        cycle_ts="2026-03-02T15:00:00Z",
        candidate_keys=[
            ("gft_5k", "EURUSD", "LONG"),
            ("gft_5k", "AUDCAD", "SHORT"),
            ("gft_5k", "GBPJPY", "SHORT"),
        ],
    )

    assert state[("gft_5k", "EURUSD", "LONG")]["thesis_state"] == "OPEN"
    assert state[("gft_5k", "EURUSD", "LONG")]["reentry_blocked"] is True
    assert state[("gft_5k", "AUDCAD", "SHORT")]["thesis_state"] == "SEEN_RECENTLY x2"
    assert state[("gft_5k", "AUDCAD", "SHORT")]["reentry_blocked"] is False
    assert state[("gft_5k", "GBPJPY", "SHORT")]["thesis_state"] == "NEW"


def test_load_thesis_display_state_resets_recent_counter_on_flip(tmp_db):
    with sqlite3.connect(tmp_db) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
                profile_id TEXT,
                ticket TEXT,
                symbol TEXT,
                direction TEXT,
                source_status TEXT,
                received_ts_utc TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
                trade_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                side TEXT NOT NULL,
                approved_cycle_ts_utc TEXT NOT NULL,
                approved_qty REAL NOT NULL,
                expected_entry REAL NOT NULL,
                expected_sl REAL NOT NULL,
                expected_tp REAL NOT NULL,
                approval_reasoning_json TEXT NOT NULL,
                status TEXT NOT NULL,
                filled_at_utc TEXT,
                closed_at_utc TEXT,
                last_synced_at_utc TEXT NOT NULL
            )
        """)
        con.executemany(
            """
            INSERT INTO vanguard_trade_journal (
                trade_id, profile_id, symbol, asset_class, side, approved_cycle_ts_utc,
                approved_qty, expected_entry, expected_sl, expected_tp,
                approval_reasoning_json, status, filled_at_utc, closed_at_utc, last_synced_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "f1", "gft_5k", "NZDUSD", "forex", "LONG", "2026-03-02T14:55:00Z",
                    1.0, 1.0, 0.9, 1.1, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T14:55:00Z",
                ),
                (
                    "f2", "gft_5k", "NZDUSD", "forex", "SHORT", "2026-03-02T14:45:00Z",
                    1.0, 1.0, 0.9, 1.1, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T14:45:00Z",
                ),
                (
                    "f3", "gft_5k", "NZDUSD", "forex", "SHORT", "2026-03-02T14:40:00Z",
                    1.0, 1.0, 0.9, 1.1, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T14:40:00Z",
                ),
            ],
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="manual", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )

    state = orch._load_thesis_display_state(
        cycle_ts="2026-03-02T15:00:00Z",
        candidate_keys=[
            ("gft_5k", "NZDUSD", "LONG"),
            ("gft_5k", "NZDUSD", "SHORT"),
        ],
    )

    assert state[("gft_5k", "NZDUSD", "LONG")]["thesis_state"] == "SEEN_RECENTLY x1"
    assert state[("gft_5k", "NZDUSD", "SHORT")]["thesis_state"] == "NEW"


def test_load_thesis_display_state_resets_recent_counter_on_empty_cycle(tmp_db):
    with sqlite3.connect(tmp_db) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
                profile_id TEXT,
                ticket TEXT,
                symbol TEXT,
                direction TEXT,
                source_status TEXT,
                received_ts_utc TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
                trade_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                side TEXT NOT NULL,
                approved_cycle_ts_utc TEXT NOT NULL,
                approved_qty REAL NOT NULL,
                expected_entry REAL NOT NULL,
                expected_sl REAL NOT NULL,
                expected_tp REAL NOT NULL,
                approval_reasoning_json TEXT NOT NULL,
                status TEXT NOT NULL,
                filled_at_utc TEXT,
                closed_at_utc TEXT,
                last_synced_at_utc TEXT NOT NULL
            )
        """)
        con.executemany(
            """
            INSERT INTO vanguard_trade_journal (
                trade_id, profile_id, symbol, asset_class, side, approved_cycle_ts_utc,
                approved_qty, expected_entry, expected_sl, expected_tp,
                approval_reasoning_json, status, filled_at_utc, closed_at_utc, last_synced_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "e1", "gft_5k", "USDCAD", "forex", "LONG", "2026-03-02T14:50:00Z",
                    1.0, 1.0, 0.9, 1.1, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T14:50:00Z",
                ),
            ],
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="manual", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )

    state = orch._load_thesis_display_state(
        cycle_ts="2026-03-02T15:00:00Z",
        candidate_keys=[
            ("gft_5k", "USDCAD", "LONG"),
        ],
    )

    assert state[("gft_5k", "USDCAD", "LONG")]["thesis_state"] == "NEW"


def test_build_shortlist_telegram_moves_repeated_thesis_out_of_approved(tmp_db):
    cycle_ts = "2026-03-02T15:00:00Z"
    with sqlite3.connect(tmp_db) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_shortlist_v2 (
                cycle_ts_utc TEXT,
                symbol TEXT,
                asset_class TEXT,
                direction TEXT,
                predicted_return REAL,
                edge_score REAL,
                rank INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_v5_selection (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                asset_class TEXT,
                direction TEXT,
                selection_rank INTEGER,
                route_tier TEXT,
                display_label TEXT,
                selection_state TEXT,
                direction_streak INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_v5_tradeability (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                direction TEXT,
                predicted_return REAL,
                economics_state TEXT,
                predicted_move_pips REAL,
                after_cost_pips REAL,
                predicted_move_bps REAL,
                after_cost_bps REAL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
                profile_id TEXT,
                ticket TEXT,
                symbol TEXT,
                direction TEXT,
                source_status TEXT,
                received_ts_utc TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
                trade_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                side TEXT NOT NULL,
                approved_cycle_ts_utc TEXT NOT NULL,
                approved_qty REAL NOT NULL,
                expected_entry REAL NOT NULL,
                expected_sl REAL NOT NULL,
                expected_tp REAL NOT NULL,
                approval_reasoning_json TEXT NOT NULL,
                status TEXT NOT NULL,
                filled_at_utc TEXT,
                closed_at_utc TEXT,
                last_synced_at_utc TEXT NOT NULL
            )
        """)
        con.executemany(
            """
            INSERT INTO vanguard_v5_selection (
                cycle_ts_utc, profile_id, symbol, asset_class, direction,
                selection_rank, route_tier, display_label, selection_state, direction_streak
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (cycle_ts, "gft_5k", "AUDCAD", "forex", "SHORT", 1, "tier_1", "SELECTED", "SELECTED", 1),
                (cycle_ts, "gft_5k", "EURUSD", "forex", "LONG", 2, "tier_1", "SELECTED", "SELECTED", 1),
            ],
        )
        con.executemany(
            """
            INSERT INTO vanguard_v5_tradeability (
                cycle_ts_utc, profile_id, symbol, direction, predicted_return, economics_state,
                predicted_move_pips, after_cost_pips, predicted_move_bps, after_cost_bps
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (cycle_ts, "gft_5k", "AUDCAD", "SHORT", 0.05, "tradeable", 60.0, 59.0, 12.0, 11.0),
                (cycle_ts, "gft_5k", "EURUSD", "LONG", 0.06, "tradeable", 70.0, 69.0, 14.0, 13.0),
            ],
        )
        con.executemany(
            """
            INSERT INTO vanguard_tradeable_portfolio (
                cycle_ts_utc, account_id, symbol, direction, status, shares_or_lots,
                entry_price, stop_price, tp_price, risk_dollars, risk_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (cycle_ts, "gft_5k", "AUDCAD", "SHORT", "APPROVED", 0.5, 0.9844, 0.9849, 0.9834, 25.0, 0.0025),
                (cycle_ts, "gft_5k", "EURUSD", "LONG", "APPROVED", 0.5, 1.1000, 1.0990, 1.1020, 25.0, 0.0025),
            ],
        )
        con.execute(
            """
            INSERT INTO vanguard_trade_journal (
                trade_id, profile_id, symbol, asset_class, side, approved_cycle_ts_utc,
                approved_qty, expected_entry, expected_sl, expected_tp, approval_reasoning_json,
                status, filled_at_utc, closed_at_utc, last_synced_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "repeat-1", "gft_5k", "AUDCAD", "forex", "SHORT", "2026-03-02T14:55:00Z",
                1.0, 0.9840, 0.9850, 0.9830, "{}", "FORWARD_TRACKED", None, None, "2026-03-02T14:55:00Z",
            ),
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="manual", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._accounts = [{"id": "gft_5k"}]

    msg = orch._build_shortlist_telegram(cycle_ts)

    assert "━━ REPEATED THESIS ━━" in msg
    repeated_block = msg.split("━━ REPEATED THESIS ━━", 1)[1]
    assert "AUDCAD" in repeated_block
    approved_block = msg.split("━━ APPROVED / FORWARD-TRACKED ━━", 1)[1].split("━━ REPEATED THESIS ━━", 1)[0]
    assert "EURUSD" in approved_block
    assert "AUDCAD" not in approved_block


def test_account_snapshot_and_thesis_state_dedupe_duplicate_position_sources(tmp_db):
    with sqlite3.connect(tmp_db) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_context_account_latest (
                profile_id TEXT,
                balance REAL,
                equity REAL,
                free_margin REAL,
                received_ts_utc TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
                profile_id TEXT,
                source TEXT,
                ticket TEXT,
                symbol TEXT,
                direction TEXT,
                lots REAL,
                floating_pnl REAL,
                holding_minutes REAL,
                open_time_utc TEXT,
                source_status TEXT,
                received_ts_utc TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
                trade_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                side TEXT NOT NULL,
                approved_cycle_ts_utc TEXT NOT NULL,
                approved_qty REAL NOT NULL,
                expected_entry REAL NOT NULL,
                expected_sl REAL NOT NULL,
                expected_tp REAL NOT NULL,
                approval_reasoning_json TEXT NOT NULL,
                status TEXT NOT NULL,
                filled_at_utc TEXT,
                closed_at_utc TEXT,
                last_synced_at_utc TEXT NOT NULL
            )
        """)
        con.execute(
            """
            INSERT INTO vanguard_context_account_latest
            (profile_id, balance, equity, free_margin, received_ts_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("ftmo_demo_100k", 100000.0, 99999.0, 99990.0, "2026-03-02T15:00:00Z"),
        )
        con.executemany(
            """
            INSERT INTO vanguard_context_positions_latest
            (profile_id, source, ticket, symbol, direction, lots, floating_pnl, holding_minutes, open_time_utc, source_status, received_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("ftmo_demo_100k", "mt5_dwx", "2001", "AUDCHF", "SHORT", 0.01, -0.12, 3.0, "2026-03-02T14:56:00Z", "OK", "2026-03-02T14:59:00Z"),
                ("ftmo_demo_100k", "ftmo_mt5_local", "2001", "AUDCHF", "SHORT", 0.01, -0.12, 3.0, "2026-03-02T14:56:00Z", "OK", "2026-03-02T15:00:00Z"),
            ],
        )
        con.commit()

    orch = VanguardOrchestrator(
        execution_mode="manual", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._accounts = [{"id": "ftmo_demo_100k"}]

    line = orch._build_account_snapshot_line()
    state = orch._load_thesis_display_state(
        cycle_ts="2026-03-02T15:00:00Z",
        candidate_keys=[("ftmo_demo_100k", "AUDCHF", "SHORT")],
    )

    assert "Pos 1" in line
    assert "UPL -0.12" in line
    assert state[("ftmo_demo_100k", "AUDCHF", "SHORT")]["thesis_state"] == "OPEN"


# ---------------------------------------------------------------------------
# Test 14: Config-driven timing (not hardcoded)
# ---------------------------------------------------------------------------


def test_asset_sessions_override_custom_global_window(tmp_db):
    """Polling follows asset sessions even when custom global window is narrower."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        session_start="10:00",
        session_end="14:00",
        eod_flatten_time="13:45",
        no_new_positions_after="13:30",
        _now_et_fn=lambda: _make_et(12, 0),
        _now_utc_fn=lambda: _make_utc(17, 0),
    )

    assert orch.is_within_session(_make_et(9, 59)) is True
    assert orch.is_within_session(_make_et(14, 0)) is True

    assert orch.session_start           == "10:00"
    assert orch.session_end             == "14:00"
    assert orch.eod_flatten_time        == "13:45"
    assert orch.no_new_positions_after  == "13:30"


# ---------------------------------------------------------------------------
# Bonus: helper unit tests
# ---------------------------------------------------------------------------


def test_parse_hhmm():
    assert _parse_hhmm("09:35") == (9, 35)
    assert _parse_hhmm("15:25") == (15, 25)
    assert _parse_hhmm("00:00") == (0, 0)


def test_invalid_execution_mode(tmp_db):
    with pytest.raises(ValueError, match="Invalid execution_mode"):
        VanguardOrchestrator(
            execution_mode="bad_mode", db_path=tmp_db,
            _now_et_fn=lambda: _make_et(10, 0),
            _now_utc_fn=lambda: _make_utc(15, 0),
        )
