"""
llm_strategy.py — LLM Qualitative Overlay Strategy (Optional, default OFF).

Runs after all structured strategies. Receives the top N candidates per
asset class and asks an LLM to select the highest-conviction picks and
flag any event-risk skips.

Config is loaded from vanguard_selection_config.json → "llm_strategy" key.
Set "enabled": true to activate.

Applies to: all asset classes (runs last, after structured strategies).
Direction: BOTH.

Location: ~/SS/Vanguard/vanguard/strategies/llm_strategy.py
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from vanguard.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class LlmStrategy(BaseStrategy):
    """
    LLM qualitative overlay. Default disabled.
    When enabled, calls an LLM API to rerank/filter structured strategy output.
    """

    name = "llm"
    asset_classes = ["equity", "forex", "index", "metal", "crypto", "commodity"]
    short_only = False

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._enabled: bool = self._config.get("enabled", False)

    def score(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        if not self._enabled:
            logger.debug("LLM strategy disabled — skipping")
            return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        try:
            return self._run_llm(df, direction, top_n)
        except Exception as exc:
            fallback = self._config.get("fallback_on_timeout", "skip")
            logger.warning(f"LLM strategy failed (fallback={fallback}): {exc}")
            if fallback == "skip":
                return pd.DataFrame()
            return pd.DataFrame()

    def _run_llm(self, df: pd.DataFrame, direction: str, top_n: int) -> pd.DataFrame:
        """
        Stub for LLM API call. Implement provider-specific logic here.

        Expected response JSON:
        {
            "picks": [{"symbol": "...", "confidence": 8, "reason": "..."}],
            "skips": [{"symbol": "...", "reason": "..."}]
        }
        """
        provider = self._config.get("provider", "anthropic")
        model    = self._config.get("model", "claude-sonnet-4-20250514")
        top_k    = self._config.get("top_k_picks", 5)

        logger.info(f"LLM strategy: calling {provider}/{model} for {direction} top {top_k}")

        # Build candidate table for prompt
        candidates = df[["symbol"]].copy()
        candidates["direction"] = direction
        candidate_json = candidates.to_dict(orient="records")

        prompt = (
            f"You are reviewing intraday trading candidates.\n"
            f"Direction: {direction}\n\n"
            f"Top candidates from structured strategies:\n"
            f"{json.dumps(candidate_json, indent=2)}\n\n"
            f"Considering qualitative context:\n"
            f"1. Which {top_k} would you select as highest conviction?\n"
            f"2. Any to SKIP due to event risk or qualitative concern?\n"
            f"3. Rank your top {top_k} with confidence 1-10 and one-line reason.\n\n"
            f'Return JSON only: {{"picks": [{{"symbol": "...", "confidence": N, "reason": "..."}}], '
            f'"skips": [{{"symbol": "...", "reason": "..."}}]}}'
        )

        # NOTE: LLM API integration not yet implemented.
        # Add provider-specific client call here when enabling.
        logger.warning("LLM API call not implemented — returning empty")
        return pd.DataFrame()
