"""
base.py — BaseStrategy abstract class for V5 Strategy Router.

Location: ~/SS/Vanguard/vanguard/strategies/base.py
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseStrategy(ABC):
    """
    Base class for all V5 scoring strategies.

    Subclasses must set:
        name          : str  — registry key (e.g. "smc", "momentum")
        asset_classes : list[str] — which asset classes this strategy applies to
        short_only    : bool — True only for Breakdown strategy

    Subclasses must implement:
        score(df, direction, top_n) -> DataFrame
    """

    name: str = "base"
    asset_classes: list[str] = []
    short_only: bool = False

    @abstractmethod
    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        """
        Score the candidate instruments and return the top N.

        Parameters
        ----------
        df        : DataFrame with V3 features + lgbm_long_prob + lgbm_short_prob.
                    Already filtered to the correct asset class + ML gate.
        direction : 'LONG' or 'SHORT'.
        top_n     : Maximum number of results to return.

        Returns
        -------
        DataFrame with columns:
            symbol, direction, strategy_score (float), strategy_rank (int)
        Sorted by strategy_score DESC. Returns empty DataFrame if no valid rows.
        """
        raise NotImplementedError

    def _build_result(
        self,
        df: pd.DataFrame,
        scores: pd.Series,
        direction: str,
        top_n: int,
    ) -> pd.DataFrame:
        """
        Shared result builder: attach scores, rank, clip to top_n, return clean DataFrame.
        """
        out = df[["symbol"]].copy()
        out["direction"] = direction
        out["strategy_score"] = scores.values
        out = out.sort_values("strategy_score", ascending=False).head(top_n).reset_index(drop=True)
        out["strategy_rank"] = out.index + 1
        return out[["symbol", "direction", "strategy_score", "strategy_rank"]]
