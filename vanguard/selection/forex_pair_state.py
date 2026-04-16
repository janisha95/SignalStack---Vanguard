"""
forex_pair_state.py — DB-backed forex pair/profile truth for V5.

This module builds `vanguard_forex_pair_state` from live context tables and
static JSON config. It does not approve trades, select candidates, or call MT5.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vanguard.config.runtime_config import get_runtime_config
from vanguard.helpers.clock import iso_utc, now_utc
from vanguard.helpers.db import connect_wal


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "vanguard_universe.db"
DEFAULT_CONFIG = REPO_ROOT / "config" / "vanguard_forex_pair_state.json"


FOREX_PAIR_STATE_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_forex_pair_state (
    cycle_ts_utc                 TEXT NOT NULL,
    profile_id                   TEXT NOT NULL,
    symbol                       TEXT NOT NULL,
    base_currency                TEXT,
    quote_currency               TEXT,
    usd_side                     TEXT,
    asset_class                  TEXT NOT NULL DEFAULT 'forex',
    session_bucket               TEXT,
    liquidity_bucket             TEXT,
    spread_pips                  REAL,
    spread_ratio_vs_baseline     REAL,
    pair_correlation_bucket      TEXT,
    currency_exposure_json       TEXT NOT NULL,
    open_exposure_overlap_json   TEXT NOT NULL,
    usd_concentration            REAL,
    same_currency_open_count     INTEGER NOT NULL DEFAULT 0,
    correlated_open_count        INTEGER NOT NULL DEFAULT 0,
    live_mid                     REAL,
    quote_ts_utc                 TEXT,
    context_ts_utc               TEXT,
    source                       TEXT,
    source_status                TEXT NOT NULL,
    source_error                 TEXT,
    config_version               TEXT,
    PRIMARY KEY (cycle_ts_utc, profile_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_forex_pair_state_profile_symbol
    ON vanguard_forex_pair_state(profile_id, symbol, cycle_ts_utc);

CREATE INDEX IF NOT EXISTS idx_forex_pair_state_cycle
    ON vanguard_forex_pair_state(cycle_ts_utc, profile_id, source_status);
"""


