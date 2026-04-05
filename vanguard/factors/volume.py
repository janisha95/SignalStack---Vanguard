"""
volume.py — Volume factor module for Vanguard V3.

Computes 5 features from 5m bars:
  1. relative_volume     — volume / SMA(volume, 20)
  2. volume_burst_z      — (volume - SMA(20)) / STD(20)
  3. down_volume_ratio   — sum(vol where close<open, 20 bars) / sum(vol, 20 bars)
  4. effort_vs_result    — abs(close - open) / (volume × close / 1e6)
  5. spread_proxy        — (high - low) / close

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/volume.py
"""
from __future__ import annotations

import math

import pandas as pd

FEATURE_NAMES = [
    "relative_volume",
    "volume_burst_z",
    "down_volume_ratio",
    "effort_vs_result",
    "spread_proxy",
]

_NAN = float("nan")
_WINDOW = 20   # rolling window for volume stats


def _nan_result() -> dict[str, float]:
    return {k: _NAN for k in FEATURE_NAMES}


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 5 volume features.

    Parameters
    ----------
    df_5m   : 5-minute bars DataFrame (bar_ts_utc, open, high, low, close, volume)
    df_1h   : 1-hour bars (not used here; included for interface uniformity)
    spy_df  : SPY 5m bars (not used here)

    Returns
    -------
    dict mapping feature_name → float (NaN if insufficient data)
    """
    if df_5m is None or len(df_5m) == 0:
        return _nan_result()

    df = df_5m.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    n = len(df)
    result: dict[str, float] = {}

    last    = df.iloc[-1]
    c       = float(last["close"]) if pd.notna(last["close"]) else _NAN
    h       = float(last["high"])  if pd.notna(last["high"])  else _NAN
    lo      = float(last["low"])   if pd.notna(last["low"])   else _NAN
    o       = float(last["open"])  if pd.notna(last["open"])  else _NAN
    cur_vol = float(last["volume"]) if pd.notna(last["volume"]) else _NAN

    # Use last _WINDOW bars (including current) for rolling stats
    window_df = df.tail(_WINDOW)
    vols      = window_df["volume"].dropna()
    total_window_vol = float(window_df["volume"].fillna(0).sum())

    if total_window_vol <= 0:
        down_mask = window_df["close"] < window_df["open"]
        result["relative_volume"] = 1.0
        result["volume_burst_z"] = 0.0
        result["down_volume_ratio"] = float(down_mask.mean()) if len(window_df) else 0.5
        result["effort_vs_result"] = abs(c - o) / c if (not math.isnan(c) and c > 0 and not math.isnan(o)) else 0.0
        if not math.isnan(h) and not math.isnan(lo) and not math.isnan(c) and c > 0:
            result["spread_proxy"] = (h - lo) / c
        else:
            result["spread_proxy"] = _NAN
        return {k: _safe(v) for k, v in result.items()}

    # ------------------------------------------------------------------
    # 1. relative_volume: current_bar_volume / SMA(volume, 20)
    # ------------------------------------------------------------------
    if len(vols) >= 2 and not math.isnan(cur_vol):
        sma_vol = float(vols.mean())
        result["relative_volume"] = cur_vol / sma_vol if sma_vol > 0 else _NAN
    else:
        result["relative_volume"] = _NAN

    # ------------------------------------------------------------------
    # 2. volume_burst_z: (volume - SMA(20)) / STD(20)
    # ------------------------------------------------------------------
    if len(vols) >= 2 and not math.isnan(cur_vol):
        sma_vol = float(vols.mean())
        std_vol = float(vols.std(ddof=1)) if len(vols) > 1 else 0.0
        result["volume_burst_z"] = (cur_vol - sma_vol) / std_vol if std_vol > 0 else 0.0
    else:
        result["volume_burst_z"] = _NAN

    # ------------------------------------------------------------------
    # 3. down_volume_ratio: sum(vol where close < open) / sum(vol), 20 bars
    #    > 0.5 = sellers dominating
    # ------------------------------------------------------------------
    if n >= 1:
        w         = df.tail(_WINDOW)
        total_vol = w["volume"].fillna(0).sum()
        down_mask = w["close"] < w["open"]
        down_vol  = w.loc[down_mask, "volume"].fillna(0).sum()
        result["down_volume_ratio"] = float(down_vol / total_vol) if total_vol > 0 else 0.5
    else:
        result["down_volume_ratio"] = _NAN

    # ------------------------------------------------------------------
    # 4. effort_vs_result: abs(close - open) / (volume × close / 1e6)
    #    Price movement per million dollars of volume.
    #    High = big move on little volume. Low = small move on big volume.
    # ------------------------------------------------------------------
    if (not math.isnan(c) and c > 0
            and not math.isnan(o)
            and not math.isnan(cur_vol) and cur_vol > 0):
        dollar_vol = cur_vol * c / 1_000_000.0
        price_chg  = abs(c - o)
        result["effort_vs_result"] = price_chg / dollar_vol if dollar_vol > 0 else _NAN
    else:
        result["effort_vs_result"] = _NAN

    # ------------------------------------------------------------------
    # 5. spread_proxy: (high - low) / close
    #    Normalized intrabar range — proxy for bid-ask spread / liquidity.
    # ------------------------------------------------------------------
    if not math.isnan(h) and not math.isnan(lo) and not math.isnan(c) and c > 0:
        result["spread_proxy"] = (h - lo) / c
    else:
        result["spread_proxy"] = _NAN

    return {k: _safe(v) for k, v in result.items()}


def _safe(v: float) -> float:
    if math.isnan(v) or math.isinf(v):
        return _NAN
    return round(v, 6)
