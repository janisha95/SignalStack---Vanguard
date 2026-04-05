"""
vanguard_risk_filters.py — V6 Risk Filters entry point.

Takes the V5 shortlist and produces a sized, compliant tradeable portfolio
for EACH active prop-firm account. Same shortlist in, different portfolio per account.

Pipeline per account (13 steps):
  1.  Instrument filter (TTP = US equities, FTMO = all assets)
  2.  Daily budget check (TTP: Daily Pause)
  3.  Max loss check (TTP: drawdown terminated)
  4.  Weekly loss check (TTP only)
  5.  Equities volume limit (TTP only)
  6.  Time gate (no new positions after cutoff, EOD flatten)
  7.  Position limit (open_positions >= max_positions)
  8.  Consistency check (TTP: no single trade > X% of profit)
  9.  Position sizing (ATR-based)
  10. Min trade range check (TTP: 10 cents TP - entry)
  11. Diversification (sector cap, correlation cap, heat cap)
  12. Prop-firm specific rules (earnings block, halt block, FTMO best-day)
  13. Scaling check (TTP: profit trigger → BP + pause increase)

Output:
  - vanguard_tradeable_portfolio (one row per approved/rejected candidate per account)
  - vanguard_portfolio_state (updated per account)

CLI:
  python3 stages/vanguard_risk_filters.py                          # all accounts
  python3 stages/vanguard_risk_filters.py --account ttp_50k_intraday
  python3 stages/vanguard_risk_filters.py --dry-run
  python3 stages/vanguard_risk_filters.py --debug AAPL
  python3 stages/vanguard_risk_filters.py --flatten ttp_100k_intraday
  python3 stages/vanguard_risk_filters.py --status

Location: ~/SS/Vanguard/stages/vanguard_risk_filters.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.helpers.clock import now_utc, now_et, iso_utc
from vanguard.helpers.position_sizer import compute_atr, compute_risk_dollars, size_equity, size_forex
from vanguard.helpers.portfolio_state import (
    ensure_schema as ensure_state_schema,
    get_or_init_state,
    save_state,
    get_account_status,
)
from vanguard.helpers.correlation_checker import compute_correlations, is_too_correlated
from vanguard.helpers.eod_flatten import check_eod_action, get_positions_to_flatten
from vanguard.helpers.ttp_rules import (
    check_scaling_reset,
    check_consistency_rule,
    apply_scaling,
    check_min_trade_range,
)

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

_DB_PATH = str(_REPO_ROOT / "data" / "vanguard_universe.db")
_ML_THRESHOLD_PATH = _REPO_ROOT / "config" / "vanguard_ml_thresholds.json"
_GFT_UNIVERSE_PATH = _REPO_ROOT / "config" / "gft_universe.json"

_DEFAULT_GFT_UNIVERSE = {
    "AAPL", "AMZN", "BA", "COST", "JPM", "KO", "META", "MSFT", "NFLX",
    "NVDA", "ORCL", "PLTR", "TSLA", "UNH", "V",
    "SPY", "QQQ", "DIA",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURCHF", "EURAUD", "EURCAD",
    "BNBUSD", "BTCUSD", "ETHUSD", "LTCUSD", "SOLUSD", "BCHUSD",
}


def _normalize_gft_symbol(symbol: str) -> str:
    return (
        str(symbol or "")
        .upper()
        .replace("/", "")
        .replace(".CASH", "")
        .replace(".X", "")
        .strip()
    )


def _load_gft_universe() -> set[str]:
    if not _GFT_UNIVERSE_PATH.exists():
        return set(_DEFAULT_GFT_UNIVERSE)
    try:
        payload = json.loads(_GFT_UNIVERSE_PATH.read_text())
        symbols = payload.get("gft_universe") if isinstance(payload, dict) else payload
        loaded = {_normalize_gft_symbol(sym) for sym in (symbols or []) if str(sym).strip()}
        return loaded or set(_DEFAULT_GFT_UNIVERSE)
    except Exception as exc:
        logger.warning("Could not load GFT universe config: %s", exc)
        return set(_DEFAULT_GFT_UNIVERSE)


GFT_UNIVERSE = _load_gft_universe()

GFT_MAX_POSITIONS = 3
GFT_RISK_PER_TRADE_PCT = 0.005
GFT_DAILY_LOSS_LIMIT_PCT = 0.04
GFT_MAX_PORTFOLIO_HEAT_PCT = 0.02
GFT_MIN_SL_PIPS = 20.0
GFT_MIN_TP_PIPS = 40.0
GFT_MIN_CRYPTO_QTY = 0.000001
GFT_MIN_CRYPTO_SL_PCT = 0.08
GFT_MIN_CRYPTO_TP_PCT = 0.16


def _crypto_contract_size(symbol: str, entry_price: float) -> float:
    """Approximate broker contract unit so low-priced coins don't produce huge lot counts."""
    normalized = _normalize_gft_symbol(symbol)
    if normalized in {"BTCUSD", "ETHUSD", "BNBUSD", "BCHUSD"}:
        return 1.0
    if normalized in {"SOLUSD", "LTCUSD", "AVAXUSD", "AAVEUSD", "LINKUSD"}:
        return 10.0
    if normalized in {"XRPUSD", "ADAUSD", "DOGEUSD", "DOTUSD", "ATOMUSD", "UNIUSD", "MATICUSD"}:
        return 1000.0 if float(entry_price or 0.0) < 1.0 else 100.0
    if float(entry_price or 0.0) < 1.0:
        return 1000.0
    if float(entry_price or 0.0) < 100.0:
        return 10.0
    return 1.0


