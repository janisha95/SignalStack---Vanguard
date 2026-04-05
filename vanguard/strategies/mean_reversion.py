"""
mean_reversion.py — Mean Reversion Strategy.

Scores candidates that are extended from their fair value and likely to revert:
  - Far from VWAP
  - At premium/discount zone extremes
  - Extended drawdown
  - Volume vs price divergence (effort vs result)

Applies to: equity.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/mean_reversion.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "session_vwap_distance",
    "premium_discount_zone",
    "daily_drawdown_from_high",
    "effort_vs_result",
]


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"
    asset_classes = ["equity"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        # Zone extremeness (same for both directions — both want EXTREME zone)
        zone_extreme = normalize((d["premium_discount_zone"] - 0.5).abs())
        effort = normalize(d["effort_vs_result"])

        if direction == "LONG":
            # Want: far BELOW VWAP (negative session_vwap_distance)
            vwap = normalize(-d["session_vwap_distance"])
            # Want: far from daily high (large negative drawdown → negate to get high rank)
            drawdown = normalize(-d["daily_drawdown_from_high"])
        else:  # SHORT
            # Want: far ABOVE VWAP (positive session_vwap_distance)
            vwap = normalize(d["session_vwap_distance"])
            # Want: price NEAR daily high (small drawdown — least negative)
            drawdown = normalize(d["daily_drawdown_from_high"])

        scores = (vwap + zone_extreme + drawdown + effort) / 4.0
        return self._build_result(d, scores, direction, top_n)
