"""
smc_5m.py — Smart Money Concepts (5m) factor module for Vanguard V3.

Computes 6 features from 5m bars using pure pandas/OHLCV patterns:
  1. ob_proximity_5m       — how close price is to nearest order block (0.0–1.0)
  2. fvg_bullish_nearest   — distance to nearest bullish fair value gap (pct)
  3. fvg_bearish_nearest   — distance to nearest bearish fair value gap (pct)
  4. structure_break       — +1.0 (bullish BOS), -1.0 (bearish BOS), 0.0 (none)
  5. liquidity_sweep        — +1.0 (sweep of lows/buy-side), -1.0 (sweep of highs/sell-side), 0.0
  6. smc_premium_discount  — 0.0–1.0 from swing low to swing high; >0.5 = premium, <0.5 = discount

All features use a 20-bar rolling window for swing detection.
No external SMC libraries required — pure Python/pandas.

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/smc_5m.py
"""
from __future__ import annotations

import math

import pandas as pd

FEATURE_NAMES = [
    "ob_proximity_5m",
    "fvg_bullish_nearest",
    "fvg_bearish_nearest",
    "structure_break",
    "liquidity_sweep",
    "smc_premium_discount",
]

_NAN   = float("nan")
_WIN   = 20   # rolling window for swing / structure detection


def _nan_result() -> dict[str, float]:
    return {k: _NAN for k in FEATURE_NAMES}


