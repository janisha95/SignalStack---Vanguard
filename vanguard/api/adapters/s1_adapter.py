"""
s1_adapter.py — Read-only adapter for S1 (SignalStack) scorer_predictions.

Reads from:
  ~/SS/Advance/data_cache/signalstack_results.db   (scorer_predictions table)
  ~/SS/Advance/evening_results/convergence_*.json   (convergence data)

Merges on (ticker, direction). Returns normalized candidate rows.

Location: ~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date as date_type
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

S1_DB       = Path.home() / "SS" / "Advance" / "data_cache" / "signalstack_results.db"
S1_CONV_DIR = Path.home() / "SS" / "Advance" / "evening_results"
MERIDIAN_DB = Path.home() / "SS" / "Meridian" / "data" / "v2_universe.db"

# Tier thresholds (mirrored from execute_daily_picks.py)
RF_DUAL_MIN       = 0.60
NN_DUAL_MIN       = 0.75
NN_NN_MIN         = 0.90
RF_RF_MIN         = 0.60
CONV_RF_MIN       = 0.80
SCORER_LONG_MIN   = 0.55
SCORER_SHORT_MIN  = 0.55


def _normalize_direction(value: str | None) -> str:
    v = (value or "").upper()
    if v == "BUY":
        return "LONG"
    if v == "SELL":
        return "SHORT"
    return v or "LONG"


def _assign_tier(
    direction: str,
    p_tp: float,
    nn_p_tp: float,
    convergence_score: float,
    scorer_prob: float,
    assigned_longs: set[str],
    ticker: str,
) -> str:
    """Return tier name. For LONG tickers, updates assigned_longs set."""
    d = _normalize_direction(direction)
    if d == "SHORT":
        return "tier_s1_short" if scorer_prob >= SCORER_SHORT_MIN else "s1_untiered"

    # LONG tier priority: dual > nn > rf > scorer_long
    if ticker in assigned_longs:
        return "_already_assigned"  # caller handles dedup

    if p_tp >= RF_DUAL_MIN and nn_p_tp >= NN_DUAL_MIN:
        return "tier_dual"
    if nn_p_tp >= NN_NN_MIN:
        return "tier_nn"
    if p_tp > RF_RF_MIN and convergence_score >= CONV_RF_MIN:
        return "tier_rf"
    if scorer_prob >= SCORER_LONG_MIN:
        return "tier_scorer_long"
    return "s1_untiered"


def _make_as_of(run_date: str) -> str:
    return f"{run_date}T17:16:00-04:00"


def _fallback_price(ticker: str) -> float:
    if not MERIDIAN_DB.exists():
        return 0.0
    try:
        con = sqlite3.connect(f"file:{MERIDIAN_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT close FROM daily_bars WHERE ticker = ? AND close IS NOT NULL ORDER BY date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0
        finally:
            con.close()
    except Exception:
        return 0.0


def load_convergence() -> dict[tuple[str, str], dict[str, Any]]:
    """
    Load latest convergence JSON.

    Returns {(ticker, direction): {convergence_score, n_strategies_agree,
                                   strategies, volume_ratio}}
    """
    if not S1_CONV_DIR.exists():
        return {}
    files = list(S1_CONV_DIR.glob("convergence_*.json"))
    if not files:
        logger.warning(f"No convergence JSON files in {S1_CONV_DIR}")
        return {}

    latest = sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)[0]
    try:
        with open(latest, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.error(f"Failed to load convergence JSON {latest.name}: {exc}")
        return {}

    # Support both top-level list and {"shortlist": [...]} wrapper
    if isinstance(data, list):
        shortlist = data
    elif isinstance(data, dict):
        shortlist = data.get("shortlist") or []
    else:
        return {}

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in shortlist:
        ticker    = (item.get("ticker") or "").upper()
        direction = (item.get("direction") or "LONG").upper()
        if not ticker:
            continue
        result[(ticker, direction)] = {
            "convergence_score":  float(item.get("convergence_score") or 0.0),
            "n_strategies_agree": int(item.get("n_agree") or item.get("n_strategies_agree") or 0),
            "strategies":         item.get("strategies") or [],
            "volume_ratio":       float(item.get("rel_volume") or item.get("volume_ratio") or 0.0),
        }

    logger.debug(f"Convergence loaded: {len(result)} entries from {latest.name}")
    return result


def get_candidates(
    run_date: str | None = None,
    direction: str | None = None,
) -> list[dict[str, Any]]:
    """
    Load scorer_predictions merged with convergence data.

    Parameters
    ----------
    run_date  : YYYY-MM-DD string. If None, uses MAX(run_date).
    direction : "LONG", "SHORT", or None (all).

    Returns
    -------
    List of normalized candidate dicts with S1-native fields at top level.
    """
    if not S1_DB.exists():
        logger.warning(f"S1 DB not found: {S1_DB}")
        return []

    try:
        con = sqlite3.connect(f"file:{S1_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            if run_date is None:
                row = con.execute(
                    "SELECT MAX(run_date) AS d FROM scorer_predictions"
                ).fetchone()
                run_date = row["d"] if row and row["d"] else str(date_type.today())

            # Dedup: one row per (ticker, direction) using best scorer_prob
            query = """
                SELECT ticker,
                       CASE
                           WHEN UPPER(direction) = 'BUY' THEN 'LONG'
                           WHEN UPPER(direction) = 'SELL' THEN 'SHORT'
                           ELSE UPPER(direction)
                       END AS direction,
                       MAX(scorer_prob)  AS scorer_prob,
                       MAX(p_tp)         AS p_tp,
                       MAX(nn_p_tp)      AS nn_p_tp,
                       MAX(price)        AS price,
                       MAX(regime)       AS regime,
                       MAX(sector)       AS sector,
                       MAX(gate_source)  AS gate_source,
                       GROUP_CONCAT(strategy) AS strategies_raw
                FROM   scorer_predictions
                WHERE  run_date = ?
            """
            params: list[Any] = [run_date]
            if direction and direction.upper() in ("LONG", "SHORT"):
                if direction.upper() == "LONG":
                    query += " AND UPPER(direction) IN ('LONG', 'BUY')"
                else:
                    query += " AND UPPER(direction) IN ('SHORT', 'SELL')"
            query += " GROUP BY ticker, CASE WHEN UPPER(direction) = 'BUY' THEN 'LONG' WHEN UPPER(direction) = 'SELL' THEN 'SHORT' ELSE UPPER(direction) END ORDER BY scorer_prob DESC"

            rows = con.execute(query, params).fetchall()
        finally:
            con.close()
    except Exception as exc:
        logger.error(f"S1Adapter error: {exc}")
        return []

    convergence = load_convergence()
    assigned_longs: set[str] = set()
    result: list[dict[str, Any]] = []

    for r in rows:
        ticker    = (r["ticker"] or "").upper()
        dir_upper = _normalize_direction(r["direction"])
        p_tp      = float(r["p_tp"]        or 0.0)
        nn_p_tp   = float(r["nn_p_tp"]     or 0.0)
        sp        = float(r["scorer_prob"]  or 0.0)
        raw_price = float(r["price"] or 0.0)
        price_is_fallback = not raw_price
        price = raw_price if raw_price else _fallback_price(ticker)

        conv_data = convergence.get((ticker, dir_upper), {})
        cs        = float(conv_data.get("convergence_score", 0.0))
        n_agree   = int(conv_data.get("n_strategies_agree", 0))
        strategies_list = conv_data.get("strategies") or []
        vol_ratio = float(conv_data.get("volume_ratio", 0.0))

        tier = _assign_tier(dir_upper, p_tp, nn_p_tp, cs, sp, assigned_longs, ticker)
        if tier == "_already_assigned":
            continue
        if dir_upper == "LONG" and tier not in ("s1_untiered",):
            assigned_longs.add(ticker)

        row: dict[str, Any] = {
            "row_id":  f"s1:{ticker}:{dir_upper}:{run_date}:scorer",
            "source":  "s1",
            "symbol":  ticker,
            "side":    dir_upper,
            "price":   price if price else None,
            "as_of":   _make_as_of(run_date),
            "tier":    tier,
            "sector":  r["sector"] or "UNKNOWN",
            "regime":  (r["regime"] or "").upper(),
            "partial_data": bool(not conv_data) or price_is_fallback,
            "provenance": {
                "base":         "scorer_predictions",
                "convergence":  "primary" if conv_data else "missing",
                "price_source": "daily_bars_fallback" if price_is_fallback else "scorer_predictions",
            },
            "lane_status": "live",
            "readiness":   "live",
            # S1-native fields (top-level for uniform filtering)
            "p_tp":               p_tp,
            "nn_p_tp":            nn_p_tp,
            "scorer_prob":        sp,
            "convergence_score":  cs,
            "n_strategies_agree": n_agree,
            "strategy":           r["gate_source"] or "",
            "volume_ratio":       vol_ratio,
            # Nested native dict (for detail endpoint)
            "native": {
                "p_tp":               p_tp,
                "nn_p_tp":            nn_p_tp,
                "scorer_prob":        sp,
                "convergence_score":  cs,
                "n_strategies_agree": n_agree,
                "strategy":           r["gate_source"] or "",
                "volume_ratio":       vol_ratio,
                "strategies":         strategies_list,
            },
        }
        result.append(row)

    logger.debug(f"S1Adapter: {len(result)} rows for run_date={run_date}")
    return result


def get_latest_date() -> str | None:
    """Return the most recent run_date in scorer_predictions, or None."""
    if not S1_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{S1_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT MAX(run_date) AS d FROM scorer_predictions"
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            con.close()
    except Exception as exc:
        logger.error(f"S1Adapter.get_latest_date error: {exc}")
        return None


def count_candidates(run_date: str | None = None) -> int:
    """Return deduped count of candidates for the given run_date."""
    rows = get_candidates(run_date)
    return len(rows)
