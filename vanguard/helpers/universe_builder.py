"""
universe_builder.py — Vanguard Universe Builder.

Loads predefined instrument universes from config/vanguard_universes.json
and provides them to V1 (cache) and V2 (prefilter).

Public API:
    load_universes(config_path)                   → loads and caches the JSON config
    get_universe(universe_name)                   → instrument list for a universe
    get_equity_universe(alpaca_adapter=None)      → TTP equity symbols (dynamic, from Alpaca)
    get_ftmo_universe()                           → flat list of 165 FTMO CFD symbols
    get_topstep_universe()                        → 42 TopStep futures with tick_size/value/margin
    get_instruments_for_account(account_id)       → correct universe for an account
    classify_asset_class(symbol)                  → "equity"|"forex"|"index"|"metal"|...
    refresh_to_db(universe_name, db, now_utc_str) → write members to vanguard_universe_members
    get_health_thresholds(universe_name)          → per-universe prefilter thresholds

CLI:
    python3 -m vanguard.helpers.universe_builder --list ttp_equity
    python3 -m vanguard.helpers.universe_builder --list ftmo_cfd
    python3 -m vanguard.helpers.universe_builder --list topstep_futures
    python3 -m vanguard.helpers.universe_builder --refresh ttp_equity
    python3 -m vanguard.helpers.universe_builder --classify EURUSD
    python3 -m vanguard.helpers.universe_builder --summary

Location: ~/SS/Vanguard/vanguard/helpers/universe_builder.py
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vanguard.config.runtime_config import (
    resolve_market_data_source_label,
    get_profiles_config,
    get_shadow_db_path,
    get_universes_config,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT     = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "vanguard_universes.json"
_DEFAULT_ACCOUNTS = _REPO_ROOT / "config" / "vanguard_accounts.json"
_TD_SYMBOLS_CONFIG = _REPO_ROOT / "config" / "twelvedata_symbols.json"
_FUTURES_CONFIG_CANDIDATES = (
    _REPO_ROOT / "docs" / "ftmo_universe_with_futures.json",
    _REPO_ROOT.parent / "ftmo_universe_with_futures.json",
)
_DB_PATH = get_shadow_db_path()

# ---------------------------------------------------------------------------
# Module-level state (loaded lazily)
# ---------------------------------------------------------------------------

_universes: dict | None = None
_accounts:  dict | None = None

# ---------------------------------------------------------------------------
# Asset-class mapping for classify_asset_class()
# Built lazily from static universe sections.
# ---------------------------------------------------------------------------
_asset_class_map: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_universes(config_path: str | Path = _DEFAULT_CONFIG) -> dict:
    """
    Load and return the universes config from JSON.
    Caches the result at module level so subsequent calls are free.

    Parameters
    ----------
    config_path : path to vanguard_universes.json

    Returns
    -------
    dict with key "universes" mapping name → universe definition
    """
    global _universes, _asset_class_map
    if Path(config_path) == Path(_DEFAULT_CONFIG):
        config_path = Path(config_path)
        legacy_data = {}
        if config_path.exists():
            with config_path.open() as f:
                legacy_data = json.load(f).get("universes", {})
        runtime_universes = get_universes_config()
        _universes = {**legacy_data, **runtime_universes}
    else:
        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Universe config not found: {config_path}")
        with config_path.open() as f:
            data = json.load(f)
        _universes = data.get("universes", {})
    _asset_class_map = None   # invalidate on reload
    logger.debug("Loaded %d universes from %s", len(_universes), config_path)
    return _universes


def _ensure_loaded() -> dict:
    """Return the cached universes dict, loading defaults if needed."""
    global _universes
    if _universes is None:
        load_universes()
    return _universes  # type: ignore[return-value]


def _load_accounts(accounts_path: str | Path = _DEFAULT_ACCOUNTS) -> dict:
    """Load and cache the accounts config."""
    global _accounts
    if _accounts is None:
        if Path(accounts_path) == Path(_DEFAULT_ACCOUNTS):
            accounts_path = Path(accounts_path)
            merged_accounts: list[dict[str, Any]] = []
            if accounts_path.exists():
                with accounts_path.open() as f:
                    merged_accounts.extend(list((json.load(f) or {}).get("accounts") or []))
            seen = {str(acc.get("account_id") or "") for acc in merged_accounts}
            for p in get_profiles_config():
                account_id = str(p.get("id") or "")
                if not account_id or account_id in seen:
                    continue
                prop_firm = str(p.get("prop_firm") or account_id.split("_", 1)[0]).lower()
                merged_accounts.append(
                    {
                        "account_id": account_id,
                        "prop_firm": prop_firm,
                        "enabled": bool(p.get("is_active", True)),
                        "execution_mode": p.get("execution_mode"),
                        "execution_bridge": p.get("execution_bridge"),
                    }
                )
            _accounts = {"accounts": merged_accounts}
        else:
            accounts_path = Path(accounts_path)
            if not accounts_path.exists():
                raise FileNotFoundError(f"Accounts config not found: {accounts_path}")
            with accounts_path.open() as f:
                _accounts = json.load(f)
    return _accounts


# ---------------------------------------------------------------------------
# Asset-class reverse mapping builder
# ---------------------------------------------------------------------------

def _build_asset_class_map() -> dict[str, str]:
    """
    Build {symbol.upper() → asset_class} reverse lookup.

    Priority (highest first):
    1. Runtime config universes (gft_universe and any other JSON-configured
       universe). This is authoritative for all GFT symbols.
    2. FTMO CFD legacy static universe (ftmo_cfd).
    3. TopStep futures legacy static universe (topstep_futures).

    Equity universe is dynamic — equity symbols are not enumerated here.
    """
    global _asset_class_map
    if _asset_class_map is not None:
        return _asset_class_map

    mapping: dict[str, str] = {}

    # ── 1. Runtime config universes (authoritative for GFT and any JSON-defined universe) ──
    try:
        from vanguard.config.runtime_config import get_runtime_config
        rt_universes = get_runtime_config().get("universes") or {}
        for universe_def in rt_universes.values():
            symbols_by_class = universe_def.get("symbols") or {}
            if not isinstance(symbols_by_class, dict):
                continue
            for asset_class, symbol_list in symbols_by_class.items():
                if not isinstance(symbol_list, list):
                    continue
                for sym in symbol_list:
                    mapping[str(sym).upper()] = str(asset_class)
    except Exception as exc:
        logger.warning("_build_asset_class_map: could not load runtime config: %s", exc)

    # ── 2. FTMO CFDs (legacy; don't overwrite runtime-config entries) ──
    universes = _ensure_loaded()
    ftmo = universes.get("ftmo_cfd", {})
    ftmo_instruments = ftmo.get("instruments", {})
    for asset_class, symbols in ftmo_instruments.items():
        ac = "equity" if asset_class == "equity_cfd" else asset_class
        for sym in symbols:
            s = sym.upper()
            if s not in mapping:
                mapping[s] = ac

    # ── 3. TopStep futures (legacy; don't overwrite) ──
    ts = universes.get("topstep_futures", {})
    ts_instruments = ts.get("instruments", {})
    for asset_class, contracts in ts_instruments.items():
        for contract in contracts:
            s = contract["symbol"].upper()
            if s not in mapping:
                mapping[s] = asset_class

    _asset_class_map = mapping
    return _asset_class_map


# ---------------------------------------------------------------------------
# Core public functions
# ---------------------------------------------------------------------------

def get_universe(universe_name: str) -> list:
    """
    Return the instrument list for a named universe.

    For "ttp_equity": returns the filter_rules dict (dynamic — call
    get_equity_universe() to resolve the actual symbol list via Alpaca).
    For "ftmo_cfd": returns the flat list of all FTMO symbols.
    For "topstep_futures": returns the list of contract dicts.

    Parameters
    ----------
    universe_name : "ttp_equity", "ftmo_cfd", or "topstep_futures"

    Returns
    -------
    list of symbols (str) or contract dicts
    """
    universes = _ensure_loaded()
    if universe_name not in universes:
        raise KeyError(f"Unknown universe: {universe_name!r}. "
                       f"Available: {sorted(universes.keys())}")

    if universe_name == "ttp_equity":
        rules = (
            universes[universe_name].get("filter_rules")
            or universes[universe_name].get("filters")
            or {}
        )
        return list(rules.keys())

    if universe_name == "ftmo_cfd":
        return get_ftmo_universe()

    if universe_name == "topstep_futures":
        return get_topstep_universe()

    # Generic fallback: return whatever is in "instruments" as a flat list
    u = universes[universe_name]
    instruments = u.get("instruments") or u.get("symbols") or {}
    if isinstance(instruments, list):
        return list(instruments)
    result = []
    for items in instruments.values():
        if items and isinstance(items[0], dict):
            result.extend(items)
        else:
            result.extend(items)
    return result


def get_equity_universe(alpaca_adapter: Any | None = None) -> list[str]:
    """
    Return the TTP equity universe by calling Alpaca with the filter rules
    from config.

    Parameters
    ----------
    alpaca_adapter : an AlpacaAdapter instance; if None, one is created using
                     ALPACA_KEY / ALPACA_SECRET environment variables.

    Returns
    -------
    list of uppercase ticker strings
    """
    universes = _ensure_loaded()
    ttp_config = universes.get("ttp_equity", {})
    rules = ttp_config.get("filter_rules") or ttp_config.get("filters") or {}

    min_price    = rules.get("min_price",     2.0)
    min_avg_vol  = rules.get("min_avg_volume", 500_000)
    lookback     = rules.get("lookback_days",  10)

    if alpaca_adapter is None:
        # Lazy import to avoid hard dependency when only used for static universes
        from vanguard.data_adapters.alpaca_adapter import AlpacaAdapter
        alpaca_adapter = AlpacaAdapter()

    symbols = alpaca_adapter.build_equity_universe(
        min_price=min_price,
        min_avg_volume=min_avg_vol,
        lookback_days=lookback,
    )
    logger.info(
        "get_equity_universe: %d symbols (min_price=%.2f, min_vol=%s, lookback=%dd)",
        len(symbols), min_price, f"{min_avg_vol:,}", lookback,
    )
    return symbols


def get_ftmo_universe() -> list[str]:
    """
    Return the flat list of all FTMO CFD symbols (165 instruments).
    Order: forex, index, metal, energy, agriculture, equity_cfd, crypto.

    Returns
    -------
    list of uppercase symbol strings
    """
    universes = _ensure_loaded()
    ftmo = universes.get("ftmo_cfd", {})
    instruments = ftmo.get("instruments") or ftmo.get("symbols") or {}
    if isinstance(instruments, list):
        return [s.upper() for s in instruments]

    symbols: list[str] = []
    for _asset_class, syms in instruments.items():
        symbols.extend(s.upper() for s in syms)
    return symbols


def get_topstep_universe() -> list[dict]:
    """
    Return the list of TopStep futures contracts with tick metadata.
    Each entry is a dict with keys:
        symbol, name, exchange, asset_class, tick_size, tick_value, margin

    Returns
    -------
    list of 42 contract dicts
    """
    universes = _ensure_loaded()
    ts = universes.get("topstep_futures", {})
    instruments = ts.get("instruments", {})

    result: list[dict] = []
    for asset_class, contracts in instruments.items():
        for contract in contracts:
            entry = dict(contract)
            entry["asset_class"] = asset_class
            result.append(entry)
    return result


def get_instruments_for_account(
    account_id: str,
    accounts_path: str | Path = _DEFAULT_ACCOUNTS,
) -> list:
    """
    Read the accounts config, find the account by ID, and return its universe.

    Mapping:
        prop_firm "ttp"                 → ttp_equity (dynamic — returns filter rules)
        prop_firm "ftmo"                → ftmo_cfd   (returns flat symbol list)
        prop_firm "topstep"/"apex"/etc  → topstep_futures (returns contract dicts)

    Parameters
    ----------
    account_id    : matches "account_id" field in vanguard_accounts.json
    accounts_path : path to vanguard_accounts.json

    Returns
    -------
    For TTP    : list[str] via get_equity_universe() — requires ALPACA env vars
    For FTMO   : list[str] via get_ftmo_universe()
    For TopStep: list[dict] via get_topstep_universe()

    Raises
    ------
    KeyError if account_id is not found.
    """
    accounts_data = _load_accounts(accounts_path)
    accounts_list = accounts_data.get("accounts", [])

    account: dict | None = None
    for acc in accounts_list:
        if acc.get("account_id") == account_id:
            account = acc
            break

    if account is None:
        raise KeyError(
            f"Account '{account_id}' not found. "
            f"Available: {[a.get('account_id') for a in accounts_list]}"
        )

    prop_firm = account.get("prop_firm", "").lower()

    if prop_firm == "ttp":
        return get_equity_universe()
    elif prop_firm == "ftmo":
        return get_ftmo_universe()
    elif prop_firm in ("topstep", "apex", "tradeday", "elitetrader"):
        return get_topstep_universe()
    else:
        raise ValueError(
            f"Unknown prop_firm '{prop_firm}' for account '{account_id}'. "
            f"Supported: ttp, ftmo, topstep, apex, tradeday"
        )


def classify_asset_class(symbol: str) -> str:
    """
    Return the asset class for a symbol based on its universe membership.

    Asset classes:
        "equity"        — US equities (TTP equity universe or equity CFDs)
        "forex"         — Spot forex pairs (EURUSD, GBPUSD, etc.)
        "index"         — Index CFDs / index futures (US500.cash, ES, NQ)
        "metal"         — Metals (XAUUSD, GC, SI, etc.)
        "crypto"        — Crypto CFDs (BTCUSD, ETHUSD, etc.)
        "energy"        — Energy (USOIL.cash, CL, NG, etc.)
        "agriculture"   — Agriculture (CORN.c, ZC, ZS, etc.)
        "interest_rate" — Treasury futures (ZB, ZN, ZF, etc.)

    Parameters
    ----------
    symbol : ticker string (case-insensitive)

    Returns
    -------
    asset class string; defaults to "equity" for unknown symbols
    """
    # Canonicalize: strip broker suffix (.x, .X), slashes
    sym = (
        str(symbol).upper()
        .replace("/", "")
        .replace(".X", "")
        .replace(".x", "")
    )
    if sym.startswith("XAU") or sym.startswith("XAG") or sym.startswith("XPT") or sym.startswith("XPD"):
        return "metal"
    if sym in {"USOIL.CASH", "UKOIL.CASH", "NATGAS.CASH", "BRENT", "WTI", "CL", "NG", "QM"}:
        return "energy"
    if sym.endswith(".CASH") or sym in {"SPX500", "NAS100", "US30", "GER40", "UK100", "JAP225", "AUS200"}:
        return "index"
    if sym.endswith(".C") or sym in {"CORN", "SOYBEAN", "WHEAT", "SUGAR", "COCOA", "COFFEE"}:
        return "agriculture"
    if sym in {"ZB", "ZN", "ZF", "ZT", "UB"}:
        return "interest_rate"
    if sym in {"6E", "6B", "6J", "6A", "6C", "6N", "6S", "M6E", "M6A", "M6B", "M6J"}:
        return "forex"

    mapping = _build_asset_class_map()
    if sym in mapping:
        return mapping[sym]

    # Heuristics for common patterns not in static lists
    if "USD" in sym and len(sym) == 6:
        # 6-char FX pair like EURUSD, GBPUSD
        return "forex"
    if sym in ("ES", "NQ", "YM", "RTY"):
        return "index"
    if sym.startswith("BTC") or sym.startswith("ETH") or sym.endswith("USD") and len(sym) > 6:
        return "crypto"

    # Default: assume equity (TTP universe is not enumerated statically)
    return "equity"


def get_health_thresholds(universe_name: str) -> dict:
    """
    Return the per-universe prefilter thresholds dict.
    Falls back to ttp_equity defaults if the universe is unknown.

    Keys returned:
        max_stale_minutes, min_rel_vol, max_spread_pct,
        min_bars_warmup, session_avg_window

    Parameters
    ----------
    universe_name : universe key string

    Returns
    -------
    dict of threshold values
    """
    universes = _ensure_loaded()
    clean_name = str(universe_name or "").split("(", 1)[0]
    u = universes.get(universe_name, {}) or universes.get(clean_name, {})
    defaults = universes.get("ttp_equity", {}).get("health_thresholds", {})
    thresholds = u.get("health_thresholds", {})
    # Merge: universe-specific values override defaults
    return {**defaults, **thresholds}


def load_twelvedata_symbols(config_path: str | Path = _TD_SYMBOLS_CONFIG) -> dict[str, list[str]]:
    """Load Twelve Data symbol groups from config."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Twelve Data symbol config not found: {config_path}")
    with config_path.open() as f:
        data = json.load(f)
    return {
        asset_class: symbols
        for asset_class, symbols in data.items()
        if not str(asset_class).startswith("_") and isinstance(symbols, list)
    }


