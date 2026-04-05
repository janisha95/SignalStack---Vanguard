"""
relative_strength.py — Relative Strength Strategy.

LONG: instruments outperforming the benchmark (leaders in a bullish market).
SHORT: instruments underperforming the benchmark (laggards in a bearish market).

Applies to: equity.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/relative_strength.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark",
    "benchmark_momentum_12bar",
]


class RelativeStrengthStrategy(BaseStrategy):
    name = "relative_strength"
    asset_classes = ["equity"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        if direction == "LONG":
            # Want: strong RS vs benchmark + rising benchmark (rising tide)
            rs_intra = normalize(d["rs_vs_benchmark_intraday"])
            rs_daily = normalize(d["daily_rs_vs_benchmark"])
            bm_mom   = normalize(d["benchmark_momentum_12bar"])
        else:  # SHORT
            # Want: weak RS vs benchmark + falling benchmark (laggard in downturn)
            rs_intra = normalize(-d["rs_vs_benchmark_intraday"])
            rs_daily = normalize(-d["daily_rs_vs_benchmark"])
            bm_mom   = normalize(-d["benchmark_momentum_12bar"])

        scores = (rs_intra + rs_daily + bm_mom) / 3.0
        return self._build_result(d, scores, direction, top_n)
