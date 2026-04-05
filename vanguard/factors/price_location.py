"""
price_location.py — Price Location factor module for Vanguard V3.

Computes 5 features from 5m bars:
  1. session_vwap_distance       — (close - VWAP) / VWAP
  2. premium_discount_zone       — (close - session_low) / (session_high - session_low)
  3. gap_pct                     — session_open / prev_close - 1
  4. session_opening_range_position — (close - OR_low) / (OR_high - OR_low)
  5. daily_drawdown_from_high    — (close - cummax_close) / cummax_close

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/price_location.py
"""
from __future__ import annotations

import math

import pandas as pd

FEATURE_NAMES = [
    "session_vwap_distance",
    "premium_discount_zone",
    "gap_pct",
    "session_opening_range_position",
    "daily_drawdown_from_high",
]

_NAN = float("nan")

# Opening range = first N 5m bars of the session
_OR_BARS = 6   # 6 × 5 min = 30 min opening range


def _nan_result() -> dict[str, float]:
    return {k: _NAN for k in FEATURE_NAMES}


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 5 price-location features.

    Parameters
    ----------
    df_5m   : 5-minute bars DataFrame with columns:
              bar_ts_utc, open, high, low, close, volume
    df_1h   : 1-hour bars DataFrame (same columns) — used for prev_close
    spy_df  : SPY 5m bars (not used in this module; included for interface uniformity)

    Returns
    -------
    dict mapping feature_name → float value (may be NaN if insufficient data)
    """
    if df_5m is None or len(df_5m) == 0:
        return _nan_result()

    df = df_5m.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Cast OHLCV to float; treat missing as NaN
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if df["close"].isna().all():
        return _nan_result()

    last = df.iloc[-1]
    close = float(last["close"])
    if math.isnan(close) or close <= 0:
        return _nan_result()

    result: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 1. session_vwap_distance:  (close - VWAP) / VWAP
    #    VWAP = Σ(typical_price × volume) / Σ(volume)
    #    typical_price = (high + low + close) / 3
    # ------------------------------------------------------------------
    df["_tp"]     = (df["high"] + df["low"] + df["close"]) / 3
    df["_tp_vol"] = df["_tp"] * df["volume"].fillna(0)
    total_vol = float(df["volume"].fillna(0).sum())
    if total_vol > 0:
        vwap = float(df["_tp_vol"].sum()) / total_vol
        result["session_vwap_distance"] = (close - vwap) / vwap if vwap > 0 else 0.0
    else:
        pseudo_vwap = float(df["_tp"].mean()) if not df["_tp"].isna().all() else close
        result["session_vwap_distance"] = (close - pseudo_vwap) / pseudo_vwap if pseudo_vwap > 0 else 0.0

    # ------------------------------------------------------------------
    # 2. premium_discount_zone: (close - session_low) / (session_high - session_low)
    #    Clipped to [0, 1]. 1.0 = at session high, 0.0 = at session low.
    # ------------------------------------------------------------------
    session_high = float(df["high"].max())
    session_low  = float(df["low"].min())
    s_range = session_high - session_low
    if s_range > 0:
        pdz = (close - session_low) / s_range
        result["premium_discount_zone"] = max(0.0, min(1.0, pdz))
    else:
        result["premium_discount_zone"] = 0.5

    # ------------------------------------------------------------------
    # 3. gap_pct: session_open / prev_session_close - 1
    #    Uses first bar's open as session open.
    #    Uses 1h bars for prev_session_close (second-to-last bar's close).
    #    Falls back to 0.0 (no gap) when prev data is unavailable.
    # ------------------------------------------------------------------
    session_open_price = float(df.iloc[0]["open"]) if not math.isnan(float(df.iloc[0]["open"])) else close
    prev_close = _get_prev_close(df_1h, session_open_price)
    if prev_close > 0:
        result["gap_pct"] = session_open_price / prev_close - 1.0
    else:
        result["gap_pct"] = 0.0

    # ------------------------------------------------------------------
    # 4. session_opening_range_position: (close - OR_low) / (OR_high - OR_low)
    #    OR = first 30 min of session (6 × 5m bars).
    # ------------------------------------------------------------------
    or_df   = df.head(_OR_BARS)
    or_high = float(or_df["high"].max())
    or_low  = float(or_df["low"].min())
    or_rng  = or_high - or_low
    if or_rng > 0:
        orp = (close - or_low) / or_rng
        result["session_opening_range_position"] = float(orp)
    else:
        result["session_opening_range_position"] = 0.5

    # ------------------------------------------------------------------
    # 5. daily_drawdown_from_high: (close - rolling_cummax) / rolling_cummax
    #    Uses all bars in df_5m as the "day" window.
    #    Always <= 0.0 (0 = at session high).
    # ------------------------------------------------------------------
    cummax = float(df["close"].cummax().iloc[-1])
    if cummax > 0:
        result["daily_drawdown_from_high"] = (close - cummax) / cummax
    else:
        result["daily_drawdown_from_high"] = 0.0

    return {k: _safe(v) for k, v in result.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_prev_close(df_1h: pd.DataFrame | None, fallback: float) -> float:
    """
    Extract previous session close from 1h bars.
    Returns the close of the bar just before the most recent one.
    Falls back to `fallback` if unavailable.
    """
    if df_1h is None or len(df_1h) < 2:
        return fallback
    df = df_1h.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    # Use the second-to-last bar's close as "prev session close"
    prev_c = float(df.iloc[-2]["close"])
    return prev_c if not math.isnan(prev_c) and prev_c > 0 else fallback


def _safe(v: float) -> float:
    """Return v if finite, else NaN."""
    if math.isnan(v) or math.isinf(v):
        return _NAN
    return round(v, 6)
