from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from vanguard.execution.forward_checkpoints import BAR_INTERVAL, CHECKPOINT_MINUTES, _calc_metrics, _parse_utc
from vanguard.execution.signal_decision_log import ensure_table as ensure_signal_decision_table
from vanguard.helpers.db import sqlite_conn


DDL = """
CREATE TABLE IF NOT EXISTS vanguard_signal_forward_checkpoints (
    decision_id            TEXT NOT NULL,
    cycle_ts_utc           TEXT NOT NULL,
    profile_id             TEXT NOT NULL,
    symbol                 TEXT NOT NULL,
    direction              TEXT NOT NULL,
    session_bucket         TEXT,
    entry_price            REAL,
    checkpoint_minutes     INTEGER NOT NULL,
    checkpoint_ts_utc      TEXT NOT NULL,
    bars_used              INTEGER NOT NULL DEFAULT 0,
    source_bar_ts_utc      TEXT,
    status                 TEXT NOT NULL,
    price_at_checkpoint    REAL,
    close_move_pips        REAL,
    close_move_bps         REAL,
    close_move_r           REAL,
    mfe_pips               REAL,
    mae_pips               REAL,
    mfe_r                  REAL,
    mae_r                  REAL,
    is_positive_direction  INTEGER,
    created_at_utc         TEXT NOT NULL,
    PRIMARY KEY (decision_id, checkpoint_minutes)
);
CREATE INDEX IF NOT EXISTS idx_vg_signal_forward_profile_cycle
    ON vanguard_signal_forward_checkpoints(profile_id, cycle_ts_utc, checkpoint_minutes);
CREATE INDEX IF NOT EXISTS idx_vg_signal_forward_symbol
    ON vanguard_signal_forward_checkpoints(symbol, checkpoint_minutes);
"""


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_utc() -> str:
    return _iso_utc(datetime.now(timezone.utc))


def _status_for_latest_bar(
    checkpoint_ts: datetime,
    latest_bar_ts: datetime | None,
) -> str:
    if latest_bar_ts is None:
        return "NO_BARS"
    if latest_bar_ts + BAR_INTERVAL >= checkpoint_ts:
        return "READY"
    return "PARTIAL"


def _normalize_r(value_pips: float | None, stop_pips: float | None) -> float | None:
    if value_pips is None:
        return None
    if stop_pips in (None, 0):
        return None
    try:
        if float(stop_pips) <= 0:
            return None
        return float(value_pips) / float(stop_pips)
    except Exception:
        return None


def ensure_table(db_path: str) -> None:
    ensure_signal_decision_table(db_path)
    with sqlite3.connect(db_path, timeout=30) as con:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        con.commit()


def _eligible_decision_rows(
    con: sqlite3.Connection,
    decision_ids: Iterable[str] | None = None,
) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    decision_ids = [str(decision_id) for decision_id in (decision_ids or []) if str(decision_id).strip()]
    params: list[Any] = []
    if decision_ids:
        sql = """
            SELECT decision_id, cycle_ts_utc, profile_id, symbol, direction,
                   session_bucket, entry_price, stop_pips
            FROM vanguard_signal_decision_log
            WHERE cycle_ts_utc IS NOT NULL
              AND symbol IS NOT NULL
              AND direction IS NOT NULL
              AND decision_id IN ({ids})
        """.format(ids=",".join("?" for _ in decision_ids))
        params.extend(decision_ids)
        return con.execute(sql, params).fetchall()

    sql = """
        WITH checkpoint_rollup AS (
            SELECT decision_id,
                   COUNT(*) AS checkpoint_count,
                   SUM(CASE WHEN status = 'READY' THEN 1 ELSE 0 END) AS ready_count
            FROM vanguard_signal_forward_checkpoints
            GROUP BY decision_id
        )
        SELECT d.decision_id, d.cycle_ts_utc, d.profile_id, d.symbol, d.direction,
               d.session_bucket, d.entry_price, d.stop_pips
        FROM vanguard_signal_decision_log d
        LEFT JOIN checkpoint_rollup c
          ON c.decision_id = d.decision_id
        WHERE d.cycle_ts_utc IS NOT NULL
          AND d.symbol IS NOT NULL
          AND d.direction IS NOT NULL
          AND (
                c.decision_id IS NULL
                OR c.checkpoint_count < ?
                OR c.ready_count < ?
          )
    """
    params.extend([len(CHECKPOINT_MINUTES), len(CHECKPOINT_MINUTES)])
    return con.execute(sql, params).fetchall()


