"""
eod_flatten.py — EOD time gate logic for V6 Risk Filters (intraday accounts).

Checks current ET time against account cutoff rules:
  - no_new_positions_after → stop opening new trades
  - eod_flatten_time → flatten all open positions

Location: ~/SS/Vanguard/vanguard/helpers/eod_flatten.py
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

from vanguard.config.runtime_config import get_shadow_db_path
from vanguard.helpers.clock import now_et
from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

_DB_PATH = get_shadow_db_path()


def parse_time(time_str: str | None) -> tuple[int, int] | None:
    """Parse "HH:MM" string to (hour, minute). Returns None if input is None/empty."""
    if not time_str:
        return None
    h, m = time_str.split(":")
    return int(h), int(m)


def check_eod_action(
    must_close_eod: bool | int,
    no_new_positions_after: str | None,
    eod_flatten_time: str | None,
    current_et: datetime | None = None,
) -> str | None:
    """
    Determine what EOD action is required.

    Returns:
        "FLATTEN_ALL"     — flatten all open positions now
        "NO_NEW_TRADES"   — no new position opens, existing managed normally
        None              — normal trading permitted
    """
    if not must_close_eod:
        return None

    if current_et is None:
        current_et = now_et()

    current_hm = (current_et.hour, current_et.minute)

    # Check flatten time first (most aggressive)
    flatten_hm = parse_time(eod_flatten_time)
    if flatten_hm and current_hm >= flatten_hm:
        logger.info(f"EOD FLATTEN: current={current_hm} >= flatten={flatten_hm}")
        return "FLATTEN_ALL"

    # Check no-new-positions cutoff
    no_new_hm = parse_time(no_new_positions_after)
    if no_new_hm and current_hm >= no_new_hm:
        logger.info(f"NO NEW TRADES: current={current_hm} >= cutoff={no_new_hm}")
        return "NO_NEW_TRADES"

    return None


def get_positions_to_flatten(
    account_id: str,
    db_path: str = _DB_PATH,
) -> list[dict[str, Any]]:
    """
    Return all currently APPROVED positions for an account.
    Used to generate flatten orders when EOD is triggered.
    """
    with sqlite_conn(db_path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT cycle_ts_utc, symbol, direction, shares_or_lots, entry_price
            FROM vanguard_tradeable_portfolio
            WHERE account_id = ? AND status = 'APPROVED'
            ORDER BY cycle_ts_utc DESC
            LIMIT 100
            """,
            (account_id,),
        ).fetchall()
    return [dict(r) for r in rows]
