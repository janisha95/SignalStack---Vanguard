"""
vanguard_risk_filters.py — V6 Risk Filters.

Active policy truth comes from Risky/config/risk_rules.json via the local
adapter/compiler. Runtime config remains the operational binding layer.

Pipeline per profile:
  1. Load runtime config + Risky JSON
  2. Compile effective risk policy for each profile
  3. Load account state
  4. Enrich shortlist candidates with DB-backed live measurements
  5. For each shortlist candidate → evaluate_candidate (policy_engine)
  6. Run V6 operational health
  7. Write decisions to vanguard_tradeable_portfolio

Output:
  - vanguard_tradeable_portfolio (one row per candidate per active profile)

CLI:
  python3 stages/vanguard_risk_filters.py
  python3 stages/vanguard_risk_filters.py --account my_profile_id
  python3 stages/vanguard_risk_filters.py --dry-run
  python3 stages/vanguard_risk_filters.py --debug BTCUSD
  python3 stages/vanguard_risk_filters.py --cycle-ts 2026-04-06T14:30:00Z
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.db import connect_wal, sqlite_conn
from vanguard.config.runtime_config import (
    get_shadow_db_path,
    load_runtime_config,
)
from vanguard.helpers.clock import now_utc, iso_utc
from vanguard.risk.policy_engine import evaluate_candidate, PolicyDecision
from vanguard.risk.account_state import load_account_state, seed_account_states
from vanguard.risk.risky_policy_adapter import (
    compile_effective_risk_policy,
    load_blackouts_or_none,
    load_risk_rules,
)
from vanguard.context_health import (
    apply_health_to_tradeable_row,
    ensure_v6_context_health_schema,
    evaluate_context_health,
    persist_context_health,
)


def _enrich_with_prices(candidates: list[dict], db_path: str) -> None:
    """
    Mutate candidate dicts in-place: fill entry_price and intraday_atr from
    vanguard_bars_5m (latest close + mean 14-bar true range) for any candidate
    where these are missing or zero.  Falls back to vanguard_bars_1h if 5m
    has no data for a symbol.
    """
    syms = list({str(c.get("symbol") or "").upper() for c in candidates if c.get("symbol")})
    if not syms:
        return

    prices: dict[str, float] = {}   # symbol -> latest close
    atrs:   dict[str, float] = {}   # symbol -> mean(H-L) over last 14 bars

    placeholders = ",".join("?" * len(syms))
    for table in ("vanguard_bars_5m", "vanguard_bars_1h"):
        missing = [s for s in syms if s not in prices]
        if not missing:
            break
        ph = ",".join("?" * len(missing))
        try:
            with sqlite_conn(db_path) as con:
                # Latest close per symbol
                rows = con.execute(
                    f"""
                    SELECT b.symbol, b.close, b.high, b.low
                    FROM {table} b
                    INNER JOIN (
                        SELECT symbol, MAX(bar_ts_utc) AS max_ts
                        FROM {table} WHERE symbol IN ({ph})
                        GROUP BY symbol
                    ) latest ON b.symbol = latest.symbol AND b.bar_ts_utc = latest.max_ts
                    """,
                    missing,
                ).fetchall()
                for sym, close, high, low in rows:
                    sym = str(sym).upper()
                    if sym not in prices and close:
                        prices[sym] = float(close)

                # ATR: mean(H-L) over last 14 bars per symbol
                atr_rows = con.execute(
                    f"""
                    SELECT symbol, AVG(high - low) AS avg_hl
                    FROM (
                        SELECT symbol, high, low,
                               ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY bar_ts_utc DESC) rn
                        FROM {table} WHERE symbol IN ({ph})
                    ) WHERE rn <= 14
                    GROUP BY symbol
                    """,
                    missing,
                ).fetchall()
                for sym, avg_hl in atr_rows:
                    sym = str(sym).upper()
                    if sym not in atrs and avg_hl:
                        atrs[sym] = float(avg_hl)
        except Exception as exc:
            logger.debug("_enrich_with_prices %s: %s", table, exc)

    enriched = 0
    for cand in candidates:
        sym = str(cand.get("symbol") or "").upper()
        if not float(cand.get("entry_price") or 0.0) and sym in prices:
            cand["entry_price"] = prices[sym]
            enriched += 1
        if not float(cand.get("intraday_atr") or 0.0) and sym in atrs:
            cand["intraday_atr"] = atrs[sym]

    logger.info(
        "_enrich_with_prices: enriched %d/%d candidates (prices=%d atrs=%d)",
        enriched, len(candidates), len(prices), len(atrs),
    )


def _enrich_with_live_measurements(
    candidate: dict[str, Any],
    profile_id: str,
    cycle_ts: str,
    db_path: str,
) -> dict[str, Any]:
    """Attach DB-backed V5/context/pair-state truth for the active profile and symbol."""
    out = dict(candidate)
    symbol = str(out.get("symbol") or "").upper()
    asset_class = str(out.get("asset_class") or "").lower()
    if not symbol or not profile_id:
        return out

    try:
        with sqlite_conn(db_path) as con:
            con.row_factory = sqlite3.Row
            # Quote truth always comes from context_quote_latest.
            quote = con.execute(
                """
                SELECT bid, ask, mid, spread_price, spread_pips, spread_rt,
                       quote_ts_utc, received_ts_utc, source, source_status
                FROM vanguard_context_quote_latest
                WHERE profile_id = ? AND symbol = ?
                ORDER BY received_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, symbol),
            ).fetchone()
            if quote is not None:
                q = dict(quote)
                if q.get("bid") is not None:
                    out["bid"] = float(q["bid"])
                if q.get("ask") is not None:
                    out["ask"] = float(q["ask"])
                if not float(out.get("entry_price") or 0.0) and q.get("mid") is not None:
                    out["entry_price"] = float(q["mid"])
                if q.get("spread_pips") is not None:
                    out["quote_spread_pips"] = float(q["spread_pips"])
                if q.get("spread_rt") is not None:
                    out["quote_spread_rt"] = float(q["spread_rt"])
                if q.get("spread_price") is not None:
                    out["quote_spread_price"] = float(q["spread_price"])
                out["quote_ts_utc"] = q.get("quote_ts_utc")
                out["context_quote_received_ts_utc"] = q.get("received_ts_utc")
                out["context_quote_source"] = q.get("source")
                out["context_quote_source_status"] = q.get("source_status")

            # Pair/symbol-state truth remains asset-specific.
            if asset_class == "crypto":
                state = con.execute(
                    """
                    SELECT live_bid, live_ask, live_mid, spread_price, spread_bps,
                           spread_ratio_vs_baseline, trade_allowed, min_qty,
                           max_qty, qty_step, tick_size, open_position_count,
                           same_symbol_open_count, correlated_open_count,
                           source_status, source_error
                    FROM vanguard_crypto_symbol_state
                    WHERE profile_id = ? AND symbol = ?
                    ORDER BY cycle_ts_utc DESC
                    LIMIT 1
                    """,
                    (profile_id, symbol),
                ).fetchone()
                if state is not None:
                    s = dict(state)
                    if s.get("live_bid") is not None:
                        out["bid"] = float(s["live_bid"])
                    if s.get("live_ask") is not None:
                        out["ask"] = float(s["live_ask"])
                    if not float(out.get("entry_price") or 0.0) and s.get("live_mid") is not None:
                        out["entry_price"] = float(s["live_mid"])
                    for src_key, dst_key in [
                        ("spread_bps", "spread_bps"),
                        ("spread_ratio_vs_baseline", "spread_ratio_vs_baseline"),
                        ("trade_allowed", "trade_allowed"),
                        ("min_qty", "min_qty"),
                        ("max_qty", "max_qty"),
                        ("qty_step", "qty_step"),
                        ("tick_size", "tick_size"),
                        ("open_position_count", "open_position_count"),
                        ("same_symbol_open_count", "same_symbol_open_count"),
                        ("correlated_open_count", "correlated_open_count"),
                        ("source_status", "symbol_state_source_status"),
                        ("source_error", "symbol_state_source_error"),
                    ]:
                        if s.get(src_key) is not None:
                            out[dst_key] = s[src_key]
            elif asset_class == "forex":
                state = con.execute(
                    """
                    SELECT spread_pips, spread_ratio_vs_baseline, session_bucket,
                           liquidity_bucket, pair_correlation_bucket,
                           currency_exposure_json, open_exposure_overlap_json,
                           usd_concentration, same_currency_open_count,
                           correlated_open_count, live_mid, quote_ts_utc,
                           context_ts_utc, source, source_status, source_error
                    FROM vanguard_forex_pair_state
                    WHERE profile_id = ? AND symbol = ?
                    ORDER BY cycle_ts_utc DESC
                    LIMIT 1
                    """,
                    (profile_id, symbol),
                ).fetchone()
                if state is not None:
                    s = dict(state)
                    if s.get("spread_pips") is not None:
                        out["spread_pips"] = float(s["spread_pips"])
                    elif out.get("quote_spread_pips") is not None:
                        out["spread_pips"] = float(out["quote_spread_pips"])
                    if not float(out.get("entry_price") or 0.0) and s.get("live_mid") is not None:
                        out["entry_price"] = float(s["live_mid"])
                    for src_key, dst_key in [
                        ("spread_ratio_vs_baseline", "spread_ratio_vs_baseline"),
                        ("session_bucket", "session_bucket"),
                        ("liquidity_bucket", "liquidity_bucket"),
                        ("pair_correlation_bucket", "pair_correlation_bucket"),
                        ("currency_exposure_json", "currency_exposure_json"),
                        ("open_exposure_overlap_json", "open_exposure_overlap_json"),
                        ("usd_concentration", "usd_concentration"),
                        ("same_currency_open_count", "same_currency_open_count"),
                        ("correlated_open_count", "correlated_open_count"),
                        ("quote_ts_utc", "pair_state_quote_ts_utc"),
                        ("context_ts_utc", "pair_state_context_ts_utc"),
                        ("source", "pair_state_source"),
                        ("source_status", "pair_state_source_status"),
                        ("source_error", "pair_state_source_error"),
                    ]:
                        if s.get(src_key) is not None:
                            out[dst_key] = s[src_key]
                elif out.get("quote_spread_pips") is not None:
                    out["spread_pips"] = float(out["quote_spread_pips"])

            specs = con.execute(
                """
                SELECT min_lot, max_lot, lot_step, pip_size, tick_size,
                       tick_value, contract_size, trade_allowed, source_status
                FROM vanguard_context_symbol_specs
                WHERE profile_id = ? AND symbol = ?
                ORDER BY updated_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, symbol),
            ).fetchone()
            if specs is not None:
                s = dict(specs)
                for src_key, dst_key in [
                    ("min_lot", "spec_min_lot"),
                    ("max_lot", "spec_max_lot"),
                    ("lot_step", "spec_lot_step"),
                    ("pip_size", "spec_pip_size"),
                    ("tick_size", "spec_tick_size"),
                    ("tick_value", "spec_tick_value"),
                    ("contract_size", "spec_contract_size"),
                    ("trade_allowed", "spec_trade_allowed"),
                    ("source_status", "spec_source_status"),
                ]:
                    if s.get(src_key) is not None:
                        out[dst_key] = s[src_key]

            acct = con.execute(
                """
                SELECT balance, equity, free_margin, margin_used, margin_level,
                       leverage, source, source_status
                FROM vanguard_context_account_latest
                WHERE profile_id = ?
                ORDER BY received_ts_utc DESC
                LIMIT 1
                """,
                (profile_id,),
            ).fetchone()
            if acct is not None:
                a = dict(acct)
                for src_key, dst_key in [
                    ("balance", "live_account_balance"),
                    ("equity", "live_account_equity"),
                    ("free_margin", "live_free_margin"),
                    ("margin_used", "live_margin_used"),
                    ("margin_level", "live_margin_level"),
                    ("leverage", "live_leverage"),
                    ("source", "live_account_source"),
                    ("source_status", "live_account_source_status"),
                ]:
                    if a.get(src_key) is not None:
                        out[dst_key] = a[src_key]

            tradeability = con.execute(
                """
                SELECT economics_state, v5_action_prelim, predicted_move_pips,
                       spread_pips, cost_pips, after_cost_pips,
                       predicted_move_bps, spread_bps, cost_bps, after_cost_bps,
                       pair_state_ref_json, context_snapshot_ref_json, metrics_json,
                       reasons_json, config_version
                FROM vanguard_v5_tradeability
                WHERE cycle_ts_utc = ? AND profile_id = ? AND symbol = ? AND direction = ?
                ORDER BY evaluated_ts_utc DESC
                LIMIT 1
                """,
                (cycle_ts, profile_id, symbol, str(out.get("direction") or "").upper()),
            ).fetchone()
            if tradeability is not None:
                t = dict(tradeability)
                for key in ["economics_state", "v5_action_prelim", "pair_state_ref_json", "context_snapshot_ref_json", "metrics_json", "reasons_json"]:
                    if t.get(key) is not None:
                        out[key] = t[key]
                for src_key, dst_key in [
                    ("predicted_move_pips", "predicted_move_pips"),
                    ("cost_pips", "cost_pips"),
                    ("after_cost_pips", "after_cost_pips"),
                    ("predicted_move_bps", "predicted_move_bps"),
                    ("cost_bps", "cost_bps"),
                    ("after_cost_bps", "after_cost_bps"),
                    ("spread_pips", "tradeability_spread_pips"),
                    ("spread_bps", "tradeability_spread_bps"),
                ]:
                    if t.get(src_key) is not None:
                        out[dst_key] = t[src_key]
                if t.get("config_version") is not None:
                    out["tradeability_config_version"] = t["config_version"]

            selection = con.execute(
                """
                SELECT selected, selection_state, selection_rank, selection_reason,
                       route_tier, display_label, v5_action, tradeability_ref_json,
                       direction_streak, top_rank_streak, strong_prediction_streak,
                       same_bucket_streak, flip_count_window, live_followthrough_pips,
                       live_followthrough_bps, live_followthrough_state,
                       metrics_json, selection_flags_json, config_version
                FROM vanguard_v5_selection
                WHERE cycle_ts_utc = ? AND profile_id = ? AND symbol = ? AND direction = ?
                ORDER BY selected_ts_utc DESC
                LIMIT 1
                """,
                (cycle_ts, profile_id, symbol, str(out.get("direction") or "").upper()),
            ).fetchone()
            if selection is not None:
                s = dict(selection)
                for key in [
                    "selected",
                    "selection_state",
                    "selection_rank",
                    "selection_reason",
                    "route_tier",
                    "display_label",
                    "v5_action",
                    "tradeability_ref_json",
                    "direction_streak",
                    "top_rank_streak",
                    "strong_prediction_streak",
                    "same_bucket_streak",
                    "flip_count_window",
                    "live_followthrough_pips",
                    "live_followthrough_bps",
                    "live_followthrough_state",
                    "selection_flags_json",
                ]:
                    if s.get(key) is not None:
                        out[key] = s[key]
                if s.get("metrics_json") is not None:
                    out["selection_metrics_json"] = s["metrics_json"]
                if s.get("config_version") is not None:
                    out["selection_config_version"] = s["config_version"]
    except Exception as exc:
        logger.debug("_enrich_with_live_measurements(%s/%s): %s", profile_id, symbol, exc)

    return out

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_risk_filters")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DB_PATH = get_shadow_db_path()


def _parse_cycle_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _build_active_theses(
    profile_id: str,
    cycle_dt: datetime,
    db_path: str,
    policy: dict[str, Any],
    live_truth: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Assemble active thesis truth strictly from broker/open-position state.

    Reentry blocking should only fire when a trade is actually open on the
    broker books. Recent FORWARD_TRACKED / PENDING_FILL journal rows are not
    treated as active theses here.
    """
    theses: dict[tuple[str, str], dict[str, Any]] = {}
    live_open_positions = list(live_truth.get("open_positions") or [])
    for position in live_open_positions:
        symbol = str(position.get("symbol") or "").upper().strip()
        side = str(position.get("side") or position.get("direction") or "").upper().strip()
        if not symbol or not side:
            continue
        age_minutes = None
        opened_at = _parse_cycle_ts(position.get("open_ts_utc"))
        if opened_at is not None:
            age_minutes = max(0.0, (cycle_dt - opened_at).total_seconds() / 60.0)
        theses[(symbol, side)] = {
            "symbol": symbol,
            "side": side,
            "status": "OPEN",
            "source": "broker",
            "age_minutes": age_minutes,
        }

    return list(theses.values())


def load_accounts(
    account_filter: str | None = None,
    db_path: str = _DB_PATH,
) -> list[dict[str, Any]]:
    """
    Compatibility loader for tests and ad-hoc scripts.

    Production V6 binds to runtime config profiles. When a non-default db_path is
    provided, prefer the local DB's account_profiles table so tests remain isolated.
    """
    if db_path != _DB_PATH:
        try:
            with sqlite3.connect(db_path) as con:
                con.row_factory = sqlite3.Row
                if account_filter:
                    rows = con.execute(
                        "SELECT * FROM account_profiles WHERE is_active = 1 AND id = ?",
                        (account_filter,),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT * FROM account_profiles WHERE is_active = 1"
                    ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []

    profiles = [p for p in (load_runtime_config().get("profiles") or []) if _is_active(p)]
    if account_filter:
        profiles = [p for p in profiles if str(p.get("id") or "") == account_filter]
    return list(profiles)


_load_accounts = load_accounts

# ---------------------------------------------------------------------------
# Output table schema
# ---------------------------------------------------------------------------

_CREATE_TRADEABLE = """
CREATE TABLE IF NOT EXISTS vanguard_tradeable_portfolio (
    cycle_ts_utc    TEXT    NOT NULL,
    account_id      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    entry_price     REAL,
    stop_price      REAL,
    tp_price        REAL,
    shares_or_lots  REAL,
    risk_dollars    REAL,
    risk_pct        REAL,
    position_value  REAL,
    intraday_atr    REAL,
    edge_score      REAL,
    rank            INTEGER,
    model_readiness TEXT,
    model_source    TEXT,
    gate_policy     TEXT,
    fallback_in_effect INTEGER DEFAULT 0,
    status          TEXT    DEFAULT 'APPROVED',
    rejection_reason TEXT,
    v6_state        TEXT    DEFAULT 'NOT_EVALUATED',
    v6_ready_to_route INTEGER DEFAULT 0,
    v6_reasons_json TEXT    DEFAULT '[]',
    v6_source_snapshot_json TEXT DEFAULT '{}',
    PRIMARY KEY (cycle_ts_utc, account_id, symbol, direction)
);
CREATE INDEX IF NOT EXISTS idx_vg_tp_account
    ON vanguard_tradeable_portfolio(account_id, cycle_ts_utc);
"""

_CREATE_RISKY_DECISIONS = """
CREATE TABLE IF NOT EXISTS vanguard_risky_decisions (
    cycle_ts_utc TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    asset_class TEXT,
    risk_profile_id TEXT,
    config_version TEXT,
    tradeability_ref_json TEXT NOT NULL DEFAULT '{}',
    selection_ref_json TEXT NOT NULL DEFAULT '{}',
    context_refs_json TEXT NOT NULL DEFAULT '{}',
    check_results_json TEXT NOT NULL DEFAULT '{}',
    sizing_inputs_json TEXT NOT NULL DEFAULT '{}',
    sizing_outputs_json TEXT NOT NULL DEFAULT '{}',
    final_verdict TEXT NOT NULL,
    final_reason_codes_json TEXT NOT NULL DEFAULT '[]',
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, profile_id, symbol, direction)
);
CREATE INDEX IF NOT EXISTS idx_vg_risky_decisions_profile
    ON vanguard_risky_decisions(profile_id, cycle_ts_utc);
"""


def _ensure_output_schema(con: sqlite3.Connection) -> None:
    for stmt in (_CREATE_TRADEABLE + "\n" + _CREATE_RISKY_DECISIONS).strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    existing = {
        row[1]
        for row in con.execute("PRAGMA table_info(vanguard_tradeable_portfolio)").fetchall()
    }
    for column, ddl in [
        ("model_readiness", "TEXT"),
        ("model_source", "TEXT"),
        ("gate_policy", "TEXT"),
        ("fallback_in_effect", "INTEGER DEFAULT 0"),
        ("v6_state", "TEXT DEFAULT 'NOT_EVALUATED'"),
        ("v6_ready_to_route", "INTEGER DEFAULT 0"),
        ("v6_reasons_json", "TEXT DEFAULT '[]'"),
        ("v6_source_snapshot_json", "TEXT DEFAULT '{}'"),
    ]:
        if column not in existing:
            con.execute(f"ALTER TABLE vanguard_tradeable_portfolio ADD COLUMN {column} {ddl}")
    ensure_v6_context_health_schema(con)
    con.commit()


def write_risky_decisions(rows: list[dict], db_path: str = _DB_PATH) -> int:
    if not rows:
        return 0
    with connect_wal(db_path) as con:
        _ensure_output_schema(con)
        con.executemany(
            """
            INSERT OR REPLACE INTO vanguard_risky_decisions
                (cycle_ts_utc, profile_id, symbol, direction, asset_class,
                 risk_profile_id, config_version, tradeability_ref_json,
                 selection_ref_json, context_refs_json, check_results_json,
                 sizing_inputs_json, sizing_outputs_json, final_verdict,
                 final_reason_codes_json, created_at_utc)
            VALUES
                (:cycle_ts_utc, :profile_id, :symbol, :direction, :asset_class,
                 :risk_profile_id, :config_version, :tradeability_ref_json,
                 :selection_ref_json, :context_refs_json, :check_results_json,
                 :sizing_inputs_json, :sizing_outputs_json, :final_verdict,
                 :final_reason_codes_json, :created_at_utc)
            """,
            rows,
        )
        con.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Shortlist loader
# ---------------------------------------------------------------------------

def load_shortlist(cycle_ts: str | None = None, db_path: str = _DB_PATH) -> list[dict]:
    """Load V5 shortlist as list of dicts. Optionally filter to specific cycle_ts."""
    try:
        with sqlite_conn(db_path) as con:
            if cycle_ts:
                rows = con.execute(
                    "SELECT * FROM vanguard_shortlist WHERE cycle_ts_utc = ?",
                    (cycle_ts,),
                ).fetchall()
            else:
                ts_row = con.execute(
                    "SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist"
                ).fetchone()
                if not ts_row or not ts_row[0]:
                    return []
                latest_ts = ts_row[0]
                rows = con.execute(
                    "SELECT * FROM vanguard_shortlist WHERE cycle_ts_utc = ?",
                    (latest_ts,),
                ).fetchall()
            if not rows:
                return []
            cols = [d[0] for d in con.execute(
                "SELECT * FROM vanguard_shortlist LIMIT 0"
            ).description or []]
            # Use row_factory approach
            con.row_factory = sqlite3.Row
            if cycle_ts:
                rows2 = con.execute(
                    "SELECT * FROM vanguard_shortlist WHERE cycle_ts_utc = ?",
                    (cycle_ts,),
                ).fetchall()
            else:
                rows2 = con.execute(
                    "SELECT * FROM vanguard_shortlist WHERE cycle_ts_utc = ?",
                    (latest_ts,),
                ).fetchall()
            return [dict(r) for r in rows2]
    except Exception as exc:
        logger.warning("load_shortlist: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Portfolio writer
# ---------------------------------------------------------------------------

def write_tradeable_portfolio(
    rows: list[dict],
    cycle_ts: str,
    account_id: str,
    db_path: str = _DB_PATH,
) -> int:
    if not rows:
        return 0
    with connect_wal(db_path) as con:
        _ensure_output_schema(con)
        con.executemany(
            """
            INSERT OR REPLACE INTO vanguard_tradeable_portfolio
                (cycle_ts_utc, account_id, symbol, direction, entry_price,
                 stop_price, tp_price, shares_or_lots, risk_dollars, risk_pct,
                 position_value, intraday_atr, edge_score, rank,
                 model_readiness, model_source, gate_policy, fallback_in_effect,
                 status, rejection_reason, v6_state, v6_ready_to_route,
                 v6_reasons_json, v6_source_snapshot_json)
            VALUES
                (:cycle_ts_utc, :account_id, :symbol, :direction, :entry_price,
                 :stop_price, :tp_price, :shares_or_lots, :risk_dollars, :risk_pct,
                 :position_value, :intraday_atr, :edge_score, :rank,
                 :model_readiness, :model_source, :gate_policy, :fallback_in_effect,
                 :status, :rejection_reason, :v6_state, :v6_ready_to_route,
                 :v6_reasons_json, :v6_source_snapshot_json)
            """,
            rows,
        )
        con.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Candidate → row dict converter
# ---------------------------------------------------------------------------

def _decision_to_row(
    dec: PolicyDecision,
    cand: dict,
    profile_id: str,
    cycle_ts: str,
) -> dict:
    """Convert PolicyDecision + candidate into a vanguard_tradeable_portfolio row."""
    is_approved = dec.decision in ("APPROVED", "RESIZED")
    sizing_outputs = dec.sizing_outputs_json or {}
    entry = float(
        sizing_outputs.get("entry_ref_price")
        or cand.get("entry_price")
        or cand.get("entry")
        or 0.0
    )
    atr = float(cand.get("intraday_atr") or cand.get("atr") or 0.0)
    asset_class = str(cand.get("asset_class") or "").lower().strip()
    symbol = str(cand.get("symbol") or "").upper().replace("/", "").strip()
    equity = float(
        cand.get("live_account_equity")
        or cand.get("account_equity")
        or 0.0
    )

    risk_dollars = float(sizing_outputs.get("risk_dollars") or 0.0)
    risk_pct = float(sizing_outputs.get("risk_pct") or 0.0)
    position_value = float(sizing_outputs.get("position_notional") or 0.0)
    if is_approved and dec.approved_qty > 0 and risk_dollars <= 0:
        sl_dist = abs(entry - dec.approved_sl) if dec.approved_sl else 0.0
        if asset_class == "crypto":
            contract_size = float(cand.get("spec_contract_size") or 1.0)
            risk_dollars = dec.approved_qty * sl_dist * contract_size
            position_value = dec.approved_qty * entry * contract_size
        elif asset_class == "forex":
            pip_size = 0.01 if symbol.endswith("JPY") else 0.0001
            pip_value = float(cand.get("pip_value_usd_per_standard_lot") or 10.0)
            sl_pips = sl_dist / pip_size if pip_size > 0 else 0.0
            risk_dollars = dec.approved_qty * sl_pips * pip_value
            contract_size = float(cand.get("spec_contract_size") or 100000.0)
            position_value = dec.approved_qty * entry * contract_size
        else:
            risk_dollars = dec.approved_qty * sl_dist
            position_value = dec.approved_qty * entry
        risk_pct = risk_dollars / equity if equity > 0 else 0.0

    return {
        "cycle_ts_utc": cycle_ts,
        "account_id": profile_id,
        "symbol": str(cand.get("symbol") or ""),
        "direction": dec.approved_side or str(cand.get("direction") or ""),
        "entry_price": entry,
        "stop_price": dec.approved_sl if is_approved else None,
        "tp_price": dec.approved_tp if is_approved else None,
        "shares_or_lots": dec.approved_qty,
        "risk_dollars": round(risk_dollars, 2),
        "risk_pct": round(risk_pct, 6),
        "position_value": round(position_value, 2),
        "intraday_atr": atr,
        "edge_score": float(cand.get("edge_score") or cand.get("strategy_score") or 0.0),
        "rank": int(cand.get("rank") or 0),
        "model_readiness": str(cand.get("model_readiness") or ""),
        "model_source": str(cand.get("model_source") or ""),
        "gate_policy": dec.sizing_method,
        "fallback_in_effect": 0,
        "status": "APPROVED" if is_approved else "REJECTED",
        "rejection_reason": dec.reject_reason,
        "v6_state": "NOT_EVALUATED",
        "v6_ready_to_route": 0,
        "v6_reasons_json": "[]",
        "v6_source_snapshot_json": "{}",
    }


def _load_live_account_truth(profile_id: str, db_path: str) -> dict[str, Any]:
    truth: dict[str, Any] = {}
    try:
        with sqlite_conn(db_path) as con:
            con.row_factory = sqlite3.Row
            try:
                acct = con.execute(
                    """
                    SELECT balance, equity, free_margin, margin_used, margin_level,
                           leverage, source, source_status
                    FROM vanguard_context_account_latest
                    WHERE profile_id = ?
                    ORDER BY received_ts_utc DESC
                    LIMIT 1
                    """,
                    (profile_id,),
                ).fetchone()
                if acct is not None:
                    truth.update(dict(acct))
            except Exception as exc:
                logger.debug("_load_live_account_truth account(%s): %s", profile_id, exc)

            try:
                rows = con.execute(
                    """
                    SELECT symbol, direction, lots, entry_price, open_time_utc, floating_pnl
                    FROM (
                        SELECT symbol,
                               direction,
                               lots,
                               entry_price,
                               open_time_utc,
                               floating_pnl,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ticket
                                   ORDER BY received_ts_utc DESC
                               ) AS rn
                        FROM vanguard_context_positions_latest
                        WHERE profile_id = ? AND source_status = 'OK'
                    )
                    WHERE rn = 1
                    """,
                    (profile_id,),
                ).fetchall()
                truth["open_positions"] = [
                    {
                        "symbol": r["symbol"],
                        "side": r["direction"],
                        "qty": float(r["lots"] or 0.0),
                        "entry": float(r["entry_price"] or 0.0),
                        "open_ts_utc": r["open_time_utc"],
                        "unrealized_pnl": float(r["floating_pnl"] or 0.0),
                    }
                    for r in rows
                ]
            except Exception as exc:
                logger.debug("_load_live_account_truth positions(%s): %s", profile_id, exc)
    except Exception as exc:
        logger.debug("_load_live_account_truth(%s): %s", profile_id, exc)
    return truth


def _merge_account_state(base_state: dict[str, Any], live_truth: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_state)
    if live_truth.get("equity") is not None:
        merged["equity"] = float(live_truth["equity"] or 0.0)
    if live_truth.get("free_margin") is not None:
        merged["free_margin"] = float(live_truth["free_margin"] or 0.0)
    if live_truth.get("margin_used") is not None:
        merged["margin_used"] = float(live_truth["margin_used"] or 0.0)
    if live_truth.get("margin_level") is not None:
        merged["margin_level"] = float(live_truth["margin_level"] or 0.0)
    if live_truth.get("leverage") is not None:
        merged["leverage"] = int(live_truth["leverage"] or 0)
    if "open_positions" in live_truth:
        merged["open_positions"] = list(live_truth["open_positions"])
    return merged


def _decision_to_risky_row(
    dec: PolicyDecision,
    cand: dict[str, Any],
    profile_id: str,
    cycle_ts: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    reason_codes = [dec.reject_reason] if dec.reject_reason else []
    context_refs = {
        "quote_source": cand.get("context_quote_source"),
        "quote_ts_utc": cand.get("quote_ts_utc"),
        "quote_source_status": cand.get("context_quote_source_status"),
        "pair_state_source": cand.get("pair_state_source") or cand.get("symbol_state_source_status"),
        "account_source": cand.get("live_account_source"),
        "account_source_status": cand.get("live_account_source_status"),
        "spec_source_status": cand.get("spec_source_status"),
    }
    tradeability_ref = {
        "economics_state": cand.get("economics_state"),
        "v5_action_prelim": cand.get("v5_action_prelim"),
        "pair_state_ref_json": cand.get("pair_state_ref_json"),
        "context_snapshot_ref_json": cand.get("context_snapshot_ref_json"),
        "config_version": cand.get("tradeability_config_version"),
    }
    selection_ref = {
        "selected": cand.get("selected"),
        "selection_state": cand.get("selection_state"),
        "selection_rank": cand.get("selection_rank"),
        "selection_reason": cand.get("selection_reason"),
        "route_tier": cand.get("route_tier"),
        "display_label": cand.get("display_label"),
        "tradeability_ref_json": cand.get("tradeability_ref_json"),
        "config_version": cand.get("selection_config_version"),
    }
    return {
        "cycle_ts_utc": cycle_ts,
        "profile_id": profile_id,
        "symbol": str(cand.get("symbol") or ""),
        "direction": str(cand.get("direction") or ""),
        "asset_class": str(cand.get("asset_class") or ""),
        "risk_profile_id": str(policy.get("_risk_profile_id") or ""),
        "config_version": str(policy.get("_risk_config_version") or ""),
        "tradeability_ref_json": json.dumps(tradeability_ref, separators=(",", ":"), default=str),
        "selection_ref_json": json.dumps(selection_ref, separators=(",", ":"), default=str),
        "context_refs_json": json.dumps(context_refs, separators=(",", ":"), default=str),
        "check_results_json": json.dumps(dec.checks_json or {}, separators=(",", ":"), default=str),
        "sizing_inputs_json": json.dumps(dec.sizing_inputs_json or {}, separators=(",", ":"), default=str),
        "sizing_outputs_json": json.dumps(dec.sizing_outputs_json or {}, separators=(",", ":"), default=str),
        "final_verdict": dec.decision,
        "final_reason_codes_json": json.dumps(reason_codes, separators=(",", ":"), default=str),
        "created_at_utc": iso_utc(now_utc()),
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(
    account_filter: str | None = None,
    dry_run: bool = False,
    debug_symbol: str | None = None,
    cycle_ts: str | None = None,
    db_path: str = _DB_PATH,
) -> dict:
    """
    V6 main entry point.

    Loads runtime bindings + Risky JSON → compiles policies → evaluates all
    shortlist candidates against each active profile → writes
    vanguard_tradeable_portfolio.
    """
    t0 = time.time()
    cycle_ts = cycle_ts or iso_utc(now_utc())

    # Parse cycle_ts to datetime for engine
    try:
        cycle_dt = datetime.fromisoformat(cycle_ts.replace("Z", "+00:00"))
    except ValueError:
        cycle_dt = datetime.now(timezone.utc)
    if cycle_dt.tzinfo is None:
        cycle_dt = cycle_dt.replace(tzinfo=timezone.utc)

    # Load active bindings + risk rules
    config = load_runtime_config()
    try:
        risk_rules = load_risk_rules()
    except Exception as exc:
        logger.error("V6 risk rule load failed: %s", exc)
        return {"status": "risk_rule_error", "error": str(exc)}

    # Ensure account state table seeded
    try:
        seed_account_states(config, db_path)
    except Exception as exc:
        logger.debug("seed_account_states: %s", exc)
    try:
        with connect_wal(db_path) as schema_con:
            _ensure_output_schema(schema_con)
    except Exception as exc:
        logger.warning("V6: output schema check skipped/failed: %s", exc)

    # Filter profiles
    profiles = [p for p in (config.get("profiles") or []) if _is_active(p)]
    if account_filter:
        profiles = [p for p in profiles if str(p.get("id") or "") == account_filter]
    if not profiles:
        logger.warning("V6: no active profiles found (filter=%s)", account_filter)
        return {"status": "no_accounts"}

    logger.info(
        "V6 Risk Filters — cycle=%s config_version=%s profiles=%s",
        cycle_ts,
        config.get("config_version", "?"),
        [p["id"] for p in profiles],
    )

    # Load shortlist
    shortlist = load_shortlist(cycle_ts, db_path)
    if not shortlist:
        logger.warning("V6: shortlist empty for cycle_ts=%s", cycle_ts)
        return {"status": "no_shortlist", "rows": 0}

    # Deduplicate: best-scored row per (symbol, direction)
    seen: dict[tuple[str, str], dict] = {}
    for cand in shortlist:
        key = (str(cand.get("symbol") or "").upper(), str(cand.get("direction") or "").upper())
        score = float(cand.get("strategy_score") or 0.0)
        if key not in seen or score > float(seen[key].get("strategy_score") or 0.0):
            seen[key] = cand
    shortlist = list(seen.values())
    logger.info("V6: shortlist=%d unique (symbol,direction) candidates", len(shortlist))

    # Enrich candidates with live prices + ATR from bars tables
    _enrich_with_prices(shortlist, db_path)

    total_rows = 0
    total_approved = 0

    for profile in profiles:
        profile_id = str(profile.get("id") or "")
        account_size = float(profile.get("account_size") or 0.0)

        # Compile active policy from Risky JSON
        try:
            policy = compile_effective_risk_policy(profile, config, risk_rules)
            blkouts = load_blackouts_or_none(profile, config, risk_rules, cycle_dt)
        except Exception as exc:
            logger.error("V6: profile %s risk policy compilation failed: %s", profile_id, exc)
            continue

        # Load account state
        state = load_account_state(profile_id, db_path, cycle_dt, seed_equity=account_size)
        state["equity"] = state["equity"] or account_size  # ensure non-zero
        live_truth = _load_live_account_truth(profile_id, db_path)
        state = _merge_account_state(state, live_truth)
        state["active_theses"] = _build_active_theses(
            profile_id=profile_id,
            cycle_dt=cycle_dt,
            db_path=db_path,
            policy=policy,
            live_truth=live_truth,
        )

        rows: list[dict] = []
        risky_rows: list[dict] = []
        approved_count = 0
        health_con: sqlite3.Connection | None = None
        try:
            health_con = connect_wal(db_path)
            ensure_v6_context_health_schema(health_con)
        except Exception as exc:
            logger.warning("V6: context health connection unavailable for %s: %s", profile_id, exc)
            health_con = None

        for cand in shortlist:
            sym = str(cand.get("symbol") or "").upper()
            direction = str(cand.get("direction") or "").upper()
            candidate = _enrich_with_live_measurements(cand, profile_id, cycle_ts, db_path)

            dec = evaluate_candidate(
                candidate=candidate,
                profile=profile,
                policy=policy,
                overrides=None,
                blackouts=blkouts,
                account_state=state,
                cycle_ts_utc=cycle_dt,
            )
            risky_rows.append(_decision_to_risky_row(dec, candidate, profile_id, cycle_ts, policy))

            row = _decision_to_row(dec, candidate, profile_id, cycle_ts)
            final_status = row["status"]
            if dec.decision in ("APPROVED", "RESIZED"):
                if health_con is None:
                    health = {
                        "cycle_ts_utc": cycle_ts,
                        "profile_id": profile_id,
                        "symbol": sym,
                        "direction": direction,
                        "health_ts_utc": iso_utc(now_utc()),
                        "context_source_id": str(profile.get("context_source_id") or ""),
                        "source": "",
                        "broker": "",
                        "mode": str(profile.get("context_health_mode") or profile.get("execution_mode") or ""),
                        "v6_state": "ERROR",
                        "ready_to_route": 0,
                        "reasons": [{
                            "severity": "ERROR",
                            "code": "CONTEXT_HEALTH_CONNECTION_UNAVAILABLE",
                            "component": "v6_health",
                            "field": "db_connection",
                            "observed": db_path,
                        }],
                        "source_snapshot": {},
                    }
                else:
                    health = evaluate_context_health(
                        health_con,
                        config=config,
                        profile=profile,
                        candidate={**candidate, "symbol": sym, "direction": direction},
                        cycle_ts_utc=cycle_ts,
                    )
                    if not dry_run:
                        persist_context_health(health_con, health)
                row = apply_health_to_tradeable_row(row, health)
                final_status = row["status"]
            rows.append(row)

            if dec.decision in ("APPROVED", "RESIZED") and final_status == "APPROVED":
                approved_count += 1
                execution_mode = str(profile.get("execution_mode") or "").strip().lower()
                if execution_mode not in {"manual", "off"}:
                    # Only simulated/live execution should consume same-cycle
                    # position capacity. Manual mode must reflect actual broker
                    # positions from context, not forward-tracked candidates.
                    pos = {
                        "symbol": sym,
                        "side": direction,
                        "qty": dec.approved_qty,
                        "entry": float(candidate.get("entry_price") or 0.0),
                        "open_ts_utc": cycle_ts,
                        "unrealized_pnl": 0.0,
                    }
                    state.setdefault("open_positions", []).append(pos)
                state.setdefault("active_theses", []).append(
                    {
                        "symbol": sym,
                        "side": direction,
                        "status": "APPROVED",
                        "source": "cycle",
                        "age_minutes": 0.0,
                    }
                )

            if debug_symbol and sym == debug_symbol.upper():
                logger.info(
                    "[DEBUG %s/%s] decision=%s reason=%s notes=%s",
                    profile_id, sym, dec.decision, dec.reject_reason, dec.notes,
                )

        if health_con is not None:
            health_con.close()

        logger.info(
            "[%s] %d candidates → %d APPROVED / %d non-approved",
            profile_id, len(rows), approved_count, len(rows) - approved_count,
        )

        if not dry_run:
            write_risky_decisions(risky_rows, db_path)
            written = write_tradeable_portfolio(rows, cycle_ts, profile_id, db_path)
            total_rows += written
            total_approved += approved_count
        else:
            total_rows += len(rows)
            total_approved += approved_count
            _print_profile_summary(profile_id, rows)

    elapsed = time.time() - t0
    logger.info("V6 complete — %d total rows (%d approved), elapsed=%.2fs", total_rows, total_approved, elapsed)

    return {
        "status": "ok",
        "rows": total_rows,
        "approved": total_approved,
        "elapsed": elapsed,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_active(profile: dict) -> bool:
    v = profile.get("is_active", 0)
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _print_profile_summary(profile_id: str, rows: list[dict]) -> None:
    approved = [r for r in rows if r["status"] == "APPROVED"]
    non_approved = [r for r in rows if r["status"] != "APPROVED"]
    print(f"\n{'─'*60}")
    print(f"  [{profile_id}]  APPROVED:{len(approved)}  NON_APPROVED:{len(non_approved)}")
    for r in approved:
        print(
            f"    ✓ {r['direction']:5s} {r['symbol']:8s}  "
            f"qty={r['shares_or_lots']}  "
            f"stop={r['stop_price']}  tp={r['tp_price']}  "
            f"risk=${r['risk_dollars']:.2f}"
        )
    if non_approved:
        reasons: dict[str, int] = {}
        for r in non_approved:
            k = f"{r.get('status') or 'unknown'}:{r.get('rejection_reason') or 'unknown'}"
            reasons[k] = reasons.get(k, 0) + 1
        for reason, count in sorted(reasons.items()):
            print(f"    ✗ {reason}: {count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="V6 Vanguard Risk Filters (Phase 2b)")
    p.add_argument("--account", default=None, help="Run only this profile ID")
    p.add_argument("--dry-run", action="store_true", help="Print results, do not write to DB")
    p.add_argument("--debug", default=None, metavar="SYMBOL", help="Trace one symbol")
    p.add_argument("--cycle-ts", default=None, dest="cycle_ts",
                   help="Force cycle timestamp (ISO UTC)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run(
        account_filter=args.account,
        dry_run=args.dry_run,
        debug_symbol=args.debug,
        cycle_ts=args.cycle_ts,
    )
    sys.exit(0 if result.get("status") in ("ok", "no_shortlist", "no_accounts") else 1)
