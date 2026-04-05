"""
htf_alignment.py — HTF Alignment Strategy (Forex).

Scores forex pairs where the intraday direction aligns with the higher
time-frame trend. Strong HTF trends + proximate HTF structure = high score.

Features: htf_trend_direction, htf_structure_break, htf_fvg_nearest,
          htf_ob_proximity, daily_adx.

Applies to: forex.
Direction: BOTH (direction-aware HTF features).

Location: ~/SS/Vanguard/vanguard/strategies/htf_alignment.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "htf_trend_direction",
    "htf_structure_break",
    "htf_fvg_nearest",
    "htf_ob_proximity",
    "daily_adx",
]


class HtfAlignmentStrategy(BaseStrategy):
    name = "htf_alignment"
    asset_classes = ["forex"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        htf_fvg   = normalize(d["htf_fvg_nearest"])
        htf_ob    = normalize(d["htf_ob_proximity"])
        adx       = normalize(d["daily_adx"])  # strong daily trend regardless of direction

        if direction == "LONG":
            htf_trend = normalize(d["htf_trend_direction"].clip(lower=0.0))
            htf_bos   = normalize(d["htf_structure_break"].clip(lower=0.0))
        else:  # SHORT
            htf_trend = normalize((-d["htf_trend_direction"]).clip(lower=0.0))
            htf_bos   = normalize((-d["htf_structure_break"]).clip(lower=0.0))

        scores = (htf_trend + htf_bos + htf_fvg + htf_ob + adx) / 5.0
        return self._build_result(d, scores, direction, top_n)
