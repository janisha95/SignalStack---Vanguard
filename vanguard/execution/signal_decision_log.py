from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


DDL = """
CREATE TABLE IF NOT EXISTS vanguard_signal_decision_log (
    decision_id              TEXT PRIMARY KEY,
    cycle_ts_utc             TEXT NOT NULL,
    profile_id               TEXT NOT NULL,
    trade_id                 TEXT,
    broker_position_id       TEXT,
    execution_request_id     TEXT,
    symbol                   TEXT NOT NULL,
    direction                TEXT NOT NULL,
    asset_class              TEXT,
    session_bucket           TEXT,
    entry_price              REAL,
    model_id                 TEXT,
    model_family             TEXT,
    model_readiness          TEXT,
    gate_policy              TEXT,
    v6_state                 TEXT,
    predicted_return         REAL,
    edge_score               REAL,
    selection_rank           INTEGER,
    risk_usd                 REAL,
    risk_pct                 REAL,
    lot_size                 REAL,
    stop_pips                REAL,
    tp_pips                  REAL,
    timeout_policy_minutes   INTEGER,
    analysis_dedupe_minutes  INTEGER,
    open_positions_count     INTEGER,
    max_slots                INTEGER,
    free_slots               INTEGER,
    execution_decision       TEXT NOT NULL,
    decision_reason_code     TEXT,
    decision_reason_json     TEXT,
    incumbent_snapshot_json  TEXT,
    created_at_utc           TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vg_signal_decision_cycle_key
    ON vanguard_signal_decision_log(cycle_ts_utc, profile_id, symbol, direction);
CREATE INDEX IF NOT EXISTS idx_vg_signal_decision_profile_cycle
    ON vanguard_signal_decision_log(profile_id, cycle_ts_utc);
CREATE INDEX IF NOT EXISTS idx_vg_signal_decision_outcome
    ON vanguard_signal_decision_log(execution_decision, session_bucket, cycle_ts_utc);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value: Any) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def ensure_table(db_path: str) -> None:
    with sqlite3.connect(db_path, timeout=30) as con:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        _ensure_column(con, "model_readiness", "TEXT")
        _ensure_column(con, "gate_policy", "TEXT")
        _ensure_column(con, "v6_state", "TEXT")
        con.commit()


def _ensure_column(con: sqlite3.Connection, name: str, ddl: str) -> None:
    cols = {row[1] for row in con.execute("PRAGMA table_info(vanguard_signal_decision_log)").fetchall()}
    if name not in cols:
        con.execute(f"ALTER TABLE vanguard_signal_decision_log ADD COLUMN {name} {ddl}")


def insert_decision(
    db_path: str,
    *,
    cycle_ts_utc: str,
    profile_id: str,
    symbol: str,
    direction: str,
    asset_class: str | None = None,
    session_bucket: str | None = None,
    entry_price: float | None = None,
    model_id: str | None = None,
    model_family: str | None = None,
    model_readiness: str | None = None,
    gate_policy: str | None = None,
    v6_state: str | None = None,
    predicted_return: float | None = None,
    edge_score: float | None = None,
    selection_rank: int | None = None,
    risk_usd: float | None = None,
    risk_pct: float | None = None,
    lot_size: float | None = None,
    stop_pips: float | None = None,
    tp_pips: float | None = None,
    timeout_policy_minutes: int | None = None,
    analysis_dedupe_minutes: int | None = None,
    open_positions_count: int | None = None,
    max_slots: int | None = None,
    free_slots: int | None = None,
    execution_decision: str,
    decision_reason_code: str | None = None,
    decision_reason_json: Any = None,
    incumbent_snapshot_json: Any = None,
    trade_id: str | None = None,
    broker_position_id: str | None = None,
    execution_request_id: str | None = None,
    decision_id: str | None = None,
) -> str:
    ensure_table(db_path)
    resolved_decision_id = str(decision_id or uuid.uuid4())
    with sqlite3.connect(db_path, timeout=30) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO vanguard_signal_decision_log (
                decision_id, cycle_ts_utc, profile_id, trade_id, broker_position_id, execution_request_id,
                symbol, direction, asset_class, session_bucket, entry_price,
                model_id, model_family, model_readiness, gate_policy, v6_state, predicted_return, edge_score, selection_rank,
                risk_usd, risk_pct, lot_size, stop_pips, tp_pips,
                timeout_policy_minutes, analysis_dedupe_minutes,
                open_positions_count, max_slots, free_slots,
                execution_decision, decision_reason_code, decision_reason_json, incumbent_snapshot_json,
                created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_decision_id,
                str(cycle_ts_utc or ""),
                str(profile_id or ""),
                trade_id,
                broker_position_id,
                execution_request_id,
                str(symbol or "").upper(),
                str(direction or "").upper(),
                str(asset_class or "").lower() or None,
                str(session_bucket or "").lower() or None,
                entry_price,
                model_id,
                model_family,
                model_readiness,
                gate_policy,
                v6_state,
                predicted_return,
                edge_score,
                selection_rank,
                risk_usd,
                risk_pct,
                lot_size,
                stop_pips,
                tp_pips,
                timeout_policy_minutes,
                analysis_dedupe_minutes,
                open_positions_count,
                max_slots,
                free_slots,
                str(execution_decision or "").upper(),
                decision_reason_code,
                _json(decision_reason_json),
                _json(incumbent_snapshot_json),
                _now_utc(),
            ),
        )
        con.commit()
    return resolved_decision_id


def update_decision(
    db_path: str,
    decision_id: str,
    *,
    trade_id: str | None = None,
    broker_position_id: str | None = None,
    execution_request_id: str | None = None,
    execution_decision: str | None = None,
    decision_reason_code: str | None = None,
    decision_reason_json: Any = None,
) -> None:
    ensure_table(db_path)
    assignments: list[str] = []
    params: list[Any] = []
    if trade_id is not None:
        assignments.append("trade_id = ?")
        params.append(trade_id)
    if broker_position_id is not None:
        assignments.append("broker_position_id = ?")
        params.append(broker_position_id)
    if execution_request_id is not None:
        assignments.append("execution_request_id = ?")
        params.append(execution_request_id)
    if execution_decision is not None:
        assignments.append("execution_decision = ?")
        params.append(str(execution_decision or "").upper())
    if decision_reason_code is not None:
        assignments.append("decision_reason_code = ?")
        params.append(decision_reason_code)
    if decision_reason_json is not None:
        assignments.append("decision_reason_json = ?")
        params.append(_json(decision_reason_json))
    if not assignments:
        return
    params.append(str(decision_id or ""))
    with sqlite3.connect(db_path, timeout=30) as con:
        con.execute(
            f"UPDATE vanguard_signal_decision_log SET {', '.join(assignments)} WHERE decision_id = ?",
            params,
        )
        con.commit()