def _safe(v: float) -> float:
    if math.isnan(v) or math.isinf(v):
        return _NAN
    return round(v, 6)


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 6 SMC features from 5m bars.

    Parameters
    ----------
    df_5m   : 5-minute bars DataFrame (bar_ts_utc, open, high, low, close, volume)
    df_1h   : 1-hour bars (not used)
    spy_df  : SPY bars (not used)

    Returns
    -------
    dict mapping feature_name → float
    """
    if df_5m is None or len(df_5m) < 3:
        return _nan_result()

    df = df_5m.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)

    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if df["close"].isna().all():
        return _nan_result()

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    open_  = df["open"]
    n      = len(df)
    c_last = float(close.iloc[-1])

    if math.isnan(c_last) or c_last <= 0:
        return _nan_result()

    result: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Rolling window reference values (last _WIN bars)
    # ------------------------------------------------------------------
    w = min(_WIN, n)
    roll_high = float(high.iloc[-w:].max())
    roll_low  = float(low.iloc[-w:].min())
    swing_rng = roll_high - roll_low

    # ------------------------------------------------------------------
    # 1. ob_proximity_5m — proximity to last significant order block
    #    Order block proxy: the last bar before a directional move.
    #    Bullish OB = last bearish bar before a strong up move.
    #    Bearish OB = last bullish bar before a strong down move.
    #    We find the most recent such bar and measure distance to its midpoint.
    # ------------------------------------------------------------------
    ob_proximity = _NAN
    if n >= 3 and swing_rng > 0:
        # Scan last _WIN bars for the most recent OB midpoint
        ob_mid = _NAN
        for i in range(n - 2, max(n - _WIN - 2, 0), -1):
            c_i  = float(close.iloc[i])
            o_i  = float(open_.iloc[i])
            c_n1 = float(close.iloc[i + 1]) if i + 1 < n else _NAN
            if math.isnan(c_i) or math.isnan(o_i) or math.isnan(c_n1):
                continue
            # Bearish bar followed by strong up = bullish OB
            if c_i < o_i and c_n1 > o_i:
                ob_mid = (c_i + o_i) / 2.0
                break
            # Bullish bar followed by strong down = bearish OB
            if c_i > o_i and c_n1 < o_i:
                ob_mid = (c_i + o_i) / 2.0
                break

        if not math.isnan(ob_mid) and swing_rng > 0:
            dist_pct = abs(c_last - ob_mid) / swing_rng
            ob_proximity = max(0.0, 1.0 - dist_pct)   # closer → higher

    result["ob_proximity_5m"] = _safe(ob_proximity)

    # ------------------------------------------------------------------
    # 2 & 3. FVG detection — 3-bar gap pattern
    #    Bullish FVG: bar[i-2].high < bar[i].low   (gap up between i-2 and i)
    #    Bearish FVG: bar[i-2].low  > bar[i].high  (gap down)
    #    Feature = distance from current close to nearest FVG midpoint (pct of close)
    # ------------------------------------------------------------------
    fvg_bull_dist = _NAN
    fvg_bear_dist = _NAN

    if n >= 3:
        for i in range(n - 1, 1, -1):
            h_prev2 = float(high.iloc[i - 2])
            l_prev2 = float(low.iloc[i - 2])
            h_cur   = float(high.iloc[i])
            l_cur   = float(low.iloc[i])
            if any(math.isnan(v) for v in (h_prev2, l_prev2, h_cur, l_cur)):
                continue

            # Bullish FVG
            if math.isnan(fvg_bull_dist) and h_prev2 < l_cur:
                mid = (h_prev2 + l_cur) / 2.0
                fvg_bull_dist = abs(c_last - mid) / c_last

            # Bearish FVG
            if math.isnan(fvg_bear_dist) and l_prev2 > h_cur:
                mid = (l_prev2 + h_cur) / 2.0
                fvg_bear_dist = abs(c_last - mid) / c_last

            if not math.isnan(fvg_bull_dist) and not math.isnan(fvg_bear_dist):
                break

    result["fvg_bullish_nearest"] = _safe(fvg_bull_dist)
    result["fvg_bearish_nearest"] = _safe(fvg_bear_dist)

    # ------------------------------------------------------------------
    # 4. structure_break (BOS — Break of Structure)
    #    +1.0 if last close > rolling _WIN-bar high (excluding last bar)
    #    -1.0 if last close < rolling _WIN-bar low  (excluding last bar)
    #     0.0 otherwise
    # ------------------------------------------------------------------
    bos = 0.0
    if n >= _WIN + 1:
        prior_high = float(high.iloc[-(  _WIN + 1):-1].max())
        prior_low  = float(low.iloc[-(_WIN + 1):-1].min())
        if not math.isnan(prior_high) and c_last > prior_high:
            bos = 1.0
        elif not math.isnan(prior_low) and c_last < prior_low:
            bos = -1.0

    result["structure_break"] = _safe(bos)

    # ------------------------------------------------------------------
    # 5. liquidity_sweep
    #    A sweep is a wick beyond the rolling extreme that closes back inside.
    #    Sweep of lows  (buy-side liquidity) → +1.0:
    #      last bar's low < rolling low BUT close > rolling low
    #    Sweep of highs (sell-side liquidity) → -1.0:
    #      last bar's high > rolling high BUT close < rolling high
    #    0.0 otherwise
    # ------------------------------------------------------------------
    sweep = 0.0
    if n >= _WIN + 1:
        prior_high_sw = float(high.iloc[-(_WIN + 1):-1].max())
        prior_low_sw  = float(low.iloc[-(_WIN + 1):-1].min())
        last_high = float(high.iloc[-1])
        last_low  = float(low.iloc[-1])

        if (not math.isnan(prior_low_sw)
                and not math.isnan(last_low)
                and last_low < prior_low_sw
                and c_last > prior_low_sw):
            sweep = 1.0
        elif (not math.isnan(prior_high_sw)
                and not math.isnan(last_high)
                and last_high > prior_high_sw
                and c_last < prior_high_sw):
            sweep = -1.0

    result["liquidity_sweep"] = _safe(sweep)

    # ------------------------------------------------------------------
    # 6. smc_premium_discount
    #    Position of current close within the rolling swing range:
    #    0.0 = at swing low (discount), 1.0 = at swing high (premium)
    # ------------------------------------------------------------------
    if swing_rng > 0 and not math.isnan(roll_low):
        pd_zone = (c_last - roll_low) / swing_rng
        pd_zone = max(0.0, min(1.0, pd_zone))
    else:
        pd_zone = _NAN

    result["smc_premium_discount"] = _safe(pd_zone)

    return result
