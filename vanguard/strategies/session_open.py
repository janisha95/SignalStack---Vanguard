"""
session_open.py — Session Open Strategy (Indices).

Scores index instruments based on opening range dynamics:
  - Gap size (energy for directional move)
  - Session opening range breakout position
  - Volume confirmation
  - Time proximity to open (early in session preferred)

Active only within the first 60 minutes of the session
(bars_since_session_open ≤ 12 bars on 5-minute chart).

Applies to: index.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/session_open.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

_REQUIRED = [
    "gap_pct",
    "session_opening_range_position",
    "relative_volume",
    "bars_since_session_open",
]

MAX_BARS_FROM_OPEN = 12  # first 60 minutes (12 × 5-minute bars)


class SessionOpenStrategy(BaseStrategy):
    name = "session_open"
    asset_classes = ["index"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        # Only score instruments early in the session
        early_mask = d["bars_since_session_open"] <= MAX_BARS_FROM_OPEN
        d_early = d[early_mask].copy()
        if d_early.empty:
            return pd.DataFrame()

        gap      = normalize(d_early["gap_pct"].abs())
        rel_vol  = normalize(d_early["relative_volume"])
        # Fewer bars since open = better (earlier = higher score)
        early    = normalize(-d_early["bars_since_session_open"])

        if direction == "LONG":
            or_pos = normalize(d_early["session_opening_range_position"])
        else:  # SHORT — below opening range is the breakout direction
            or_pos = normalize(-d_early["session_opening_range_position"])

        scores = (gap + or_pos + rel_vol + early) / 4.0
        return self._build_result(d_early, scores, direction, top_n)
