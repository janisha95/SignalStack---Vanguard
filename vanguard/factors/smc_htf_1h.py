"""
smc_htf_1h.py — Smart Money Concepts (Higher Timeframe, 1h) factor module for Vanguard V3.

Computes 4 features from 1h bars — mirrors smc_5m logic at the daily timeframe:
  1. htf_trend_direction  — +1.0 (uptrend), -1.0 (downtrend), 0.0 (ranging)
  2. htf_structure_break  — +1.0 (bullish BOS on 1h), -1.0 (bearish BOS), 0.0
  3. htf_fvg_nearest      — distance to nearest 1h FVG midpoint (pct of close)
  4. htf_ob_proximity     — proximity to nearest 1h order block (0.0–1.0)

If df_1h is empty or insufficient, all features default to 0.0 (neutral, not NaN)
so that 5m factors still run cleanly.

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/smc_htf_1h.py
"""
from __future__ import annotations

import math

import pandas as pd

FEATURE_NAMES = [
    "htf_trend_direction",
    "htf_structure_break",
    "htf_fvg_nearest",
    "htf_ob_proximity",
]

_NAN  = float("nan")
_WIN  = 10   # rolling window on 1h bars (≈10 hours = ~1.5 trading days)


def _neutral_result() -> dict[str, float]:
    """Return neutral (0.0) when 1h data is unavailable — not NaN."""
    return {k: 0.0 for k in FEATURE_NAMES}


def _safe(v: float, default: float = 0.0) -> float:
    if math.isnan(v) or math.isinf(v):
        return default
    return round(v, 6)


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 4 HTF SMC features from 1h bars.

    Parameters
    ----------
    df_5m   : 5-minute bars (not used here)
    df_1h   : 1-hour bars DataFrame (bar_ts_utc, open, high, low, close, volume)
    spy_df  : SPY bars (not used)

    Returns
    -------
    dict mapping feature_name → float (0.0 neutral when 1h data unavailable)
    """
    if df_1h is None or len(df_1h) < 3:
        return _neutral_result()

    df = df_1h.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)

    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if df["close"].isna().all():
        return _neutral_result()

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    open_  = df["open"]
    n      = len(df)
    c_last = float(close.iloc[-1])

    if math.isnan(c_last) or c_last <= 0:
        return _neutral_result()

    result: dict[str, float] = {}

    w         = min(_WIN, n)
    roll_high = float(high.iloc[-w:].max())
    roll_low  = float(low.iloc[-w:].min())
    swing_rng = roll_high - roll_low

    # ------------------------------------------------------------------
    # 1. htf_trend_direction
    #    Compare first half vs second half of rolling window average close.
    #    Up = second half avg > first half avg; Down = opposite; 0.0 = flat.
    # ------------------------------------------------------------------
    trend = 0.0
    if w >= 4:
        half     = w // 2
        first_avg = float(close.iloc[-(w):-(w - half)].mean())
        second_avg = float(close.iloc[-(half):].mean())
        if not math.isnan(first_avg) and not math.isnan(second_avg) and first_avg > 0:
            chg = (second_avg - first_avg) / first_avg
            if chg > 0.001:
                trend = 1.0
            elif chg < -0.001:
                trend = -1.0

    result["htf_trend_direction"] = _safe(trend)

    # ------------------------------------------------------------------
    # 2. htf_structure_break
    #    +1.0 if last close > rolling high (excluding last bar)
    #    -1.0 if last close < rolling low  (excluding last bar)
    # ------------------------------------------------------------------
    bos = 0.0
    if n >= _WIN + 1:
        prior_high = float(high.iloc[-(_WIN + 1):-1].max())
        prior_low  = float(low.iloc[-(_WIN + 1):-1].min())
        if not math.isnan(prior_high) and c_last > prior_high:
            bos = 1.0
        elif not math.isnan(prior_low) and c_last < prior_low:
            bos = -1.0

    result["htf_structure_break"] = _safe(bos)

    # ------------------------------------------------------------------
    # 3. htf_fvg_nearest — nearest 1h FVG distance (pct of close)
    #    Bullish FVG: bar[i-2].high < bar[i].low
    #    Bearish FVG: bar[i-2].low  > bar[i].high
    #    Use the closer of the two; return 0.0 if none found (neutral)
    # ------------------------------------------------------------------
    fvg_bull = _NAN
    fvg_bear = _NAN

    if n >= 3:
        for i in range(n - 1, 1, -1):
            h2   = float(high.iloc[i - 2])
            l2   = float(low.iloc[i - 2])
            hi   = float(high.iloc[i])
            li   = float(low.iloc[i])
            if any(math.isnan(v) for v in (h2, l2, hi, li)):
                continue
            if math.isnan(fvg_bull) and h2 < li:
                fvg_bull = abs(c_last - (h2 + li) / 2.0) / c_last
            if math.isnan(fvg_bear) and l2 > hi:
                fvg_bear = abs(c_last - (l2 + hi) / 2.0) / c_last
            if not math.isnan(fvg_bull) and not math.isnan(fvg_bear):
                break

    # Use closer FVG; 0.0 if none found
    if math.isnan(fvg_bull) and math.isnan(fvg_bear):
        fvg_nearest = 0.0
    elif math.isnan(fvg_bull):
        fvg_nearest = fvg_bear
    elif math.isnan(fvg_bear):
        fvg_nearest = fvg_bull
    else:
        fvg_nearest = min(fvg_bull, fvg_bear)

    result["htf_fvg_nearest"] = _safe(fvg_nearest)

    # ------------------------------------------------------------------
    # 4. htf_ob_proximity — proximity to nearest 1h order block
    #    Same logic as smc_5m: last bearish-before-up or bullish-before-down bar.
    #    Returns 0.0 (neutral) if no OB found.
    # ------------------------------------------------------------------
    ob_proximity = 0.0
    if n >= 3 and swing_rng > 0:
        for i in range(n - 2, max(n - _WIN - 2, 0), -1):
            c_i  = float(close.iloc[i])
            o_i  = float(open_.iloc[i])
            c_n1 = float(close.iloc[i + 1]) if i + 1 < n else _NAN
            if math.isnan(c_i) or math.isnan(o_i) or math.isnan(c_n1):
                continue
            if (c_i < o_i and c_n1 > o_i) or (c_i > o_i and c_n1 < o_i):
                ob_mid = (c_i + o_i) / 2.0
                dist_pct = abs(c_last - ob_mid) / swing_rng
                ob_proximity = max(0.0, 1.0 - dist_pct)
                break

    result["htf_ob_proximity"] = _safe(ob_proximity)

    return result