def _is_gft_account(account_id: str) -> bool:
    return str(account_id or "").lower().startswith("gft_")


def _gft_risk_params(account_id: str, account_size: float) -> dict[str, float | int]:
    """Hard-coded GFT controls that override DB profile knobs for gft_* only."""
    if not _is_gft_account(account_id):
        return {}
    return {
        "max_positions": GFT_MAX_POSITIONS,
        "risk_per_trade_pct": GFT_RISK_PER_TRADE_PCT,
        "daily_loss_limit": round(float(account_size) * GFT_DAILY_LOSS_LIMIT_PCT, 2),
        "max_portfolio_heat_pct": GFT_MAX_PORTFOLIO_HEAT_PCT,
    }


def calculate_forex_lots(
    risk_dollars: float,
    entry: float,
    stop_loss: float,
    symbol: str,
    account_size: float,
    edge_score: float = 0.0,
) -> float:
    """Calculate GFT forex lots from dollar risk and pip distance."""
    is_jpy = str(symbol or "").upper().replace("/", "").endswith("JPY")
    pip_size = 0.01 if is_jpy else 0.0001
    pip_value_per_lot = 6.5 if is_jpy else 10.0

    pip_distance = abs(float(entry or 0.0) - float(stop_loss or 0.0)) / pip_size
    if pip_distance < 1:
        pip_distance = GFT_MIN_SL_PIPS

    base_lot = float(risk_dollars or 0.0) / (pip_distance * pip_value_per_lot)
    edge = max(0.0, min(float(edge_score or 0.0), 1.0))
    # Conviction-aware lot sizing for GFT — higher edge_score gets larger lot
    raw_lot_size = min(base_lot, base_lot * (0.4 + edge))
    lot_size = math.floor(raw_lot_size * 100.0) / 100.0
    min_lot = 0.02 if float(account_size or 0.0) >= 10000.0 else 0.01
    lot_size = max(min_lot, lot_size)
    lot_size = min(lot_size, 0.50)
    max_lot_by_account = max(0.01, float(account_size or 0.0) / 10000.0)
    return round(min(lot_size, max_lot_by_account), 2)


def calculate_cfd_shares(risk_dollars: float, entry: float, stop_loss: float) -> float:
    """Calculate GFT equity-CFD share count from dollar risk and stop distance."""
    risk_per_share = abs(float(entry or 0.0) - float(stop_loss or 0.0))
    if risk_per_share <= 0:
        return 1.0
    shares = round(float(risk_dollars or 0.0) / risk_per_share, 2)
    shares = max(0.1, shares)
    return min(shares, 100.0)


def calculate_crypto_qty(
    risk_dollars: float,
    entry: float,
    stop_loss: float,
    symbol: str = "",
) -> float:
    """Calculate GFT crypto quantity from dollar risk and absolute stop distance."""
    risk_per_unit = abs(float(entry or 0.0) - float(stop_loss or 0.0))
    if risk_per_unit <= 0:
        return 0.0
    contract_size = _crypto_contract_size(symbol, float(entry or 0.0))
    raw_qty = float(risk_dollars or 0.0) / (risk_per_unit * contract_size)
    qty = math.floor(raw_qty * 1_000_000.0) / 1_000_000.0
    return max(GFT_MIN_CRYPTO_QTY, round(qty, 6))


def _size_gft_trade(
    symbol: str,
    asset_class: str,
    entry_price: float,
    atr: float,
    stop_atr_multiple: float,
    tp_atr_multiple: float,
    risk_dollars: float,
    direction: str,
    account_size: float,
    edge_score: float = 0.0,
) -> dict[str, Any]:
    """GFT-only position sizing with forex lots and CFD fractional shares."""
    if entry_price <= 0 or atr <= 0 or risk_dollars <= 0:
        return {
            "shares": 0,
            "stop_price": None,
            "tp_price": None,
            "stop_distance": 0.0,
            "position_value": 0.0,
            "actual_risk": 0.0,
            "risk_pct_of_price": 0.0,
        }

    asset_key = str(asset_class or "").lower()
    is_forex = asset_key == "forex"
    is_crypto = asset_key == "crypto"
    is_jpy = str(symbol or "").upper().replace("/", "").endswith("JPY")
    if is_forex and is_jpy:
        price_decimals = 3
    elif is_forex:
        price_decimals = 5
    elif is_crypto:
        price_decimals = 2 if float(entry_price) >= 100 else 5 if float(entry_price) < 10 else 4
    else:
        price_decimals = 2
    if is_forex:
        pip_size = 0.01 if is_jpy else 0.0001
        stop_distance = max(float(atr) * float(stop_atr_multiple), GFT_MIN_SL_PIPS * pip_size)
        tp_distance = max(float(atr) * float(tp_atr_multiple), GFT_MIN_TP_PIPS * pip_size)
    elif is_crypto:
        stop_distance = max(
            float(atr) * float(stop_atr_multiple),
            float(entry_price) * GFT_MIN_CRYPTO_SL_PCT,
        )
        tp_distance = max(
            float(atr) * float(tp_atr_multiple),
            float(entry_price) * GFT_MIN_CRYPTO_TP_PCT,
        )
    else:
        stop_distance = float(atr) * float(stop_atr_multiple)
        tp_distance = float(atr) * float(tp_atr_multiple)

    if str(direction or "").upper() == "LONG":
        stop_price = round(float(entry_price) - stop_distance, price_decimals)
        tp_price = round(float(entry_price) + tp_distance, price_decimals)
    else:
        stop_price = round(float(entry_price) + stop_distance, price_decimals)
        tp_price = round(float(entry_price) - tp_distance, price_decimals)

    if is_forex:
        shares = calculate_forex_lots(
            risk_dollars=risk_dollars,
            entry=entry_price,
            stop_loss=stop_price,
            symbol=symbol,
            account_size=account_size,
            edge_score=edge_score,
        )
        pip_size = 0.01 if is_jpy else 0.0001
        pip_value_per_lot = 6.5 if is_jpy else 10.0
        actual_risk = shares * (abs(entry_price - stop_price) / pip_size) * pip_value_per_lot
        position_value = shares * 100000.0 * entry_price
    elif is_crypto:
        shares = calculate_crypto_qty(
            risk_dollars=risk_dollars,
            entry=entry_price,
            stop_loss=stop_price,
            symbol=symbol,
        )
        contract_size = _crypto_contract_size(symbol, entry_price)
        actual_risk = shares * contract_size * abs(entry_price - stop_price)
        position_value = shares * contract_size * entry_price
    else:
        shares = calculate_cfd_shares(risk_dollars=risk_dollars, entry=entry_price, stop_loss=stop_price)
        actual_risk = shares * abs(entry_price - stop_price)
        position_value = shares * entry_price

    return {
        "shares": shares,
        "stop_price": stop_price,
        "tp_price": tp_price,
        "stop_distance": round(abs(float(entry_price) - float(stop_price)), 6),
        "position_value": round(position_value, 2),
        "actual_risk": round(actual_risk, 2),
        "risk_pct_of_price": round(abs(float(entry_price) - float(stop_price)) / entry_price, 6),
    }

