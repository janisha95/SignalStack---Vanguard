"""
cross_asset.py — Cross-Asset Strategy (Metals).

Scores metals (primarily gold/XAU) based on their cross-asset relationship
with the USD (DXY). Gold moves inversely to the dollar — strong DXY bearish
momentum = gold bullish signal, and vice versa.

Features:
  - cross_asset_correlation (inverse DXY relationship)
  - benchmark_momentum_12bar (USD/DXY directional momentum)
  - daily_conviction (daily system consensus)

Applies to: metal.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/cross_asset.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "cross_asset_correlation",
    "benchmark_momentum_12bar",
    "daily_conviction",
]


class CrossAssetStrategy(BaseStrategy):
    name = "cross_asset"
    asset_classes = ["metal"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        daily_conv = normalize(d["daily_conviction"])

        if direction == "LONG":
            # Gold LONG: DXY weak + USD bearish momentum → gold bullish
            dxy_inverse = normalize(-d["cross_asset_correlation"])    # inverse DXY
            usd_weak    = normalize(-d["benchmark_momentum_12bar"])   # USD bearish
        else:  # SHORT
            # Gold SHORT: DXY strong + USD bullish momentum → gold bearish
            dxy_inverse = normalize(d["cross_asset_correlation"])
            usd_weak    = normalize(d["benchmark_momentum_12bar"])

        scores = (dxy_inverse + usd_weak + daily_conv) / 3.0
        return self._build_result(d, scores, direction, top_n)
