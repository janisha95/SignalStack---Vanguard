"""
momentum_breakout.py — Momentum Breakout Strategy (Crypto).

Scores crypto instruments showing violent directional moves confirmed by
volume and structure breaks. Crypto-specific: these moves are faster and
larger than equities, so volume + BOS confirmation is critical.

Features: momentum_3bar, momentum_acceleration, atr_expansion,
          volume_burst_z, structure_break (BOS confirmation).

Applies to: crypto.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/momentum_breakout.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "momentum_3bar",
    "momentum_acceleration",
    "atr_expansion",
    "volume_burst_z",
    "structure_break",
]


class MomentumBreakoutStrategy(BaseStrategy):
    name = "momentum_breakout"
    asset_classes = ["crypto"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        atr = normalize(d["atr_expansion"])
        vbz = normalize(d["volume_burst_z"])

        if direction == "LONG":
            mom3  = normalize(d["momentum_3bar"])
            macc  = normalize(d["momentum_acceleration"])
            bos   = normalize(d["structure_break"].clip(lower=0.0))
        else:  # SHORT
            mom3  = normalize(-d["momentum_3bar"])
            macc  = normalize(-d["momentum_acceleration"])
            bos   = normalize((-d["structure_break"]).clip(lower=0.0))

        scores = (mom3 + macc + atr + vbz + bos) / 5.0
        return self._build_result(d, scores, direction, top_n)
