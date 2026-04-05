"""
normalize.py — Cross-sectional feature normalization for V5 Strategy Router.

Location: ~/SS/Vanguard/vanguard/helpers/normalize.py
"""
from __future__ import annotations

import pandas as pd


def normalize(series: pd.Series) -> pd.Series:
    """
    Cross-sectional min-max normalize to [0, 1].
    If all values are equal, returns 0.5 for every row.
    """
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series(0.5, index=series.index, dtype=float)
    return (series - mn) / (mx - mn)


def normalize_abs(series: pd.Series) -> pd.Series:
    """Normalize the absolute value of a series."""
    return normalize(series.abs())


def safe_fill(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """
    Return a copy of df with each listed column guaranteed to exist.
    Missing columns are added as 0.0; existing NaN values are filled with 0.0.
    Absent feature = no signal — safest default.
    """
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = 0.0
        else:
            out[c] = out[c].fillna(0.0)
    return out
