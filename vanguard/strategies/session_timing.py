"""
session_timing.py — Session Timing Strategy (Forex).

Scores forex pairs based on session quality:
  - London/NY overlap is peak (1.0), Asian is low (0.3)
  - High relative volume confirms active session
  - Early in session is preferred
  - Tight spread (liquid pair) preferred

Applies to: forex.
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/session_timing.py
"""
from __future__ import annotations

import pandas as pd

from vanguard.strategies.base import BaseStrategy
from vanguard.helpers.normalize import normalize, safe_fill

# session_phase from V3:
# 1.0 = London/NY overlap, 0.8 = London, 0.7 = NY, 0.3 = Asian, 0.0 = off-hours
_SESSION_QUALITY = {1.0: 1.0, 0.8: 0.8, 0.7: 0.7, 0.3: 0.3, 0.0: 0.0}

_REQUIRED = [
    "session_phase",
    "relative_volume",
    "time_in_session_pct",
    "spread_proxy",
]


def _session_quality_score(phase: pd.Series) -> pd.Series:
    """Map session_phase → quality score using the lookup table."""
    return phase.map(lambda x: _SESSION_QUALITY.get(float(round(x, 1)), 0.0))


class SessionTimingStrategy(BaseStrategy):
    name = "session_timing"
    asset_classes = ["forex"]
    short_only = False

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        d = safe_fill(df.reset_index(drop=True), _REQUIRED)

        session_qual = _session_quality_score(d["session_phase"])
        rel_vol      = normalize(d["relative_volume"])
        early        = normalize(1.0 - d["time_in_session_pct"])   # early in session = better
        tight_spread = normalize(-d["spread_proxy"])               # tight spread = liquid

        scores = (session_qual + rel_vol + early + tight_spread) / 4.0
        return self._build_result(d, scores, direction, top_n)