def load_futures_config() -> dict[str, Any] | None:
    """Load staged futures config if present."""
    for path in _FUTURES_CONFIG_CANDIDATES:
        if path.exists():
            with path.open() as f:
                return json.load(f)
    return None


def _runtime_data_source_for_asset_class(asset_class: str, *, fallback: str = "unknown") -> str:
    resolved = resolve_market_data_source_label(asset_class)
    if resolved and resolved != "unknown":
        return resolved
    return fallback


def _parse_money_value(value: Any) -> float | None:
    """Convert '$12.50/tick' style metadata into a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    cleaned = (
        text.replace("$", "")
        .replace("/tick", "")
        .replace("/point", "")
        .replace(",", "")
        .strip()
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def materialize_universe_members(
    db: Any,
    now_utc_str: str | None = None,
    alpaca_adapter: Any | None = None,
    skip_equity: bool = False,
    equity_symbols_override: list[str] | None = None,
) -> int:
    """
    Populate vanguard_universe_members from all configured sources.

    Live rows:
      - Alpaca equities (skipped when skip_equity=True or overridden by equity_symbols_override)
      - Twelve Data non-equities
    Staged rows:
      - Futures from ftmo_universe_with_futures.json (inactive)

    Parameters
    ----------
    skip_equity             : When True, skip the Alpaca equity fetch entirely (Bug 9: OOS).
    equity_symbols_override : When set, use this explicit list instead of calling Alpaca (Bug 9: enforce mode).
    """
    if now_utc_str is None:
        now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows: list[dict] = []

    if skip_equity:
        equity_symbols: list[str] = []
        logger.info("materialize_universe_members: equity skipped (skip_equity=True)")
    elif equity_symbols_override is not None:
        equity_symbols = [str(s).upper() for s in equity_symbols_override]
        logger.info(
            "materialize_universe_members: using %d override equity symbols (enforce mode)",
            len(equity_symbols),
        )
    else:
        try:
            equity_symbols = get_equity_universe(alpaca_adapter)
        except Exception as exc:
            logger.warning(
                "materialize_universe_members: dynamic equity fetch failed (%s) — "
                "falling back to cached Alpaca symbols from DB",
                exc,
            )
            with db.connect() as conn:
                cached = conn.execute(
                    """
                    SELECT symbol
                    FROM vanguard_universe_members
                    WHERE data_source = 'alpaca' AND is_active = 1
                    ORDER BY symbol
                    """
                ).fetchall()
                if not cached:
                    latest_cycle = conn.execute(
                        "SELECT MAX(cycle_ts_utc) FROM vanguard_health"
                    ).fetchone()[0]
                    if latest_cycle:
                        cached = conn.execute(
                            """
                            SELECT symbol
                            FROM vanguard_health
                            WHERE cycle_ts_utc = ? AND status = 'ACTIVE'
                            ORDER BY symbol
                            """,
                            (latest_cycle,),
                        ).fetchall()
                if not cached:
                    cached = conn.execute(
                        """
                        SELECT DISTINCT symbol
                        FROM vanguard_bars_5m
                        WHERE asset_class = 'equity' AND data_source = 'alpaca'
                        ORDER BY symbol
                        """
                    ).fetchall()
            equity_symbols = [row[0] for row in cached]

    for symbol in equity_symbols:
        rows.append({
            "symbol": symbol.upper(),
            "asset_class": "equity",
            "data_source": "alpaca",
            "universe": "ttp_equities",
            "exchange": None,
            "session_start": "09:30",
            "session_end": "16:00",
            "session_tz": "America/New_York",
            "is_active": 1,
            "added_at": now_utc_str,
            "last_seen_at": now_utc_str,
        })

    td_groups = load_twelvedata_symbols()
    for asset_class, symbols in td_groups.items():
        data_source = _runtime_data_source_for_asset_class(asset_class, fallback="twelvedata")
        for symbol in symbols:
            rows.append({
                "symbol": str(symbol).upper(),
                "asset_class": asset_class,
                "data_source": data_source,
                "universe": f"ftmo_{asset_class}",
                "exchange": None,
                "session_start": None,
                "session_end": None,
                "session_tz": "America/New_York",
                "is_active": 1,
                "added_at": now_utc_str,
                "last_seen_at": now_utc_str,
            })

    futures_cfg = load_futures_config() or {}
    futures_instruments = futures_cfg.get("futures_instruments", {})
    for group_name, group in futures_instruments.items():
        if not isinstance(group, dict):
            continue
        for instrument in group.get("symbols", []):
            if not isinstance(instrument, dict):
                continue
            session = group.get("session") or {}
            if isinstance(session, dict):
                session_start = session.get("start")
                session_end = session.get("end")
                session_tz = session.get("tz", "America/New_York")
            else:
                session_start = None
                session_end = None
                session_tz = "America/New_York"
            rows.append({
                "symbol": str(instrument.get("symbol", "")).upper(),
                "asset_class": "futures",
                "data_source": "ibkr",
                "universe": "topstep_futures",
                "exchange": instrument.get("exchange"),
                "tick_size": _parse_money_value(instrument.get("tick_size")),
                "tick_value": _parse_money_value(instrument.get("tick_value")),
                "point_value": _parse_money_value(instrument.get("point_value")),
                "margin": _parse_money_value(instrument.get("margin")),
                "session_start": session_start,
                "session_end": session_end,
                "session_tz": session_tz,
                "is_active": 0,
                "added_at": now_utc_str,
                "last_seen_at": now_utc_str,
            })

    written = db.upsert_universe_members(rows)
    logger.info("materialize_universe_members: wrote %d rows", written)
    return written


# ---------------------------------------------------------------------------
# DB refresh
# ---------------------------------------------------------------------------

def refresh_to_db(
    universe_name: str,
    db: Any,
    now_utc_str: str,
    alpaca_adapter: Any | None = None,
) -> int:
    """
    Write (or refresh) all members of a universe to vanguard_universe_members.

    Parameters
    ----------
    universe_name   : "ttp_equity", "ftmo_cfd", or "topstep_futures"
    db              : VanguardDB instance
    now_utc_str     : ISO UTC timestamp string for last_refreshed_utc
    alpaca_adapter  : only needed for "ttp_equity"

    Returns
    -------
    number of rows written
    """
    rows: list[dict] = []

    if universe_name == "ttp_equity":
        symbols = get_equity_universe(alpaca_adapter)
        for sym in symbols:
            rows.append({
                "symbol":             sym,
                "data_source":        "alpaca",
                "universe":           universe_name,
                "asset_class":        "equity",
                "tick_size":          None,
                "tick_value":         None,
                "point_value":        None,
                "margin":             None,
                "session_start":      "09:30",
                "session_end":        "16:00",
                "session_tz":         "America/New_York",
                "is_active":          1,
                "added_at":           now_utc_str,
                "last_seen_at":       now_utc_str,
            })

    elif universe_name == "ftmo_cfd":
        universes = _ensure_loaded()
        instruments = universes.get("ftmo_cfd", {}).get("instruments", {})
        for asset_class, syms in instruments.items():
            ac = "equity" if asset_class == "equity_cfd" else asset_class
            data_source = _runtime_data_source_for_asset_class(ac, fallback="twelvedata")
            for sym in syms:
                rows.append({
                    "symbol":             sym.upper(),
                    "data_source":        data_source,
                    "universe":           universe_name,
                    "asset_class":        ac,
                    "tick_size":          None,
                    "tick_value":         None,
                    "point_value":        None,
                    "margin":             None,
                    "session_start":      None,
                    "session_end":        None,
                    "session_tz":         "America/New_York",
                    "is_active":          1,
                    "added_at":           now_utc_str,
                    "last_seen_at":       now_utc_str,
                })

    elif universe_name == "topstep_futures":
        contracts = get_topstep_universe()
        for c in contracts:
                rows.append({
                    "symbol":             c["symbol"].upper(),
                    "data_source":        "ibkr",
                    "universe":           universe_name,
                    "asset_class":        c["asset_class"],
                    "exchange":           c.get("exchange"),
                    "tick_size":          c.get("tick_size"),
                    "tick_value":         c.get("tick_value"),
                    "point_value":        c.get("point_value"),
                    "margin":             c.get("margin"),
                    "session_start":      None,
                    "session_end":        None,
                    "session_tz":         "America/New_York",
                    "is_active":          0,
                    "added_at":           now_utc_str,
                    "last_seen_at":       now_utc_str,
                })

    else:
        raise ValueError(f"refresh_to_db: unknown universe '{universe_name}'")

    written = db.upsert_universe_members(rows)
    logger.info("refresh_to_db: wrote %d rows for universe '%s'", written, universe_name)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Vanguard Universe Builder — inspect and refresh instrument universes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m vanguard.helpers.universe_builder --list ttp_equity
  python3 -m vanguard.helpers.universe_builder --list ftmo_cfd
  python3 -m vanguard.helpers.universe_builder --list topstep_futures
  python3 -m vanguard.helpers.universe_builder --refresh ttp_equity
  python3 -m vanguard.helpers.universe_builder --classify EURUSD
  python3 -m vanguard.helpers.universe_builder --summary
""",
    )
    p.add_argument(
        "--list",
        metavar="UNIVERSE",
        default=None,
        help="Show instruments in a universe (ttp_equity|ftmo_cfd|topstep_futures)",
    )
    p.add_argument(
        "--refresh",
        metavar="UNIVERSE",
        default=None,
        help="Refresh universe members in DB from source",
    )
    p.add_argument(
        "--classify",
        metavar="SYMBOL",
        default=None,
        help="Classify a symbol's asset class",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print symbol counts per universe from the config",
    )
    p.add_argument(
        "--config",
        default=str(_DEFAULT_CONFIG),
        help=f"Path to vanguard_universes.json (default: {_DEFAULT_CONFIG})",
    )
    return p