def ensure_forex_pair_state_schema(con: sqlite3.Connection) -> None:
    for stmt in FOREX_PAIR_STATE_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def build_forex_pair_state(
    *,
    db_path: str | Path = DEFAULT_DB,
    runtime_config: dict[str, Any] | None = None,
    pair_state_config: dict[str, Any] | None = None,
    cycle_ts_utc: str | None = None,
    profile_ids: list[str] | None = None,
    symbols: list[str] | None = None,
    persist: bool = True,
) -> list[dict[str, Any]]:
    """Build and optionally persist one forex pair-state row per profile/symbol."""
    runtime_config = runtime_config or get_runtime_config(refresh=True)
    pair_state_config = pair_state_config or load_pair_state_config()
    cycle_ts = cycle_ts_utc or iso_utc(now_utc())
    active_profiles = _active_profiles(runtime_config, profile_ids)
    forex_symbols = symbols or _runtime_forex_symbols(runtime_config)
    baseline_map = _load_spread_baselines(pair_state_config)
    bucket_map = _correlation_bucket_map(pair_state_config)
    now = _parse_ts(cycle_ts) or now_utc()
    session_bucket = _session_bucket(now, pair_state_config)

    rows: list[dict[str, Any]] = []
    with connect_wal(str(db_path)) as con:
        ensure_forex_pair_state_schema(con)
        latest_quotes = _latest_quotes(con)
        latest_quotes_by_source_symbol = _latest_quotes_by_source(latest_quotes)
        latest_positions = _latest_positions(con)
        for profile in active_profiles:
            profile_id = profile["id"]
            context_source_id = str(profile.get("context_source_id") or "")
            source = _context_source(runtime_config, context_source_id)
            source_name = str(source.get("source") or "mt5_dwx")
            broker = str(source.get("broker") or "")
            exposure = _currency_exposure(latest_positions.get((profile_id, source_name), []))
            for symbol in forex_symbols:
                quote = latest_quotes.get((profile_id, source_name, symbol))
                if quote is None:
                    shared_quote = latest_quotes_by_source_symbol.get((source_name, symbol))
                    if shared_quote is not None:
                        quote = dict(shared_quote)
                        shared_from = str(shared_quote.get("profile_id") or "")
                        if shared_from and shared_from != profile_id:
                            quote["source_error"] = f"quote_shared_from_profile={shared_from}"
                positions = latest_positions.get((profile_id, source_name), [])
                row = _build_row(
                    cycle_ts_utc=cycle_ts,
                    profile_id=profile_id,
                    symbol=symbol,
                    quote=quote,
                    positions=positions,
                    currency_exposure=exposure,
                    baseline_map=baseline_map,
                    bucket_map=bucket_map,
                    session_bucket=session_bucket,
                    pair_state_config=pair_state_config,
                    source_name=source_name,
                    broker=broker,
                )
                rows.append(row)
        if persist and rows:
            con.executemany(
                """
                INSERT OR REPLACE INTO vanguard_forex_pair_state (
                    cycle_ts_utc, profile_id, symbol, base_currency, quote_currency,
                    usd_side, asset_class, session_bucket, liquidity_bucket,
                    spread_pips, spread_ratio_vs_baseline, pair_correlation_bucket,
                    currency_exposure_json, open_exposure_overlap_json,
                    usd_concentration, same_currency_open_count, correlated_open_count,
                    live_mid, quote_ts_utc, context_ts_utc, source, source_status,
                    source_error, config_version
                ) VALUES (
                    :cycle_ts_utc, :profile_id, :symbol, :base_currency, :quote_currency,
                    :usd_side, :asset_class, :session_bucket, :liquidity_bucket,
                    :spread_pips, :spread_ratio_vs_baseline, :pair_correlation_bucket,
                    :currency_exposure_json, :open_exposure_overlap_json,
                    :usd_concentration, :same_currency_open_count, :correlated_open_count,
                    :live_mid, :quote_ts_utc, :context_ts_utc, :source, :source_status,
                    :source_error, :config_version
                )
                """,
                rows,
            )
            con.commit()
    return rows


def load_pair_state_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        return {"schema_version": "forex_pair_state_v1"}
    return json.loads(cfg_path.read_text())


def _build_row(
    *,
    cycle_ts_utc: str,
    profile_id: str,
    symbol: str,
    quote: dict[str, Any] | None,
    positions: list[dict[str, Any]],
    currency_exposure: dict[str, float],
    baseline_map: dict[str, float],
    bucket_map: dict[str, str],
    session_bucket: str,
    pair_state_config: dict[str, Any],
    source_name: str,
    broker: str,
) -> dict[str, Any]:
    base, quote_ccy = _split_pair(symbol)
    spread_pips = _as_float((quote or {}).get("spread_pips"), None)
    baseline = baseline_map.get(symbol)
    spread_ratio = (spread_pips / baseline) if spread_pips is not None and baseline and baseline > 0 else None
    corr_bucket = bucket_map.get(symbol) or _default_bucket(symbol, base, quote_ccy)
    overlap = _open_exposure_overlap(symbol, base, quote_ccy, corr_bucket, positions, bucket_map)
    liquidity_bucket = _liquidity_bucket(symbol, session_bucket, pair_state_config)
    status = str((quote or {}).get("source_status") or "MISSING").upper()
    source_error = (quote or {}).get("source_error") if quote else "quote_missing_for_profile_symbol"
    if status == "OK" and broker:
        source_status = "OK"
    elif status == "OK":
        source_status = "OK"
    else:
        source_status = status

    return {
        "cycle_ts_utc": cycle_ts_utc,
        "profile_id": profile_id,
        "symbol": symbol,
        "base_currency": base,
        "quote_currency": quote_ccy,
        "usd_side": _usd_side(base, quote_ccy),
        "asset_class": "forex",
        "session_bucket": session_bucket,
        "liquidity_bucket": liquidity_bucket,
        "spread_pips": spread_pips,
        "spread_ratio_vs_baseline": spread_ratio,
        "pair_correlation_bucket": corr_bucket,
        "currency_exposure_json": json.dumps(currency_exposure, sort_keys=True),
        "open_exposure_overlap_json": json.dumps(overlap, sort_keys=True),
        "usd_concentration": _usd_concentration(currency_exposure),
        "same_currency_open_count": int(overlap["same_currency_open_count"]),
        "correlated_open_count": int(overlap["correlated_open_count"]),
        "live_mid": _as_float((quote or {}).get("mid"), None),
        "quote_ts_utc": (quote or {}).get("quote_ts_utc"),
        "context_ts_utc": (quote or {}).get("received_ts_utc"),
        "source": source_name,
        "source_status": source_status,
        "source_error": source_error,
        "config_version": str(pair_state_config.get("schema_version") or "unknown"),
    }


