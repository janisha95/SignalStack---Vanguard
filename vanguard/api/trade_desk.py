"""
trade_desk.py — Trade picks by tier + execution via SignalStack webhook.

Reuses tier logic from scripts/execute_daily_picks.py.
Logs all execution attempts to execution_log table in vanguard_universe.db.

Location: ~/SS/Vanguard/vanguard/api/trade_desk.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vanguard.helpers.clock import now_et

logger = logging.getLogger(__name__)

VANGUARD_ROOT = Path(__file__).resolve().parent.parent.parent
VANGUARD_DB   = VANGUARD_ROOT / "data" / "vanguard_universe.db"
ML_THRESHOLD_CONFIG = VANGUARD_ROOT / "config" / "vanguard_ml_thresholds.json"

CREATE_EXECUTION_LOG = """
CREATE TABLE IF NOT EXISTS execution_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at          TEXT NOT NULL DEFAULT (datetime('now')),
    symbol               TEXT NOT NULL,
    direction            TEXT NOT NULL,
    tier                 TEXT NOT NULL,
    shares               INTEGER NOT NULL,
    entry_price          REAL,
    stop_loss            REAL,
    take_profit          REAL,
    order_type           TEXT,
    limit_buffer         REAL,
    status               TEXT DEFAULT 'SUBMITTED',
    signalstack_response TEXT,
    account              TEXT,
    source_scores        TEXT,
    tags                 TEXT,
    notes                TEXT,
    exit_price           REAL,
    exit_date            TEXT,
    pnl_dollars          REAL,
    pnl_pct              REAL,
    outcome              TEXT,
    days_held            INTEGER,
    execution_fee        REAL DEFAULT 0,
    fill_price           REAL,
    filled_at            TEXT,
    created_at           TEXT DEFAULT (datetime('now'))
);
"""

# Columns added after initial release — added via ALTER TABLE for live DBs
_NEW_COLUMNS: list[tuple[str, str]] = [
    ("tags",    "TEXT"),
    ("notes",   "TEXT"),
    ("stop_loss", "REAL"),
    ("take_profit", "REAL"),
    ("order_type", "TEXT"),
    ("limit_buffer", "REAL"),
    ("exit_price",   "REAL"),
    ("exit_date",    "TEXT"),
    ("pnl_dollars",  "REAL"),
    ("pnl_pct",      "REAL"),
    ("outcome",      "TEXT"),
    ("days_held",    "INTEGER"),
    ("execution_fee","REAL DEFAULT 0"),
    ("fill_price",   "REAL"),
    ("filled_at",    "TEXT"),
]

TIER_LABELS: dict[str, tuple[str, str]] = {
    "tier_dual":            ("Dual Filter (S1)",        "s1"),
    "tier_nn":              ("NN Only (S1)",             "s1"),
    "tier_rf":              ("RF + Convergence (S1)",    "s1"),
    "tier_scorer_long":     ("Scorer Long (S1)",         "s1"),
    "tier_s1_short":        ("Scorer Short (S1)",        "s1"),
    "tier_meridian_long":   ("Meridian Longs",           "meridian"),
    "tier_meridian_short":  ("Meridian Shorts",          "meridian"),
}

DEFAULT_RISK_PROFILE: dict[str, Any] = {
    "id": "default_demo",
    "name": "Default Demo",
    "prop_firm": "ttp",
    "account_type": "swing",
    "account_size": 50_000,
    "daily_loss_limit": 1_000.0,
    "max_drawdown": 2_000.0,
    "max_positions": 5,
    "must_close_eod": 0,
    "is_active": 1,
    "max_single_position_pct": 0.10,
    "max_batch_exposure_pct": 0.50,
    "dll_headroom_pct": 0.70,
    "dd_headroom_pct": 0.80,
    "max_per_sector": 3,
    "block_duplicate_symbols": 1,
    "environment": "demo",
    "instrument_scope": "us_equities",
    "holding_style": "swing",
}

_ML_THRESHOLDS_CACHE: dict[str, dict[str, float]] | None = None


def _load_ml_thresholds() -> dict[str, dict[str, float]]:
    global _ML_THRESHOLDS_CACHE
    if _ML_THRESHOLDS_CACHE is None:
        try:
            with open(ML_THRESHOLD_CONFIG) as fh:
                _ML_THRESHOLDS_CACHE = json.load(fh)
        except Exception as exc:
            logger.warning("Could not load ML thresholds: %s", exc)
            _ML_THRESHOLDS_CACHE = {}
    return _ML_THRESHOLDS_CACHE


def init_table() -> None:
    """Create the execution_log table if it doesn't exist, then migrate columns."""
    with sqlite3.connect(str(VANGUARD_DB)) as con:
        con.execute(CREATE_EXECUTION_LOG)
        con.commit()
        # Add any new columns that may be missing from an existing table
        existing = {row[1] for row in con.execute("PRAGMA table_info(execution_log)").fetchall()}
        for col_name, col_type in _NEW_COLUMNS:
            if col_name not in existing:
                con.execute(f"ALTER TABLE execution_log ADD COLUMN {col_name} {col_type}")
        # Backfill estimated entry-side fees for older rows created before execution_fee existed.
        rows = con.execute(
            "SELECT id, shares, execution_fee FROM execution_log WHERE execution_fee IS NULL OR execution_fee = 0"
        ).fetchall()
        for row_id, shares, current_fee in rows:
            est = estimate_ttp_fee(int(shares or 0))
            if est > 0 and (current_fee is None or float(current_fee) == 0):
                con.execute(
                    "UPDATE execution_log SET execution_fee = ? WHERE id = ?",
                    (est, row_id),
                )
        con.commit()


# ── Tier picks ────────────────────────────────────────────────────────────────

