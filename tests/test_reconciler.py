from __future__ import annotations

import sqlite3
import tempfile

from vanguard.execution.open_positions_writer import ensure_table as ensure_open_positions
from vanguard.execution.reconciler import Divergence, ensure_table as ensure_recon_table, resolve_db_open_broker_closed
from vanguard.execution.trade_journal import (
    ensure_table as ensure_journal_table,
    insert_approval_row,
    update_filled,
)


def _make_db() -> str:
    f = tempfile.NamedTemporaryFile(prefix="vanguard_universe_", suffix=".db", delete=False)
    f.close()
    ensure_recon_table(f.name)
    ensure_journal_table(f.name)
    ensure_open_positions(f.name)
    return f.name


def test_reconciled_close_uses_last_open_position_pnl_snapshot() -> None:
    db_path = _make_db()
    trade_id = insert_approval_row(
        db_path=db_path,
        profile_id="ftmo_demo_100k",
        candidate={
            "symbol": "AUDUSD",
            "asset_class": "forex",
            "side": "SHORT",
            "entry_price": 0.71769,
            "stop_price": 0.71804,
            "tp_price": 0.71714,
            "shares_or_lots": 16.0,
        },
        policy_decision={"decision": "APPROVED"},
        cycle_ts_utc="2026-04-15T19:04:52Z",
    )
    update_filled(
        db_path=db_path,
        trade_id=trade_id,
        broker_position_id="426897189",
        fill_price=0.71769,
        fill_qty=16.0,
        filled_at_utc="2026-04-15T19:06:08Z",
        expected_entry=0.71769,
        side="SHORT",
    )

    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO vanguard_open_positions (
                profile_id, broker_position_id, symbol, side, qty, entry_price,
                current_sl, current_tp, opened_at_utc, unrealized_pnl, last_synced_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ftmo_demo_100k",
                "426897189",
                "AUDUSD",
                "SHORT",
                16.0,
                0.71769,
                0.71804,
                0.71714,
                "2026-04-15T19:06:08Z",
                -24.28,
                "2026-04-15T20:04:20Z",
            ),
        )
        con.commit()

    divergence = Divergence(
        divergence_type="DB_OPEN_BROKER_CLOSED",
        profile_id="ftmo_demo_100k",
        broker_position_id="426897189",
        db_trade_id=trade_id,
        symbol="AUDUSD",
        details={},
    )

    resolve_db_open_broker_closed(db_path, divergence, broker_last_known_deal=None)

    with sqlite3.connect(db_path) as con:
        row = con.execute(
            """
            SELECT status, close_reason, broker_close_reason, close_detected_via,
                   realized_pnl, notes
            FROM vanguard_trade_journal
            WHERE trade_id = ?
            """,
            (trade_id,),
        ).fetchone()

    assert row[0] == "RECONCILED_CLOSED"
    assert row[1] == "RECONCILED"
    assert row[2] == "UNKNOWN_EXTERNAL_CLOSE"
    assert row[3] == "reconciler"
    assert row[4] == -24.28
    assert "realized_pnl_source=open_positions_unrealized_snapshot" in (row[5] or "")
