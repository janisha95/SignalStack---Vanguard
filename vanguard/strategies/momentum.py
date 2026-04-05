"""
momentum.py — Momentum Strategy.

Scores candidates on price momentum, acceleration, ATR expansion, and
volume burst. High scores = strong momentum in the signal direction.

Applies to: equity, index, commodity.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/momentum.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "momentum_3bar",
    "momentum_12bar",
    "momentum_acceleration",
    "atr_expansion",
    "volume_burst_z",
]


class MomentumStrategy(BaseStrategy):
    name = "momentum"
    asset_classes = ["equity", "index", "commodity"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        if direction == "LONG":
            m3   = normalize(d["momentum_3bar"])
            m12  = normalize(d["momentum_12bar"])
            macc = normalize(d["momentum_acceleration"])
        else:  # SHORT — negative momentum is the signal
            m3   = normalize(-d["momentum_3bar"])
            m12  = normalize(-d["momentum_12bar"])
            macc = normalize(-d["momentum_acceleration"])

        atr = normalize(d["atr_expansion"])
        vbz = normalize(d["volume_burst_z"])

        scores = (m3 + m12 + macc + atr + vbz) / 5.0
        return self._build_result(d, scores, direction, top_n)