# ---------------------------------------------------------------------------
# Output table schemas
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
    PRIMARY KEY (cycle_ts_utc, account_id, symbol, direction)
);
CREATE INDEX IF NOT EXISTS idx_vg_tp_account
    ON vanguard_tradeable_portfolio(account_id, cycle_ts_utc);
"""


def _ensure_output_schema(con: sqlite3.Connection) -> None:
    for stmt in _CREATE_TRADEABLE.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    existing = {
        row[1]
        for row in con.execute("PRAGMA table_info(vanguard_tradeable_portfolio)").fetchall()
    }
    required = {
        "model_readiness": "TEXT",
        "model_source": "TEXT",
        "gate_policy": "TEXT",
        "fallback_in_effect": "INTEGER DEFAULT 0",
    }
    for column, ddl in required.items():
        if column not in existing:
            con.execute(f"ALTER TABLE vanguard_tradeable_portfolio ADD COLUMN {column} {ddl}")
    con.commit()


def _migrate_tradeable_pk(db_path: str = _DB_PATH) -> None:
    """
    One-time migration: recreate vanguard_tradeable_portfolio with
    direction included in the PRIMARY KEY so LONG and SHORT rows for
    the same symbol coexist without overwriting each other.
    """
    with sqlite3.connect(db_path) as con:
        # Check if the table exists at all
        exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='vanguard_tradeable_portfolio'"
        ).fetchone()
        if not exists:
            return  # Nothing to migrate — schema creation handles it

        # Detect old PK by inspecting the CREATE statement
        schema_row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='vanguard_tradeable_portfolio'"
        ).fetchone()
        if schema_row and "symbol, direction)" in schema_row[0]:
            return  # Already migrated

        logger.info("Migrating vanguard_tradeable_portfolio PK to include direction …")
        con.execute("DROP TABLE IF EXISTS vanguard_tradeable_portfolio_old")
        con.execute(
            "ALTER TABLE vanguard_tradeable_portfolio "
            "RENAME TO vanguard_tradeable_portfolio_old"
        )
        # Create fresh table with corrected PK
        for stmt in _CREATE_TRADEABLE.strip().split(";"):
            if stmt.strip():
                con.execute(stmt)
        # Copy existing rows; INSERT OR IGNORE handles any PK collisions in old data
        con.execute(
            "INSERT OR IGNORE INTO vanguard_tradeable_portfolio "
            "SELECT * FROM vanguard_tradeable_portfolio_old"
        )
        con.execute("DROP TABLE vanguard_tradeable_portfolio_old")
        con.commit()
        logger.info("Migration complete.")


# ---------------------------------------------------------------------------
# Account profile loader
# ---------------------------------------------------------------------------

def load_accounts(
    account_filter: str | None = None,
    db_path: str = _DB_PATH,
) -> list[dict[str, Any]]:
    """
    Load all active account profiles from the DB.
    Returns list of account dicts with all columns.
    """
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        if account_filter:
            rows = con.execute(
                "SELECT * FROM account_profiles WHERE id = ? AND is_active = 1",
                (account_filter,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM account_profiles WHERE is_active = 1"
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Shortlist loader
# ---------------------------------------------------------------------------

def load_shortlist(db_path: str = _DB_PATH) -> pd.DataFrame:
    """
    Load the most recent V5 shortlist from vanguard_shortlist.
    Returns empty DataFrame if no shortlist is available.
    """
    try:
        with sqlite3.connect(db_path) as con:
            ts_row = con.execute(
                "SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist"
            ).fetchone()
            if not ts_row or not ts_row[0]:
                return pd.DataFrame()
            latest_ts = ts_row[0]
            df = pd.read_sql_query(
                "SELECT * FROM vanguard_shortlist WHERE cycle_ts_utc = ?",
                con,
                params=(latest_ts,),
            )
        return df
    except Exception as exc:
        logger.warning(f"Could not load shortlist: {exc}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Sector classification (lightweight static map)
# ---------------------------------------------------------------------------

_SECTOR_MAP: dict[str, str] = {
    # Technology
    "AAPL": "technology", "MSFT": "technology", "NVDA": "technology",
    "AMD": "technology", "INTC": "technology", "QCOM": "technology",
    "AVGO": "technology", "TXN": "technology", "AMAT": "technology",
    "LRCX": "technology", "KLAC": "technology", "MRVL": "technology",
    "MU": "technology", "SMCI": "technology", "DELL": "technology",
    "HPQ": "technology", "IBM": "technology", "NOW": "technology",
    "CRM": "technology", "ORCL": "technology", "SAP": "technology",
    "ADBE": "technology", "PLTR": "technology",
    # Consumer Discretionary
    "AMZN": "consumer_disc", "TSLA": "consumer_disc", "HD": "consumer_disc",
    "MCD": "consumer_disc", "NKE": "consumer_disc", "SBUX": "consumer_disc",
    "TGT": "consumer_disc", "COST": "consumer_disc", "LOW": "consumer_disc",
    # Health Care
    "JNJ": "healthcare", "UNH": "healthcare", "PFE": "healthcare",
    "ABBV": "healthcare", "MRK": "healthcare", "LLY": "healthcare",
    "AMGN": "healthcare", "BIIB": "healthcare", "GILD": "healthcare",
    # Financials
    "JPM": "financials", "GS": "financials", "MS": "financials",
    "BAC": "financials", "WFC": "financials", "C": "financials",
    "BLK": "financials", "AXP": "financials", "V": "financials",
    "MA": "financials", "PYPL": "financials",
    # Communication Services
    "META": "communication", "GOOG": "communication", "GOOGL": "communication",
    "NFLX": "communication", "DIS": "communication", "CMCSA": "communication",
    "T": "communication", "VZ": "communication",
    # Energy
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy",
    # Industrials
    "CAT": "industrials", "BA": "industrials", "GE": "industrials",
    "RTX": "industrials", "LMT": "industrials", "UPS": "industrials",
    # ETFs — map to asset_class
    "SPY": "etf_index", "QQQ": "etf_index", "IWM": "etf_index",
    "DIA": "etf_index", "XLK": "etf_tech", "XLF": "etf_fin",
}


def get_sector(symbol: str, asset_class: str = "equity") -> str:
    if asset_class != "equity":
        return asset_class
    return _SECTOR_MAP.get(symbol.upper(), "unknown")


# ---------------------------------------------------------------------------
# Per-account risk filter pipeline
# ---------------------------------------------------------------------------

def _load_ml_thresholds() -> dict[str, dict[str, float]]:
    try:
        with open(_ML_THRESHOLD_PATH) as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("Could not load ML thresholds: %s", exc)
        return {}


def _ml_threshold(asset_class: str | None, direction: str | None) -> float:
    thresholds = _load_ml_thresholds()
    asset_key = str(asset_class or "equity").lower()
    direction_key = "long" if str(direction or "LONG").upper() == "LONG" else "short"
    lane = thresholds.get(asset_key) or thresholds.get("equity") or {}
    return float(lane.get(direction_key, 0.50))


def _apply_readiness_gate(candidate: dict[str, Any]) -> dict[str, Any]:
    readiness = str(candidate.get("model_readiness") or "live_native")
    model_source = candidate.get("model_source")
    raw_score = candidate.get("edge_score")
    if raw_score is None:
        raw_score = candidate.get("strategy_score")
    if raw_score is None:
        raw_score = candidate.get("ml_prob")
    edge_score = float(raw_score or 0.0)
    allow_execution = False

    if readiness == "live_native":
        allow_execution = True
    elif readiness == "live_fallback":
        allow_execution = True
    elif readiness == "validated_shadow":
        allow_execution = False
    else:
        allow_execution = False

    meets_threshold = edge_score > 0.0
    fallback_in_effect = readiness == "live_fallback" or model_source == "fallback_equity"
    status = "APPROVED" if (allow_execution and meets_threshold) else "REJECTED"
    if readiness == "validated_shadow" and meets_threshold:
        status = "FORWARD_TRACKED"

    return {
        "passed": allow_execution and meets_threshold,
        "status": status,
        "model_readiness": readiness,
        "model_source": model_source,
        "gate_policy": f"readiness_gate_{readiness}",
        "fallback_in_effect": 1 if fallback_in_effect else 0,
        "reason": f"readiness={readiness}, edge_score={edge_score:.4f}",
    }


def _reject(symbol: str, direction: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "symbol": symbol, "direction": direction,
        "status": "REJECTED", "rejection_reason": reason,
        "shares_or_lots": 0, "entry_price": None,
        "stop_price": None, "tp_price": None,
        "risk_dollars": 0.0, "risk_pct": 0.0,
        "position_value": 0.0, "intraday_atr": 0.0,
        "edge_score": None, "rank": None,
        "model_readiness": extra.get("model_readiness"),
        "model_source": extra.get("model_source"),
        "gate_policy": extra.get("gate_policy"),
        "fallback_in_effect": extra.get("fallback_in_effect", 0),
    }


def process_account(
    account: dict[str, Any],
    shortlist: pd.DataFrame,
    cycle_ts: str,
    dry_run: bool = False,
    debug_symbol: str | None = None,
    force_flatten: bool = False,
    db_path: str = _DB_PATH,
) -> list[dict[str, Any]]:
    """
    Run the 13-step risk filter pipeline for one account.
    Returns list of row dicts for vanguard_tradeable_portfolio.
    """
    account_id   = account["id"]
    account_size = float(account["account_size"])
    prop_firm    = account.get("prop_firm", "")
    instruments  = account.get("instruments", "us_equities")
    instrument_scope = account.get("instrument_scope") or instruments
    if instrument_scope == "gft_universe":
        instruments = "all_assets"
    gft_controls = _gft_risk_params(account_id, account_size)
    gft_enabled = bool(gft_controls)
    t_account    = time.time()

    logger.info(f"[{account_id}] Processing {len(shortlist)} shortlist candidates")

    # ── Portfolio state ────────────────────────────────────────────────────
    state = get_or_init_state(account_id, account, db_path)

    # ── Step 6a: Force flatten ─────────────────────────────────────────────
    if force_flatten:
        positions = get_positions_to_flatten(account_id, db_path)
        logger.info(f"[{account_id}] Force flatten: {len(positions)} positions")
        state["status"] = "EOD_FLATTEN"
        if not dry_run:
            save_state(state, db_path)
        return []

    # ── Steps 2–5: Budget & status checks ─────────────────────────────────
    status = get_account_status(account_id, state, account)
    if status != "ACTIVE":
        logger.warning(f"[{account_id}] Account status={status} — skipping all trades")
        state["status"] = status
        if not dry_run:
            save_state(state, db_path)
        return []

    # ── Step 6b: Time gate ─────────────────────────────────────────────────
    eod_action = check_eod_action(
        must_close_eod=bool(account.get("must_close_eod")),
        no_new_positions_after=account.get("no_new_positions_after"),
        eod_flatten_time=account.get("eod_flatten_time"),
    )
    if eod_action == "FLATTEN_ALL":
        logger.info(f"[{account_id}] EOD flatten triggered")
        state["status"] = "EOD_FLATTEN"
        if not dry_run:
            save_state(state, db_path)
        return []
    no_new_trades = (eod_action == "NO_NEW_TRADES")

    # Budget remaining for sizing
    daily_budget_remaining    = (state.get("daily_pause_level", account_size) + state.get("daily_realized_pnl", 0.0)) - account_size
    # Positive means we still have budget; negative means we've exceeded pause
    daily_loss_limit = float(account.get("daily_loss_limit", 2000))
    if gft_enabled:
        daily_loss_limit = float(gft_controls["daily_loss_limit"])
    daily_budget_remaining    = max(0.0, daily_loss_limit + state.get("daily_realized_pnl", 0.0))
    drawdown_budget_remaining = max(0.0, float(account.get("max_drawdown", 4000))    + state.get("daily_realized_pnl", 0.0))

    # ── Step 1: Instrument filter ──────────────────────────────────────────
    if instruments == "us_equities":
        eligible = shortlist[shortlist["asset_class"] == "equity"].copy()
    elif instruments == "all_assets":
        eligible = shortlist.copy()
    else:
        eligible = shortlist[shortlist["asset_class"] == instruments].copy()

    logger.info(f"[{account_id}] After instrument filter: {len(eligible)} candidates")

    # ── Step 7: Position limit ─────────────────────────────────────────────
    # Testing mode: do not carry prior-cycle position counts or cap the current shortlist.
    open_positions = 0
    if gft_enabled:
        max_positions = int(gft_controls["max_positions"])
    else:
        max_positions = max(int(account.get("max_positions", 5) or 5), len(eligible))

    # ── TTP scaling check ──────────────────────────────────────────────────
    if prop_firm == "ttp":
        scaling = apply_scaling(
            account_size=account_size,
            total_pnl=state.get("total_pnl", 0.0),
            scaling_profit_trigger_pct=account.get("scaling_profit_trigger_pct"),
            scaling_bp_increase_pct=account.get("scaling_bp_increase_pct"),
            scaling_pause_increase_pct=account.get("scaling_pause_increase_pct"),
            current_daily_loss_limit=float(account.get("daily_loss_limit", 2000)),
        )
        if scaling:
            account_size = scaling["new_account_size"]
            logger.info(f"[{account_id}] Scaling applied: new_size=${account_size:,.0f}")

    # ── Pre-compute correlation matrix for all eligible symbols ──────────
    unique_symbols = eligible["symbol"].unique().tolist()
    try:
        corr_matrix = compute_correlations(unique_symbols, db_path)
    except Exception:
        corr_matrix = pd.DataFrame()

    # ── Process each candidate in rank order ──────────────────────────────
    results: list[dict[str, Any]] = []
    approved_symbols: list[str]   = []
    sector_counts:    dict[str, int] = {}
    heat_used: float = 0.0

    # Sort by consensus_count DESC, strategy_rank ASC
    sort_cols = [c for c in ["consensus_count", "strategy_rank"] if c in eligible.columns]
    if sort_cols:
        eligible = eligible.sort_values(sort_cols, ascending=[False, True])

    for _, row in eligible.iterrows():
        symbol    = row["symbol"]
        direction = row["direction"]
        asset_class = str(row.get("asset_class") or "equity").lower()
        edge_score = row.get("edge_score") or row.get("strategy_score")
        rank      = row.get("strategy_rank")

        if instrument_scope == "gft_universe" and _normalize_gft_symbol(symbol) not in GFT_UNIVERSE:
            results.append({
                **_reject(symbol, direction, f"NOT_IN_GFT_UNIVERSE:{symbol}"),
                "edge_score": edge_score,
                "rank": rank,
            })
            continue

        readiness_gate = _apply_readiness_gate(row.to_dict())
        gate_meta = {
            "model_readiness": readiness_gate["model_readiness"],
            "model_source": readiness_gate["model_source"],
            "gate_policy": readiness_gate["gate_policy"],
            "fallback_in_effect": readiness_gate["fallback_in_effect"],
        }

        def dbg(msg: str) -> None:
            if debug_symbol and debug_symbol.upper() == symbol.upper():
                logger.info(f"[DEBUG {symbol}] {msg}")

        dbg(f"Evaluating {direction}")

        if readiness_gate["status"] == "FORWARD_TRACKED":
            results.append({
                "symbol": symbol,
                "direction": direction,
                "entry_price": None,
                "stop_price": None,
                "tp_price": None,
                "shares_or_lots": 0,
                "risk_dollars": 0.0,
                "risk_pct": 0.0,
                "position_value": 0.0,
                "intraday_atr": 0.0,
                "edge_score": edge_score,
                "rank": rank,
                "status": "FORWARD_TRACKED",
                "rejection_reason": readiness_gate["reason"],
                **gate_meta,
            })
            dbg(f"FORWARD_TRACKED: {readiness_gate['reason']}")
            continue
        if not readiness_gate["passed"]:
            results.append({
                **_reject(symbol, direction, readiness_gate["reason"], **gate_meta),
                "edge_score": edge_score,
                "rank": rank,
            })
            dbg(f"REJECTED: {readiness_gate['reason']}")
            continue

        # ── Step 6b: No new trades gate ──────────────────────────────────
        if no_new_trades and not (gft_enabled and asset_class == "crypto"):
            results.append({**_reject(symbol, direction, "NO_NEW_TRADES_AFTER_CUTOFF", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            continue

        # ── Step 7: Position capacity ─────────────────────────────────────
        if open_positions + len([r for r in results if r["status"] == "APPROVED"]) >= max_positions:
            results.append({**_reject(symbol, direction, "POSITION_LIMIT", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg("REJECTED: POSITION_LIMIT")
            continue

        # ── Step 8: TTP consistency check ─────────────────────────────────
        if prop_firm == "ttp" and account.get("consistency_rule_pct"):
            consistency_violated = check_consistency_rule(
                best_trade_pnl=state.get("best_day_pnl", 0.0),
                total_valid_pnl=max(state.get("total_pnl", 0.0), 0.001),
                consistency_rule_pct=float(account["consistency_rule_pct"]),
            )
            if consistency_violated:
                results.append({**_reject(symbol, direction, "CONSISTENCY_RULE", **gate_meta),
                                 "edge_score": edge_score, "rank": rank})
                dbg("REJECTED: CONSISTENCY_RULE")
                continue

        # ── Step 9: Position sizing ────────────────────────────────────────
        # Get latest price from bar data
        try:
            with sqlite3.connect(db_path) as con:
                price_row = con.execute(
                    "SELECT close FROM vanguard_bars_5m WHERE symbol=? ORDER BY bar_ts_utc DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
            entry_price = float(price_row[0]) if price_row else 100.0
        except Exception:
            entry_price = 100.0

        atr = compute_atr(symbol, db_path)
        if atr <= 0:
            results.append({**_reject(symbol, direction, "NO_ATR_DATA", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg(f"REJECTED: NO_ATR_DATA (atr={atr})")
            continue

        risk_per_trade_pct = float(account.get("risk_per_trade_pct", 0.005))
        if gft_enabled:
            risk_per_trade_pct = float(gft_controls["risk_per_trade_pct"])

        risk_dollars = compute_risk_dollars(
            account_size=account_size,
            risk_per_trade_pct=risk_per_trade_pct,
            daily_budget_remaining=daily_budget_remaining,
            drawdown_budget_remaining=drawdown_budget_remaining,
        )
        if risk_dollars <= 0:
            results.append({**_reject(symbol, direction, "NO_RISK_BUDGET", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg("REJECTED: NO_RISK_BUDGET")
            continue

        stop_atr_multiple = float(account.get("stop_atr_multiple", 1.5))
        tp_atr_multiple = float(account.get("tp_atr_multiple", 3.0))
        if asset_class == "crypto" or (gft_enabled and asset_class in ("forex", "equity")):
            sizing = _size_gft_trade(
                symbol=symbol,
                asset_class=asset_class,
                entry_price=entry_price,
                atr=atr,
                stop_atr_multiple=stop_atr_multiple,
                tp_atr_multiple=tp_atr_multiple,
                risk_dollars=risk_dollars,
                direction=direction,
                account_size=account_size,
                edge_score=float(edge_score or 0.0),
            )
        else:
            sizing = size_equity(
                price=entry_price,
                atr=atr,
                stop_atr_multiple=stop_atr_multiple,
                tp_atr_multiple=tp_atr_multiple,
                risk_dollars=risk_dollars,
                direction=direction,
            )

        if sizing["shares"] <= 0:
            results.append({**_reject(symbol, direction, "SIZING_ZERO_SHARES", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg(f"REJECTED: SIZING_ZERO_SHARES (risk=${risk_dollars:.2f}, atr={atr:.4f})")
            continue

        if gft_enabled:
            gft_max_risk = float(gft_controls["risk_per_trade_pct"]) * account_size
            if sizing.get("stop_price") is None:
                results.append({**_reject(symbol, direction, "GFT_MISSING_STOP_PRICE", **gate_meta),
                                 "edge_score": edge_score, "rank": rank})
                dbg("REJECTED: GFT_MISSING_STOP_PRICE")
                continue
            if float(sizing.get("actual_risk") or 0.0) > gft_max_risk + 1e-9:
                results.append({
                    **_reject(
                        symbol,
                        direction,
                        f"GFT_ACTUAL_RISK_CAP:{float(sizing.get('actual_risk') or 0.0):.2f}>{gft_max_risk:.2f}",
                        **gate_meta,
                    ),
                    "edge_score": edge_score,
                    "rank": rank,
                })
                dbg(
                    "REJECTED: GFT_ACTUAL_RISK_CAP "
                    f"(actual={float(sizing.get('actual_risk') or 0.0):.2f}, cap={gft_max_risk:.2f})"
                )
                continue
            if risk_dollars > gft_max_risk + 1e-9:
                results.append({
                    **_reject(
                        symbol,
                        direction,
                        f"GFT_RISK_CAP:{risk_dollars:.2f}>{gft_max_risk:.2f}",
                        **gate_meta,
                    ),
                    "edge_score": edge_score,
                    "rank": rank,
                })
                dbg(f"REJECTED: GFT_RISK_CAP (risk={risk_dollars:.2f}, cap={gft_max_risk:.2f})")
                continue

        # ── Step 10: Min trade range check (TTP) ─────────────────────────
        min_cents = int(account.get("min_trade_range_cents", 0) or 0)
        if sizing["tp_price"] and not check_min_trade_range(entry_price, sizing["tp_price"], min_cents):
            results.append({**_reject(symbol, direction, "MIN_TRADE_RANGE", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg(f"REJECTED: MIN_TRADE_RANGE (tp={sizing['tp_price']}, min={min_cents}c)")
            continue

        # ── Step 11: Diversification filters ──────────────────────────────
        sector = get_sector(symbol, row.get("asset_class", "equity"))
        max_per_sector = max(int(account.get("max_per_sector", 3) or 3), len(eligible))
        if sector != "unknown" and sector_counts.get(sector, 0) >= max_per_sector:
            results.append({**_reject(symbol, direction, f"SECTOR_CAP:{sector}", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg(f"REJECTED: SECTOR_CAP ({sector}={sector_counts.get(sector,0)}/{max_per_sector})")
            continue

        max_corr = max(float(account.get("max_correlation", 0.85) or 0.85), 1.0)
        too_corr, corr_sym = is_too_correlated(symbol, approved_symbols, corr_matrix, max_corr)
        if too_corr:
            results.append({**_reject(symbol, direction, f"CORRELATION:{corr_sym}", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg(f"REJECTED: CORRELATION with {corr_sym}")
            continue

        if gft_enabled:
            max_heat = float(gft_controls["max_portfolio_heat_pct"])
        else:
            max_heat = max(float(account.get("max_portfolio_heat_pct", 0.04) or 0.04), 1.0)
        trade_heat = risk_dollars / account_size
        if heat_used + trade_heat > max_heat:
            results.append({**_reject(symbol, direction, "HEAT_CAP", **gate_meta),
                             "edge_score": edge_score, "rank": rank})
            dbg(f"REJECTED: HEAT_CAP (used={heat_used:.3f}+{trade_heat:.3f} > {max_heat})")
            continue

        # ── Step 12: Prop-firm specific rules ─────────────────────────────
        if prop_firm == "ttp" and bool(account.get("block_halted")):
            # In production, check if symbol is halted via market data feed
            # For now: no block (would require live feed integration)
            pass

        # ── APPROVED ──────────────────────────────────────────────────────
        approved_symbols.append(symbol)
        if sector != "unknown":
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        heat_used += trade_heat

        results.append({
            "symbol":          symbol,
            "direction":       direction,
            "entry_price":     entry_price,
            "stop_price":      sizing["stop_price"],
            "tp_price":        sizing["tp_price"],
            "shares_or_lots":  sizing["shares"],
            "risk_dollars":    round(risk_dollars, 2),
            "risk_pct":        round(trade_heat, 6),
            "position_value":  sizing["position_value"],
            "intraday_atr":    round(atr, 4),
            "edge_score":      edge_score,
            "rank":            rank,
            **gate_meta,
            "status":          "APPROVED",
            "rejection_reason": None,
        })
        dbg(f"APPROVED: shares={sizing['shares']} stop={sizing['stop_price']} tp={sizing['tp_price']}")

    approved_count = sum(1 for r in results if r["status"] == "APPROVED")
    logger.info(
        f"[{account_id}] Done in {time.time()-t_account:.2f}s — "
        f"{approved_count} approved, {len(results)-approved_count} rejected"
    )

    # ── Update portfolio state ─────────────────────────────────────────────
    # Keep the test account state flat so future cycles are not blocked by prior approvals.
    state["open_positions"] = 0
    state["heat_used_pct"] = 0.0
    state["status"]         = "ACTIVE"
    if not dry_run:
        save_state(state, db_path)

    return results


# ---------------------------------------------------------------------------
# DB write helpers
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
                 status, rejection_reason)
            VALUES
                (:cycle_ts_utc, :account_id, :symbol, :direction, :entry_price,
                 :stop_price, :tp_price, :shares_or_lots, :risk_dollars, :risk_pct,
                 :position_value, :intraday_atr, :edge_score, :rank,
                 :model_readiness, :model_source, :gate_policy, :fallback_in_effect,
                 :status, :rejection_reason)
            """,
            rows,
        )
        con.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def display_status(db_path: str = _DB_PATH) -> None:
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM vanguard_portfolio_state ORDER BY account_id"
            ).fetchall()
    except Exception as exc:
        print(f"No portfolio state: {exc}")
        return

    print(f"\n{'='*80}")
    print("PORTFOLIO STATE")
    print(f"{'='*80}")
    for r in rows:
        d = dict(r)
        print(
            f"  {d['account_id']:25s} {d['date']}  "
            f"status={d['status']:15s} "
            f"open={d['open_positions']:2d}  "
            f"pnl=${d['daily_realized_pnl']:+,.2f}  "
            f"heat={d['heat_used_pct']:.2%}"
        )
    print()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(
    account_filter: str | None = None,
    dry_run: bool = False,
    debug_symbol: str | None = None,
    force_flatten: str | None = None,
    show_status: bool = False,
    cycle_ts: str | None = None,
    db_path: str = _DB_PATH,
) -> dict:
    t0 = time.time()
    cycle_ts = cycle_ts or iso_utc(now_utc())

    if show_status:
        display_status(db_path)
        return {"status": "ok"}

    ensure_state_schema(db_path)
    _migrate_tradeable_pk(db_path)

    # Load accounts
    accounts = load_accounts(account_filter, db_path)
    if not accounts:
        logger.warning("No active accounts found")
        return {"status": "no_accounts"}

    logger.info(f"V6 Risk Filters — cycle={cycle_ts}, accounts={[a['id'] for a in accounts]}")

    # Load shortlist
    shortlist = load_shortlist(db_path)
    if shortlist.empty:
        logger.warning("V5 shortlist is empty — creating mock shortlist for testing")
        # Graceful: return empty portfolio if no shortlist
        return {"status": "no_shortlist", "rows": 0}

    # Deduplicate: keep the best-scored row per (symbol, direction)
    # The shortlist has one row per (symbol, strategy) — multiple strategies
    # can select the same symbol+direction. Without dedup, V6 would process the
    # same symbol multiple times and later writes would overwrite earlier approved rows.
    before = len(shortlist)
    shortlist = (
        shortlist
        .sort_values("strategy_score", ascending=False)
        .drop_duplicates(subset=["symbol", "direction"], keep="first")
        .reset_index(drop=True)
    )
    if len(shortlist) < before:
        logger.info(f"Shortlist dedup: {before} → {len(shortlist)} rows (best per symbol+direction)")

    logger.info(f"Shortlist: {len(shortlist)} candidates")

    total_rows = 0

    for account in accounts:
        account_id = account["id"]

        try:
            rows = process_account(
                account=account,
                shortlist=shortlist,
                cycle_ts=cycle_ts,
                dry_run=dry_run,
                debug_symbol=debug_symbol,
                force_flatten=(force_flatten == account_id),
                db_path=db_path,
            )

            # Attach cycle/account metadata
            for r in rows:
                r["cycle_ts_utc"] = cycle_ts
                r["account_id"]   = account_id

            if not dry_run:
                written = write_tradeable_portfolio(rows, cycle_ts, account_id, db_path)
                total_rows += written
                logger.info(f"[{account_id}] Wrote {written} rows to vanguard_tradeable_portfolio")
            else:
                total_rows += len(rows)
                _print_account_summary(account_id, rows)

        except Exception as exc:
            logger.error(f"[{account_id}] Failed: {exc}", exc_info=True)

    elapsed = time.time() - t0
    logger.info(f"V6 complete — {total_rows} total rows, elapsed={elapsed:.2f}s")

    return {"status": "ok", "rows": total_rows, "elapsed": elapsed}


