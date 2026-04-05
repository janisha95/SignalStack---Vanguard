"""
quality.py — Data Quality factor module for Vanguard V3.

Computes 2 meta-features about data quality:
  1. bars_available — total number of 5m bars available for this symbol
  2. nan_ratio      — fraction of all computed features that are NaN
                      (placeholder 0.0 here; engine overwrites with actual value)

These are "last module" features — quality.py should always be registered last
so that bars_available reflects the actual input size.

Interface: compute(df_5m, df_1h, spy_df) -> dict[str, float]

Location: ~/SS/Vanguard/vanguard/factors/quality.py
"""
from __future__ import annotations

import pandas as pd

FEATURE_NAMES = [
    "bars_available",
    "nan_ratio",
]

_NAN = float("nan")


def compute(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Compute 2 data-quality features.

    Parameters
    ----------
    df_5m   : 5-minute bars DataFrame (bar_ts_utc, open, high, low, close, volume)
    df_1h   : 1-hour bars (not used)
    spy_df  : SPY bars (not used)

    Returns
    -------
    dict with:
      bars_available: float count of 5m bars (0.0 if no data)
      nan_ratio:      0.0 placeholder — overwritten by engine after all modules run
    """
    if df_5m is None or len(df_5m) == 0:
        bars = 0.0
    else:
        bars = float(len(df_5m))

    return {
        "bars_available": bars,
        "nan_ratio":      0.0,   # engine overwrites this after full feature computation
    }
