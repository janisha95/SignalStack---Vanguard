"""
momentum.py — Momentum factor module for Vanguard V3.

Computes 5 features from 5m and 1h bars:
  1. momentum_3bar         — (close - close_3bars_ago) / close_3bars_ago
  2. momentum_12bar        — (close - close_12bars_ago) / close_12bars_ago
  3. momentum_acceleration — momentum_3bar - momentum_3bar_3bars_ago
  4. atr_expansion         — ATR(14) / ATR(50)
  5. daily_adx             — simplified Wilder ADX(14) on 1h bars (or 5m fallback)

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/momentum.py
"""
from __future__ import annotations

import math

import pandas as pd

FEATURE_NAMES = [
    "momentum_3bar",
    "momentum_12bar",
    "momentum_acceleration",
    "atr_expansion",
    "daily_adx",
]

_NAN = float("nan")

# ATR periods
_ATR_SHORT  = 14
_ATR_LONG   = 50
# ADX period
_ADX_PERIOD = 14
# Minimum 5m bars for various features
_MIN_MOM3   = 4    # need close[i-3]
_MIN_MOM12  = 13   # need close[i-12]
_MIN_MOM_ACC = 7   # need close[i-3] and close[i-6] back
_MIN_ATR    = _ATR_LONG + 1  # 51 bars for ATR(50) — uses true range so need 1 prev bar
_MIN_ADX    = _ADX_PERIOD + 1


def _nan_result() -> dict[str, float]:
    return {k: _NAN for k in FEATURE_NAMES}


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 5 momentum features.

    Parameters
    ----------
    df_5m  : 5-minute bars DataFrame with columns: bar_ts_utc, open, high, low, close, volume
    df_1h  : 1-hour bars DataFrame — used for daily_adx (preferred over 5m)
    spy_df : SPY 5m bars (not used here; included for interface uniformity)

    Returns
    -------
    dict mapping feature_name → float (may be NaN if insufficient data)
    """
    if df_5m is None or len(df_5m) < _MIN_MOM3:
        return _nan_result()

    df = df_5m.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)

    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    n     = len(df)

    result: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 1. momentum_3bar: (close[-1] - close[-4]) / close[-4]
    # ------------------------------------------------------------------
    if n >= _MIN_MOM3:
        c_now  = float(close.iloc[-1])
        c_prev = float(close.iloc[-4])
        result["momentum_3bar"] = (c_now - c_prev) / c_prev if c_prev > 0 else _NAN
    else:
        result["momentum_3bar"] = _NAN

    # ------------------------------------------------------------------
    # 2. momentum_12bar: (close[-1] - close[-13]) / close[-13]
    # ------------------------------------------------------------------
    if n >= _MIN_MOM12:
        c_now  = float(close.iloc[-1])
        c_prev = float(close.iloc[-13])
        result["momentum_12bar"] = (c_now - c_prev) / c_prev if c_prev > 0 else _NAN
    else:
        result["momentum_12bar"] = _NAN

    # ------------------------------------------------------------------
    # 3. momentum_acceleration: mom_3bar(now) - mom_3bar(3 bars ago)
    #    mom_3bar_3bars_ago = (close[-4] - close[-7]) / close[-7]
    # ------------------------------------------------------------------
    if n >= _MIN_MOM_ACC + 3:   # need at least 10 bars (indices 0..9)
        c0 = float(close.iloc[-1])
        c3 = float(close.iloc[-4])
        c6 = float(close.iloc[-7])
        if c3 > 0 and c6 > 0:
            mom_now  = (c0 - c3) / c3
            mom_prev = (c3 - c6) / c6
            result["momentum_acceleration"] = mom_now - mom_prev
        else:
            result["momentum_acceleration"] = _NAN
    else:
        result["momentum_acceleration"] = _NAN

    # ------------------------------------------------------------------
    # 4. atr_expansion: ATR(14) / ATR(50)
    #    ATR computed as simple mean of True Range over the window.
    # ------------------------------------------------------------------
    if n >= _MIN_ATR:
        atr14 = _simple_atr(high, low, close, _ATR_SHORT)
        atr50 = _simple_atr(high, low, close, _ATR_LONG)
        if atr50 > 0 and not math.isnan(atr14) and not math.isnan(atr50):
            result["atr_expansion"] = atr14 / atr50
        else:
            result["atr_expansion"] = _NAN
    else:
        result["atr_expansion"] = _NAN

    # ------------------------------------------------------------------
    # 5. daily_adx: simplified Wilder ADX(14)
    #    Use 1h bars if available (>= 15); otherwise fall back to 5m.
    # ------------------------------------------------------------------
    adx_df = None
    if df_1h is not None and len(df_1h) >= _MIN_ADX:
        adx_df = df_1h.copy()
        adx_df.sort_values("bar_ts_utc", inplace=True)
        adx_df.reset_index(drop=True, inplace=True)
        for col in ("high", "low", "close"):
            adx_df[col] = pd.to_numeric(adx_df[col], errors="coerce")
    elif n >= _MIN_ADX:
        adx_df = df

    if adx_df is not None and len(adx_df) >= _MIN_ADX:
        result["daily_adx"] = _compute_adx(
            adx_df["high"], adx_df["low"], adx_df["close"], _ADX_PERIOD
        )
    else:
        result["daily_adx"] = _NAN

    return {k: _safe(v) for k, v in result.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
) -> float:
    """
    Simple ATR: mean of True Range over the last `period` bars.
    True Range = max(H-L, |H-C_prev|, |L-C_prev|).
    """
    n = len(close)
    if n < period + 1:
        return _NAN
    tr_vals = []
    start = n - period
    for i in range(start, n):
        h  = float(high.iloc[i])
        lo = float(low.iloc[i])
        cp = float(close.iloc[i - 1])
        tr = max(h - lo, abs(h - cp), abs(lo - cp))
        tr_vals.append(tr)
    return sum(tr_vals) / len(tr_vals) if tr_vals else _NAN


def _compute_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> float:
    """
    Simplified ADX(period).
    Uses the last `period` bars to compute directional movement.
    Returns ADX as a float 0–100 (higher = stronger trend).
    """
    n = len(close)
    if n < period + 1:
        return _NAN

    start = n - period
    dm_plus_vals:  list[float] = []
    dm_minus_vals: list[float] = []
    tr_vals:       list[float] = []

    for i in range(start, n):
        h   = float(high.iloc[i])
        lo  = float(low.iloc[i])
        hp  = float(high.iloc[i - 1])
        lp  = float(low.iloc[i - 1])
        cp  = float(close.iloc[i - 1])

        up_move   = h - hp
        down_move = lp - lo
        dm_plus_vals.append(up_move   if up_move   > down_move and up_move   > 0 else 0.0)
        dm_minus_vals.append(down_move if down_move > up_move  and down_move > 0 else 0.0)

        tr = max(h - lo, abs(h - cp), abs(lo - cp))
        tr_vals.append(tr)

    avg_tr = sum(tr_vals) / len(tr_vals) if tr_vals else 0.0
    if avg_tr <= 0:
        return _NAN

    dmi_plus  = (sum(dm_plus_vals)  / len(dm_plus_vals))  / avg_tr * 100.0
    dmi_minus = (sum(dm_minus_vals) / len(dm_minus_vals)) / avg_tr * 100.0
    dmi_sum   = dmi_plus + dmi_minus

    if dmi_sum <= 0:
        return 0.0

    dx  = abs(dmi_plus - dmi_minus) / dmi_sum * 100.0
    return dx


def _safe(v: float) -> float:
    if math.isnan(v) or math.isinf(v):
        return _NAN
    return round(v, 6)
