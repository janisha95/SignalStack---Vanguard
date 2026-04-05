"""
accounts.py — Account profile CRUD for the SignalStack Unified API.

Stores prop-firm account profiles in vanguard_universe.db.
Seeded with 6 default profiles on startup.

Location: ~/SS/Vanguard/vanguard/api/accounts.py
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VANGUARD_ROOT = Path(__file__).resolve().parent.parent.parent
VANGUARD_DB   = VANGUARD_ROOT / "data" / "vanguard_universe.db"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS account_profiles (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    prop_firm           TEXT NOT NULL,
    account_type        TEXT NOT NULL,
    account_size        INTEGER NOT NULL,
    daily_loss_limit    REAL NOT NULL,
    max_drawdown        REAL NOT NULL,
    max_positions       INTEGER NOT NULL,
    must_close_eod      INTEGER DEFAULT 0,
    is_active           INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT (datetime('now'))
);
"""

_NEW_COLUMNS: list[tuple[str, str]] = [
    ("max_single_position_pct", "REAL DEFAULT 0.10"),
    ("max_batch_exposure_pct", "REAL DEFAULT 0.50"),
    ("dll_headroom_pct", "REAL DEFAULT 0.70"),
    ("dd_headroom_pct", "REAL DEFAULT 0.80"),
    ("max_per_sector", "INTEGER DEFAULT 3"),
    ("block_duplicate_symbols", "INTEGER DEFAULT 1"),
    ("environment", "TEXT DEFAULT 'demo'"),
    ("instrument_scope", "TEXT DEFAULT 'us_equities'"),
    ("holding_style", "TEXT DEFAULT 'swing'"),
    ("webhook_url", "TEXT"),
    ("webhook_api_key", "TEXT"),
    ("execution_bridge", "TEXT DEFAULT 'signalstack'"),
]

SEED_PROFILES: list[dict[str, Any]] = [
    {"id": "ttp_20k_swing",     "name": "TTP 20K Swing",     "prop_firm": "ttp",
     "account_type": "swing",    "account_size": 20000,
     "daily_loss_limit": 600,   "max_drawdown": 1400,  "max_positions": 5,  "must_close_eod": 0,
     "max_single_position_pct": 0.15, "max_batch_exposure_pct": 0.60, "dll_headroom_pct": 0.70,
     "dd_headroom_pct": 0.80, "max_per_sector": 2, "block_duplicate_symbols": 1,
     "environment": "demo", "instrument_scope": "us_equities", "holding_style": "swing",
     "webhook_url": "", "webhook_api_key": "", "execution_bridge": "signalstack"},
    {"id": "ttp_40k_swing",     "name": "TTP 40K Swing",     "prop_firm": "ttp",
     "account_type": "swing",    "account_size": 40000,
     "daily_loss_limit": 1200,  "max_drawdown": 2800,  "max_positions": 5,  "must_close_eod": 0,
     "max_single_position_pct": 0.12, "max_batch_exposure_pct": 0.50, "dll_headroom_pct": 0.70,
     "dd_headroom_pct": 0.80, "max_per_sector": 3, "block_duplicate_symbols": 1,
     "environment": "demo", "instrument_scope": "us_equities", "holding_style": "swing",
     "webhook_url": "", "webhook_api_key": "", "execution_bridge": "signalstack"},
    {"id": "ttp_50k_intraday",  "name": "TTP 50K Intraday",  "prop_firm": "ttp",
     "account_type": "intraday", "account_size": 50000,
     "daily_loss_limit": 1000,  "max_drawdown": 2000,  "max_positions": 5,  "must_close_eod": 1,
     "max_single_position_pct": 0.10, "max_batch_exposure_pct": 0.40, "dll_headroom_pct": 0.65,
     "dd_headroom_pct": 0.80, "max_per_sector": 2, "block_duplicate_symbols": 1,
     "environment": "demo", "instrument_scope": "us_equities", "holding_style": "intraday",
     "webhook_url": "", "webhook_api_key": "", "execution_bridge": "signalstack"},
    {"id": "ttp_100k_intraday", "name": "TTP 100K Intraday", "prop_firm": "ttp",
     "account_type": "intraday", "account_size": 100000,
     "daily_loss_limit": 2000,  "max_drawdown": 4000,  "max_positions": 5,  "must_close_eod": 1,
     "max_single_position_pct": 0.08, "max_batch_exposure_pct": 0.40, "dll_headroom_pct": 0.65,
     "dd_headroom_pct": 0.80, "max_per_sector": 3, "block_duplicate_symbols": 1,
     "environment": "demo", "instrument_scope": "us_equities", "holding_style": "intraday",
     "webhook_url": "", "webhook_api_key": "", "execution_bridge": "signalstack"},
    {"id": "topstep_100k",      "name": "TopStep 100K",      "prop_firm": "topstep",
     "account_type": "intraday", "account_size": 100000,
     "daily_loss_limit": 2000,  "max_drawdown": 3000,  "max_positions": 10, "must_close_eod": 1,
     "max_single_position_pct": 0.10, "max_batch_exposure_pct": 0.50, "dll_headroom_pct": 0.70,
     "dd_headroom_pct": 0.80, "max_per_sector": 3, "block_duplicate_symbols": 1,
     "environment": "demo", "instrument_scope": "futures", "holding_style": "intraday",
     "webhook_url": "", "webhook_api_key": "", "execution_bridge": "disabled"},
    {"id": "ftmo_100k",         "name": "FTMO 100K",         "prop_firm": "ftmo",
     "account_type": "swing",    "account_size": 100000,
     "daily_loss_limit": 5000,  "max_drawdown": 10000, "max_positions": 10, "must_close_eod": 0,
     "max_single_position_pct": 0.08, "max_batch_exposure_pct": 0.40, "dll_headroom_pct": 0.70,
     "dd_headroom_pct": 0.80, "max_per_sector": 3, "block_duplicate_symbols": 1,
     "environment": "demo", "instrument_scope": "forex_cfd", "holding_style": "intraday",
     "webhook_url": "", "webhook_api_key": "", "execution_bridge": "mt5"},
]


