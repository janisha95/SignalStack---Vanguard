"""
vanguard_adapter.py — Reads from vanguard_shortlist and returns normalized candidates.

Deduplicates by (symbol, direction), returning the best-scored row per pair
(highest strategy_score, which already reflects consensus ranking).

Location: ~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any

from vanguard.helpers.clock import utc_to_et
from vanguard.helpers.bars import parse_utc
from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

_DB = os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db")


def _latest_prices_for_symbols(
    con: sqlite3.Connection,
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch the latest 5m close for each symbol, when available."""
    if not symbols:
        return {}

    placeholders = ",".join("?" for _ in symbols)
    rows = con.execute(
        f"""
        SELECT b.symbol, b.bar_ts_utc, b.close, b.data_source
        FROM vanguard_bars_5m b
        JOIN (
            SELECT symbol, MAX(bar_ts_utc) AS bar_ts_utc
            FROM vanguard_bars_5m
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        ) latest
          ON b.symbol = latest.symbol
         AND b.bar_ts_utc = latest.bar_ts_utc
        """,
        symbols,
    ).fetchall()

    prices: dict[str, dict[str, Any]] = {}
    for row in rows:
        close_px = row["close"]
        price = None
        if close_px is not None and float(close_px) > 0:
            price = round(float(close_px), 6)
        prices[row["symbol"]] = {
            "price": price,
            "price_bar_ts_utc": row["bar_ts_utc"],
            "price_source": row["data_source"] or "vanguard_bars_5m",
        }
    return prices


def get_candidates(
    trade_date: str | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    """
    Read the latest Vanguard shortlist and return normalized candidate rows.

    Returns a dict with:
      candidates  — list of normalized rows
      readiness   — "risk_ready" | "shortlist_ready" | "data_ready"
      lane_status — "staged" (Vanguard is pre-execution, not yet live)

    Deduplicates by (symbol, direction) — returns the best-scored row per pair.
    `trade_date` is accepted but ignored (always returns the latest cycle).
    `direction` filters to LONG or SHORT if provided.
    """
    try:
        with sqlite_conn(_DB) as con:
            latest_row = con.execute("SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist").fetchone()
            latest = latest_row[0] if latest_row else None
            if not latest:
                return {"candidates": [], "readiness": "data_ready", "lane_status": "staged"}

            rows = con.execute(
                """
                SELECT symbol, direction, asset_class, strategy,
                       strategy_rank, strategy_score, ml_prob, edge_score,
                       consensus_count, strategies_matched, regime, cycle_ts_utc,
                       model_family, model_source, model_readiness,
                       feature_profile, tbm_profile
                FROM vanguard_shortlist
                WHERE cycle_ts_utc = ?
                ORDER BY consensus_count DESC, strategy_score DESC
                """,
                (latest,),
            ).fetchall()
            price_map = _latest_prices_for_symbols(
                con,
                sorted({str(row["symbol"]) for row in rows if row["symbol"]}),
            )
    except Exception as exc:
        logger.warning(f"VanguardAdapter: query failed: {exc}")
        return {"candidates": [], "readiness": "data_ready", "lane_status": "staged"}

    # Deduplicate: keep highest-scored row per (symbol, direction)
    seen: set[tuple[str, str]] = set()
    candidates: list[dict[str, Any]] = []

    for row in rows:
        sym = row["symbol"]
        side = row["direction"]

        if direction and side != direction.upper():
            continue

        key = (sym, side)
        if key in seen:
            continue
        seen.add(key)
        px_info = price_map.get(sym) or {}
        price = px_info.get("price")
        price_unavailable_reason = None
        if price is None:
            price_unavailable_reason = "missing_latest_vanguard_bars_5m"

        # strategies_matched may be comma-separated or JSON list
        strats_raw = row["strategies_matched"] or ""
        if strats_raw.startswith("["):
            try:
                strats = json.loads(strats_raw)
            except Exception:
                strats = [s.strip() for s in strats_raw.split(",") if s.strip()]
        else:
            strats = [s.strip() for s in strats_raw.split(",") if s.strip()]

        candidates.append({
            "row_id":  f"vanguard:{sym}:{side}:{latest}",
            "source":  "vanguard",
            "symbol":  sym,
            "side":    side,
            "price":   price,
            "price_unavailable_reason": price_unavailable_reason,
            "as_of":   latest,
            "as_of_display": utc_to_et(parse_utc(latest)).isoformat(),
            "tier":    f"vanguard_{side.lower()}",
            "sector":  row["asset_class"] or "UNKNOWN",
            "regime":  row["regime"] or "UNKNOWN",
            "partial_data": False,  # Vanguard shortlist is self-contained
            "provenance": {
                "base":  "vanguard_shortlist",
                "cycle": latest,
                "price_source": px_info.get("price_source") or "vanguard_bars_5m_latest",
                "price_bar_ts_utc": px_info.get("price_bar_ts_utc"),
            },
            "lane_status": "staged",
            "readiness":   "shortlist_ready",
            "native": {
                "strategy":           row["strategy"],
                "strategy_rank":      row["strategy_rank"],
                "strategy_score":     round(float(row["strategy_score"]), 4) if row["strategy_score"] is not None else None,
                "ml_prob":            round(float(row["ml_prob"]), 4)        if row["ml_prob"]         is not None else None,
                "edge_score":         round(float(row["edge_score"]), 4)     if row["edge_score"]      is not None else None,
                "consensus_count":    int(row["consensus_count"] or 0),
                "strategies_matched": strats,
                "asset_class":        row["asset_class"],
                "model_family":       row["model_family"],
                "model_source":       row["model_source"],
                "model_readiness":    row["model_readiness"],
                "feature_profile":    row["feature_profile"],
                "tbm_profile":        row["tbm_profile"],
                "price_source":       px_info.get("price_source"),
                "price_bar_ts_utc":   px_info.get("price_bar_ts_utc"),
            },
        })

    # Determine overall readiness: risk_ready if any approved rows exist in portfolio
    has_portfolio = False
    try:
        with sqlite_conn(_DB) as _con:
            tp = _con.execute(
                "SELECT COUNT(*) FROM vanguard_tradeable_portfolio WHERE status='APPROVED'"
            ).fetchone()[0]
            has_portfolio = tp > 0
    except Exception:
        pass

    if has_portfolio:
        readiness = "risk_ready"
    else:
        readiness = "shortlist_ready" if candidates else "data_ready"

    logger.debug(
        f"VanguardAdapter: {len(candidates)} candidates "
        f"(cycle={latest}, readiness={readiness})"
    )
    return {
        "candidates":  candidates,
        "readiness":   readiness,
        "lane_status": "staged",
    }


def get_latest_date() -> str | None:
    """Return the latest cycle_ts_utc from vanguard_shortlist, or None."""
    try:
        with sqlite_conn(_DB) as con:
            row = con.execute(
                "SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist"
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def count_candidates(trade_date: str | None = None) -> int:
    """Return distinct (symbol, direction) count for the latest cycle."""
    result = get_candidates(trade_date=trade_date)
    return len(result.get("candidates", []))