def _cmd_list(universe_name: str) -> None:
    if universe_name == "topstep_futures":
        contracts = get_topstep_universe()
        print(f"\n=== topstep_futures ({len(contracts)} contracts) ===")
        print(f"  {'Symbol':<8} {'Asset Class':<16} {'Tick Size':<14} "
              f"{'Tick Value':>12} {'Margin':>10}  Name")
        print("  " + "-" * 80)
        for c in contracts:
            print(
                f"  {c['symbol']:<8} {c['asset_class']:<16} "
                f"{c['tick_size']:<14} {c['tick_value']:>12.4f} "
                f"{c['margin']:>10.0f}  {c.get('name', '')}"
            )
    elif universe_name == "ftmo_cfd":
        symbols = get_ftmo_universe()
        print(f"\n=== ftmo_cfd ({len(symbols)} symbols) ===")
        col = 6
        for i, sym in enumerate(symbols):
            end = "\n" if (i + 1) % col == 0 else "  "
            print(f"  {sym:<18}", end=end)
        print()
    elif universe_name == "ttp_equity":
        universes = _ensure_loaded()
        rules = universes["ttp_equity"].get("filter_rules", {})
        print("\n=== ttp_equity (dynamic — resolved from Alpaca) ===")
        print("  Filter rules:")
        for k, v in rules.items():
            print(f"    {k}: {v}")
        print("\n  (Run --refresh ttp_equity to fetch and write the actual symbol list)")
    else:
        raise ValueError(f"Unknown universe: {universe_name!r}")


