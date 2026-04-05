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
    db_path = str(tmp_path / "test_vanguard.db")
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
                status TEXT, shares_or_lots INTEGER DEFAULT 100,
                entry_price REAL, stop_loss REAL, take_profit REAL, tier TEXT,
                feature_scores TEXT
            )
        """)
        con.commit()
    _ensure_execution_log(db_path)
    return db_path


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
    """10:00 ET on Monday is inside default 09:35-15:25 window."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    assert orch.is_within_session(_make_et(10, 0)) is True
    assert orch.is_within_session(_make_et(9, 34)) is False     # before start
    assert orch.is_within_session(_make_et(15, 25)) is False    # at end (exclusive)
    assert orch.is_within_session(_make_et(16, 0)) is False     # after close


def test_is_within_session_weekend(tmp_db):
    """Saturday is never within session regardless of time."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    saturday = _make_et(10, 0, weekday_offset=5)   # Mon + 5 = Sat
    assert orch.is_within_session(saturday) is False


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
    v3_mock = MagicMock(return_value=[])
    v5_mock = MagicMock(return_value={"status": "no_shortlist", "rows": 0})
    v6_mock = MagicMock(return_value={"status": "ok", "rows": 0})

    with patch("stages.vanguard_factor_engine.run", v3_mock), \
         patch("stages.vanguard_selection.run", v5_mock), \
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
    v3_mock = MagicMock(return_value=[])
    v5_mock = MagicMock(return_value={"status": "no_features", "rows": 0})
    v6_mock = MagicMock(return_value={"status": "ok", "rows": 0})

    with patch("stages.vanguard_factor_engine.run", v3_mock), \
         patch("stages.vanguard_selection.run", v5_mock), \
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
    v3_mock = MagicMock(return_value=[{"symbol": "AAPL"}])
    v5_mock = MagicMock(return_value={"status": "ok", "rows": 3})
    v6_mock = MagicMock(return_value={"status": "ok", "rows": 3})

    with patch("stages.vanguard_factor_engine.run", v3_mock), \
         patch("stages.vanguard_selection.run", v5_mock), \
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


def test_execute_paper_calls_signalstack(tmp_db):
    """Paper mode calls SignalStack and writes FILLED on success."""
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'paper_acct','MSFT','LONG','APPROVED',
                       50, 420.00, 410.00, 440.00, 'vanguard')"""
        )
        con.commit()

    mock_adapter = MagicMock()
    mock_adapter.send_order.return_value = {
        "success": True, "status_code": 200,
        "response_body": "OK", "latency_ms": 50,
        "payload": {}, "error": None,
    }

    orch = VanguardOrchestrator(
        execution_mode="paper", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._ss_adapter = mock_adapter
    orch._execute_approved()

    mock_adapter.send_order.assert_called_once_with(
        symbol="MSFT", direction="LONG",
        quantity=50, operation="open", broker="ttp",
    )

    with sqlite3.connect(tmp_db) as con:
        row = con.execute(
            "SELECT status FROM execution_log WHERE symbol='MSFT'"
        ).fetchone()
    assert row[0] == "FILLED"


# ---------------------------------------------------------------------------
# Test 9: _wind_down() triggers EOD flatten for intraday accounts
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
# Test 10: _wind_down() skips flatten for swing accounts
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
# Test 11: 3 consecutive failures trigger session abort
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
    assert r["execution_mode"]  == "off"
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


def test_telegram_trade_executed_alert(tmp_db):
    """Successful paper trade calls alert_trade_executed on the Telegram adapter."""
    with sqlite3.connect(tmp_db) as con:
        con.execute(
            """INSERT INTO vanguard_tradeable_portfolio
               (cycle_ts_utc, account_id, symbol, direction, status,
                shares_or_lots, entry_price, stop_loss, take_profit, tier)
               VALUES (datetime('now'),'tg_acct','TSLA','LONG','APPROVED',
                       25, 300.00, 290.00, 320.00, 'vanguard')"""
        )
        con.commit()

    mock_adapter = MagicMock()
    mock_adapter.send_order.return_value = {
        "success": True, "status_code": 200, "response_body": "OK",
        "latency_ms": 30, "payload": {}, "error": None,
    }
    mock_tg = MagicMock()
    mock_tg.send.return_value = True

    orch = VanguardOrchestrator(
        execution_mode="paper", dry_run=False, db_path=tmp_db,
        _now_et_fn=lambda: _make_et(10, 0),
        _now_utc_fn=lambda: _make_utc(15, 0),
    )
    orch._ss_adapter = mock_adapter
    orch._telegram   = mock_tg

    orch._execute_approved()

    mock_tg.alert_trade_executed.assert_called_once()
    kwargs = mock_tg.alert_trade_executed.call_args[1]
    assert kwargs["symbol"]    == "TSLA"
    assert kwargs["direction"] == "LONG"


# ---------------------------------------------------------------------------
# Test 14: Config-driven timing (not hardcoded)
# ---------------------------------------------------------------------------


def test_config_driven_timing(tmp_db):
    """Custom session_start/session_end/eod_flatten_time are respected."""
    orch = VanguardOrchestrator(
        execution_mode="off", dry_run=True, db_path=tmp_db,
        session_start="10:00",
        session_end="14:00",
        eod_flatten_time="13:45",
        no_new_positions_after="13:30",
        _now_et_fn=lambda: _make_et(12, 0),
        _now_utc_fn=lambda: _make_utc(17, 0),
    )

    assert orch.is_within_session(_make_et(10, 0))  is True
    assert orch.is_within_session(_make_et(9, 59))  is False   # before custom start
    assert orch.is_within_session(_make_et(14, 0))  is False   # at custom end (exclusive)
    assert orch.is_within_session(_make_et(13, 59)) is True    # just inside
    assert orch.is_within_session(_make_et(15, 0))  is False   # default end, but custom ends earlier

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
