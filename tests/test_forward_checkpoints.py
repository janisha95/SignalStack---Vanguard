from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from vanguard.execution.forward_checkpoints import CHECKPOINT_MINUTES, refresh_checkpoints


def _make_db() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def _seed_trade_journal(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE vanguard_trade_journal (
            trade_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            approved_cycle_ts_utc TEXT NOT NULL,
            expected_entry REAL NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    con.executemany(
        """
        INSERT INTO vanguard_trade_journal (
            trade_id, profile_id, symbol, side, approved_cycle_ts_utc, expected_entry, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("trade-ready", "gft_5k", "EURUSD", "LONG", "2026-04-14T10:00:00Z", 1.1000, "FORWARD_TRACKED"),
            ("trade-seconds", "gft_5k", "GBPUSD", "LONG", "2026-04-14T10:00:10Z", 1.2500, "FORWARD_TRACKED"),
            ("trade-empty", "gft_5k", "USDJPY", "SHORT", "2026-04-14T10:00:00Z", 154.00, "FORWARD_TRACKED"),
        ],
    )


def _seed_bars(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE vanguard_bars_1m (
            symbol TEXT NOT NULL,
            bar_ts_utc TEXT NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL
        )
        """
    )
    start = datetime(2026, 4, 14, 10, 1, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        ts = (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
        close = 1.1000 + (minute + 1) * 0.0001
        rows.append(("EURUSD", ts, close + 0.0002, close - 0.0002, close))
        close_gbp = 1.2500 + (minute + 1) * 0.0001
        rows.append(("GBPUSD", ts, close_gbp + 0.0002, close_gbp - 0.0002, close_gbp))
    con.executemany(
        "INSERT INTO vanguard_bars_1m (symbol, bar_ts_utc, high, low, close) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def test_refresh_checkpoints_persists_ready_partial_and_no_bars() -> None:
    db_path = _make_db()
    with sqlite3.connect(db_path) as con:
        _seed_trade_journal(con)
        _seed_bars(con)
        con.commit()

    written = refresh_checkpoints(db_path)

    assert written == 3 * len(CHECKPOINT_MINUTES)

    with sqlite3.connect(db_path) as con:
        ready_15 = con.execute(
            """
            SELECT bars_used, status, is_positive_direction, close_move_pips, mfe_pips, mae_pips
            FROM vanguard_forward_checkpoints
            WHERE trade_id = 'trade-ready' AND checkpoint_minutes = 15
            """
        ).fetchone()
        partial_30 = con.execute(
            """
            SELECT bars_used, status
            FROM vanguard_forward_checkpoints
            WHERE trade_id = 'trade-ready' AND checkpoint_minutes = 30
            """
        ).fetchone()
        no_bars = con.execute(
            """
            SELECT bars_used, status
            FROM vanguard_forward_checkpoints
            WHERE trade_id = 'trade-empty' AND checkpoint_minutes = 15
            """
        ).fetchone()
        ready_seconds = con.execute(
            """
            SELECT bars_used, status
            FROM vanguard_forward_checkpoints
            WHERE trade_id = 'trade-seconds' AND checkpoint_minutes = 15
            """
        ).fetchone()

    assert ready_15[0] == 15
    assert ready_15[1] == "READY"
    assert ready_15[2] == 1
    assert ready_15[3] == pytest.approx(15.0)
    assert ready_15[4] == pytest.approx(17.0)
    assert ready_15[5] == pytest.approx(-1.0)
    assert partial_30 == (20, "PARTIAL")
    assert no_bars == (0, "NO_BARS")
    assert ready_seconds == (15, "READY")
