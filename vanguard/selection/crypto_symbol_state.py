"""
crypto_symbol_state.py — DB-backed crypto symbol/profile truth for QA validation.

This module builds `vanguard_crypto_symbol_state` from live context quote/spec
rows when available. Bar fallback exists only for validation mode. It publishes
facts only; V5 owns economics, V6 owns health, and policy/risk owns approval.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vanguard.config.runtime_config import get_runtime_config, resolve_market_data_source_label
from vanguard.helpers.clock import iso_utc, now_utc
from vanguard.helpers.db import connect_wal


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "vanguard_universe.db"
DEFAULT_CONFIG = REPO_ROOT / "config" / "vanguard_crypto_context.json"


CRYPTO_SYMBOL_STATE_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_crypto_symbol_state (
    cycle_ts_utc                 TEXT NOT NULL,
    profile_id                   TEXT NOT NULL,
    symbol                       TEXT NOT NULL,
    asset_class                  TEXT NOT NULL DEFAULT 'crypto',
    quote_ts_utc                 TEXT,
    source                       TEXT,
    live_bid                     REAL,
    live_ask                     REAL,
    live_mid                     REAL,
    spread_price                 REAL,
    spread_bps                   REAL,
    spread_ratio_vs_baseline     REAL,
    session_bucket               TEXT,
    liquidity_bucket             TEXT,
    trade_allowed                INTEGER NOT NULL DEFAULT 0,
    min_qty                      REAL,
    max_qty                      REAL,
    qty_step                     REAL,
    tick_size                    REAL,
    open_position_count          INTEGER NOT NULL DEFAULT 0,
    same_symbol_open_count       INTEGER NOT NULL DEFAULT 0,
    correlated_open_count        INTEGER NOT NULL DEFAULT 0,
    asset_state_json             TEXT NOT NULL DEFAULT '{}',
    source_status                TEXT NOT NULL,
    source_error                 TEXT,
    config_version               TEXT,
    created_at                   TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, profile_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_crypto_symbol_state_profile_symbol
    ON vanguard_crypto_symbol_state(profile_id, symbol, cycle_ts_utc);

CREATE INDEX IF NOT EXISTS idx_crypto_symbol_state_cycle
    ON vanguard_crypto_symbol_state(cycle_ts_utc, profile_id, source_status);
"""