def _cmd_summary() -> None:
    universes = _ensure_loaded()
    print("\n=== Vanguard Universe Summary ===\n")
    for name, u in universes.items():
        if name.startswith("_"):
            continue
        desc = u.get("description", "")
        source = u.get("source", "?")
        dynamic = u.get("dynamic", False)
        if dynamic:
            print(f"  {name:<25} dynamic ({source})  — {desc}")
        elif name == "ftmo_cfd":
            total = len(get_ftmo_universe())
            instruments = u.get("instruments", {})
            breakdown = ", ".join(
                f"{ac}: {len(syms)}" for ac, syms in instruments.items()
            )
            print(f"  {name:<25} {total:>5} symbols  — {breakdown}")
        elif name == "topstep_futures":
            total = len(get_topstep_universe())
            instruments = u.get("instruments", {})
            breakdown = ", ".join(
                f"{ac}: {len(cs)}" for ac, cs in instruments.items()
            )
            print(f"  {name:<25} {total:>5} contracts — {breakdown}")
    print()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    p = _build_parser()
    args = p.parse_args()

    # Load config (possibly overridden by --config)
    load_universes(args.config)

    if args.classify:
        ac = classify_asset_class(args.classify)
        print(f"{args.classify.upper()} → {ac}")
        return

    if args.summary:
        _cmd_summary()
        return

    if args.list:
        _cmd_list(args.list)
        return

    if args.refresh:
        from vanguard.helpers.db import VanguardDB
        from vanguard.helpers.clock import now_utc, iso_utc

        db  = VanguardDB(_DB_PATH)
        now = iso_utc(now_utc())

        if args.refresh == "ttp_equity":
            # Requires Alpaca credentials
            try:
                from vanguard.data_adapters.alpaca_adapter import AlpacaAdapter
                alpaca = AlpacaAdapter()
            except Exception as e:
                print(f"[ERROR] Cannot create AlpacaAdapter: {e}")
                sys.exit(1)
            n = refresh_to_db("ttp_equity", db, now, alpaca_adapter=alpaca)
        else:
            n = refresh_to_db(args.refresh, db, now)

        print(f"[REFRESH] Wrote {n} rows for universe '{args.refresh}'")
        return

    p.print_help()


if __name__ == "__main__":
    main()