def _get_s1_tiers(trade_date: str) -> dict[str, list[dict]]:
    """Load S1 scorer + convergence and apply 7-tier logic."""
    # Import lazily to avoid circular imports
    from vanguard.api.adapters.s1_adapter import get_candidates as s1_get
    rows = s1_get(run_date=trade_date)

    tier_dual: list[dict] = []
    tier_nn: list[dict] = []
    tier_rf: list[dict] = []
    tier_scorer_long: list[dict] = []
    tier_s1_short: list[dict] = []
    assigned: set[str] = set()

    # Priority: dual > nn > rf > scorer_long (same logic as execute_daily_picks.py)
    for row in rows:
        ticker = row["symbol"]
        d      = row["side"]
        p      = row.get("p_tp", 0.0)
        nn     = row.get("nn_p_tp", 0.0)
        conv   = row.get("convergence_score", 0.0)
        sp     = row.get("scorer_prob", 0.0)

        if d == "LONG":
            if p >= 0.60 and nn >= 0.75 and ticker not in assigned:
                tier_dual.append(row)
                assigned.add(ticker)
            elif nn >= 0.90 and ticker not in assigned:
                tier_nn.append(row)
                assigned.add(ticker)
            elif p > 0.60 and conv >= 0.80 and ticker not in assigned:
                tier_rf.append(row)
                assigned.add(ticker)
            elif sp >= 0.55 and ticker not in assigned:
                tier_scorer_long.append(row)
                assigned.add(ticker)
        else:
            if sp >= 0.55:
                tier_s1_short.append(row)

    tier_scorer_long.sort(key=lambda x: -x.get("scorer_prob", 0))
    tier_scorer_long = tier_scorer_long[:10]
    tier_s1_short.sort(key=lambda x: -x.get("scorer_prob", 0))

    return {
        "tier_dual":        tier_dual,
        "tier_nn":          tier_nn,
        "tier_rf":          tier_rf,
        "tier_scorer_long": tier_scorer_long,
        "tier_s1_short":    tier_s1_short,
    }


def _get_meridian_tiers(trade_date: str) -> dict[str, list[dict]]:
    """Load Meridian shortlist and keep the already-filtered top 5 per side."""
    from vanguard.api.adapters.meridian_adapter import get_candidates as mer_get
    rows = mer_get(trade_date=trade_date)

    tier_meridian_long: list[dict] = []
    tier_meridian_short: list[dict] = []

    for row in rows:
        d   = row["side"]
        if d == "LONG":
            tier_meridian_long.append(row)
        elif d == "SHORT":
            tier_meridian_short.append(row)

    tier_meridian_long.sort(
        key=lambda x: -float(
            x.get("tcn_long_score")
            if x.get("tcn_long_score") is not None
            else x.get("final_score", 0.0) or 0.0
        )
    )
    tier_meridian_short.sort(
        key=lambda x: -float(
            x.get("tcn_short_score")
            if x.get("tcn_short_score") is not None
            else x.get("final_score", 0.0) or 0.0
        )
    )

    return {
        "tier_meridian_long":  tier_meridian_long[:5],
        "tier_meridian_short": tier_meridian_short[:5],
    }


def get_picks_today(trade_date: str | None = None) -> dict[str, Any]:
    """
    Return all 7 tiers with today's picks.

    Returns
    -------
    {
        "date": "YYYY-MM-DD",
        "tiers": [{"tier": str, "label": str, "source": str, "picks": [...]}],
        "total_picks": int,
    }
    """
    from datetime import date as date_type
    date_str = trade_date or str(date_type.today())

    all_tiers: dict[str, list[dict]] = {}
    all_tiers.update(_get_s1_tiers(date_str))
    all_tiers.update(_get_meridian_tiers(date_str))

    canonical_order = [
        "tier_dual", "tier_nn", "tier_rf", "tier_scorer_long",
        "tier_s1_short", "tier_meridian_long", "tier_meridian_short",
    ]

    tiers_out: list[dict] = []
    total = 0
    for tier_name in canonical_order:
        picks_raw = all_tiers.get(tier_name, [])
        label, source = TIER_LABELS.get(tier_name, (tier_name, "unknown"))
        picks_formatted = []
        for p in picks_raw:
            scores: dict[str, Any] = {}
            for k in (
                "p_tp",
                "nn_p_tp",
                "scorer_prob",
                "convergence_score",
                "tcn_long_score",
                "tcn_short_score",
                "tcn_score",
                "final_score",
            ):
                v = p.get(k)
                if v is not None:
                    scores[k] = round(float(v), 4)
            picks_formatted.append({
                "symbol":    p["symbol"],
                "direction": p["side"],
                "shares":    100,
                "price":     p.get("price", 0.0),
                "scores":    scores,
            })
        tiers_out.append({
            "tier":   tier_name,
            "label":  label,
            "source": source,
            "picks":  picks_formatted,
        })
        total += len(picks_formatted)

    return {"date": date_str, "tiers": tiers_out, "total_picks": total}


# ── Execution ─────────────────────────────────────────────────────────────────

