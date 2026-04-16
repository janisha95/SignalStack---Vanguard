from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from vanguard.execution.forward_checkpoints import refresh_checkpoints
from vanguard.execution.timeout_policy import ensure_schema, refresh_timeout_policy_data


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
            ("london-1", "gft_10k", "EURUSD", "LONG", "2026-04-15T04:00:00Z", 1.1000, "FORWARD_TRACKED"),
            ("london-2", "gft_10k", "EURUSD", "LONG", "2026-04-15T05:00:00Z", 1.1005, "FORWARD_TRACKED"),
            ("tokyo-1", "gft_10k", "USDJPY", "SHORT", "2026-04-15T00:00:00Z", 155.0000, "FORWARD_TRACKED"),
        ],
    )


def _seed_pair_state(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE vanguard_forex_pair_state (
            cycle_ts_utc TEXT NOT NULL,
            symbol TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            session_bucket TEXT
        )
        """
    )
    con.executemany(
        "INSERT INTO vanguard_forex_pair_state (cycle_ts_utc, symbol, profile_id, session_bucket) VALUES (?, ?, ?, ?)",
        [
            ("2026-04-15T04:00:00Z", "EURUSD", "gft_10k", "London"),
            ("2026-04-15T05:00:00Z", "EURUSD", "gft_10k", "London"),
            ("2026-04-15T00:00:00Z", "USDJPY", "gft_10k", "Tokyo"),
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
    rows: list[tuple[str, str, float, float, float]] = []
    start = datetime(2026, 4, 15, 0, 1, tzinfo=timezone.utc)
    for minute in range(360):
        ts = (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(("USDJPY", ts, 155.0000, 154.9800, 154.9900))
    start = datetime(2026, 4, 15, 4, 1, tzinfo=timezone.utc)
    for minute in range(180):
        ts = (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
        close = 1.1000 + min(minute, 60) * 0.0001
        rows.append(("EURUSD", ts, close + 0.0002, close - 0.0002, close))
    con.executemany(
        "INSERT INTO vanguard_bars_1m (symbol, bar_ts_utc, high, low, close) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def test_refresh_timeout_policy_data_enriches_checkpoints_and_summaries() -> None:
    db_path = _make_db()
    with sqlite3.connect(db_path) as con:
        _seed_trade_journal(con)
        _seed_pair_state(con)
        _seed_bars(con)
        con.commit()

    refresh_checkpoints(db_path)
    refresh_timeout_policy_data(db_path)

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT session_bucket, policy_timeout_minutes, policy_dedupe_minutes,
                   dedupe_chain_120, dedupe_is_primary_120
            FROM vanguard_forward_checkpoints
            WHERE trade_id = 'london-1' AND checkpoint_minutes = 30
            """
        ).fetchone()
        shell_count = con.execute(
            "SELECT COUNT(*) FROM vanguard_forward_shell_replays WHERE trade_id = 'london-1'"
        ).fetchone()[0]
        summary = con.execute(
            """
            SELECT n, wins, losses
            FROM vanguard_timeout_policy_summary
            WHERE session_bucket = 'london' AND dedupe_mode = 120 AND timeout_minutes = 30
            """
        ).fetchone()

    assert row[0] == "london"
    assert row[1] == 30
    assert row[2] == 120
    assert row[3]
    assert row[4] == 1
    assert shell_count > 0
    assert summary is not None
    assert int(summary[0]) >= 1


def test_refresh_timeout_policy_data_falls_back_to_cycle_session_when_pair_state_missing() -> None:
    db_path = _make_db()
    with sqlite3.connect(db_path) as con:
        _seed_trade_journal(con)
        _seed_bars(con)
        con.commit()

    refresh_checkpoints(db_path)
    refresh_timeout_policy_data(db_path)

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT session_bucket, policy_timeout_minutes, policy_dedupe_minutes
            FROM vanguard_forward_checkpoints
            WHERE trade_id = 'london-1' AND checkpoint_minutes = 30
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "asian"
    assert row[1] == 120
    assert row[2] == 90


def test_ensure_schema_bootstraps_forward_checkpoints_table() -> None:
    db_path = _make_db()
    ensure_schema(db_path)

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'vanguard_forward_checkpoints'
            """
        ).fetchone()
        cols = {
            value[1]
            for value in con.execute("PRAGMA table_info(vanguard_forward_checkpoints)").fetchall()
        }

    assert row is not None
    assert "session_bucket" in cols
    assert "policy_timeout_minutes" in cols
