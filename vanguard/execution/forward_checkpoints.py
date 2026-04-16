"""
forward_checkpoints.py — Model-truth checkpoints for forward-tracked/live trades.

Writes one row per (trade_id, checkpoint_minutes) so model behavior can be
measured independently from shell outcome.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from vanguard.execution.timeout_policy import ensure_schema as ensure_timeout_policy_schema


CHECKPOINT_MINUTES = (15, 30, 45, 60, 90, 120, 150, 180, 240, 300)
ELIGIBLE_STATUSES = ("FORWARD_TRACKED", "OPEN", "CLOSED")
BAR_INTERVAL = timedelta(minutes=1)

DDL = """
CREATE TABLE IF NOT EXISTS vanguard_forward_checkpoints (
    trade_id             TEXT NOT NULL,
    profile_id           TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    side                 TEXT NOT NULL,
    approved_cycle_ts_utc TEXT NOT NULL,
    checkpoint_minutes   INTEGER NOT NULL,
    checkpoint_ts_utc    TEXT NOT NULL,
    bars_used            INTEGER NOT NULL DEFAULT 0,
    price_at_checkpoint  REAL,
    close_move_pips      REAL,
    close_move_bps       REAL,
    is_positive_direction INTEGER,
    mfe_pips             REAL,
    mae_pips             REAL,
    status               TEXT NOT NULL,
    source_bar_ts_utc    TEXT,
    created_at_utc       TEXT NOT NULL,
    PRIMARY KEY (trade_id, checkpoint_minutes)
);
CREATE INDEX IF NOT EXISTS idx_vg_forward_checkpoints_symbol
    ON vanguard_forward_checkpoints(symbol, checkpoint_minutes);
