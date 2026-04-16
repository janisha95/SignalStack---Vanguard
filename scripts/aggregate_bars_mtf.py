#!/usr/bin/env python3
"""
aggregate_bars_mtf.py — Build 15m and 30m bars from existing 5m bars.

Sprint 2, Day 1. Uses pandas.resample() on existing vanguard_bars_5m.
Run once to backfill, then integrate into V1 for ongoing aggregation.

Usage:
  python3 scripts/aggregate_bars_mtf.py                    # all symbols
  python3 scripts/aggregate_bars_mtf.py --asset-class forex
  python3 scripts/aggregate_bars_mtf.py --symbols EURUSD,GBPUSD
  python3 scripts/aggregate_bars_mtf.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = str(ROOT / "data" / "vanguard_universe.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("aggregate_bars_mtf")

# Target timeframes to build from 5m bars
TIMEFRAMES = {
    "10m": "10min",   # pandas resample rule
    "15m": "15min",
    "30m": "30min",
}

CREATE_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS vanguard_bars_{tf} (
    symbol TEXT NOT NULL,
    bar_ts_utc TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    asset_class TEXT,
    source TEXT DEFAULT 'aggregated_from_5m',
    PRIMARY KEY (symbol, bar_ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_vg_bars_{tf}_symbol_ts
    ON vanguard_bars_{tf}(symbol, bar_ts_utc);
"""


def ensure_tables(con: sqlite3.Connection) -> None:
    for tf in TIMEFRAMES:
        for stmt in CREATE_TABLE_TEMPLATE.format(tf=tf).strip().split(";"):
            if stmt.strip():
                con.execute(stmt)
    con.commit()


def get_symbols(con: sqlite3.Connection, asset_class: str | None = None) -> list[str]:
    if asset_class:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM vanguard_bars_5m WHERE asset_class = ? ORDER BY symbol",
            (asset_class,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM vanguard_bars_5m ORDER BY symbol"
        ).fetchall()
    return [r[0] for r in rows]


def aggregate_symbol(con: sqlite3.Connection, symbol: str, tf_name: str,
                     resample_rule: str, dry_run: bool = False) -> int:
    """Aggregate 5m bars to target timeframe for one symbol."""
    table = f"vanguard_bars_{tf_name}"

    # Find what we already have
    last_row = con.execute(
        f"SELECT MAX(bar_ts_utc) FROM {table} WHERE symbol = ?", (symbol,)
    ).fetchone()
    last_ts = last_row[0] if last_row and last_row[0] else "2020-01-01T00:00:00Z"

    # Load 5m bars after last aggregated bar
    df = pd.read_sql_query(
        """SELECT bar_ts_utc, open, high, low, close, volume, asset_class
           FROM vanguard_bars_5m
           WHERE symbol = ? AND bar_ts_utc > ?
           ORDER BY bar_ts_utc""",
        con,
        params=(symbol, last_ts),
    )
    if df.empty or len(df) < 3:
        return 0

    asset_class = df["asset_class"].iloc[0]
    df["bar_ts_utc"] = pd.to_datetime(df["bar_ts_utc"])
    df = df.set_index("bar_ts_utc")

    # OHLCV-correct aggregation
    resampled = df.resample(resample_rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open"])

    if resampled.empty:
        return 0

    resampled = resampled.reset_index()
    resampled["bar_ts_utc"] = resampled["bar_ts_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    resampled["symbol"] = symbol
    resampled["asset_class"] = asset_class
    resampled["source"] = "aggregated_from_5m"

    if dry_run:
        logger.info("[DRY RUN] %s %s: %d bars would be written", symbol, tf_name, len(resampled))
        return len(resampled)

    con.executemany(
        f"""INSERT OR REPLACE INTO {table}
            (symbol, bar_ts_utc, open, high, low, close, volume, asset_class, source)
            VALUES (:symbol, :bar_ts_utc, :open, :high, :low, :close, :volume, :asset_class, :source)""",
        resampled.to_dict("records"),
    )
    return len(resampled)


def main():
    parser = argparse.ArgumentParser(description="Aggregate 5m bars to 15m/30m")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--asset-class", default=None, help="Filter by asset class")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeframe", default=None, help="Only build one TF: 15m or 30m")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")
    ensure_tables(con)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        symbols = get_symbols(con, args.asset_class)

    tfs = {args.timeframe: TIMEFRAMES[args.timeframe]} if args.timeframe else TIMEFRAMES

    logger.info("Aggregating %d symbols across %s timeframes", len(symbols), list(tfs.keys()))
    t0 = time.time()
    total_bars = 0

    for tf_name, rule in tfs.items():
        tf_bars = 0
        for symbol in symbols:
            n = aggregate_symbol(con, symbol, tf_name, rule, dry_run=args.dry_run)
            tf_bars += n

        if not args.dry_run:
            con.commit()

        logger.info("[%s] %d bars written across %d symbols", tf_name, tf_bars, len(symbols))
        total_bars += tf_bars

    elapsed = time.time() - t0
    logger.info("Done: %d total bars in %.1fs", total_bars, elapsed)

    # Quick validation
    for tf_name in tfs:
        table = f"vanguard_bars_{tf_name}"
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        distinct = con.execute(f"SELECT COUNT(DISTINCT symbol) FROM {table}").fetchone()[0]
        latest = con.execute(f"SELECT MAX(bar_ts_utc) FROM {table}").fetchone()[0]
        logger.info("[%s] Total rows=%d, symbols=%d, latest=%s", tf_name, count, distinct, latest)

    con.close()


if __name__ == "__main__":
    main()