def ensure_crypto_symbol_state_schema(con: sqlite3.Connection) -> None:
    for stmt in CRYPTO_SYMBOL_STATE_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def build_crypto_symbol_state(
    *,
    db_path: str | Path = DEFAULT_DB,
    runtime_config: dict[str, Any] | None = None,
    context_config: dict[str, Any] | None = None,
    cycle_ts_utc: str | None = None,
    profile_ids: list[str] | None = None,
    symbols: list[str] | None = None,
    persist: bool = True,
) -> list[dict[str, Any]]:
    runtime_config = runtime_config or get_runtime_config(refresh=True)
    context_config = context_config or load_crypto_context_config()
    cycle_ts = cycle_ts_utc or iso_utc(now_utc())
    created_at = iso_utc(now_utc())
    profiles = _active_profiles(runtime_config, profile_ids)
    crypto_symbols = symbols or _runtime_crypto_symbols(runtime_config)
    spread_cfg = runtime_config.get("crypto_spread_config") or {}
    fallback_spreads = {
        _normalise_symbol(sym): float(value)
        for sym, value in (spread_cfg.get("fallback_spreads_absolute") or {}).items()
    }
    defaults = context_config.get("defaults") or {}

    rows: list[dict[str, Any]] = []
    with connect_wal(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        ensure_crypto_symbol_state_schema(con)
        validation_mode = bool(defaults.get("validation_mode", True))
        latest_bars = (
            _latest_crypto_bars(
                con,
                crypto_symbols,
                preferred_source=_crypto_source(runtime_config, defaults),
                strict_source=True,
            )
            if validation_mode
            else {}
        )
        latest_quotes = _latest_context_quotes(con, crypto_symbols)
        latest_specs = _latest_symbol_specs(con, crypto_symbols)
        latest_positions = _latest_positions(con)
        for profile in profiles:
            profile_id = str(profile.get("id") or "")
            source = _crypto_source(runtime_config, defaults)
            positions = latest_positions.get(profile_id, [])
            for symbol in crypto_symbols:
                quote = _resolve_context_row(
                    latest_quotes,
                    profile_id,
                    symbol,
                    preferred_source=source,
                    strict_source=validation_mode,
                )
                spec = _resolve_context_row(latest_specs, profile_id, symbol, preferred_source=source)
                row = _build_row(
                    cycle_ts_utc=cycle_ts,
                    profile_id=profile_id,
                    symbol=symbol,
                    quote=quote,
                    spec=spec,
                    bar=latest_bars.get(symbol),
                    positions=positions,
                    fallback_spreads=fallback_spreads,
                    defaults=defaults,
                    source=source,
                    config_version=str(context_config.get("schema_version") or "unknown"),
                    created_at=created_at,
                )
                rows.append(row)
        if persist and rows:
            con.executemany(
                """
                INSERT OR REPLACE INTO vanguard_crypto_symbol_state (
                    cycle_ts_utc, profile_id, symbol, asset_class, quote_ts_utc,
                    source, live_bid, live_ask, live_mid, spread_price, spread_bps,
                    spread_ratio_vs_baseline, session_bucket, liquidity_bucket,
                    trade_allowed, min_qty, max_qty, qty_step, tick_size,
                    open_position_count, same_symbol_open_count, correlated_open_count,
                    asset_state_json, source_status, source_error, config_version,
                    created_at
                ) VALUES (
                    :cycle_ts_utc, :profile_id, :symbol, :asset_class, :quote_ts_utc,
                    :source, :live_bid, :live_ask, :live_mid, :spread_price, :spread_bps,
                    :spread_ratio_vs_baseline, :session_bucket, :liquidity_bucket,
                    :trade_allowed, :min_qty, :max_qty, :qty_step, :tick_size,
                    :open_position_count, :same_symbol_open_count, :correlated_open_count,
                    :asset_state_json, :source_status, :source_error, :config_version,
                    :created_at
                )
                """,
                rows,
            )
            con.commit()
    return rows


def load_crypto_context_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        return {"schema_version": "crypto_context_v1", "defaults": {}}
    return json.loads(cfg_path.read_text())


def _build_row(
    *,
    cycle_ts_utc: str,
    profile_id: str,
    symbol: str,
    quote: dict[str, Any] | None,
    spec: dict[str, Any] | None,
    bar: dict[str, Any] | None,
    positions: list[dict[str, Any]],
    fallback_spreads: dict[str, float],
    defaults: dict[str, Any],
    source: str,
    config_version: str,
    created_at: str,
) -> dict[str, Any]:
    validation_mode = bool(defaults.get("validation_mode", True))
    context_quote_ok = _quote_ok(quote)
    fallback_used = False
    if context_quote_ok:
        bid = _as_float((quote or {}).get("bid"), None)
        ask = _as_float((quote or {}).get("ask"), None)
        mid = _as_float((quote or {}).get("mid"), None)
        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        spread_price = _as_float((quote or {}).get("spread_price"), None)
        if spread_price is None and bid is not None and ask is not None:
            spread_price = ask - bid
        quote_ts = (quote or {}).get("quote_ts_utc") or (quote or {}).get("received_ts_utc")
        source = str((quote or {}).get("source") or source)
        source_status = "OK"
        source_error = None
    elif validation_mode:
        mid = _as_float((bar or {}).get("close"), None)
        quote_ts = (bar or {}).get("bar_ts_utc")
        spread_price = fallback_spreads.get(symbol)
        if spread_price is None and mid and mid > 0:
            spread_price = mid * (float(defaults.get("emergency_default_spread_bps") or 50.0) / 10_000.0)
        bid = mid - spread_price / 2.0 if mid and spread_price is not None else None
        ask = mid + spread_price / 2.0 if mid and spread_price is not None else None
        fallback_used = True
        source_status = "OK" if mid and mid > 0 and quote_ts else "MISSING"
        source_error = None if source_status == "OK" else "latest_crypto_bar_missing"
    else:
        bid = ask = mid = spread_price = None
        quote_ts = None
        source_status = "MISSING"
        source_error = "context_quote_missing"
    spread_bps = (spread_price / mid) * 10_000.0 if mid and mid > 0 and spread_price is not None else None
    baseline_bps = _as_float((defaults.get("baseline_spread_bps") or {}).get(symbol), None)
    spread_ratio = spread_bps / baseline_bps if spread_bps is not None and baseline_bps and baseline_bps > 0 else None
    same_symbol = [p for p in positions if _normalise_symbol(p.get("symbol")) == symbol]
    spec_trade_allowed = (spec or {}).get("trade_allowed")
    trade_allowed_value = bool(defaults.get("trade_allowed", True)) if spec_trade_allowed is None else bool(spec_trade_allowed)
    trade_allowed = int(trade_allowed_value and source_status == "OK")
    asset_state = {
        "active_source": source,
        "context_quote_used": context_quote_ok,
        "context_quote_source": (quote or {}).get("source"),
        "context_quote_status": (quote or {}).get("source_status"),
        "context_spec_status": (spec or {}).get("source_status"),
        "bar_source": (bar or {}).get("data_source"),
        "fallback_spread_used": fallback_used,
        "validation_mode": validation_mode,
    }
    return {
        "cycle_ts_utc": cycle_ts_utc,
        "profile_id": profile_id,
        "symbol": symbol,
        "asset_class": "crypto",
        "quote_ts_utc": quote_ts,
        "source": source,
        "live_bid": bid,
        "live_ask": ask,
        "live_mid": mid,
        "spread_price": spread_price,
        "spread_bps": spread_bps,
        "spread_ratio_vs_baseline": spread_ratio,
        "session_bucket": "24x7",
        "liquidity_bucket": _liquidity_bucket(symbol, defaults),
        "trade_allowed": trade_allowed,
        "min_qty": _as_float((spec or {}).get("min_lot"), _as_float(defaults.get("min_qty"), 0.000001)),
        "max_qty": _as_float((spec or {}).get("max_lot"), _as_float(defaults.get("max_qty"), None)),
        "qty_step": _as_float((spec or {}).get("lot_step"), _as_float(defaults.get("qty_step"), 0.000001)),
        "tick_size": _as_float((spec or {}).get("tick_size"), _as_float(defaults.get("tick_size"), None)),
        "open_position_count": len(positions),
        "same_symbol_open_count": len(same_symbol),
        "correlated_open_count": len(same_symbol),
        "asset_state_json": json.dumps(asset_state, sort_keys=True),
        "source_status": source_status,
        "source_error": source_error,
        "config_version": config_version,
        "created_at": created_at,
    }


def _latest_crypto_bars(
    con: sqlite3.Connection,
    symbols: list[str],
    *,
    preferred_source: str | None = None,
    strict_source: bool = False,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        aliases = _symbol_aliases(symbol)
        placeholders = ",".join("?" for _ in aliases)
        row = None
        if preferred_source:
            row = con.execute(
                f"""
                SELECT symbol, bar_ts_utc, close, data_source
                FROM vanguard_bars_1m
                WHERE asset_class = 'crypto' AND symbol IN ({placeholders}) AND data_source = ?
                ORDER BY bar_ts_utc DESC
                LIMIT 1
                """,
                [*aliases, preferred_source],
            ).fetchone()
            if row is None:
                row = con.execute(
                    f"""
                    SELECT symbol, bar_ts_utc, close, data_source
                    FROM vanguard_bars_5m
                    WHERE asset_class = 'crypto' AND symbol IN ({placeholders}) AND data_source = ?
                    ORDER BY bar_ts_utc DESC
                    LIMIT 1
                    """,
                    [*aliases, preferred_source],
                ).fetchone()
        if row is None and not strict_source:
            row = con.execute(
                f"""
                SELECT symbol, bar_ts_utc, close, data_source
                FROM vanguard_bars_1m
                WHERE asset_class = 'crypto' AND symbol IN ({placeholders})
                ORDER BY bar_ts_utc DESC
                LIMIT 1
                """,
                aliases,
            ).fetchone()
            if row is None:
                row = con.execute(
                    f"""
                    SELECT symbol, bar_ts_utc, close, data_source
                    FROM vanguard_bars_5m
                    WHERE asset_class = 'crypto' AND symbol IN ({placeholders})
                    ORDER BY bar_ts_utc DESC
                    LIMIT 1
                    """,
                    aliases,
                ).fetchone()
        if row is not None:
            out[symbol] = dict(row)
    return out


def _latest_context_quotes(con: sqlite3.Connection, symbols: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    try:
        rows = con.execute("SELECT * FROM vanguard_context_quote_latest").fetchall()
    except sqlite3.OperationalError:
        return {}
    wanted = set(symbols)
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        item = dict(row)
        symbol = _normalise_symbol(item.get("symbol"))
        if symbol not in wanted:
            continue
        profile_id = str(item.get("profile_id") or "")
        out.setdefault(profile_id, {}).setdefault(symbol, []).append(item)
    return out


def _latest_symbol_specs(con: sqlite3.Connection, symbols: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    try:
        rows = con.execute("SELECT * FROM vanguard_context_symbol_specs").fetchall()
    except sqlite3.OperationalError:
        return {}
    wanted = set(symbols)
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        item = dict(row)
        symbol = _normalise_symbol(item.get("symbol"))
        if symbol not in wanted:
            continue
        profile_id = str(item.get("profile_id") or "")
        out.setdefault(profile_id, {}).setdefault(symbol, []).append(item)
    return out


def _resolve_context_row(
    rows_by_profile: dict[str, dict[str, list[dict[str, Any]]]],
    profile_id: str,
    symbol: str,
    *,
    preferred_source: str,
    strict_source: bool = False,
) -> dict[str, Any] | None:
    direct_rows = (rows_by_profile.get(profile_id) or {}).get(symbol) or []
    for row in direct_rows:
        if str(row.get("source") or "") == preferred_source:
            return row
    preferred = None
    for profile_rows in rows_by_profile.values():
        rows = (profile_rows or {}).get(symbol) or []
        for row in rows:
            if str(row.get("source") or "") == preferred_source:
                return row
            preferred = preferred or row
    if strict_source:
        return None
    if direct_rows:
        return direct_rows[0]
    return preferred


def _latest_positions(con: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    try:
        rows = con.execute("SELECT * FROM vanguard_context_positions_latest").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        out.setdefault(str(item.get("profile_id") or ""), []).append(item)
    return out


def _quote_ok(quote: dict[str, Any] | None) -> bool:
    if not quote or str(quote.get("source_status") or "").upper() != "OK":
        return False
    bid = _as_float(quote.get("bid"), None)
    ask = _as_float(quote.get("ask"), None)
    return bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid


def _active_profiles(cfg: dict[str, Any], profile_ids: list[str] | None) -> list[dict[str, Any]]:
    wanted = {p for p in (profile_ids or []) if p}
    profiles = [dict(p) for p in cfg.get("profiles") or [] if p.get("is_active", True)]
    if wanted:
        profiles = [p for p in profiles if str(p.get("id")) in wanted]
    return profiles


def _runtime_crypto_symbols(cfg: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()
    for universe in (cfg.get("universes") or {}).values():
        symbol_map = universe.get("symbols") if isinstance(universe.get("symbols"), dict) else universe
        for sym in symbol_map.get("crypto", []) or []:
            clean = _normalise_symbol(sym)
            if clean:
                symbols.add(clean)
    return sorted(symbols)


def _crypto_source(runtime_config: dict[str, Any], defaults: dict[str, Any]) -> str:
    resolved = resolve_market_data_source_label("crypto", runtime_config=runtime_config)
    if resolved and resolved != "unknown":
        return resolved
    return str(defaults.get("source") or "twelvedata")


def _liquidity_bucket(symbol: str, defaults: dict[str, Any]) -> str:
    buckets = defaults.get("liquidity_buckets") or {}
    for bucket, symbols in buckets.items():
        if symbol in {_normalise_symbol(s) for s in symbols or []}:
            return str(bucket)
    return str(defaults.get("default_liquidity_bucket") or "unknown")


def _symbol_aliases(symbol: str) -> list[str]:
    clean = _normalise_symbol(symbol)
    if clean.endswith("USD"):
        return [clean, f"{clean[:-3]}/USD"]
    return [clean]


def _normalise_symbol(value: Any) -> str:
    return str(value or "").replace("/", "").upper().split(".", 1)[0]


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
