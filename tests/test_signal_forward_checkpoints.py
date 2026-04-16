from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from vanguard.execution.signal_decision_log import insert_decision
from vanguard.execution.signal_forward_checkpoints import CHECKPOINT_MINUTES, refresh_checkpoints


def _make_db() -> str:
    f = tempfile.NamedTemporaryFile(prefix="vanguard_universe_", suffix=".db", delete=False)
    f.close()
    return f.name


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
    start = datetime(2026, 4, 15, 13, 1, tzinfo=timezone.utc)
    rows = []
    for minute in range(20):
        ts = (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
        close = 1.2000 + (minute + 1) * 0.0001
        rows.append(("EURUSD", ts, close + 0.0002, close - 0.0002, close))
    con.executemany(
        "INSERT INTO vanguard_bars_1m (symbol, bar_ts_utc, high, low, close) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def test_refresh_signal_forward_checkpoints_tracks_all_candidates() -> None:
    db_path = _make_db()
    with sqlite3.connect(db_path) as con:
        _seed_bars(con)
        con.commit()

    decision_id = insert_decision(
        db_path,
        cycle_ts_utc="2026-04-15T13:00:10Z",
        profile_id="gft_10k",
        symbol="EURUSD",
        direction="LONG",
        asset_class="forex",
        session_bucket="ny",
        entry_price=1.2,
        stop_pips=20.0,
        execution_decision="SKIPPED_NO_CAPACITY",
        decision_reason_code="TEST",
    )

    written = refresh_checkpoints(db_path)
    assert written == len(CHECKPOINT_MINUTES)

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT bars_used, status, close_move_pips, close_move_r
            FROM vanguard_signal_forward_checkpoints
            WHERE decision_id = ? AND checkpoint_minutes = 15
            """,
            (decision_id,),
        ).fetchone()
    assert row[0] == 15
    assert row[1] == "READY"
    assert row[2] is not None
    assert row[3] is not None


def test_refresh_signal_forward_checkpoints_revisits_pending_rows() -> None:
    db_path = _make_db()
    with sqlite3.connect(db_path) as con:
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
        start = datetime(2026, 4, 15, 13, 1, tzinfo=timezone.utc)
        early_rows = []
        for minute in range(5):
            ts = (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
            close = 1.2000 + (minute + 1) * 0.0001
            early_rows.append(("EURUSD", ts, close + 0.0002, close - 0.0002, close))
        con.executemany(
            "INSERT INTO vanguard_bars_1m (symbol, bar_ts_utc, high, low, close) VALUES (?, ?, ?, ?, ?)",
            early_rows,
        )
        con.commit()

    decision_id = insert_decision(
        db_path,
        cycle_ts_utc="2026-04-15T13:00:10Z",
        profile_id="gft_10k",
        symbol="EURUSD",
        direction="LONG",
        asset_class="forex",
        session_bucket="ny",
        entry_price=1.2,
        stop_pips=20.0,
        execution_decision="SKIPPED_NO_CAPACITY",
        decision_reason_code="TEST",
    )

    refresh_checkpoints(db_path, decision_ids=[decision_id])

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT bars_used, status
            FROM vanguard_signal_forward_checkpoints
            WHERE decision_id = ? AND checkpoint_minutes = 15
            """,
            (decision_id,),
        ).fetchone()
        assert row == (5, "PARTIAL")

        later_rows = []
        for minute in range(5, 16):
            ts = (start + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")
            close = 1.2000 + (minute + 1) * 0.0001
            later_rows.append(("EURUSD", ts, close + 0.0002, close - 0.0002, close))
        con.executemany(
            "INSERT INTO vanguard_bars_1m (symbol, bar_ts_utc, high, low, close) VALUES (?, ?, ?, ?, ?)",
            later_rows,
        )
        con.commit()

    refresh_checkpoints(db_path)

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT bars_used, status
            FROM vanguard_signal_forward_checkpoints
            WHERE decision_id = ? AND checkpoint_minutes = 15
            """,
            (decision_id,),
        ).fetchone()
    assert row == (15, "READY")
