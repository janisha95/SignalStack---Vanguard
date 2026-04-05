"""
risk_reward_edge.py — Risk-Reward Edge Strategy.

Scores based on the expected value formula:
    edge = (ml_prob × tp_pct) − ((1 − ml_prob) × sl_pct) − spread_cost
    time_adjusted_edge = edge / sqrt(expected_bars)

Higher edge = better expected value relative to risk and time.

Applies to: equity, forex, index, metal, crypto, commodity (all asset classes).
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/risk_reward_edge.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = ["spread_proxy", "tp_pct", "sl_pct"]
_SPREAD_COST_DEFAULT = 0.0005
_EXPECTED_BARS_DEFAULT = 12.0


class RiskRewardEdgeStrategy(BaseStrategy):
    name = "risk_reward"
    asset_classes = ["equity", "forex", "index", "metal", "crypto", "commodity"]
    short_only = False

    def __init__(
        self,
        spread_cost: float = _SPREAD_COST_DEFAULT,
        expected_bars: float = _EXPECTED_BARS_DEFAULT,
    ) -> None:
        self._spread_cost = spread_cost
        self._expected_bars = expected_bars

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        if direction == "LONG":
            ml_prob = d["lgbm_long_prob"] if "lgbm_long_prob" in d.columns else pd.Series(0.5, index=d.index)
        else:
            ml_prob = d["lgbm_short_prob"] if "lgbm_short_prob" in d.columns else pd.Series(0.5, index=d.index)

        tp = d["tp_pct"]
        sl = d["sl_pct"]
        spread = d["spread_proxy"].clip(lower=0.0)

        edge = (ml_prob * tp) - ((1.0 - ml_prob) * sl) - spread
        time_adjusted = edge / np.sqrt(self._expected_bars)

        scores = normalize(time_adjusted)
        return self._build_result(d, scores, direction, top_n)
