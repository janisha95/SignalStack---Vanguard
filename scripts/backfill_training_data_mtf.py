#!/usr/bin/env python3
"""
backfill_training_data_mtf.py — Assemble MTF training dataset.

For each (symbol, timeframe) combination:
  1. Read bars at that timeframe
  2. Read Volume Profile features (joined by symbol + timestamp)
  3. Compute forward returns at the horizon appropriate to the TF
  4. Compute Triple Barrier Method (TBM) labels
  5. Write to vanguard_training_data_mtf

CRITICAL: No forward leakage. forward_return uses STRICTLY future bars.

Reads:  vanguard_bars_5m, vanguard_bars_10m, vanguard_bars_15m, vanguard_bars_30m,
        vanguard_features_vp, vanguard_universe_members
Writes: vanguard_training_data_mtf

Prerequisites:
  - backfill_mtf_bars.py must have completed (to populate 10m/15m/30m tables)
  - backfill_volume_profile.py must have completed (for VP features on 5m bars)

Usage:
  python3 scripts/backfill_training_data_mtf.py
  python3 scripts/backfill_training_data_mtf.py --asset-class forex
  python3 scripts/backfill_training_data_mtf.py --symbols EURUSD,GBPUSD
  python3 scripts/backfill_training_data_mtf.py --timeframe 15m

Estimated runtime: 4–6 hours.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_training_data_mtf")

DB_PATH = str(ROOT / "data" / "vanguard_universe.db")

# Forward horizon in bars for each timeframe
HORIZONS: dict[str, int] = {
    "5m":  12,  # 12 × 5m  = 60 min
    "10m":  6,  # 6  × 10m = 60 min
    "15m":  4,  # 4  × 15m = 60 min
    "30m":  4,  # 4  × 30m = 120 min
}

# TBM parameters
TBM_PROFIT_MULT = 2.0   # TP at profit_mult × ATR
TBM_STOP_MULT   = 1.0   # SL at stop_mult × ATR
ATR_PERIOD      = 14

# Commit every N symbols
COMMIT_EVERY = 5

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS vanguard_training_data_mtf (
    symbol TEXT NOT NULL,
    asset_class TEXT,
    timeframe TEXT NOT NULL,
    asof_ts TEXT NOT NULL,
    forward_return REAL,
    tbm_label REAL,
    direction TEXT,
    PRIMARY KEY (symbol, timeframe, asof_ts)
);
CREATE INDEX IF NOT EXISTS idx_vg_train_mtf_asset_tf
    ON vanguard_training_data_mtf(asset_class, timeframe);
"""

# VP feature column names (joined from vanguard_features_vp)
VP_FEATURES = [
    "poc", "vah", "val",
    "poc_distance", "vah_distance", "val_distance",
    "vp_skew", "volume_delta", "cum_delta", "delta_divergence",
]