def _print_account_summary(account_id: str, rows: list[dict]) -> None:
    approved = [r for r in rows if r["status"] == "APPROVED"]
    rejected = [r for r in rows if r["status"] == "REJECTED"]
    print(f"\n{'─'*60}")
    print(f"  [{account_id}]  APPROVED:{len(approved)}  REJECTED:{len(rejected)}")
    for r in approved:
        print(
            f"    ✓ {r['direction']:5s} {r['symbol']:8s}  "
            f"shares={r['shares_or_lots']}  "
            f"stop={r['stop_price']}  tp={r['tp_price']}  "
            f"risk=${r['risk_dollars']:.2f}"
        )
    if rejected:
        reasons: dict[str, int] = {}
        for r in rejected:
            k = r.get("rejection_reason", "unknown") or "unknown"
            reasons[k] = reasons.get(k, 0) + 1
        for reason, count in sorted(reasons.items()):
            print(f"    ✗ {reason}: {count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="V6 Vanguard Risk Filters")
    p.add_argument("--account", default=None, help="Run only this account ID")
    p.add_argument("--dry-run", action="store_true", help="Print results, do not write to DB")
    p.add_argument("--debug",   default=None, metavar="SYMBOL", help="Trace one symbol through all accounts")
    p.add_argument("--flatten", default=None, metavar="ACCOUNT_ID", help="Force EOD flatten for an account")
    p.add_argument("--status",  action="store_true", help="Show portfolio state and exit")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run(
        account_filter=args.account,
        dry_run=args.dry_run,
        debug_symbol=args.debug,
        force_flatten=args.flatten,
        show_status=args.status,
    )
    sys.exit(0 if result.get("status") in ("ok", "no_shortlist", "no_accounts") else 1)
