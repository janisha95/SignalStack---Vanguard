"""
portfolio_state.py — Per-account portfolio state management for V6 Risk Filters.

Reads/writes vanguard_portfolio_state table. Each row = one account × one date.
Daily Pause level is computed once at start of day and stored — it never
changes intraday (per TTP platform rules).

Location: ~/SS/Vanguard/vanguard/helpers/portfolio_state.py
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Any

from vanguard.config.runtime_config import get_shadow_db_path
from vanguard.helpers.clock import now_utc
from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

_DB_PATH = get_shadow_db_path()

_CREATE_STATE = """
CREATE TABLE IF NOT EXISTS vanguard_portfolio_state (
    account_id              TEXT    NOT NULL,
    date                    TEXT    NOT NULL,
    open_positions          INTEGER DEFAULT 0,
    trades_today            INTEGER DEFAULT 0,
    daily_realized_pnl      REAL    DEFAULT 0.0,
    daily_unrealized_pnl    REAL    DEFAULT 0.0,
    weekly_realized_pnl     REAL    DEFAULT 0.0,
    total_pnl               REAL    DEFAULT 0.0,
    max_drawdown            REAL    DEFAULT 0.0,
    heat_used_pct           REAL    DEFAULT 0.0,
    best_day_pnl            REAL    DEFAULT 0.0,
    todays_volume           INTEGER DEFAULT 0,
    todays_fees             REAL    DEFAULT 0.0,
    daily_pause_level       REAL,
    max_loss_level          REAL,
    status                  TEXT    DEFAULT 'ACTIVE',
    last_updated_utc        TEXT,
    PRIMARY KEY (account_id, date)
);
"""


def ensure_schema(db_path: str = _DB_PATH) -> None:
    with sqlite_conn(db_path) as con:
        con.execute(_CREATE_STATE)
        con.commit()


def load_state(
    account_id: str,
    date_str: str | None = None,
    db_path: str = _DB_PATH,
) -> dict[str, Any] | None:
    """Load state for account_id on date_str (default: today)."""
    if date_str is None:
        date_str = date.today().isoformat()
    with sqlite_conn(db_path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM vanguard_portfolio_state WHERE account_id = ? AND date = ?",
            (account_id, date_str),
        ).fetchone()
    return dict(row) if row else None


def save_state(state: dict[str, Any], db_path: str = _DB_PATH) -> None:
    """Upsert a state row."""
    ensure_schema(db_path)
    state["last_updated_utc"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    cols = list(state.keys())
    vals = list(state.values())
    placeholders = ", ".join("?" * len(cols))
    col_names    = ", ".join(cols)
    with sqlite_conn(db_path) as con:
        con.execute(
            f"INSERT OR REPLACE INTO vanguard_portfolio_state ({col_names}) VALUES ({placeholders})",
            vals,
        )
        con.commit()


def get_or_init_state(
    account_id: str,
    account: dict[str, Any],
    db_path: str = _DB_PATH,
) -> dict[str, Any]:
    """
    Load today's state for account_id, or create a fresh one.

    Daily Pause level is computed ONCE at the start of the day:
        daily_pause_level = account_size - daily_loss_limit
    Max Loss level similarly:
        max_loss_level = account_size - max_drawdown
    """
    ensure_schema(db_path)
    date_str = date.today().isoformat()
    state = load_state(account_id, date_str, db_path)

    if state is None:
        account_size     = float(account.get("account_size", 100_000))
        daily_loss_limit = float(account.get("daily_loss_limit", 2_000))
        max_drawdown     = float(account.get("max_drawdown", 4_000))

        state = {
            "account_id":           account_id,
            "date":                 date_str,
            "open_positions":       0,
            "trades_today":         0,
            "daily_realized_pnl":   0.0,
            "daily_unrealized_pnl": 0.0,
            "weekly_realized_pnl":  0.0,
            "total_pnl":            0.0,
            "max_drawdown":         0.0,
            "heat_used_pct":        0.0,
            "best_day_pnl":         0.0,
            "todays_volume":        0,
            "todays_fees":          0.0,
            # Computed ONCE at start of day — fixed for the entire day
            "daily_pause_level":    account_size - daily_loss_limit,
            "max_loss_level":       account_size - max_drawdown,
            "status":               "ACTIVE",
            "last_updated_utc":     None,
        }
        save_state(state, db_path)
        logger.info(
            f"Initialized state for {account_id}: "
            f"daily_pause_level=${state['daily_pause_level']:,.0f} "
            f"max_loss_level=${state['max_loss_level']:,.0f}"
        )

    return state


def update_state_field(
    account_id: str,
    field: str,
    value: Any,
    db_path: str = _DB_PATH,
) -> None:
    """Update a single field in today's state."""
    date_str = date.today().isoformat()
    with sqlite_conn(db_path) as con:
        con.execute(
            f"UPDATE vanguard_portfolio_state SET {field} = ?, last_updated_utc = ? "
            "WHERE account_id = ? AND date = ?",
            (value, now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), account_id, date_str),
        )
        con.commit()


def get_account_status(
    account_id: str,
    state: dict[str, Any],
    account: dict[str, Any],
) -> str:
    """
    Determine account trading status from state.
    Returns one of: ACTIVE, DAILY_PAUSED, MAX_LOSS, WEEKLY_PAUSED, VOLUME_LIMIT
    """
    # Already terminated
    if state.get("status") in ("MAX_LOSS", "PAUSED"):
        return state["status"]

    account_size  = float(account.get("account_size", 100_000))
    running_bal   = account_size + state.get("daily_realized_pnl", 0.0)

    # Max Loss check (absolute — account terminated)
    max_loss_level = state.get("max_loss_level", account_size - float(account.get("max_drawdown", 4_000)))
    if running_bal <= max_loss_level:
        return "MAX_LOSS"

    # Daily Pause check (fixed at start of day)
    daily_pause_level = state.get("daily_pause_level", account_size - float(account.get("daily_loss_limit", 2_000)))
    if running_bal <= daily_pause_level:
        return "DAILY_PAUSED"

    # Weekly Loss check
    weekly_limit = account.get("weekly_loss_limit")
    if weekly_limit and abs(state.get("weekly_realized_pnl", 0.0)) >= weekly_limit:
        return "WEEKLY_PAUSED"

    # Volume Limit check (TTP equities volume)
    volume_limit = account.get("volume_limit")
    if volume_limit and state.get("todays_volume", 0) >= volume_limit:
        return "VOLUME_LIMIT"

    return "ACTIVE"
