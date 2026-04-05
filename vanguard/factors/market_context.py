"""
market_context.py — Market Context factor module for Vanguard V3.

Computes 5 features using ticker 5m bars and SPY benchmark:
  1. rs_vs_benchmark_intraday  — ticker 20-bar return minus SPY 20-bar return
  2. daily_rs_vs_benchmark     — ticker 60-bar return minus SPY 60-bar return
  3. benchmark_momentum_12bar  — SPY pct_change(12)
  4. cross_asset_correlation   — placeholder 0.0 (needs DXY/peer data, Phase 5)
  5. daily_conviction          — placeholder 0.0 (no S1/Meridian coupling yet)

If SPY bars are unavailable, rs features default to 0.0 (neutral).

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/market_context.py
"""
from __future__ import annotations

import math

import pandas as pd

FEATURE_NAMES = [
    "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark",
    "benchmark_momentum_12bar",
    "cross_asset_correlation",
    "daily_conviction",
]

_NAN = float("nan")


def _nan_result() -> dict[str, float]:
    return {k: _NAN for k in FEATURE_NAMES}


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 5 market-context features.

    Parameters
    ----------
    df_5m   : 5-minute bars for the ticker (bar_ts_utc, open, high, low, close, volume)
    df_1h   : 1-hour bars (not used here; included for interface uniformity)
    spy_df  : SPY 5m bars — benchmark for rs and momentum features

    Returns
    -------
    dict mapping feature_name → float (NaN or 0.0 where unavailable)
    """
    if df_5m is None or len(df_5m) == 0:
        return _nan_result()

    df = df_5m.copy()
    df.sort_values("bar_ts_utc", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    result: dict[str, float] = {}

    # Prepare SPY close series (aligned by position, not by timestamp)
    spy_close: pd.Series | None = None
    if spy_df is not None and len(spy_df) >= 13:
        spy = spy_df.copy()
        spy.sort_values("bar_ts_utc", inplace=True)
        spy.reset_index(drop=True, inplace=True)
        spy["close"] = pd.to_numeric(spy["close"], errors="coerce")
        spy_close = spy["close"]

    close = df["close"]
    n     = len(close)

    # ------------------------------------------------------------------
    # 1. rs_vs_benchmark_intraday: ticker_ret_20bar - spy_ret_20bar
    #    20-bar return = (close[-1] - close[-21]) / close[-21]
    # ------------------------------------------------------------------
    ticker_ret_20 = _pct_ret(close, 20)
    if spy_close is not None and not math.isnan(ticker_ret_20):
        spy_ret_20 = _pct_ret(spy_close, 20)
        result["rs_vs_benchmark_intraday"] = (
            ticker_ret_20 - spy_ret_20 if not math.isnan(spy_ret_20) else ticker_ret_20
        )
    elif not math.isnan(ticker_ret_20):
        result["rs_vs_benchmark_intraday"] = 0.0   # neutral when SPY unavailable
    else:
        result["rs_vs_benchmark_intraday"] = _NAN

    # ------------------------------------------------------------------
    # 2. daily_rs_vs_benchmark: ticker_ret_60bar - spy_ret_60bar
    #    60-bar return ≈ 5 hours of 5m bars
    # ------------------------------------------------------------------
    ticker_ret_60 = _pct_ret(close, 60)
    if spy_close is not None and not math.isnan(ticker_ret_60):
        spy_ret_60 = _pct_ret(spy_close, 60)
        result["daily_rs_vs_benchmark"] = (
            ticker_ret_60 - spy_ret_60 if not math.isnan(spy_ret_60) else ticker_ret_60
        )
    elif not math.isnan(ticker_ret_60):
        result["daily_rs_vs_benchmark"] = 0.0
    else:
        result["daily_rs_vs_benchmark"] = _NAN

    # ------------------------------------------------------------------
    # 3. benchmark_momentum_12bar: SPY pct_change(12)
    #    12-bar = 60-minute momentum on 5m bars
    # ------------------------------------------------------------------
    if spy_close is not None:
        result["benchmark_momentum_12bar"] = _pct_ret(spy_close, 12)
    else:
        result["benchmark_momentum_12bar"] = 0.0   # neutral — not NaN

    # ------------------------------------------------------------------
    # 4. cross_asset_correlation — placeholder
    #    Full implementation requires DXY/peer data (Phase 5).
    # ------------------------------------------------------------------
    result["cross_asset_correlation"] = 0.0

    # ------------------------------------------------------------------
    # 5. daily_conviction — placeholder
    #    Will be set to +1/-1 when S1/Meridian daily picks are coupled.
    # ------------------------------------------------------------------
    result["daily_conviction"] = 0.0

    return {k: _safe(v) for k, v in result.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct_ret(close: pd.Series, periods: int) -> float:
    """
    Percentage return over `periods` bars: (close[-1] - close[-1-periods]) / close[-1-periods].
    Returns NaN if insufficient data.
    """
    n = len(close)
    if n < periods + 1:
        return _NAN
    c_now  = float(close.iloc[-1])
    c_prev = float(close.iloc[-(periods + 1)])
    if math.isnan(c_prev) or c_prev <= 0:
        return _NAN
    return (c_now - c_prev) / c_prev


def _safe(v: float) -> float:
    if math.isnan(v) or math.isinf(v):
        return _NAN
    return round(v, 6)