def refresh_checkpoints(db_path: str, decision_ids: Iterable[str] | None = None) -> int:
    ensure_table(db_path)
    written = 0
    with sqlite_conn(db_path) as con:
        con.row_factory = sqlite3.Row
        decisions = _eligible_decision_rows(con, decision_ids=decision_ids)
        if not decisions:
            return 0

        symbols = sorted({str(row["symbol"] or "").upper() for row in decisions if str(row["symbol"] or "").strip()})
        earliest = min((_parse_utc(row["cycle_ts_utc"]) for row in decisions), default=None)
        if not symbols or earliest is None:
            return 0

        start_ts = _iso_utc(earliest - timedelta(minutes=1))
        bar_sql = """
            SELECT symbol, bar_ts_utc, high, low, close
            FROM vanguard_bars_1m
            WHERE bar_ts_utc >= ?
              AND symbol IN ({symbols})
            ORDER BY symbol, bar_ts_utc
        """.format(symbols=",".join("?" for _ in symbols))
        bar_rows = con.execute(bar_sql, [start_ts, *symbols]).fetchall()
        bars_by_symbol: dict[str, list[sqlite3.Row]] = {}
        for row in bar_rows:
            bars_by_symbol.setdefault(str(row["symbol"] or "").upper(), []).append(row)

        now_s = _now_utc()
        for decision in decisions:
            cycle_dt = _parse_utc(decision["cycle_ts_utc"])
            if cycle_dt is None:
                continue
            symbol = str(decision["symbol"] or "").upper()
            direction = str(decision["direction"] or "").upper()
            entry_price = float(decision["entry_price"] or 0.0)
            stop_pips = float(decision["stop_pips"] or 0.0) if decision["stop_pips"] not in (None, "") else None
            parsed_bars: list[tuple[datetime, sqlite3.Row]] = []
            for bar in bars_by_symbol.get(symbol, []):
                bar_ts = _parse_utc(bar["bar_ts_utc"])
                if bar_ts is None:
                    continue
                parsed_bars.append((bar_ts, bar))

            for minutes in CHECKPOINT_MINUTES:
                checkpoint_dt = cycle_dt + timedelta(minutes=int(minutes))
                eligible_bars = [bar for bar_ts, bar in parsed_bars if cycle_dt < bar_ts <= checkpoint_dt]
                latest_bar_ts = None
                latest_bar_dt = None
                if eligible_bars:
                    latest_bar_ts = str(eligible_bars[-1]["bar_ts_utc"])
                    latest_bar_dt = _parse_utc(latest_bar_ts)
                status = _status_for_latest_bar(checkpoint_dt, latest_bar_dt)
                price_at_checkpoint, close_move_pips, is_positive, close_move_bps, mfe_pips, mae_pips = _calc_metrics(
                    side=direction,
                    entry_price=entry_price,
                    symbol=symbol,
                    bars=eligible_bars,
                )
                con.execute(
                    """
                    INSERT INTO vanguard_signal_forward_checkpoints (
                        decision_id, cycle_ts_utc, profile_id, symbol, direction, session_bucket,
                        entry_price, checkpoint_minutes, checkpoint_ts_utc, bars_used,
                        source_bar_ts_utc, status, price_at_checkpoint, close_move_pips,
                        close_move_bps, close_move_r, mfe_pips, mae_pips, mfe_r, mae_r,
                        is_positive_direction, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(decision_id, checkpoint_minutes) DO UPDATE SET
                        bars_used = excluded.bars_used,
                        source_bar_ts_utc = excluded.source_bar_ts_utc,
                        status = excluded.status,
                        price_at_checkpoint = excluded.price_at_checkpoint,
                        close_move_pips = excluded.close_move_pips,
                        close_move_bps = excluded.close_move_bps,
                        close_move_r = excluded.close_move_r,
                        mfe_pips = excluded.mfe_pips,
                        mae_pips = excluded.mae_pips,
                        mfe_r = excluded.mfe_r,
                        mae_r = excluded.mae_r,
                        is_positive_direction = excluded.is_positive_direction
                    """,
                    (
                        str(decision["decision_id"] or ""),
                        str(decision["cycle_ts_utc"] or ""),
                        str(decision["profile_id"] or ""),
                        symbol,
                        direction,
                        str(decision["session_bucket"] or "").lower() or None,
                        entry_price,
                        int(minutes),
                        _iso_utc(checkpoint_dt),
                        len(eligible_bars),
                        latest_bar_ts,
                        status,
                        price_at_checkpoint,
                        close_move_pips,
                        close_move_bps,
                        _normalize_r(close_move_pips, stop_pips),
                        mfe_pips,
                        mae_pips,
                        _normalize_r(mfe_pips, stop_pips),
                        _normalize_r(mae_pips, stop_pips),
                        is_positive,
                        now_s,
                    ),
                )
                written += 1
    return written