def _active_profiles(cfg: dict[str, Any], profile_ids: list[str] | None) -> list[dict[str, Any]]:
    wanted = {p for p in (profile_ids or []) if p}
    profiles = [dict(p) for p in cfg.get("profiles") or [] if p.get("is_active", True)]
    if wanted:
        profiles = [p for p in profiles if str(p.get("id")) in wanted]
    return profiles


def _runtime_forex_symbols(cfg: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()
    for universe in (cfg.get("universes") or {}).values():
        symbol_map = universe.get("symbols") if isinstance(universe.get("symbols"), dict) else universe
        for sym in symbol_map.get("forex", []) or []:
            clean = _normalise_symbol(sym)
            if len(clean) == 6:
                symbols.add(clean)
    return sorted(symbols)


def _context_source(cfg: dict[str, Any], source_id: str) -> dict[str, Any]:
    return dict((cfg.get("context_sources") or {}).get(source_id) or {})


def _latest_quotes(con: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM vanguard_context_quote_latest").fetchall()
    return {
        (str(r["profile_id"]), str(r["source"]), _normalise_symbol(r["symbol"])): dict(r)
        for r in rows
    }


def _latest_quotes_by_source(
    latest_quotes: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (_profile_id, source, symbol), row in sorted(latest_quotes.items()):
        if str(row.get("source_status") or "").upper() == "OK":
            out.setdefault((source, symbol), row)
    return out


def _latest_positions(con: sqlite3.Connection) -> dict[tuple[str, str], list[dict[str, Any]]]:
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM vanguard_context_positions_latest").fetchall()
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["profile_id"]), str(row["source"]))
        out.setdefault(key, []).append(dict(row))
    return out


def _load_spread_baselines(cfg: dict[str, Any]) -> dict[str, float]:
    baseline_path = str(cfg.get("spread_baseline_path") or "").strip()
    if not baseline_path:
        return {}
    path = Path(baseline_path).expanduser()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    symbols = raw.get("symbols") if isinstance(raw.get("symbols"), dict) else raw
    out: dict[str, float] = {}
    for sym, val in (symbols or {}).items():
        clean = _normalise_symbol(sym)
        if isinstance(val, dict):
            spread = _as_float(val.get("median_spread_points"), None)
        else:
            spread = _as_float(val, None)
        if spread is not None:
            out[clean] = spread
    return out


def _correlation_bucket_map(cfg: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for bucket, symbols in (cfg.get("correlation_buckets") or {}).items():
        for sym in symbols or []:
            out[_normalise_symbol(sym)] = str(bucket)
    return out


def _session_bucket(now: datetime, cfg: dict[str, Any]) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    minutes = now.astimezone(timezone.utc).hour * 60 + now.minute
    sessions = cfg.get("sessions_utc") or {}
    for name in cfg.get("session_order") or list(sessions):
        spec = sessions.get(name) or {}
        if _time_in_window(minutes, str(spec.get("start") or "00:00"), str(spec.get("end") or "24:00")):
            return str(name)
    return "unknown"


def _liquidity_bucket(symbol: str, session_bucket: str, cfg: dict[str, Any]) -> str:
    sessions = cfg.get("sessions_utc") or {}
    bucket = str((sessions.get(session_bucket) or {}).get("liquidity_bucket") or "UNKNOWN").upper()
    if session_bucket == "asian":
        if "JPY" in symbol:
            return str((cfg.get("liquidity_overrides") or {}).get("JPY_IN_ASIAN") or bucket).upper()
        if _usd_side(*_split_pair(symbol)) == "CROSS":
            return str((cfg.get("liquidity_overrides") or {}).get("CROSS_IN_ASIAN") or bucket).upper()
    return bucket


def _time_in_window(current_minute: int, start: str, end: str) -> bool:
    start_min = _parse_hhmm(start, 0)
    end_min = _parse_hhmm(end, 24 * 60)
    if end_min >= start_min:
        return start_min <= current_minute < end_min
    return current_minute >= start_min or current_minute < end_min


def _parse_hhmm(value: str, default: int) -> int:
    try:
        hh, mm = value.split(":", 1)
        if hh == "24":
            return 24 * 60
        return int(hh) * 60 + int(mm)
    except Exception:
        return default


def _currency_exposure(positions: list[dict[str, Any]]) -> dict[str, float]:
    exposure: dict[str, float] = {}
    for pos in positions:
        symbol = _normalise_symbol(pos.get("symbol"))
        if len(symbol) != 6:
            continue
        base, quote = _split_pair(symbol)
        lots = _as_float(pos.get("lots"), 0.0) or 0.0
        direction = str(pos.get("direction") or "").upper()
        sign = 1.0 if direction == "LONG" else -1.0 if direction == "SHORT" else 0.0
        exposure[base] = exposure.get(base, 0.0) + sign * lots
        exposure[quote] = exposure.get(quote, 0.0) - sign * lots
    return {k: round(v, 6) for k, v in sorted(exposure.items()) if abs(v) > 1e-12}


def _open_exposure_overlap(
    symbol: str,
    base: str,
    quote: str,
    corr_bucket: str,
    positions: list[dict[str, Any]],
    bucket_map: dict[str, str],
) -> dict[str, Any]:
    same_currency: list[str] = []
    correlated: list[str] = []
    same_symbol = False
    for pos in positions:
        pos_symbol = _normalise_symbol(pos.get("symbol"))
        if len(pos_symbol) != 6:
            continue
        pbase, pquote = _split_pair(pos_symbol)
        if pos_symbol == symbol:
            same_symbol = True
        if base in {pbase, pquote} or quote in {pbase, pquote}:
            same_currency.append(pos_symbol)
        pos_bucket = bucket_map.get(pos_symbol) or _default_bucket(pos_symbol, pbase, pquote)
        if pos_bucket == corr_bucket:
            correlated.append(pos_symbol)
    return {
        "same_symbol_open": same_symbol,
        "same_currency_symbols": sorted(set(same_currency)),
        "correlated_symbols": sorted(set(correlated)),
        "same_currency_open_count": len(set(same_currency)),
        "correlated_open_count": len(set(correlated)),
    }


def _usd_concentration(exposure: dict[str, float]) -> float | None:
    total = sum(abs(v) for v in exposure.values())
    if total <= 0:
        return 0.0
    return abs(exposure.get("USD", 0.0)) / total


def _default_bucket(symbol: str, base: str, quote: str) -> str:
    if "USD" in {base, quote}:
        return "USD_MAJOR"
    if "JPY" in {base, quote}:
        return "JPY_CROSS"
    if base == "EUR" or quote == "EUR":
        return "EURO_CROSS"
    if base == "GBP" or quote == "GBP":
        return "STERLING_CROSS"
    return "FOREX_CROSS"


def _split_pair(symbol: str) -> tuple[str, str]:
    clean = _normalise_symbol(symbol)
    return clean[:3], clean[3:6]


def _usd_side(base: str, quote: str) -> str:
    if base == "USD":
        return "BASE"
    if quote == "USD":
        return "QUOTE"
    return "CROSS"


def _normalise_symbol(value: Any) -> str:
    return str(value or "").replace("/", "").replace(".X", "").replace(".x", "").replace(".", "").upper().strip()


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
