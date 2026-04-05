"""
correlation_checker.py — Pairwise rolling correlation for V6 Risk Filters.

Computes Pearson correlation of hourly log-returns over the last 60 trading
days (≈ 390 1h bars). Used to enforce the correlation cap filter.

Location: ~/SS/Vanguard/vanguard/helpers/correlation_checker.py
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_ROOT    = Path(__file__).resolve().parent.parent.parent
_DB_PATH = str(_ROOT / "data" / "vanguard_universe.db")

_LOOKBACK_BARS = 390   # ≈ 60 trading days × 6.5h


def compute_correlations(
    symbols: list[str],
    db_path: str = _DB_PATH,
    lookback: int = _LOOKBACK_BARS,
) -> pd.DataFrame:
    """
    Compute pairwise Pearson correlations from hourly bar close prices.

    Returns a DataFrame (symbol × symbol) with correlation values.
    Returns an empty DataFrame if insufficient data.
    """
    if not symbols:
        return pd.DataFrame()

    placeholders = ",".join("?" * len(symbols))
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            f"""
            SELECT symbol, bar_ts_utc, close
            FROM vanguard_bars_1h
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, bar_ts_utc DESC
            """,
            symbols,
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    # Build DataFrame — last `lookback` bars per symbol
    df = pd.DataFrame(rows, columns=["symbol", "ts", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    # Pivot: index=ts, columns=symbol, values=close
    # Take most recent `lookback` bars per symbol
    trimmed = (
        df.groupby("symbol", group_keys=False)
        .apply(lambda g: g.head(lookback))
        .reset_index(drop=True)
    )
    wide = trimmed.pivot_table(index="ts", columns="symbol", values="close")
    wide.sort_index(inplace=True)

    if wide.shape[0] < 10:
        logger.warning("Insufficient bar data for correlation computation")
        return pd.DataFrame()

    # Log returns
    log_ret = np.log(wide / wide.shift(1)).dropna(how="all")

    corr = log_ret.corr()
    return corr


def is_too_correlated(
    symbol: str,
    existing_symbols: list[str],
    corr_matrix: pd.DataFrame,
    threshold: float = 0.85,
) -> tuple[bool, str | None]:
    """
    Check whether `symbol` is too correlated with any existing position.

    Returns (True, correlated_symbol) if correlation > threshold with any
    existing symbol, else (False, None).
    """
    if not existing_symbols or corr_matrix.empty:
        return False, None

    if symbol not in corr_matrix.columns:
        return False, None

    for existing in existing_symbols:
        if existing not in corr_matrix.columns:
            continue
        corr_val = corr_matrix.loc[symbol, existing]
        if pd.notna(corr_val) and abs(corr_val) > threshold:
            return True, existing

    return False, None
