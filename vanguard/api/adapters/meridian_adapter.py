"""
meridian_adapter.py — Read-only adapter for Meridian shortlist_daily.

Reads from ~/SS/Meridian/data/v2_universe.db (READ ONLY).
Returns normalized candidate rows for the unified API.

Location: ~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date as date_type, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MERIDIAN_DB = Path.home() / "SS" / "Meridian" / "data" / "v2_universe.db"
_ET = ZoneInfo("America/New_York")

def _assign_tier(direction: str, tcn: float | None) -> str:
    d = direction.upper()
    if tcn is None:
        return "meridian_untiered"
    if d == "LONG":
        return "tier_meridian_long"
    if d == "SHORT":
        return "tier_meridian_short"
    return "meridian_untiered"


def _make_as_of(date_str: str) -> str:
    """Return ISO timestamp for market-close + 1h on the given date (ET)."""
    return datetime.fromisoformat(f"{date_str}T17:00:00").replace(tzinfo=_ET).isoformat()


def _normalize_row(r: sqlite3.Row, date_str: str) -> dict[str, Any]:
    ticker    = (r["ticker"] or "").upper()
    direction = (r["direction"] or "LONG").upper()
    row_keys = set(r.keys())
    raw_tcn_long = r["tcn_long_score"] if "tcn_long_score" in row_keys else None
    raw_tcn_short = r["tcn_short_score"] if "tcn_short_score" in row_keys else None
    tcn_long = float(raw_tcn_long) if raw_tcn_long is not None else None
    tcn_short = float(raw_tcn_short) if raw_tcn_short is not None else None
    raw_tcn = r["tcn_score"] if "tcn_score" in row_keys else None
    if raw_tcn is not None:
        raw_tcn = float(raw_tcn)
        if direction == "LONG" and tcn_long is None:
            tcn_long = raw_tcn
        if direction == "SHORT" and tcn_short is None:
            tcn_short = raw_tcn
    tcn = tcn_long if direction == "LONG" else tcn_short
    if tcn == 0.5:
        # 0.5 is Meridian's known neutral fallback when TCN cannot build a valid sequence.
        tcn = None
    fr   = float(r["factor_rank"] or 0.0) if "factor_rank" in row_keys else 0.0
    lgbm_long = float(r["lgbm_long_prob"] or 0.0) if "lgbm_long_prob" in row_keys and r["lgbm_long_prob"] is not None else None
    lgbm_short = float(r["lgbm_short_prob"] or 0.0) if "lgbm_short_prob" in row_keys and r["lgbm_short_prob"] is not None else None
    # No synthetic fallback — lgbm_long_prob and lgbm_short_prob come exclusively from
    # predictions_daily (written by lgbm_scorer.py stage 4). If NULL, scorer hasn't run yet.
    predictions_missing = lgbm_long is None and lgbm_short is None
    resolved_price = float(r["resolved_price"]) if r["resolved_price"] is not None else None
    final_score = float(r["final_score"] or 0.0) if "final_score" in row_keys else float(tcn or 0.0)
    residual_alpha = float(r["residual_alpha"] or 0.0) if "residual_alpha" in row_keys else 0.0
    predicted_return = None
    beta = float(r["beta"] or 0.0) if "beta" in row_keys else 0.0

    row: dict[str, Any] = {
        "row_id":  f"meridian:{ticker}:{direction}:{date_str}",
        "source":  "meridian",
        "symbol":  ticker,
        "side":    direction,
        "price":   resolved_price,
        "as_of":   _make_as_of(date_str),
        "tier":    _assign_tier(direction, tcn),
        "sector":  r["sector"] or "UNKNOWN",
        "regime":  (r["regime"] or "").upper(),
        "partial_data": predictions_missing,
        "provenance": {
            "base":              "shortlist_daily",
            "predictions_daily": "missing" if predictions_missing else "joined",
            "price_source":      "resolved",
        },
        "lane_status": "live",
        "readiness":   "live",
        "tcn_long_score":  tcn_long,
        "tcn_short_score": tcn_short,
        # Meridian-native fields (also exposed at top level for uniform filtering)
        "tcn_score":       tcn,
        "factor_rank":     fr,
        "final_score":     final_score,
        "residual_alpha":  residual_alpha,
        "predicted_return": predicted_return,
        "beta":            beta,
        "rank":            int(r["rank"] or 0),
        "m_lgbm_long_prob":  lgbm_long,
        "m_lgbm_short_prob": lgbm_short,
        # Nested native dict — keys MUST match field registry (unified_api.py FIELD_REGISTRY)
        "native": {
            "tcn_long_score":     tcn_long,
            "tcn_short_score":    tcn_short,
            "tcn_score":          tcn,
            "factor_rank":        fr,
            "final_score":        final_score,
            "residual_alpha":     residual_alpha,
            "predicted_return":   predicted_return,
            "beta":               beta,
            "rank":               int(r["rank"] or 0),
            "m_lgbm_long_prob":   lgbm_long,
            "m_lgbm_short_prob":  lgbm_short,
        },
    }
    return row


def get_candidates(
    trade_date: str | None = None,
    direction: str | None = None,
) -> list[dict[str, Any]]:
    """
    Load all shortlist_daily rows for the given date (default: latest date).

    Parameters
    ----------
    trade_date : YYYY-MM-DD string. If None, uses MAX(date).
    direction  : "LONG", "SHORT", or None (all).

    Returns
    -------
    List of normalized candidate dicts (flat — native fields promoted to top level).
    """
    if not MERIDIAN_DB.exists():
        logger.warning(f"Meridian DB not found: {MERIDIAN_DB}")
        return []

    try:
        con = sqlite3.connect(f"file:{MERIDIAN_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            if trade_date is None:
                row = con.execute(
                    "SELECT MAX(date) AS d FROM shortlist_daily"
                ).fetchone()
                trade_date = row["d"] if row and row["d"] else str(date_type.today())

            query_new = """
                SELECT s.date,
                       s.ticker,
                       s.direction,
                       s.predicted_return,
                       s.beta,
                       s.market_component,
                       s.residual_alpha,
                       s.rank,
                       s.regime,
                       s.sector,
                       COALESCE(
                           s.price,
                           p.price,
                           (
                               SELECT db.close
                               FROM daily_bars db
                               WHERE db.ticker = s.ticker
                                 AND db.close IS NOT NULL
                                 AND db.date <= s.date
                               ORDER BY db.date DESC
                               LIMIT 1
                           )
                       ) AS resolved_price,
                       s.tcn_long_score,
                       s.tcn_short_score,
                       s.final_score,
                       p.lgbm_long_prob,
                       p.lgbm_short_prob
                FROM shortlist_daily s
                LEFT JOIN predictions_daily p
                  ON p.date = s.date AND p.ticker = s.ticker
                WHERE s.date = ?
            """
            query_old = """
                SELECT s.date,
                       s.ticker,
                       s.direction,
                       s.predicted_return,
                       s.beta,
                       s.market_component,
                       s.residual_alpha,
                       s.rank,
                       s.regime,
                       s.sector,
                       COALESCE(
                           s.price,
                           p.price,
                           (
                               SELECT db.close
                               FROM daily_bars db
                               WHERE db.ticker = s.ticker
                                 AND db.close IS NOT NULL
                                 AND db.date <= s.date
                               ORDER BY db.date DESC
                               LIMIT 1
                           )
                       ) AS resolved_price,
                       s.tcn_score,
                       s.factor_rank,
                       s.final_score,
                       p.lgbm_long_prob,
                       p.lgbm_short_prob
                FROM shortlist_daily s
                LEFT JOIN predictions_daily p
                  ON p.date = s.date AND p.ticker = s.ticker
                WHERE s.date = ?
            """
            params: list[Any] = [trade_date]
            if direction and direction.upper() in ("LONG", "SHORT"):
                query_new += " AND UPPER(direction) = ?"
                query_old += " AND UPPER(direction) = ?"
                params.append(direction.upper())
            query_new += " ORDER BY final_score DESC"
            query_old += " ORDER BY final_score DESC"

            try:
                rows = con.execute(query_new, params).fetchall()
            except sqlite3.Error:
                rows = con.execute(query_old, params).fetchall()
        finally:
            con.close()
    except Exception as exc:
        logger.error(f"MeridianAdapter error: {exc}")
        return []

    result = [_normalize_row(r, trade_date) for r in rows]
    logger.debug(f"MeridianAdapter: {len(result)} rows for date={trade_date}")
    return result


def get_latest_date() -> str | None:
    """Return the most recent date present in shortlist_daily, or None."""
    if not MERIDIAN_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{MERIDIAN_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT MAX(date) AS d FROM shortlist_daily"
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            con.close()
    except Exception as exc:
        logger.error(f"MeridianAdapter.get_latest_date error: {exc}")
        return None


def count_candidates(trade_date: str | None = None) -> int:
    """Return total count of candidates for the given date."""
    rows = get_candidates(trade_date)
    return len(rows)
