"""
liquidity_grab_reversal.py — Liquidity Grab Reversal Strategy (Forex/Metals).

Identifies setups where stops have been swept above/below equal highs/lows
and price is reversing into an order block in the opposite zone.

LONG: sell-side liquidity swept → reversal up into OB in discount zone.
SHORT: buy-side liquidity swept → reversal down into OB in premium zone.

S1 validation: forex shorts after buy-side liquidity grabs are among the
highest probability SMC setups (institutions sweep retail stops above
equal highs then reverse hard).

Applies to: forex, metal.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/liquidity_grab_reversal.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "liquidity_sweep",
    "premium_discount_zone",
    "effort_vs_result",
    "momentum_acceleration",
    "ob_proximity_5m",
]


class LiquidityGrabReversalStrategy(BaseStrategy):
    name = "liquidity_grab_reversal"
    asset_classes = ["forex", "metal"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        sweep   = normalize(d["liquidity_sweep"])
        effort  = normalize(d["effort_vs_result"])
        ob_prox = normalize(d["ob_proximity_5m"])

        if direction == "LONG":
            # Sell-side swept → reversal up into OB in discount zone
            zone_extreme = normalize(1.0 - d["premium_discount_zone"])   # discount extreme
            reversal_mom = normalize(-d["momentum_acceleration"])         # momentum decelerating (reversal)
        else:  # SHORT
            # Buy-side swept → reversal down into OB in premium zone
            zone_extreme = normalize(d["premium_discount_zone"])          # premium extreme
            reversal_mom = normalize(d["momentum_acceleration"])          # momentum accelerating downward

        scores = (sweep + zone_extreme + effort + reversal_mom + ob_prox) / 5.0
        return self._build_result(d, scores, direction, top_n)
