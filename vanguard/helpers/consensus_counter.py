"""
consensus_counter.py — Counts strategy agreement per (symbol, direction).

After all strategies produce their shortlists, this module:
  1. Counts how many strategies selected each (symbol, direction) pair.
  2. Annotates each result row with consensus_count and strategies_matched.

Location: ~/SS/Vanguard/vanguard/helpers/consensus_counter.py
"""
from __future__ import annotations

import logging
from collections import defaultdict

import pandas as pd

logger = logging.getLogger(__name__)


def count_consensus(results: list[pd.DataFrame]) -> pd.DataFrame:
    """
    Merge all per-strategy result DataFrames and annotate consensus.

    Each DataFrame in `results` must have columns:
        symbol, direction, strategy, strategy_score, strategy_rank, ml_prob,
        asset_class, regime

    Returns a single DataFrame with the same columns PLUS:
        consensus_count     (int)   — how many strategies picked this row
        strategies_matched  (str)   — comma-joined strategy names
    """
    if not results:
        return pd.DataFrame()

    combined = pd.concat(results, ignore_index=True)

    # Build consensus index: (symbol, direction) → {strategy_names}
    agreement: dict[tuple[str, str], set[str]] = defaultdict(set)
    for _, row in combined.iterrows():
        key = (row["symbol"], row["direction"])
        agreement[key].add(row["strategy"])

    # Annotate
    combined["consensus_count"] = combined.apply(
        lambda r: len(agreement[(r["symbol"], r["direction"])]), axis=1
    )
    combined["strategies_matched"] = combined.apply(
        lambda r: ",".join(sorted(agreement[(r["symbol"], r["direction"])])), axis=1
    )

    return combined