def ensure_table(con: sqlite3.Connection) -> None:
    for stmt in CREATE_TABLE.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def _add_vp_columns(con: sqlite3.Connection) -> None:
    """Add VP feature columns to vanguard_training_data_mtf if they don't exist."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(vanguard_training_data_mtf)").fetchall()}
    for col in VP_FEATURES:
        if col not in existing:
            try:
                con.execute(f"ALTER TABLE vanguard_training_data_mtf ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass
    con.commit()


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """True Range and ATR using Wilder smoothing."""
    high = df["high"].astype(float)
    low  = df["low"].astype(float)
    prev_close = df["close"].shift(1).astype(float)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_tbm_labels(df: pd.DataFrame, horizon: int) -> tuple[pd.Series, pd.Series]:
    """
    Triple Barrier Method labels.

    For each bar at index t:
      - Upper barrier: close[t] + TBM_PROFIT_MULT × ATR[t]
      - Lower barrier: close[t] - TBM_STOP_MULT  × ATR[t]
      - Vertical barrier: t + horizon bars

    Label: 1.0 = upper hit first (long win), 0.0 = lower hit first (long loss),
           0.5 = neither hit within horizon.

    Forward return: (close[t + horizon] - close[t]) / close[t]

    Returns: (forward_return Series, tbm_label Series)
    """
    closes = df["close"].astype(float).values
    highs  = df["high"].astype(float).values
    lows   = df["low"].astype(float).values
    atrs   = compute_atr(df).values

    n = len(df)
    fwd_returns = np.full(n, np.nan)
    tbm_labels  = np.full(n, np.nan)

    for t in range(n - horizon):
        entry = closes[t]
        atr   = atrs[t]
        if np.isnan(atr) or atr <= 0 or entry <= 0:
            continue

        upper = entry + TBM_PROFIT_MULT * atr
        lower = entry - TBM_STOP_MULT   * atr

        # Forward return (no leakage: use bar at t+horizon)
        fwd_returns[t] = (closes[t + horizon] - entry) / entry

        # TBM: scan future bars for barrier hit
        label = 0.5  # default: neither barrier hit
        for k in range(1, horizon + 1):
            if t + k >= n:
                break
            h_k = highs[t + k]
            l_k = lows[t + k]
            if h_k >= upper:
                label = 1.0
                break
            if l_k <= lower:
                label = 0.0
                break

        tbm_labels[t] = label

    return pd.Series(fwd_returns, index=df.index), pd.Series(tbm_labels, index=df.index)


def load_bars(con: sqlite3.Connection, symbol: str, timeframe: str) -> pd.DataFrame:
    """Load OHLCV bars for symbol at the given timeframe."""
    table_map = {"5m": "vanguard_bars_5m", "10m": "vanguard_bars_10m",
                 "15m": "vanguard_bars_15m", "30m": "vanguard_bars_30m"}
    table = table_map.get(timeframe)
    if not table:
        return pd.DataFrame()

    df = pd.read_sql_query(
        f"SELECT bar_ts_utc, open, high, low, close, volume FROM {table} WHERE symbol = ? ORDER BY bar_ts_utc",
        con,
        params=(symbol,),
    )
    return df


def load_vp_features(con: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    """Load VP features for symbol (5m resolution)."""
    try:
        df = pd.read_sql_query(
            f"SELECT bar_ts_utc, {', '.join(VP_FEATURES)} FROM vanguard_features_vp WHERE symbol = ? ORDER BY bar_ts_utc",
            con,
            params=(symbol,),
        )
        return df
    except Exception:
        return pd.DataFrame()


def process_symbol_tf(
    con: sqlite3.Connection, symbol: str, asset_class: str | None, timeframe: str
) -> int:
    """Build training rows for one (symbol, timeframe) pair. Returns rows written."""
    horizon = HORIZONS[timeframe]

    # Find the latest already-assembled row.
    last_row = con.execute(
        "SELECT MAX(asof_ts) FROM vanguard_training_data_mtf WHERE symbol = ? AND timeframe = ?",
        (symbol, timeframe),
    ).fetchone()
    last_ts = last_row[0] if last_row and last_row[0] else "2000-01-01T00:00:00Z"

    df = load_bars(con, symbol, timeframe)
    if len(df) < horizon + ATR_PERIOD + 10:
        return 0

    # Filter to only new bars (after last processed)
    df = df[df["bar_ts_utc"] > last_ts].reset_index(drop=True)
    if len(df) < horizon + ATR_PERIOD + 5:
        return 0

    # Compute forward returns and TBM labels
    fwd, labels = compute_tbm_labels(df, horizon)

    # Drop rows with NaN labels (last `horizon` bars have no label yet)
    valid_mask = labels.notna() & fwd.notna()
    df_valid = df[valid_mask].copy()
    if df_valid.empty:
        return 0

    directions = np.where(labels[valid_mask] >= 0.75, "LONG",
                 np.where(labels[valid_mask] <= 0.25, "SHORT", "FLAT"))

    # Optionally join VP features (only available at 5m resolution)
    vp_df = load_vp_features(con, symbol)
    if not vp_df.empty:
        df_valid = df_valid.merge(vp_df, on="bar_ts_utc", how="left")
    else:
        for col in VP_FEATURES:
            df_valid[col] = np.nan

    rows: list[dict[str, Any]] = []
    for idx, (_, row) in enumerate(df_valid.iterrows()):
        record: dict[str, Any] = {
            "symbol":       symbol,
            "asset_class":  asset_class,
            "timeframe":    timeframe,
            "asof_ts":      str(row["bar_ts_utc"]),
            "forward_return": float(fwd.iloc[valid_mask.values.nonzero()[0][idx]]),
            "tbm_label":    float(labels.iloc[valid_mask.values.nonzero()[0][idx]]),
            "direction":    directions[idx],
        }
        for col in VP_FEATURES:
            record[col] = float(row[col]) if col in row and pd.notna(row[col]) else None
        rows.append(record)

    if not rows:
        return 0

    col_names = ["symbol", "asset_class", "timeframe", "asof_ts", "forward_return",
                 "tbm_label", "direction"] + VP_FEATURES
    placeholders = ", ".join(f":{c}" for c in col_names)
    col_str = ", ".join(col_names)

    con.executemany(
        f"INSERT OR REPLACE INTO vanguard_training_data_mtf ({col_str}) VALUES ({placeholders})",
        rows,
    )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble MTF training dataset")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--asset-class", default=None)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--timeframe", default=None, choices=list(HORIZONS.keys()))
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_table(con)
    _add_vp_columns(con)

    # Build (symbol, asset_class) list from universe
    if args.symbols:
        symbol_list = [(s.strip(), None) for s in args.symbols.split(",")]
    elif args.asset_class:
        rows = con.execute(
            "SELECT DISTINCT symbol, asset_class FROM vanguard_universe_members WHERE asset_class = ? ORDER BY symbol",
            (args.asset_class,),
        ).fetchall()
        symbol_list = [(r[0], r[1]) for r in rows]
    else:
        rows = con.execute(
            "SELECT DISTINCT symbol, asset_class FROM vanguard_universe_members ORDER BY symbol"
        ).fetchall()
        symbol_list = [(r[0], r[1]) for r in rows]

    timeframes = [args.timeframe] if args.timeframe else list(HORIZONS.keys())

    logger.info(
        "Processing %d symbols × %s timeframes = %d combinations",
        len(symbol_list), timeframes, len(symbol_list) * len(timeframes),
    )
    t0 = time.time()
    total_rows = 0
    combo_count = 0

    for i, (symbol, ac) in enumerate(symbol_list):
        for tf in timeframes:
            try:
                n = process_symbol_tf(con, symbol, ac, tf)
                total_rows += n
            except Exception as exc:
                logger.warning("  %s/%s: failed — %s", symbol, tf, exc)

        combo_count += len(timeframes)

        if (i + 1) % COMMIT_EVERY == 0:
            con.commit()
            elapsed = time.time() - t0
            pct = (i + 1) / len(symbol_list) * 100
            logger.info(
                "  Progress: %d/%d symbols (%.0f%%) — %d rows — %.0fs",
                i + 1, len(symbol_list), pct, total_rows, elapsed,
            )

    con.commit()
    con.close()

    elapsed = time.time() - t0
    logger.info("Done: %d training rows written in %.0fs", total_rows, elapsed)

    # Print summary
    con2 = sqlite3.connect(args.db)
    for tf in timeframes:
        row = con2.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM vanguard_training_data_mtf WHERE timeframe = ?",
            (tf,),
        ).fetchone()
        logger.info("  %s: %d rows, %d symbols", tf, row[0] or 0, row[1] or 0)
    con2.close()


if __name__ == "__main__":
    main()
