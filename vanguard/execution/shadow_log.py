"""
shadow_log.py — Shadow execution log for Phase 5.

Single responsibility: DDL + insert helper for vanguard_shadow_execution_log.
Every would-be broker submit in manual/test mode (and live mode for comparison)
writes a row here. Used later to diff paper decisions vs real fills.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS vanguard_shadow_execution_log (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts_utc                TEXT NOT NULL,
    profile_id                  TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    side                        TEXT NOT NULL,
    would_have_submitted_at_utc TEXT NOT NULL,
    qty                         REAL NOT NULL,
    expected_entry              REAL NOT NULL,
    expected_sl                 REAL NOT NULL,
    expected_tp                 REAL NOT NULL,
    execution_mode              TEXT NOT NULL,
    metaapi_payload_json        TEXT NOT NULL,
    notes                       TEXT
);
CREATE INDEX IF NOT EXISTS idx_shadow_log_profile_cycle
    ON vanguard_shadow_execution_log(profile_id, cycle_ts_utc);
"""

DDL_AUDIT = """
CREATE TABLE IF NOT EXISTS vanguard_config_audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    updated_at_utc      TEXT NOT NULL,
    old_config_version  TEXT,
    new_config_version  TEXT NOT NULL,
    section_changed     TEXT NOT NULL,
    diff_summary        TEXT NOT NULL,
    full_new_config_json TEXT NOT NULL
);
"""


def ensure_tables(db_path: str) -> None:
    """Create shadow_execution_log and config_audit_log tables if absent."""
    with sqlite3.connect(db_path) as con:
        for stmt in DDL.strip().split(";"):
            s = stmt.strip()
            if s:
                con.execute(s)
        for stmt in DDL_AUDIT.strip().split(";"):
            s = stmt.strip()
            if s:
                con.execute(s)
        con.commit()


def insert_shadow_row(
    db_path: str,
    *,
    cycle_ts_utc: str,
    profile_id: str,
    symbol: str,
    side: str,
    qty: float,
    expected_entry: float,
    expected_sl: float,
    expected_tp: float,
    execution_mode: str,
    metaapi_payload: dict,
    notes: str | None = None,
) -> int:
    """
    Insert one row into vanguard_shadow_execution_log. Returns the new row id.
    Safe to call from test/manual/live mode.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload_json = json.dumps(metaapi_payload, default=str)
    try:
        with sqlite3.connect(db_path) as con:
            cur = con.execute(
                """
                INSERT INTO vanguard_shadow_execution_log
                  (cycle_ts_utc, profile_id, symbol, side,
                   would_have_submitted_at_utc, qty, expected_entry,
                   expected_sl, expected_tp, execution_mode,
                   metaapi_payload_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_ts_utc, profile_id, symbol, side,
                    now_utc, float(qty), float(expected_entry),
                    float(expected_sl), float(expected_tp), execution_mode,
                    payload_json, notes,
                ),
            )
            con.commit()
            return cur.lastrowid or 0
    except Exception as exc:
        logger.error("shadow_log.insert_shadow_row failed: %s", exc)
        return 0


def insert_audit_row(
    db_path: str,
    *,
    old_version: str | None,
    new_version: str,
    section_changed: str,
    diff_summary: str,
    full_new_config: dict,
) -> None:
    """Append a row to vanguard_config_audit_log."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with sqlite3.connect(db_path) as con:
            con.execute(
                """
                INSERT INTO vanguard_config_audit_log
                  (updated_at_utc, old_config_version, new_config_version,
                   section_changed, diff_summary, full_new_config_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    now_utc, old_version, new_version,
                    section_changed, diff_summary,
                    json.dumps(full_new_config, default=str),
                ),
            )
            con.commit()
    except Exception as exc:
        logger.error("shadow_log.insert_audit_row failed: %s", exc)
