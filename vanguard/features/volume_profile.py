"""
Shared rolling volume-profile helpers for training and live inference.

This module is the single source of truth for computing and incrementally
updating `vanguard_features_vp`. Live factor generation should call the same
logic that the historical backfill script uses so VP45 sees the same feature
contract in training and inference.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

import numpy as np
import pandas as pd

VP_WINDOW = 48
VALUE_AREA_PCT = 0.70
VP_BINS = 50
DELTA_DIV_LOOKBACK = 12

VP_FEATURE_NAMES = (
    "poc",
    "vah",
    "val",
    "poc_distance",
    "vah_distance",
    "val_distance",
    "vp_skew",
    "volume_delta",
    "cum_delta",
    "delta_divergence",
)

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
    for stmt in CREATE_TABLE.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def compute_volume_profile(bars: pd.DataFrame) -> dict[str, float]:
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
    delta = df["volume"].copy().astype(float)
    delta[df["close"] < df["open"]] *= -1.0
    delta[df["close"] == df["open"]] *= 0.0
    return delta


def process_symbol_rows(
    con: sqlite3.Connection,
    symbol: str,
    *,
    max_bar_ts_utc: str | None = None,
) -> int:
    last_row = con.execute(
        "SELECT MAX(bar_ts_utc) FROM vanguard_features_vp WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    last_ts = last_row[0] if last_row and last_row[0] else "2000-01-01T00:00:00Z"

    where_sql = "WHERE symbol = ?"
    params: list[object] = [symbol]
    if max_bar_ts_utc:
        where_sql += " AND bar_ts_utc <= ?"
        params.append(max_bar_ts_utc)

    df = pd.read_sql_query(
        f"""
        SELECT bar_ts_utc, open, high, low, close, volume
        FROM vanguard_bars_5m
        {where_sql}
        ORDER BY bar_ts_utc
        """,
        con,
        params=params,
    )

    if len(df) < VP_WINDOW + 10:
        return 0

    df["bar_ts_utc"] = pd.to_datetime(df["bar_ts_utc"], utc=True)
    df = df.sort_values("bar_ts_utc").reset_index(drop=True)

    last_dt = pd.Timestamp(last_ts, tz="UTC")
    newer_mask = df["bar_ts_utc"] > last_dt
    start_idx = max(VP_WINDOW, int(newer_mask.idxmax()) if newer_mask.any() else len(df))
    if start_idx >= len(df):
        return 0

    deltas = compute_volume_delta(df)
    cum_delta = deltas.cumsum()

    results: list[dict[str, object]] = []
    for j in range(start_idx, len(df)):
        window = df.iloc[j - VP_WINDOW : j]
        vp = compute_volume_profile(window)

        current_close = float(df.iloc[j]["close"])
        ts_str = df.iloc[j]["bar_ts_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")

        poc = vp["poc"]
        vah = vp["vah"]
        val = vp["val"]
        poc_dist = (current_close - poc) / poc if poc else 0.0
        vah_dist = (current_close - vah) / vah if vah else 0.0
        val_dist = (current_close - val) / val if val else 0.0

        delta_div = 0.0
        if j >= VP_WINDOW + DELTA_DIV_LOOKBACK:
            price_chg = float(df.iloc[j]["close"]) - float(df.iloc[j - DELTA_DIV_LOOKBACK]["close"])
            delta_chg = float(cum_delta.iloc[j]) - float(cum_delta.iloc[j - DELTA_DIV_LOOKBACK])
            delta_div = -1.0 if (price_chg * delta_chg < 0) else 1.0

        results.append(
            {
                "symbol": symbol,
                "bar_ts_utc": ts_str,
                "poc": poc,
                "vah": vah,
                "val": val,
                "poc_distance": poc_dist,
                "vah_distance": vah_dist,
                "val_distance": val_dist,
                "vp_skew": vp["vp_skew"],
                "volume_delta": float(deltas.iloc[j]),
                "cum_delta": float(cum_delta.iloc[j]),
                "delta_divergence": delta_div,
            }
        )

    if not results:
        return 0

    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_features_vp
        (symbol, bar_ts_utc, poc, vah, val, poc_distance, vah_distance,
         val_distance, vp_skew, volume_delta, cum_delta, delta_divergence)
        VALUES (:symbol, :bar_ts_utc, :poc, :vah, :val, :poc_distance,
                :vah_distance, :val_distance, :vp_skew, :volume_delta,
                :cum_delta, :delta_divergence)
        """,
        results,
    )
    return len(results)


def update_symbols(
    con: sqlite3.Connection,
    symbols: Iterable[str],
    *,
    max_bar_ts_utc: str | None = None,
) -> dict[str, int]:
    ensure_table(con)
    written: dict[str, int] = {}
    for symbol in symbols:
        n = process_symbol_rows(con, symbol, max_bar_ts_utc=max_bar_ts_utc)
        if n:
            written[symbol] = n
    if written:
        con.commit()
    return written
