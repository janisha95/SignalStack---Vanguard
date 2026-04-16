"""
account_state.py — Per-profile account state loader (Phase 4 canonical location).

Single responsibility: load_account_state() and seed_account_states() are the
only DB-touching helpers for reading vanguard_account_state. All policy-check
modules receive account state as a plain dict; they never call this directly.

Phase 2b: seeds from profile.account_size if vanguard_account_state row absent.
Phase 3: MetaApi replaces these values with real broker state.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from vanguard.config.runtime_config import get_shadow_db_path
from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

_CREATE_ACCOUNT_STATE = """
CREATE TABLE IF NOT EXISTS vanguard_account_state (
  profile_id            TEXT PRIMARY KEY,
  equity                REAL NOT NULL,
  starting_equity_today REAL NOT NULL,
  daily_pnl_pct         REAL NOT NULL DEFAULT 0.0,
  trailing_dd_pct       REAL NOT NULL DEFAULT 0.0,
  open_positions_json   TEXT NOT NULL DEFAULT '[]',
  paused_until_utc      TEXT,
  pause_reason          TEXT,
  updated_at_utc        TEXT NOT NULL
);
"""

_DEFAULT_STATE: dict[str, Any] = {
    "equity": 0.0,
    "starting_equity_today": 0.0,
    "daily_pnl_pct": 0.0,
    "trailing_dd_pct": 0.0,
    "open_positions": [],
    "paused_until_utc": None,
    "pause_reason": None,
}


def _ensure_schema(db_path: str) -> None:
    with sqlite_conn(db_path) as con:
        con.execute(_CREATE_ACCOUNT_STATE)
        con.commit()


def load_account_state(
    profile_id: str,
    db_path: str | None = None,
    cycle_ts_utc: datetime | None = None,
    seed_equity: float | None = None,
) -> dict[str, Any]:
    """
    Returns account state dict for profile_id:
      {
        "equity": float,
        "starting_equity_today": float,
        "daily_pnl_pct": float,
        "trailing_dd_pct": float,
        "paused_until_utc": str | None,
        "pause_reason": str | None,
        "open_positions": [ {symbol, side, qty, entry, open_ts_utc, unrealized_pnl}, ... ]
      }

    If no row exists, seeds from seed_equity (or 0.0) and returns defaults.
    """
    import json

    db_path = db_path or get_shadow_db_path()

    try:
        _ensure_schema(db_path)

        with sqlite_conn(db_path) as con:
            row = con.execute(
                "SELECT equity, starting_equity_today, daily_pnl_pct, trailing_dd_pct, "
                "open_positions_json, paused_until_utc, pause_reason "
                "FROM vanguard_account_state WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()

        if row:
            try:
                open_positions = json.loads(row[4] or "[]")
            except (ValueError, TypeError):
                open_positions = []
            return {
                "equity": float(row[0] or 0.0),
                "starting_equity_today": float(row[1] or 0.0),
                "daily_pnl_pct": float(row[2] or 0.0),
                "trailing_dd_pct": float(row[3] or 0.0),
                "open_positions": open_positions,
                "paused_until_utc": row[5],
                "pause_reason": row[6],
            }

    except Exception as exc:
        logger.warning("load_account_state(%s): DB error: %s", profile_id, exc)

    # Seed defaults
    equity = float(seed_equity or 0.0)
    now_iso = (cycle_ts_utc or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with sqlite_conn(db_path) as con:
            con.execute(
                "INSERT OR IGNORE INTO vanguard_account_state "
                "(profile_id, equity, starting_equity_today, daily_pnl_pct, "
                " trailing_dd_pct, open_positions_json, updated_at_utc) "
                "VALUES (?, ?, ?, 0.0, 0.0, '[]', ?)",
                (profile_id, equity, equity, now_iso),
            )
            con.commit()
    except Exception as exc:
        logger.debug("load_account_state(%s): seed insert skipped: %s", profile_id, exc)

    return {
        "equity": equity,
        "starting_equity_today": equity,
        "daily_pnl_pct": 0.0,
        "trailing_dd_pct": 0.0,
        "open_positions": [],
        "paused_until_utc": None,
        "pause_reason": None,
    }


def seed_account_states(config: dict, db_path: str | None = None) -> None:
    """
    Seed vanguard_account_state for all active profiles from profile.account_size.
    Idempotent — skips profiles that already have a row.
    """
    db_path = db_path or get_shadow_db_path()
    _ensure_schema(db_path)

    profiles = config.get("profiles") or []
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with sqlite_conn(db_path) as con:
        for p in profiles:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            equity = float(p.get("account_size") or 0.0)
            con.execute(
                "INSERT OR IGNORE INTO vanguard_account_state "
                "(profile_id, equity, starting_equity_today, daily_pnl_pct, "
                " trailing_dd_pct, open_positions_json, updated_at_utc) "
                "VALUES (?, ?, ?, 0.0, 0.0, '[]', ?)",
                (pid, equity, equity, now_iso),
            )
        con.commit()
    logger.info("seed_account_states: seeded %d profiles", len(profiles))
