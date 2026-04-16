"""
accounts.py — Account profile CRUD for the SignalStack Unified API.

Stores prop-firm account profiles in vanguard_universe.db.
Seeded with 6 default profiles on startup.

Location: ~/SS/Vanguard/vanguard/api/accounts.py
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from vanguard.config.runtime_config import get_profiles_config, get_shadow_db_path
from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

VANGUARD_DB = get_shadow_db_path()

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
    ("risk_per_trade_pct", "REAL DEFAULT 0.005"),
    ("weekly_loss_limit", "REAL"),
    ("volume_limit", "INTEGER"),
    ("no_new_positions_after", "TEXT"),
    ("eod_flatten_time", "TEXT"),
    ("consistency_rule_pct", "REAL"),
    ("max_correlation", "REAL DEFAULT 0.85"),
    ("max_portfolio_heat_pct", "REAL DEFAULT 0.04"),
    ("block_earnings_overnight", "INTEGER DEFAULT 0"),
    ("block_halted", "INTEGER DEFAULT 1"),
    ("stop_atr_multiple", "REAL DEFAULT 1.5"),
    ("tp_atr_multiple", "REAL DEFAULT 3.0"),
    ("min_trade_duration_sec", "INTEGER DEFAULT 0"),
    ("min_trade_range_cents", "REAL DEFAULT 0.0"),
    ("max_trades_per_day", "INTEGER DEFAULT 999"),
    ("scaling_profit_trigger_pct", "REAL DEFAULT 0.0"),
    ("scaling_bp_increase_pct", "REAL DEFAULT 0.0"),
    ("scaling_pause_increase_pct", "REAL DEFAULT 0.0"),
    ("instruments", "TEXT"),
    ("plan_type", "TEXT"),
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
    return sqlite_conn(VANGUARD_DB)


def _default_profile_name(profile_id: str) -> str:
    return " ".join(
        part.upper() if part.isalpha() and len(part) <= 5 else part.capitalize()
        for part in profile_id.split("_")
    )


def _infer_prop_firm(profile_id: str, profile: dict[str, Any]) -> str:
    if profile.get("prop_firm"):
        return str(profile["prop_firm"])
    return str(profile_id.split("_", 1)[0] or "unknown")


def _infer_account_type(profile_id: str, profile: dict[str, Any]) -> str:
    if profile.get("account_type"):
        return str(profile["account_type"])
    if profile.get("holding_style"):
        return str(profile["holding_style"])
    lowered = profile_id.lower()
    if "challenge" in lowered:
        return "challenge"
    if "swing" in lowered:
        return "swing"
    return "intraday"


def _complete_profile(profile: dict[str, Any]) -> dict[str, Any]:
    profile_id = str(profile.get("id") or "").strip()
    if not profile_id:
        raise ValueError("profile id is required")

    base = next((dict(seed) for seed in SEED_PROFILES if seed.get("id") == profile_id), {})
    merged = {**base, **dict(profile)}
    account_size = int(merged.get("account_size") or base.get("account_size") or 0)

    merged["id"] = profile_id
    merged["name"] = str(merged.get("name") or base.get("name") or _default_profile_name(profile_id))
    merged["prop_firm"] = _infer_prop_firm(profile_id, merged)
    merged["account_type"] = _infer_account_type(profile_id, merged)
    merged["account_size"] = account_size
    merged["daily_loss_limit"] = float(
        merged.get("daily_loss_limit")
        or base.get("daily_loss_limit")
        or round(account_size * 0.05, 2)
    )
    merged["max_drawdown"] = float(
        merged.get("max_drawdown")
        or base.get("max_drawdown")
        or round(account_size * 0.10, 2)
    )
    merged["max_positions"] = int(merged.get("max_positions") or base.get("max_positions") or 3)
    merged["must_close_eod"] = int(merged.get("must_close_eod", base.get("must_close_eod", 0)) or 0)
    merged["is_active"] = int(merged.get("is_active", base.get("is_active", 1)) or 0)
    return merged


def _seed_profiles() -> list[dict[str, Any]]:
    profiles = get_profiles_config()
    if not profiles:
        return [dict(p) for p in SEED_PROFILES]
    return [_complete_profile(profile) for profile in profiles]


def init_table() -> None:
    with sqlite_conn(VANGUARD_DB) as con:
        con.execute(CREATE_TABLE)
        existing = {row[1] for row in con.execute("PRAGMA table_info(account_profiles)").fetchall()}
        for col_name, col_type in _NEW_COLUMNS:
            if col_name not in existing:
                con.execute(f"ALTER TABLE account_profiles ADD COLUMN {col_name} {col_type}")
        con.commit()


def seed_profiles() -> None:
    """Insert seed profiles if they don't already exist (idempotent)."""
    init_table()
    with sqlite_conn(VANGUARD_DB) as con:
        table_cols = {
            row[1]
            for row in con.execute("PRAGMA table_info(account_profiles)").fetchall()
            if row[1] != "created_at"
        }
        existing = {r[0] for r in con.execute("SELECT id FROM account_profiles").fetchall()}
        for p in _seed_profiles():
            profile_data = {
                key: value
                for key, value in dict(p).items()
                if key in table_cols
            }
            if "must_close_eod" in table_cols:
                profile_data["must_close_eod"] = int(p.get("must_close_eod", 0) or 0)
            if "is_active" in table_cols:
                profile_data["is_active"] = int(p.get("is_active", 1) or 0)
            if p["id"] not in existing:
                cols = list(profile_data.keys())
                placeholders = ", ".join("?" for _ in cols)
                con.execute(
                    f"""INSERT INTO account_profiles
                       ({", ".join(cols)})
                       VALUES ({placeholders})""",
                    [profile_data[col] for col in cols],
                )
            else:
                update_cols = [col for col in profile_data.keys() if col != "id"]
                if not update_cols:
                    continue
                con.execute(
                    f"""UPDATE account_profiles
                       SET {", ".join(f"{col} = ?" for col in update_cols)}
                       WHERE id = ?""",
                    [profile_data[col] for col in update_cols] + [p["id"]],
                )
        con.commit()
    logger.info("Account profiles seeded")


def list_profiles() -> list[dict[str, Any]]:
    with sqlite_conn(VANGUARD_DB) as con:
        rows = con.execute(
            "SELECT * FROM account_profiles WHERE is_active = 1 ORDER BY account_size ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_profile(profile_id: str) -> dict[str, Any] | None:
    with sqlite_conn(VANGUARD_DB) as con:
        row = con.execute(
            "SELECT * FROM account_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return dict(row) if row else None


def create_profile(data: dict[str, Any]) -> dict[str, Any]:
    with sqlite_conn(VANGUARD_DB) as con:
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
    with sqlite_conn(VANGUARD_DB) as con:
        con.execute(
            f"UPDATE account_profiles SET {set_clause} WHERE id = ?",
            [*updates.values(), profile_id],
        )
        con.commit()
        row = con.execute("SELECT * FROM account_profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row) if row else None


def delete_profile(profile_id: str) -> bool:
    with sqlite_conn(VANGUARD_DB) as con:
        cur = con.execute("DELETE FROM account_profiles WHERE id = ?", (profile_id,))
        con.commit()
    return cur.rowcount > 0
