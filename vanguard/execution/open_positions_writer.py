"""
open_positions_writer.py — DB upsert for MetaApi open positions mirror.

Semantics:
  - positions == list   → DELETE all rows for profile_id, INSERT new rows (full replace).
  - positions == []     → DELETE all rows for profile_id (broker has no open positions).
  - positions is None   → Do NOTHING (outage case — preserve stale data with old timestamp).

All operations are in a single transaction.
"""
from __future__ import annotations

import logging
from typing import Optional

from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS vanguard_open_positions (
    profile_id            TEXT NOT NULL,
    broker_position_id    TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    qty                   REAL NOT NULL,
    entry_price           REAL NOT NULL,
    current_sl            REAL,
    current_tp            REAL,
    opened_at_utc         TEXT NOT NULL,
    unrealized_pnl        REAL,
    last_synced_at_utc    TEXT NOT NULL,
    PRIMARY KEY (profile_id, broker_position_id)
);
"""


def ensure_table(db_path: str) -> None:
    """Create vanguard_open_positions if it does not exist."""
    with sqlite_conn(db_path) as con:
        con.execute(DDL)


def upsert_open_positions(
    db_path: str,
    profile_id: str,
    positions: Optional[list[dict]],
    synced_at_utc: str,
) -> None:
    """
    Replace all open-position rows for profile_id with current broker truth.

    Args:
        db_path:      Path to SQLite DB.
        profile_id:   Which account profile this sync is for.
        positions:    List of position dicts (from MetaApiClient.get_open_positions()).
                      Pass [] to clear (broker has no open positions).
                      Pass None to skip write entirely (outage — preserve stale data).
        synced_at_utc: ISO-8601 UTC timestamp for last_synced_at_utc column.
    """
    if positions is None:
        logger.warning(
            "[open_positions_writer] outage for profile=%s — skipping write, stale data preserved",
            profile_id,
        )
        return

    ensure_table(db_path)

    with sqlite_conn(db_path) as con:
        con.execute(
            "DELETE FROM vanguard_open_positions WHERE profile_id = ?",
            (profile_id,),
        )
        for pos in positions:
            con.execute(
                """
                INSERT INTO vanguard_open_positions (
                    profile_id, broker_position_id, symbol, side,
                    qty, entry_price, current_sl, current_tp,
                    opened_at_utc, unrealized_pnl, last_synced_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    str(pos["broker_position_id"]),
                    str(pos["symbol"]),
                    str(pos["side"]),
                    float(pos["qty"]),
                    float(pos["entry_price"]),
                    float(pos["current_sl"])  if pos.get("current_sl")  is not None else None,
                    float(pos["current_tp"])  if pos.get("current_tp")  is not None else None,
                    str(pos["opened_at_utc"]),
                    float(pos.get("unrealized_pnl") or 0.0),
                    synced_at_utc,
                ),
            )

    logger.info(
        "[open_positions_writer] profile=%s upserted %d positions at %s",
        profile_id, len(positions), synced_at_utc,
    )