def _get_webhook_url() -> str:
    """Load SIGNALSTACK_WEBHOOK_URL from env or .env file."""
    url = os.environ.get("SIGNALSTACK_WEBHOOK_URL", "")
    if not url:
        env_file = VANGUARD_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("SIGNALSTACK_WEBHOOK_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
    return url


def _resolve_execution_bridge(profile: dict[str, Any]) -> str:
    return str(profile.get("execution_bridge") or "signalstack").strip().lower()


def _log_execution(
    symbol: str,
    direction: str,
    tier: str,
    shares: int,
    entry_price: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    order_type: str | None,
    limit_buffer: float | None,
    status: str,
    response_json: dict,
    account: str | None = None,
    source_scores: dict | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> int | None:
    """Write one row to execution_log. Returns the new row id."""
    execution_fee = estimate_ttp_fee(shares)
    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            cur = con.execute(
                """
                INSERT INTO execution_log
                    (symbol, direction, tier, shares, entry_price, stop_loss, take_profit,
                     order_type, limit_buffer, status, signalstack_response, account, source_scores, tags, notes,
                     execution_fee)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol, direction, tier, shares, entry_price, stop_loss, take_profit,
                    order_type, limit_buffer, status,
                    json.dumps(response_json),
                    account,
                    json.dumps(source_scores) if source_scores else None,
                    json.dumps(tags) if tags else None,
                    notes,
                    execution_fee,
                ),
            )
            con.commit()
            return cur.lastrowid
    except Exception as exc:
        logger.error(f"Failed to log execution: {exc}")
        return None


def _parse_iso_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def _normalize_profile(profile_id: str | None) -> dict[str, Any]:
    from vanguard.api import accounts as acc_module

    if profile_id:
        profile = acc_module.get_profile(profile_id)
        if profile:
            merged = dict(DEFAULT_RISK_PROFILE)
            merged.update(profile)
            return merged
    return dict(DEFAULT_RISK_PROFILE)


def _profile_audit_metadata(profile: dict[str, Any], rules_fired: list[str] | None = None) -> dict[str, Any]:
    return {
        "profile_id": str(profile.get("id") or DEFAULT_RISK_PROFILE["id"]),
        "profile_name": str(profile.get("name") or DEFAULT_RISK_PROFILE["name"]),
        "environment": str(profile.get("environment") or "unknown"),
        "instrument_scope": str(profile.get("instrument_scope") or "all"),
        "holding_style": str(profile.get("holding_style") or "unknown"),
        "rules_fired": list(rules_fired or []),
    }


def _load_execution_rows_for_account(account_id: str | None) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if account_id:
        where = "WHERE COALESCE(account, '') IN (?, '')"
        params.append(account_id)
    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"SELECT * FROM execution_log {where} ORDER BY executed_at DESC",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("load_execution_rows_for_account error: %s", exc)
        return []


def _get_live_prices_batch(symbols: list[str]) -> dict[str, float]:
    try:
        import yfinance as yf
    except ImportError:
        return {}

    if not symbols:
        return {}

    prices: dict[str, float] = {}
    try:
        data = yf.download(symbols, period="1d", group_by="ticker", progress=False, auto_adjust=False)
        for sym in symbols:
            try:
                close = data["Close"].iloc[-1] if len(symbols) == 1 else data[sym]["Close"].iloc[-1]
                if close and float(close) > 0:
                    prices[sym] = round(float(close), 2)
            except Exception:
                continue
    except Exception as exc:
        logger.warning("risk gate live-price batch failed: %s", exc)
    return prices


def _unrealized_pnl(row: dict[str, Any], live_price: float | None) -> float:
    if live_price is None:
        return 0.0
    entry = float(row.get("fill_price") or row.get("entry_price") or 0.0)
    shares = int(row.get("shares") or 0)
    if entry <= 0 or shares <= 0:
        return 0.0
    direction = str(row.get("direction") or "LONG").upper()
    fee = float(row.get("execution_fee") or 0.0)
    pnl_per = (live_price - entry) if direction == "LONG" else (entry - live_price)
    return round((pnl_per * shares) - fee, 2)


def _compute_account_pnl(rows: list[dict[str, Any]]) -> tuple[float, float]:
    today = now_et().date().isoformat()
    open_rows = [r for r in rows if not r.get("outcome") or str(r.get("outcome")).upper() == "OPEN"]
    live_prices = _get_live_prices_batch(sorted({str(r["symbol"]).upper() for r in open_rows if r.get("symbol")}))

    daily_pnl = 0.0
    total_pnl = 0.0

    for row in rows:
        outcome = str(row.get("outcome") or "").upper()
        if outcome and outcome != "OPEN":
            pnl = float(row.get("pnl_dollars") or 0.0)
            total_pnl += pnl
            row_date = _parse_iso_date(row.get("exit_date") or row.get("executed_at"))
            if row_date == today:
                daily_pnl += pnl

    for row in open_rows:
        live = live_prices.get(str(row["symbol"]).upper())
        unrealized = _unrealized_pnl(row, live)
        total_pnl += unrealized
        daily_pnl += unrealized

    return round(daily_pnl, 2), round(total_pnl, 2)


def _compute_weekly_pnl(rows: list[dict[str, Any]]) -> float:
    today = now_et().date()
    week_start = today - timedelta(days=today.weekday())
    open_rows = [r for r in rows if not r.get("outcome") or str(r.get("outcome")).upper() == "OPEN"]
    live_prices = _get_live_prices_batch(sorted({str(r["symbol"]).upper() for r in open_rows if r.get("symbol")}))

    weekly_pnl = 0.0
    for row in rows:
        outcome = str(row.get("outcome") or "").upper()
        if outcome and outcome != "OPEN":
            pnl = float(row.get("pnl_dollars") or 0.0)
            row_date = _parse_iso_date(row.get("exit_date") or row.get("executed_at"))
            if row_date:
                dt = date_type.fromisoformat(row_date)
                if dt >= week_start:
                    weekly_pnl += pnl

    for row in open_rows:
        live = live_prices.get(str(row["symbol"]).upper())
        weekly_pnl += _unrealized_pnl(row, live)

    return round(weekly_pnl, 2)


def _get_trades_today(rows: list[dict[str, Any]]) -> int:
    today = now_et().date().isoformat()
    return sum(1 for row in rows if _parse_iso_date(row.get("executed_at")) == today)


def _get_volume_today(rows: list[dict[str, Any]]) -> int:
    today = now_et().date().isoformat()
    return sum(
        int(row.get("shares") or 0)
        for row in rows
        if _parse_iso_date(row.get("executed_at")) == today
    )


def _sector_lookup() -> dict[str, str]:
    from vanguard.api.adapters import meridian_adapter, s1_adapter

    lookup: dict[str, str] = {}
    try:
        mer_date = meridian_adapter.get_latest_date()
        if mer_date:
            for row in meridian_adapter.get_candidates(trade_date=mer_date):
                sector = row.get("sector")
                if sector:
                    lookup[str(row["symbol"]).upper()] = str(sector)
    except Exception as exc:
        logger.warning("risk gate meridian sector lookup failed: %s", exc)
    try:
        s1_date = s1_adapter.get_latest_date()
        if s1_date:
            for row in s1_adapter.get_candidates(run_date=s1_date):
                sector = row.get("sector")
                if sector and str(row["symbol"]).upper() not in lookup:
                    lookup[str(row["symbol"]).upper()] = str(sector)
    except Exception as exc:
        logger.warning("risk gate s1 sector lookup failed: %s", exc)
    return lookup


def _risk_for_trade(trade: dict[str, Any]) -> float:
    entry = float(trade.get("entry_price") or 0.0)
    stop = float(trade.get("stop_loss") or 0.0)
    shares = int(trade.get("shares") or 0)
    if entry <= 0 or stop <= 0 or shares <= 0:
        return 0.0
    return abs(entry - stop) * shares


def _normalize_asset_class(trade: dict[str, Any]) -> str | None:
    asset_class = trade.get("asset_class")
    if asset_class is None:
        return None
    text = str(asset_class).strip().lower()
    return text or None


def _get_ml_threshold(asset_class: str | None, direction: str | None) -> float:
    thresholds = _load_ml_thresholds()
    asset_key = str(asset_class or "equity").lower()
    direction_key = "long" if str(direction or "LONG").upper() == "LONG" else "short"
    lane = thresholds.get(asset_key) or thresholds.get("equity") or {}
    return float(lane.get(direction_key, 0.50))


def _apply_model_readiness_gate(trade: dict[str, Any]) -> dict[str, Any]:
    readiness = trade.get("model_readiness")
    model_source = trade.get("model_source")
    if readiness is None and model_source is None and trade.get("ml_prob") is None:
        return {
            "applied": False,
            "passed": True,
            "gate_policy": None,
            "fallback_in_effect": False,
            "model_readiness": None,
            "model_source": None,
            "reason": None,
        }

    readiness_str = str(readiness or "not_trained")
    ml_prob = float(trade.get("ml_prob") or 0.0)
    threshold = _get_ml_threshold(_normalize_asset_class(trade), trade.get("direction"))
    allow_execution = False

    if readiness_str == "live_native":
        allow_execution = True
    elif readiness_str == "live_fallback":
        threshold += 0.05
        allow_execution = True
    elif readiness_str == "validated_shadow":
        allow_execution = False
    else:
        threshold = 1.0
        allow_execution = False

    fallback_in_effect = readiness_str == "live_fallback" or model_source == "fallback_equity"
    return {
        "applied": True,
        "passed": allow_execution and (ml_prob >= threshold),
        "gate_policy": f"readiness_gate_{readiness_str}",
        "fallback_in_effect": fallback_in_effect,
        "model_readiness": readiness_str,
        "model_source": model_source,
        "reason": f"readiness={readiness_str}, threshold={threshold:.3f}, prob={ml_prob:.3f}",
    }


def _evaluate_trade_batch(
    trades: list[dict[str, Any]],
    profile_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    profile = _normalize_profile(profile_id)
    account_key = str(profile.get("id") or DEFAULT_RISK_PROFILE["id"])
    account_rows = _load_execution_rows_for_account(account_key if profile_id else None)
    open_positions = [r for r in account_rows if not r.get("outcome") or str(r.get("outcome")).upper() == "OPEN"]
    daily_pnl, total_pnl = _compute_account_pnl(account_rows)
    weekly_pnl = _compute_weekly_pnl(account_rows)
    trades_today = _get_trades_today(account_rows)
    volume_today = _get_volume_today(account_rows)
    existing_symbols = {(str(r.get("symbol") or "").upper(), str(r.get("direction") or "").upper()) for r in open_positions}
    sector_map = _sector_lookup()
    existing_sector_counts = Counter(
        sector_map.get(str(r.get("symbol") or "").upper())
        for r in open_positions
        if sector_map.get(str(r.get("symbol") or "").upper())
    )
    existing_risk = sum(
        abs(float(r.get("fill_price") or r.get("entry_price") or 0.0) - float(r.get("stop_loss") or 0.0))
        * int(r.get("shares") or 0)
        for r in open_positions
        if (r.get("fill_price") or r.get("entry_price")) and r.get("stop_loss")
    )

    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    queued_symbols: set[tuple[str, str]] = set()
    queued_sector_counts: Counter[str] = Counter()
    batch_exposure = 0.0
    batch_risk = 0.0

    if not int(profile.get("is_active", 1)):
        for trade in trades:
            audit = _profile_audit_metadata(profile, ["account_active"])
            rejected.append({
                "client_order_key": trade.get("client_order_key"),
                "symbol": str(trade.get("symbol") or "").upper(),
                "direction": str(trade.get("direction") or "").upper(),
                "reason": "Account is disabled",
                "check": "account_active",
                "resized": False,
                "resize_reason": None,
                **audit,
            })
        summary = {
            "submitted": 0,
            "rejected": len(rejected),
            "total_risk": 0.0,
            "total_exposure": 0.0,
            "account_profile": account_key,
            "profile_name": str(profile.get("name") or DEFAULT_RISK_PROFILE["name"]),
            "daily_pnl": daily_pnl,
            "weekly_pnl": weekly_pnl,
            "total_pnl": total_pnl,
        }
        return summary, [], rejected

    for trade in trades:
        working_trade = dict(trade)
        symbol = str(trade.get("symbol") or "").upper()
        direction = str(trade.get("direction") or "LONG").upper()
        shares = int(working_trade.get("shares") or 0)
        entry = float(working_trade.get("entry_price") or 0.0)
        stop = float(working_trade.get("stop_loss") or 0.0)
        sector = sector_map.get(symbol)
        asset_class = _normalize_asset_class(working_trade)
        rules_fired: list[str] = []

        reject_reason: tuple[str, str] | None = None
        if shares <= 0 or entry <= 0 or stop <= 0:
            reject_reason = ("invalid_trade", "Trade missing valid shares, entry, or stop")
        elif str(profile.get("instrument_scope") or "all") == "us_equities" and asset_class not in (None, "equity", "us_equity"):
            reject_reason = ("instrument_scope", f"Profile {account_key} only allows US equities, got {asset_class}")
        elif str(profile.get("instrument_scope") or "all") == "forex_cfd" and asset_class not in ("forex", "index", "metal", "energy", "crypto", "commodity"):
            reject_reason = ("instrument_scope", f"Profile {account_key} only allows forex/CFD instruments")
        elif str(profile.get("instrument_scope") or "all") == "futures" and asset_class != "futures":
            reject_reason = ("instrument_scope", f"Profile {account_key} only allows futures instruments")
        elif abs(daily_pnl) >= float(profile["daily_loss_limit"]) * float(profile.get("dll_headroom_pct", 0.70)):
            pct = abs(daily_pnl) / float(profile["daily_loss_limit"]) * 100 if profile["daily_loss_limit"] else 0
            reject_reason = ("daily_loss_budget", f"Daily loss at {pct:.0f}% of limit — headroom too low")
        elif float(profile.get("weekly_loss_limit") or 0.0) > 0 and abs(weekly_pnl) >= float(profile["weekly_loss_limit"]) * 0.70:
            pct = abs(weekly_pnl) / float(profile["weekly_loss_limit"]) * 100 if profile["weekly_loss_limit"] else 0
            reject_reason = ("weekly_loss_limit", f"Weekly loss at {pct:.0f}% of ${float(profile['weekly_loss_limit']):,.0f}")
        elif int(profile.get("max_trades_per_day") or 0) > 0 and (trades_today + len(approved)) >= int(profile["max_trades_per_day"]):
            reject_reason = ("max_trades_per_day", f"Max trades/day: {trades_today + len(approved)} >= {int(profile['max_trades_per_day'])}")
        elif float(profile.get("volume_limit") or 0.0) > 0 and (volume_today + sum(int(t.get('shares') or 0) for t in approved) + shares) >= float(profile["volume_limit"]):
            reject_reason = ("volume_limit", f"Volume limit: {volume_today + sum(int(t.get('shares') or 0) for t in approved) + shares} >= {float(profile['volume_limit']):,.0f}")
        elif abs(total_pnl) >= float(profile["max_drawdown"]) * float(profile.get("dd_headroom_pct", 0.80)):
            pct = abs(total_pnl) / float(profile["max_drawdown"]) * 100 if profile["max_drawdown"] else 0
            reject_reason = ("max_drawdown_budget", f"Drawdown at {pct:.0f}% of limit")
        elif len(open_positions) + len(approved) >= int(profile["max_positions"]):
            reject_reason = (
                "position_count",
                f"Position limit: {len(open_positions)} open + {len(approved)} queued ≥ {int(profile['max_positions'])} max",
            )
        elif str(profile.get("holding_style") or "").lower() == "intraday" and int(profile.get("must_close_eod", 0)):
            from vanguard.helpers.eod_flatten import check_eod_action
            eod_action = check_eod_action(
                profile.get("must_close_eod", 0),
                profile.get("no_new_positions_after"),
                profile.get("eod_flatten_time"),
            )
            if eod_action in {"FLATTEN_ALL", "NO_NEW_TRADES"}:
                reject_reason = ("intraday_cutoff", f"Profile {account_key} blocked by {eod_action}")

        if reject_reason:
            check, reason = reject_reason
            audit = _profile_audit_metadata(profile, [check])
            rejected.append({
                "client_order_key": trade.get("client_order_key"),
                "symbol": symbol,
                "direction": direction,
                "reason": reason,
                "check": check,
                "resized": False,
                "resize_reason": None,
                "model_readiness": working_trade.get("model_readiness"),
                "model_source": working_trade.get("model_source"),
                "gate_policy": None,
                "fallback_in_effect": False,
                **audit,
            })
            continue

        readiness_gate = _apply_model_readiness_gate(working_trade)
        if readiness_gate["applied"]:
            rules_fired.append(str(readiness_gate["gate_policy"]))
            if not readiness_gate["passed"]:
                audit = _profile_audit_metadata(profile, rules_fired)
                rejected.append({
                    "client_order_key": trade.get("client_order_key"),
                    "symbol": symbol,
                    "direction": direction,
                    "reason": readiness_gate["reason"],
                    "check": "model_readiness",
                    "resized": False,
                    "resize_reason": None,
                    "model_readiness": readiness_gate["model_readiness"],
                    "model_source": readiness_gate["model_source"],
                    "gate_policy": readiness_gate["gate_policy"],
                    "fallback_in_effect": readiness_gate["fallback_in_effect"],
                    **audit,
                })
                continue
        else:
            readiness_gate = {
                "model_readiness": working_trade.get("model_readiness"),
                "model_source": working_trade.get("model_source"),
                "gate_policy": None,
                "fallback_in_effect": False,
            }

        max_single_pct = float(profile.get("max_single_position_pct", 0.10))
        max_position_value = float(profile["account_size"]) * max_single_pct
        position_value = shares * entry
        if position_value > max_position_value:
            new_shares = int(max_position_value / entry) if entry > 0 else 0
            if new_shares <= 0:
                audit = _profile_audit_metadata(profile, ["profile_sizing_cap"])
                rejected.append({
                    "client_order_key": trade.get("client_order_key"),
                    "symbol": symbol,
                    "direction": direction,
                    "reason": f"Trade cannot be resized within {max_single_pct * 100:.0f}% single-position cap",
                    "check": "profile_sizing_cap",
                    "resized": False,
                    "resize_reason": None,
                    "model_readiness": readiness_gate["model_readiness"],
                    "model_source": readiness_gate["model_source"],
                    "gate_policy": readiness_gate["gate_policy"],
                    "fallback_in_effect": readiness_gate["fallback_in_effect"],
                    **audit,
                })
                continue
            working_trade["original_shares"] = shares
            working_trade["shares"] = new_shares
            working_trade["resized"] = True
            working_trade["resize_reason"] = (
                f"Capped from {shares} to {new_shares} shares "
                f"(max {max_single_pct * 100:.0f}% of account)"
            )
            shares = new_shares
            position_value = shares * entry
            rules_fired.append("profile_sizing_cap")

        new_risk = _risk_for_trade(working_trade)

        if (batch_exposure + position_value) > float(profile["account_size"]) * float(profile.get("max_batch_exposure_pct", 0.50)):
            reject_reason = (
                "total_batch_exposure",
                f"Batch exposure ${batch_exposure + position_value:,.0f} exceeds {float(profile.get('max_batch_exposure_pct', 0.50)) * 100:.0f}% of ${float(profile['account_size']):,.0f} account",
            )
        elif (existing_risk + batch_risk + new_risk) > float(profile["daily_loss_limit"]) * 0.80:
            reject_reason = (
                "total_portfolio_risk",
                f"Total portfolio risk ${existing_risk + batch_risk + new_risk:,.0f} exceeds 80% of ${float(profile['daily_loss_limit']):,.0f} daily pause",
            )
        elif int(profile.get("block_duplicate_symbols", 1)) and (
            (symbol, direction) in existing_symbols or (symbol, direction) in queued_symbols
        ):
            reject_reason = ("duplicate_symbol", f"Already have {direction} position in {symbol}")
        elif sector and (existing_sector_counts[sector] + queued_sector_counts[sector]) >= int(profile.get("max_per_sector", 3)):
            reject_reason = (
                "sector_concentration",
                f"Sector {sector}: {existing_sector_counts[sector] + queued_sector_counts[sector]} positions ≥ {int(profile.get('max_per_sector', 3))} max",
            )
        elif str(profile.get("prop_firm", "")).lower() == "ttp" and abs(entry - stop) < 0.10:
            reject_reason = (
                "prop_firm_rule_ttp_min_range",
                f"Stop distance ${abs(entry - stop):.2f} below TTP $0.10 minimum range",
            )

        if reject_reason:
            check, reason = reject_reason
            audit = _profile_audit_metadata(profile, [*rules_fired, check])
            rejected.append({
                "client_order_key": trade.get("client_order_key"),
                "symbol": symbol,
                "direction": direction,
                "reason": reason,
                "check": check,
                "resized": bool(working_trade.get("resized", False)),
                "resize_reason": working_trade.get("resize_reason"),
                "model_readiness": readiness_gate["model_readiness"],
                "model_source": readiness_gate["model_source"],
                "gate_policy": readiness_gate["gate_policy"],
                "fallback_in_effect": readiness_gate["fallback_in_effect"],
                **audit,
            })
            continue

        approved_trade = dict(working_trade)
        approved_trade["symbol"] = symbol
        approved_trade["direction"] = direction
        approved_trade["account"] = account_key
        approved_trade["sector"] = sector
        approved_trade.update(_profile_audit_metadata(profile, rules_fired))
        approved_trade["resized"] = bool(approved_trade.get("resized", False))
        approved_trade["resize_reason"] = approved_trade.get("resize_reason")
        approved_trade["model_readiness"] = readiness_gate["model_readiness"]
        approved_trade["model_source"] = readiness_gate["model_source"]
        approved_trade["gate_policy"] = readiness_gate["gate_policy"]
        approved_trade["fallback_in_effect"] = readiness_gate["fallback_in_effect"]
        approved.append(approved_trade)
        batch_exposure += position_value
        batch_risk += new_risk
        queued_symbols.add((symbol, direction))
        if sector:
            queued_sector_counts[sector] += 1

    summary = {
        "submitted": 0,
        "rejected": len(rejected),
        "total_risk": round(batch_risk, 2),
        "total_exposure": round(batch_exposure, 2),
        "account_profile": account_key,
        "profile_name": str(profile.get("name") or DEFAULT_RISK_PROFILE["name"]),
        "daily_pnl": daily_pnl,
        "weekly_pnl": weekly_pnl,
        "total_pnl": total_pnl,
    }
    return summary, approved, rejected


def execute_trades(
    trades: list[dict[str, Any]],
    profile_id: str | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    """
    Execute a list of trades via SignalStack webhook.

    Parameters
    ----------
    trades : list of {symbol, direction, shares, tier}

    Returns
    -------
    list of {symbol, direction, tier, shares, success, status, response, error}
    """
    from vanguard.execution.signalstack_adapter import SignalStackAdapter
    from vanguard.api import accounts as acc_module

    def _reject_all(reason: str, pid: str | None = None) -> dict[str, Any]:
        rejected_rows = []
        for trade in trades:
            rejected_rows.append({
                "client_order_key": trade.get("client_order_key"),
                "symbol": str(trade.get("symbol") or "").upper(),
                "direction": str(trade.get("direction") or "").upper(),
                "reason": reason,
                "check": "profile_validation",
                "profile_id": pid,
                "profile_name": None,
                "environment": "unknown",
                "instrument_scope": "all",
                "holding_style": "unknown",
                "rules_fired": ["profile_validation"],
                "resized": False,
                "resize_reason": None,
                "model_readiness": trade.get("model_readiness"),
                "model_source": trade.get("model_source"),
                "gate_policy": None,
                "fallback_in_effect": False,
            })
        return {
            "error": reason,
            "approved": [],
            "rejected": rejected_rows,
            "summary": {
                "submitted": 0,
                "rejected": len(rejected_rows),
                "total_risk": 0.0,
                "total_exposure": 0.0,
                "account_profile": pid,
                "profile_name": None,
                "daily_pnl": 0.0,
                "weekly_pnl": 0.0,
                "total_pnl": 0.0,
            },
            "submitted": 0,
            "failed": len(rejected_rows),
            "results": [],
        }

    if not profile_id or profile_id == "default":
        return _reject_all("No active profile selected. Select a profile before executing.", profile_id)

    selected_profile = acc_module.get_profile(profile_id)
    if not selected_profile:
        return _reject_all(f"Profile {profile_id} not found", profile_id)
    if not int(selected_profile.get("is_active", 1)):
        return _reject_all(f"Profile {profile_id} is inactive", profile_id)

    execution_bridge = _resolve_execution_bridge(selected_profile)
    adapter = SignalStackAdapter() if execution_bridge == "signalstack" else None
    if execution_bridge == "signalstack" and not str(selected_profile.get("webhook_url") or "").strip():
        logger.warning("No webhook_url configured for profile %s — orders will be forward-tracked only", profile_id)
    summary, approved_trades, rejected = _evaluate_trade_batch(trades, profile_id=profile_id)
    if preview_only:
        return {
            "approved": [
                {
                    "client_order_key": trade.get("client_order_key"),
                    "symbol": str(trade.get("symbol") or "").upper(),
                    "direction": str(trade.get("direction") or "").upper(),
                    "shares": int(trade.get("shares") or 0),
                    "tier": str(trade.get("tier") or "manual"),
                    "status": "APPROVED",
                    "profile_id": trade.get("profile_id"),
                    "profile_name": trade.get("profile_name"),
                    "environment": trade.get("environment"),
                    "instrument_scope": trade.get("instrument_scope"),
                    "holding_style": trade.get("holding_style"),
                    "rules_fired": trade.get("rules_fired", []),
                    "resized": bool(trade.get("resized", False)),
                    "resize_reason": trade.get("resize_reason"),
                    "model_readiness": trade.get("model_readiness"),
                    "model_source": trade.get("model_source"),
                    "gate_policy": trade.get("gate_policy"),
                    "fallback_in_effect": bool(trade.get("fallback_in_effect", False)),
                }
                for trade in approved_trades
            ],
            "rejected": rejected,
            "summary": summary,
            "submitted": 0,
            "failed": 0,
            "results": [],
        }

    results: list[dict[str, Any]] = []

    for trade in approved_trades:
        symbol    = str(trade["symbol"]).upper()
        direction = str(trade["direction"]).upper()
        shares    = int(trade.get("shares", 100))
        tier      = str(trade.get("tier", "manual"))
        scores    = trade.get("scores")
        tags      = trade.get("tags") or []
        notes     = trade.get("notes") or None
        account   = trade.get("account")
        entry_price = float(trade["entry_price"]) if trade.get("entry_price") is not None else None
        stop_loss = float(trade["stop_loss"]) if trade.get("stop_loss") is not None else None
        take_profit = float(trade["take_profit"]) if trade.get("take_profit") is not None else None
        order_type = str(trade["order_type"]) if trade.get("order_type") is not None else None
        limit_buffer = float(trade["limit_buffer"]) if trade.get("limit_buffer") is not None else None

        if adapter and str(selected_profile.get("webhook_url") or "").strip():
            try:
                limit_price = None
                stop_price = None
                if order_type and order_type.lower() in {"limit", "stop_limit"}:
                    limit_price = entry_price
                if order_type and order_type.lower() == "stop_limit":
                    stop_price = stop_loss
                resp = adapter.send_order(
                    symbol=symbol,
                    direction=direction,
                    quantity=shares,
                    operation="open",
                    limit_price=limit_price,
                    stop_price=stop_price,
                    broker="ttp",
                    profile=selected_profile,
                )
                success = resp.get("success", False)
                status  = "SUBMITTED" if success else "FAILED"
                error   = resp.get("error")
            except Exception as exc:
                resp    = {}
                success = False
                status  = "ERROR"
                error   = str(exc)
        else:
            # Non-SignalStack bridge or no webhook configured — forward-track only
            resp    = {
                "forward_tracked": True,
                "execution_bridge": execution_bridge,
                "profile_id": selected_profile.get("id"),
            }
            success = True
            status  = "FORWARD_TRACKED"
            error   = None

        row_id = _log_execution(
            symbol,
            direction,
            tier,
            shares,
            entry_price,
            stop_loss,
            take_profit,
            order_type,
            limit_buffer,
            status,
            resp,
            account,
            scores,
            tags,
            notes,
        )

        results.append({
            "id":        row_id,
            "client_order_key": trade.get("client_order_key"),
            "symbol":    symbol,
            "direction": direction,
            "tier":      tier,
            "shares":    shares,
            "success":   success,
            "status":    status,
            "response":  resp,
            "error":     error,
            "profile_id": trade.get("profile_id"),
            "profile_name": trade.get("profile_name"),
            "environment": trade.get("environment"),
            "instrument_scope": trade.get("instrument_scope"),
            "holding_style": trade.get("holding_style"),
            "rules_fired": trade.get("rules_fired", []),
            "resized": bool(trade.get("resized", False)),
            "resize_reason": trade.get("resize_reason"),
            "model_readiness": trade.get("model_readiness"),
            "model_source": trade.get("model_source"),
            "gate_policy": trade.get("gate_policy"),
            "fallback_in_effect": bool(trade.get("fallback_in_effect", False)),
        })

    submitted = sum(1 for row in results if row["success"])
    failed = len(results) - submitted
    summary["submitted"] = submitted
    return {
        "approved": results,
        "rejected": rejected,
        "summary": summary,
        "submitted": submitted,
        "failed": failed,
        "results": results,
    }


# ── Execution log query ────────────────────────────────────────────────────────

def get_execution_log(
    date: str | None = None,
    limit: int = 100,
    tag: str | None = None,
    outcome: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return execution log entries.

    Parameters
    ----------
    date    : YYYY-MM-DD. If None, defaults to today.
    limit   : max rows to return.
    tag     : filter rows whose tags JSON array contains this string.
    outcome : filter by outcome value (WIN|LOSE|TIMEOUT|OPEN).
    """
    from datetime import date as date_type
    date_str = date or str(date_type.today())

    clauses = ["DATE(executed_at) = ?"]
    params: list[Any] = [date_str]

    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome.upper())

    where = " AND ".join(clauses)

    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"""
                SELECT * FROM execution_log
                WHERE  {where}
                ORDER  BY executed_at DESC
                LIMIT  ?
                """,
                (*params, limit),
            ).fetchall()
        result = [dict(r) for r in rows]
        if tag:
            # Filter in Python — SQLite JSON_EACH not reliably available
            result = [
                r for r in result
                if tag in (json.loads(r["tags"]) if r.get("tags") else [])
            ]
        return result
    except Exception as exc:
        logger.error(f"get_execution_log error: {exc}")
        return []


def estimate_ttp_fee(shares: int) -> float:
    return max(0.75, round(shares * 0.005, 2))


def get_execution(execution_id: int) -> dict[str, Any] | None:
    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM execution_log WHERE id = ?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.error(f"get_execution error: {exc}")
        return None


_FILLABLE_FIELDS = {"fill_price", "filled_at", "execution_fee"}


def update_execution_fill(execution_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    fields = {k: v for k, v in data.items() if k in _FILLABLE_FIELDS}
    if not fields:
        return get_execution(execution_id)
    set_parts = [f"{k} = ?" for k in fields]
    values = list(fields.values()) + [execution_id]
    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            con.row_factory = sqlite3.Row
            if not con.execute(
                "SELECT id FROM execution_log WHERE id = ?",
                (execution_id,),
            ).fetchone():
                return None
            con.execute(
                f"UPDATE execution_log SET {', '.join(set_parts)} WHERE id = ?",
                values,
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM execution_log WHERE id = ?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.error(f"update_execution_fill error: {exc}")
        return None


# ── Forward tracking ───────────────────────────────────────────────────────────

_UPDATABLE_FIELDS = {
    "tags", "notes", "exit_price", "exit_date",
    "pnl_dollars", "pnl_pct", "outcome", "days_held",
}


def update_execution(
    execution_id: int,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Update forward-tracking fields on an execution log row.

    Returns updated row dict, or None if not found.
    """
    fields = {k: v for k, v in data.items() if k in _UPDATABLE_FIELDS}
    if not fields:
        # Nothing to update — return current row
        try:
            with sqlite3.connect(str(VANGUARD_DB)) as con:
                con.row_factory = sqlite3.Row
                row = con.execute(
                    "SELECT * FROM execution_log WHERE id = ?", (execution_id,)
                ).fetchone()
            return dict(row) if row else None
        except Exception as exc:
            logger.error(f"update_execution fetch error: {exc}")
            return None

    # Serialize tags list to JSON string
    if "tags" in fields and isinstance(fields["tags"], list):
        fields["tags"] = json.dumps(fields["tags"])
    if "outcome" in fields and fields["outcome"]:
        fields["outcome"] = str(fields["outcome"]).upper()

    set_parts = [f"{k} = ?" for k in fields]
    values = list(fields.values()) + [execution_id]

    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            con.row_factory = sqlite3.Row
            if not con.execute(
                "SELECT id FROM execution_log WHERE id = ?", (execution_id,)
            ).fetchone():
                return None
            con.execute(
                f"UPDATE execution_log SET {', '.join(set_parts)} WHERE id = ?",
                values,
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM execution_log WHERE id = ?", (execution_id,)
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.error(f"update_execution error: {exc}")
        return None


def delete_execution(execution_id: int) -> bool:
    """Delete one execution log row by id."""
    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            cur = con.execute("DELETE FROM execution_log WHERE id = ?", (execution_id,))
            con.commit()
            return cur.rowcount > 0
    except Exception as exc:
        logger.error(f"delete_execution error: {exc}")
        return False


def delete_executions_bulk(
    ids: list[int] | None = None,
    before_date: str | None = None,
) -> int:
    """
    Delete multiple execution log rows.

    ids         : list of integer row IDs to delete.
    before_date : YYYY-MM-DD — delete all rows where DATE(executed_at) < before_date.

    Returns count of deleted rows.
    """
    if not ids and not before_date:
        return 0
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if ids:
            placeholders = ",".join("?" * len(ids))
            clauses.append(f"id IN ({placeholders})")
            params.extend(ids)
        if before_date:
            clauses.append("DATE(executed_at) < ?")
            params.append(before_date)
        where = " AND ".join(clauses)
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            cur = con.execute(f"DELETE FROM execution_log WHERE {where}", params)
            con.commit()
            return cur.rowcount
    except Exception as exc:
        logger.error(f"delete_executions_bulk error: {exc}")
        return 0


def get_analytics(period: str = "last_30_days") -> dict[str, Any]:
    """
    Return signal attribution analytics over a rolling period.

    Aggregates win_rate, avg_pnl_pct by tag, tier, and source.
    Rows with outcome=NULL / 'OPEN' are counted as open.

    period: 'last_7_days' | 'last_30_days' | 'last_90_days' | 'all'
    """
    period_map = {
        "last_7_days":  "-7 days",
        "last_30_days": "-30 days",
        "last_90_days": "-90 days",
        "all":          None,
    }
    offset = period_map.get(period, "-30 days")

    try:
        with sqlite3.connect(str(VANGUARD_DB)) as con:
            con.row_factory = sqlite3.Row
            if offset:
                rows = con.execute(
                    """
                    SELECT id, tier, tags, outcome, pnl_pct
                    FROM   execution_log
                    WHERE  DATE(executed_at) >= DATE('now', ?)
                    """,
                    (offset,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT id, tier, tags, outcome, pnl_pct FROM execution_log"
                ).fetchall()
    except Exception as exc:
        logger.error(f"get_analytics error: {exc}")
        return {"by_tag": {}, "by_tier": {}, "by_source": {}, "period": period}

    # Helper to compute stats for a group of rows
    def _stats(group: list[dict]) -> dict[str, Any]:
        wins   = sum(1 for r in group if r["outcome"] == "WIN")
        losses = sum(1 for r in group if r["outcome"] == "LOSE")
        open_  = sum(1 for r in group if not r["outcome"] or r["outcome"] == "OPEN")
        total  = len(group)
        settled = wins + losses
        pnls = [r["pnl_pct"] for r in group if r["pnl_pct"] is not None]
        return {
            "trades":       total,
            "wins":         wins,
            "losses":       losses,
            "open":         open_,
            "win_rate":     round(wins / settled, 3) if settled else None,
            "avg_pnl_pct":  round(sum(pnls) / len(pnls), 3) if pnls else None,
        }

    # Normalise rows to plain dicts
    data = [dict(r) for r in rows]

    # ── by_tier ──
    tier_groups: dict[str, list[dict]] = {}
    for r in data:
        tier_groups.setdefault(r["tier"], []).append(r)
    by_tier = {t: _stats(g) for t, g in sorted(tier_groups.items())}

    # ── by_source (derived from tier prefix) ──
    source_groups: dict[str, list[dict]] = {}
    for r in data:
        src = "meridian" if r["tier"].startswith("tier_meridian") else "s1"
        source_groups.setdefault(src, []).append(r)
    by_source = {s: _stats(g) for s, g in sorted(source_groups.items())}

    # ── by_tag ──
    tag_groups: dict[str, list[dict]] = {}
    for r in data:
        tags = json.loads(r["tags"]) if r.get("tags") else []
        for t in tags:
            tag_groups.setdefault(t, []).append(r)
    by_tag = {t: _stats(g) for t, g in sorted(tag_groups.items())}

    return {
        "by_tag":    by_tag,
        "by_tier":   by_tier,
        "by_source": by_source,
        "period":    period,
        "total_rows": len(data),
    }
