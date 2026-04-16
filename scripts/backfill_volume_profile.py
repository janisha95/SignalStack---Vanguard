#!/usr/bin/env python3
"""
backfill_volume_profile.py — Compute Volume Profile features from 5m bars.

For each symbol, for each 5m bar (after a warm-up window):
  - Rolling Volume Profile over the last VP_WINDOW bars
  - POC (Point of Control), VAH, VAL, vp_skew
  - Volume delta and cumulative delta
  - Delta divergence (price vs delta direction disagreement)

Reads:  vanguard_bars_5m
Writes: vanguard_features_vp (symbol, bar_ts_utc, poc, vah, val,
        poc_distance, vah_distance, val_distance, vp_skew,
        volume_delta, cum_delta, delta_divergence)

Usage:
  python3 scripts/backfill_volume_profile.py
  python3 scripts/backfill_volume_profile.py --asset-class forex
  python3 scripts/backfill_volume_profile.py --symbols EURUSD,GBPUSD

Estimated runtime: 4–6 hours.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vanguard.features.volume_profile import (
    ensure_table as shared_ensure_table,
    process_symbol_rows as shared_process_symbol_rows,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_volume_profile")

DB_PATH = str(ROOT / "data" / "vanguard_universe.db")

# Rolling window of 5m bars for VP calculation (48 bars = 4 hours)
VP_WINDOW = 48

# Value Area encloses this fraction of total volume
VALUE_AREA_PCT = 0.70

# Price bins per window
VP_BINS = 50

# Divergence lookback in bars
DELTA_DIV_LOOKBACK = 12

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS vanguard_features_vp (
    symbol TEXT NOT NULL,
    bar_ts_utc TEXT NOT NULL,
    poc REAL,
    vah REAL,
    val REAL,
    poc_distance REAL,
    vah_distance REAL,
    val_distance REAL,
    vp_skew REAL,
    volume_delta REAL,
    cum_delta REAL,
    delta_divergence REAL,
    PRIMARY KEY (symbol, bar_ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_vg_features_vp_symbol_ts
    ON vanguard_features_vp(symbol, bar_ts_utc);
"""


def ensure_table(con: sqlite3.Connection) -> None:
    shared_ensure_table(con)


def compute_volume_profile(bars: pd.DataFrame) -> dict:
    """
    Compute Volume Profile statistics from a window of OHLCV bars.

    Returns:
        poc:     Price level with highest volume
        vah:     Value Area High
        val:     Value Area Low
        vp_skew: (VAH - POC) / (POC - VAL) — >1 = bullish skew
    """
    if len(bars) < 10:
        return {"poc": np.nan, "vah": np.nan, "val": np.nan, "vp_skew": np.nan}

    price_min = float(bars["low"].min())
    price_max = float(bars["high"].max())
    if price_max <= price_min:
        return {"poc": np.nan, "vah": np.nan, "val": np.nan, "vp_skew": np.nan}

    bins = np.linspace(price_min, price_max, VP_BINS + 1)
    centers = (bins[:-1] + bins[1:]) / 2.0
    vol_profile = np.zeros(VP_BINS)

    for _, row in bars.iterrows():
        bar_vol = float(row["volume"]) if float(row["volume"]) > 0 else 1.0
        lo, hi = float(row["low"]), float(row["high"])
        mask = (centers >= lo) & (centers <= hi)
        covered = int(mask.sum())
        if covered > 0:
            vol_profile[mask] += bar_vol / covered

    poc_idx = int(np.argmax(vol_profile))
    poc = float(centers[poc_idx])

    total_vol = float(vol_profile.sum())
    if total_vol <= 0:
        return {"poc": poc, "vah": poc, "val": poc, "vp_skew": 1.0}

    target_vol = total_vol * VALUE_AREA_PCT
    lo_idx, hi_idx = poc_idx, poc_idx
    current_vol = float(vol_profile[poc_idx])

    while current_vol < target_vol and (lo_idx > 0 or hi_idx < VP_BINS - 1):
        expand_lo = float(vol_profile[lo_idx - 1]) if lo_idx > 0 else 0.0
        expand_hi = float(vol_profile[hi_idx + 1]) if hi_idx < VP_BINS - 1 else 0.0
        if expand_lo >= expand_hi and lo_idx > 0:
            lo_idx -= 1
            current_vol += expand_lo
        elif hi_idx < VP_BINS - 1:
            hi_idx += 1
            current_vol += expand_hi
        elif lo_idx > 0:
            lo_idx -= 1
            current_vol += expand_lo
        else:
            break

    val = float(centers[lo_idx])
    vah = float(centers[hi_idx])
    denom = poc - val if abs(poc - val) > 1e-10 else 1e-4
    vp_skew = (vah - poc) / denom

    return {"poc": poc, "vah": vah, "val": val, "vp_skew": vp_skew}


def compute_volume_delta(df: pd.DataFrame) -> pd.Series:
    """
    Approximate buy/sell delta per bar.

    Heuristic: close > open → positive delta (buyers dominant),
               close < open → negative delta (sellers dominant).
    """
    delta = df["volume"].copy().astype(float)
    delta[df["close"] < df["open"]] *= -1.0
    delta[df["close"] == df["open"]] *= 0.0
    return delta


def process_symbol(con: sqlite3.Connection, symbol: str, asset_class: str | None) -> int:
    """Compute VP features for all bars of one symbol. Returns rows written."""
    return shared_process_symbol_rows(con, symbol)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Volume Profile features")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--asset-class", default=None)
    parser.add_argument("--symbols", default=None, help="Comma-separated list")
    parser.add_argument("--commit-every", type=int, default=5, help="Commit after N symbols")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_table(con)

    if args.symbols:
        symbols_and_ac = [(s.strip(), None) for s in args.symbols.split(",")]
    elif args.asset_class:
        rows = con.execute(
            "SELECT DISTINCT symbol, asset_class FROM vanguard_bars_5m WHERE asset_class = ? ORDER BY symbol",
            (args.asset_class,),
        ).fetchall()
        symbols_and_ac = [(r[0], r[1]) for r in rows]
    else:
        rows = con.execute(
            "SELECT DISTINCT symbol, asset_class FROM vanguard_bars_5m ORDER BY symbol"
        ).fetchall()
        symbols_and_ac = [(r[0], r[1]) for r in rows]

    logger.info("Processing %d symbols for Volume Profile features", len(symbols_and_ac))
    t0 = time.time()
    total_rows = 0

    for i, (symbol, ac) in enumerate(symbols_and_ac):
        try:
            n = process_symbol(con, symbol, ac)
            total_rows += n
            if n:
                logger.debug("  %s: %d rows written", symbol, n)
        except Exception as exc:
            logger.warning("  %s: failed — %s", symbol, exc)

        if (i + 1) % args.commit_every == 0:
            con.commit()
            elapsed = time.time() - t0
            pct = (i + 1) / len(symbols_and_ac) * 100
            logger.info(
                "  Progress: %d/%d (%.0f%%) — %d total rows — %.0fs elapsed",
                i + 1, len(symbols_and_ac), pct, total_rows, elapsed,
            )

    con.commit()
    con.close()

    elapsed = time.time() - t0
    logger.info("Done: %d rows written in %.0fs", total_rows, elapsed)


if __name__ == "__main__":
    main()
