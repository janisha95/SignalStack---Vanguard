"""
meridian_adapter.py — Read-only adapter for Meridian meridian_shortlist_final.

Reads from ~/SS/Meridian/data/v2_universe.db (READ ONLY).
Returns normalized candidate rows for the unified API.

Primary table: meridian_shortlist_final (written by meridian_daily_shortlist.py)
  Schema (confirmed 2026-04-06):
    id, run_date, ticker, direction, tcn_score, final_score, rank,
    regime, sector, price, created_at

Enriched by LEFT JOIN to predictions_daily for dual-TCN scores:
    tcn_long_score, tcn_short_score, lgbm_long_prob, lgbm_short_prob

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
    row_keys  = set(r.keys())

    # tcn_score is the directional score stored in meridian_shortlist_final
    raw_tcn = r["tcn_score"] if "tcn_score" in row_keys else None
    tcn = float(raw_tcn) if raw_tcn is not None else None
    if tcn == 0.5:
        # 0.5 is Meridian's known neutral fallback when TCN cannot build a valid sequence.
        tcn = None

    # Dual-TCN scores from predictions_daily join (may be NULL if scorer hasn't run)
    raw_tcn_long  = r["tcn_long_score"]  if "tcn_long_score"  in row_keys else None
    raw_tcn_short = r["tcn_short_score"] if "tcn_short_score" in row_keys else None
    tcn_long  = float(raw_tcn_long)  if raw_tcn_long  is not None else None
    tcn_short = float(raw_tcn_short) if raw_tcn_short is not None else None

    lgbm_long  = float(r["lgbm_long_prob"])  if "lgbm_long_prob"  in row_keys and r["lgbm_long_prob"]  is not None else None
    lgbm_short = float(r["lgbm_short_prob"]) if "lgbm_short_prob" in row_keys and r["lgbm_short_prob"] is not None else None
    predictions_missing = lgbm_long is None and lgbm_short is None

    final_score   = float(r["final_score"] or 0.0) if "final_score" in row_keys else float(tcn or 0.0)
    resolved_price = float(r["resolved_price"]) if r["resolved_price"] is not None else None

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
            "base":              "meridian_shortlist_final",
            "predictions_daily": "missing" if predictions_missing else "joined",
            "price_source":      "resolved",
        },
        "lane_status": "live",
        "readiness":   "live",
        "tcn_long_score":  tcn_long,
        "tcn_short_score": tcn_short,
        # Meridian-native fields (also exposed at top level for uniform filtering)
        "tcn_score":       tcn,
        "final_score":     final_score,
        "rank":            int(r["rank"] or 0),
        "m_lgbm_long_prob":  lgbm_long,
        "m_lgbm_short_prob": lgbm_short,
        # Nested native dict — keys MUST match field registry (unified_api.py FIELD_REGISTRY)
        "native": {
            "tcn_long_score":     tcn_long,
            "tcn_short_score":    tcn_short,
            "tcn_score":          tcn,
            "final_score":        final_score,
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
    Load all meridian_shortlist_final rows for the given date (default: latest date).

    Parameters
    ----------
    trade_date : YYYY-MM-DD string. If None, uses MAX(run_date).
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
                    "SELECT MAX(run_date) AS d FROM meridian_shortlist_final"
                ).fetchone()
                trade_date = row["d"] if row and row["d"] else str(date_type.today())

            query = """
                SELECT f.run_date,
                       f.ticker,
                       f.direction,
                       f.rank,
                       f.regime,
                       f.sector,
                       f.final_score,
                       f.tcn_score,
                       COALESCE(
                           f.price,
                           (
                               SELECT db.close
                               FROM daily_bars db
                               WHERE db.ticker = f.ticker
                                 AND db.close IS NOT NULL
                                 AND db.date <= f.run_date
                               ORDER BY db.date DESC
                               LIMIT 1
                           )
                       ) AS resolved_price,
                       p.tcn_long_score,
                       p.tcn_short_score,
                       p.lgbm_long_prob,
                       p.lgbm_short_prob
                FROM meridian_shortlist_final f
                LEFT JOIN predictions_daily p
                  ON p.date = f.run_date AND p.ticker = f.ticker
                WHERE f.run_date = ?
            """
            params: list[Any] = [trade_date]
            if direction and direction.upper() in ("LONG", "SHORT"):
                query += " AND UPPER(f.direction) = ?"
                params.append(direction.upper())
            query += " ORDER BY f.final_score DESC"

            rows = con.execute(query, params).fetchall()
        finally:
            con.close()
    except Exception as exc:
        logger.error(f"MeridianAdapter error: {exc}")
        return []

    result = [_normalize_row(r, trade_date) for r in rows]
    logger.debug(f"MeridianAdapter: {len(result)} rows for date={trade_date}")
    return result


def get_latest_date() -> str | None:
    """Return the most recent run_date in meridian_shortlist_final, or None."""
    if not MERIDIAN_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{MERIDIAN_DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT MAX(run_date) AS d FROM meridian_shortlist_final"
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
