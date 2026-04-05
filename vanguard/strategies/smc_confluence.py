"""
smc_confluence.py — SMC Confluence Strategy.

Scores candidates based on Smart Money Concepts:
  order block proximity, FVG alignment, structure break (BOS/CHoCH),
  liquidity sweep, premium/discount zone, HTF trend alignment.

Applies to: equity, forex, metal, crypto.
Direction: BOTH (different FVG and zone features used per direction).

Location: ~/SS/Vanguard/vanguard/strategies/smc_confluence.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "ob_proximity_5m",
    "fvg_bullish_nearest",
    "fvg_bearish_nearest",
    "structure_break",
    "liquidity_sweep",
    "premium_discount_zone",
    "htf_trend_direction",
]


class SmcConfluenceStrategy(BaseStrategy):
    name = "smc"
    asset_classes = ["equity", "forex", "metal", "crypto"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        # --- Feature that applies to both directions ---
        ob    = normalize(d["ob_proximity_5m"])
        sweep = normalize(d["liquidity_sweep"])

        # --- Direction-specific features ---
        if direction == "LONG":
            fvg   = normalize(d["fvg_bullish_nearest"])
            bos   = normalize(d["structure_break"].clip(lower=0.0))
            zone  = normalize(1.0 - d["premium_discount_zone"])   # discount zone
            htf   = normalize(d["htf_trend_direction"].clip(lower=0.0))
        else:  # SHORT
            fvg   = normalize(d["fvg_bearish_nearest"])
            bos   = normalize((-d["structure_break"]).clip(lower=0.0))
            zone  = normalize(d["premium_discount_zone"])          # premium zone
            htf   = normalize((-d["htf_trend_direction"]).clip(lower=0.0))

        scores = (ob + fvg + bos + sweep + zone + htf) / 6.0
        return self._build_result(d, scores, direction, top_n)