def _get_conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(VANGUARD_DB))
    con.row_factory = sqlite3.Row
    return con


def init_table() -> None:
    with _get_conn() as con:
        con.execute(CREATE_TABLE)
        existing = {row[1] for row in con.execute("PRAGMA table_info(account_profiles)").fetchall()}
        for col_name, col_type in _NEW_COLUMNS:
            if col_name not in existing:
                con.execute(f"ALTER TABLE account_profiles ADD COLUMN {col_name} {col_type}")
        con.commit()


def seed_profiles() -> None:
    """Insert seed profiles if they don't already exist (idempotent)."""
    init_table()
    with _get_conn() as con:
        existing = {r[0] for r in con.execute("SELECT id FROM account_profiles").fetchall()}
        for p in SEED_PROFILES:
            if p["id"] not in existing:
                con.execute(
                    """INSERT INTO account_profiles
                       (id, name, prop_firm, account_type, account_size,
                        daily_loss_limit, max_drawdown, max_positions, must_close_eod,
                        max_single_position_pct, max_batch_exposure_pct, dll_headroom_pct,
                        dd_headroom_pct, max_per_sector, block_duplicate_symbols,
                        environment, instrument_scope, holding_style, webhook_url, webhook_api_key, execution_bridge)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (p["id"], p["name"], p["prop_firm"], p["account_type"],
                     p["account_size"], p["daily_loss_limit"], p["max_drawdown"],
                     p["max_positions"], p["must_close_eod"],
                     p["max_single_position_pct"], p["max_batch_exposure_pct"], p["dll_headroom_pct"],
                     p["dd_headroom_pct"], p["max_per_sector"], p["block_duplicate_symbols"],
                     p["environment"], p["instrument_scope"], p["holding_style"],
                     p["webhook_url"], p["webhook_api_key"], p["execution_bridge"]),
                )
            else:
                con.execute(
                    """UPDATE account_profiles
                       SET max_single_position_pct = ?,
                           max_batch_exposure_pct = ?,
                           dll_headroom_pct = ?,
                           dd_headroom_pct = ?,
                           max_per_sector = ?,
                           block_duplicate_symbols = ?,
                           environment = ?,
                           instrument_scope = ?,
                           holding_style = ?,
                           webhook_url = ?,
                           webhook_api_key = ?,
                           execution_bridge = ?
                       WHERE id = ?""",
                    (
                        p["max_single_position_pct"],
                        p["max_batch_exposure_pct"],
                        p["dll_headroom_pct"],
                        p["dd_headroom_pct"],
                        p["max_per_sector"],
                        p["block_duplicate_symbols"],
                        p["environment"],
                        p["instrument_scope"],
                        p["holding_style"],
                        p["webhook_url"],
                        p["webhook_api_key"],
                        p["execution_bridge"],
                        p["id"],
                    ),
                )
        con.commit()
    logger.info("Account profiles seeded")


def list_profiles() -> list[dict[str, Any]]:
    with _get_conn() as con:
        rows = con.execute(
            "SELECT * FROM account_profiles WHERE is_active = 1 ORDER BY account_size ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_profile(profile_id: str) -> dict[str, Any] | None:
    with _get_conn() as con:
        row = con.execute(
            "SELECT * FROM account_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return dict(row) if row else None


def create_profile(data: dict[str, Any]) -> dict[str, Any]:
    with _get_conn() as con:
        con.execute(
            """INSERT INTO account_profiles
               (id, name, prop_firm, account_type, account_size,
                daily_loss_limit, max_drawdown, max_positions, must_close_eod, is_active,
                max_single_position_pct, max_batch_exposure_pct, dll_headroom_pct,
                dd_headroom_pct, max_per_sector, block_duplicate_symbols,
                environment, instrument_scope, holding_style, webhook_url, webhook_api_key, execution_bridge)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data["id"], data["name"], data["prop_firm"], data["account_type"],
             data["account_size"], data["daily_loss_limit"], data["max_drawdown"],
             data["max_positions"], data.get("must_close_eod", 0), data.get("is_active", 1),
             data.get("max_single_position_pct", 0.10),
             data.get("max_batch_exposure_pct", 0.50),
             data.get("dll_headroom_pct", 0.70),
             data.get("dd_headroom_pct", 0.80),
             data.get("max_per_sector", 3),
             data.get("block_duplicate_symbols", 1),
             data.get("environment", "demo"),
             data.get("instrument_scope", "us_equities"),
             data.get("holding_style", "swing"),
             data.get("webhook_url"),
             data.get("webhook_api_key"),
             data.get("execution_bridge", "signalstack")),
        )
        con.commit()
        row = con.execute("SELECT * FROM account_profiles WHERE id = ?", (data["id"],)).fetchone()
    return dict(row) if row else {}


def update_profile(profile_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    allowed = ["name", "prop_firm", "account_type", "account_size",
               "daily_loss_limit", "max_drawdown", "max_positions",
               "must_close_eod", "is_active", "max_single_position_pct",
               "max_batch_exposure_pct", "dll_headroom_pct", "dd_headroom_pct",
               "max_per_sector", "block_duplicate_symbols", "environment",
               "instrument_scope", "holding_style", "webhook_url",
               "webhook_api_key", "execution_bridge"]
    updates = {k: data[k] for k in allowed if k in data}
    if not updates:
        return get_profile(profile_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _get_conn() as con:
        con.execute(
            f"UPDATE account_profiles SET {set_clause} WHERE id = ?",
            [*updates.values(), profile_id],
        )
        con.commit()
        row = con.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row) if row else None


def delete_profile(profile_id: str) -> bool:
    with _get_conn() as con:
        cur = con.execute("DELETE FROM account_profiles WHERE id = ?", (profile_id,))
        con.commit()
    return cur.rowcount > 0
