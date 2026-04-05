"""
breakdown.py — Breakdown / Damage Strategy (SHORT ONLY).

Derived from S1's Bearish Reversal Factor (BRF), validated at 71% WR.
Identifies stocks exhibiting institutional selling pressure through:
  - Volume profile (down_volume_ratio)
  - Price structure damage (negative structure_break / bearish BOS)
  - Drawdown from daily high
  - ATR expansion (breakdown in progress)
  - HTF bearish alignment

Only produces SHORT candidates (no LONG equivalent).
Applies to: equity.

Location: ~/SS/Vanguard/vanguard/strategies/breakdown.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "daily_drawdown_from_high",
    "down_volume_ratio",
    "momentum_12bar",
    "structure_break",
    "htf_trend_direction",
    "atr_expansion",
]


class BreakdownStrategy(BaseStrategy):
    name = "breakdown"
    asset_classes = ["equity"]
    short_only = True

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        # Enforce SHORT ONLY
        if direction != "SHORT":
            return pd.DataFrame()
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        # All features inverted/negated for maximum SHORT conviction
        drawdown   = normalize(-d["daily_drawdown_from_high"])       # falling hard from high
        down_vol   = normalize(d["down_volume_ratio"])                # selling volume dominates
        neg_mom    = normalize(-d["momentum_12bar"])                  # strong negative momentum
        bearish_bos = normalize((-d["structure_break"]).clip(lower=0.0))  # bearish BOS/CHoCH
        htf_bear   = normalize((-d["htf_trend_direction"]).clip(lower=0.0))  # HTF bearish
        atr_exp    = normalize(d["atr_expansion"])                    # volatility expanding

        scores = (drawdown + down_vol + neg_mom + bearish_bos + htf_bear + atr_exp) / 6.0
        return self._build_result(d, scores, "SHORT", top_n)