"""


def ensure_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        con.execute(
            """
            UPDATE vanguard_forward_checkpoints
            SET status = 'PARTIAL'
            WHERE status NOT IN ('READY', 'PARTIAL', 'NO_BARS')
            """
        )
        con.commit()
    ensure_timeout_policy_schema(db_path)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


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


def _pip_size(symbol: str) -> float | None:
    sym = str(symbol or "").upper().replace("/", "").strip()
    if not sym:
        return None
    return 0.01 if sym.endswith("JPY") else 0.0001


def _calc_metrics(
    *,
    side: str,
    entry_price: float,
    symbol: str,
    bars: list[sqlite3.Row],
) -> tuple[float | None, float | None, int | None, float | None, float | None, float | None]:
    if not bars or entry_price <= 0:
        return None, None, None, None, None, None

    last = bars[-1]
    close = float(last["close"])
    high_series = [float(b["high"]) for b in bars]
    low_series = [float(b["low"]) for b in bars]
    side_upper = str(side or "").upper()
    pip_size = _pip_size(symbol)

    if side_upper == "LONG":
        close_native = close - entry_price
        mfe_native = max(high_series) - entry_price
        mae_native = min(low_series) - entry_price
    else:
        close_native = entry_price - close
        mfe_native = entry_price - min(low_series)
        mae_native = entry_price - max(high_series)

    close_bps = (close_native / entry_price) * 10000.0
    close_pips = (close_native / pip_size) if pip_size and pip_size > 0 else None
    mfe_pips = (mfe_native / pip_size) if pip_size and pip_size > 0 else None
    mae_pips = (mae_native / pip_size) if pip_size and pip_size > 0 else None
    positive = 1 if close_native > 0 else 0
    return close, close_pips, positive, close_bps, mfe_pips, mae_pips


def _eligible_trade_rows(
    con: sqlite3.Connection,
    trade_ids: Iterable[str] | None = None,
) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    params: list[Any] = list(ELIGIBLE_STATUSES)
    sql = """
        SELECT trade_id, profile_id, symbol, side, approved_cycle_ts_utc, expected_entry, status
        FROM vanguard_trade_journal
        WHERE status IN ({statuses})
    """.format(statuses=",".join("?" for _ in ELIGIBLE_STATUSES))
    trade_ids = [str(tid) for tid in (trade_ids or []) if str(tid).strip()]
    if trade_ids:
        sql += " AND trade_id IN ({ids})".format(ids=",".join("?" for _ in trade_ids))
        params.extend(trade_ids)
    return con.execute(sql, params).fetchall()


def refresh_checkpoints(db_path: str, trade_ids: Iterable[str] | None = None) -> int:
    """
    Upsert checkpoint truth for eligible trade_journal rows.

    Recomputes available checkpoints on every call so PARTIAL/NO_BARS rows can
    later become READY as more bars arrive.
    """
    ensure_table(db_path)
    written = 0
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        trades = _eligible_trade_rows(con, trade_ids=trade_ids)
        if not trades:
            return 0

        symbols = sorted({str(row["symbol"] or "").upper() for row in trades if str(row["symbol"] or "").strip()})
        if not symbols:
            return 0
        earliest = min((_parse_utc(row["approved_cycle_ts_utc"]) for row in trades), default=None)
        if earliest is None:
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
            bars_by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)

        now_s = _now_utc()
        for trade in trades:
            trade_id = str(trade["trade_id"])
            symbol = str(trade["symbol"]).upper()
            side = str(trade["side"]).upper()
            approved_ts = _parse_utc(trade["approved_cycle_ts_utc"])
            entry_price = float(trade["expected_entry"] or 0.0)
            if approved_ts is None or not symbol:
                continue
            symbol_bars = bars_by_symbol.get(symbol, [])
            parsed_bars: list[tuple[datetime, sqlite3.Row]] = []
            for bar in symbol_bars:
                bar_ts = _parse_utc(bar["bar_ts_utc"])
                if bar_ts is None:
                    continue
                parsed_bars.append((bar_ts, bar))

            for minutes in CHECKPOINT_MINUTES:
                checkpoint_ts = approved_ts + timedelta(minutes=int(minutes))
                eligible_bars = [bar for bar_ts, bar in parsed_bars if approved_ts < bar_ts <= checkpoint_ts]
                latest_bar_ts = None
                latest_bar_dt = None
                if eligible_bars:
                    latest_bar_ts = str(eligible_bars[-1]["bar_ts_utc"])
                    latest_bar_dt = _parse_utc(latest_bar_ts)
                status = _status_for_latest_bar(checkpoint_ts, latest_bar_dt)
                price_at_checkpoint, close_move_pips, is_positive, close_move_bps, mfe_pips, mae_pips = _calc_metrics(
                    side=side,
                    entry_price=entry_price,
                    symbol=symbol,
                    bars=eligible_bars,
                )
                con.execute(
                    """
                    INSERT INTO vanguard_forward_checkpoints (
                        trade_id, profile_id, symbol, side, approved_cycle_ts_utc,
                        checkpoint_minutes, checkpoint_ts_utc, bars_used,
                        price_at_checkpoint, close_move_pips, close_move_bps,
                        is_positive_direction, mfe_pips, mae_pips, status,
                        source_bar_ts_utc, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_id, checkpoint_minutes) DO UPDATE SET
                        checkpoint_ts_utc = excluded.checkpoint_ts_utc,
                        bars_used = excluded.bars_used,
                        price_at_checkpoint = excluded.price_at_checkpoint,
                        close_move_pips = excluded.close_move_pips,
                        close_move_bps = excluded.close_move_bps,
                        is_positive_direction = excluded.is_positive_direction,
                        mfe_pips = excluded.mfe_pips,
                        mae_pips = excluded.mae_pips,
                        status = excluded.status,
                        source_bar_ts_utc = excluded.source_bar_ts_utc
                    """,
                    (
                        trade_id,
                        str(trade["profile_id"] or ""),
                        symbol,
                        side,
                        str(trade["approved_cycle_ts_utc"] or ""),
                        int(minutes),
                        _iso_utc(checkpoint_ts),
                        len(eligible_bars),
                        price_at_checkpoint,
                        close_move_pips,
                        close_move_bps,
                        is_positive,
                        mfe_pips,
                        mae_pips,
                        status,
                        latest_bar_ts,
                        now_s,
                    ),
                )
                written += 1
        con.commit()
    return written
