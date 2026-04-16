#!/usr/bin/env python3
"""
dwx_live_context_daemon.py — Persist MT5/DWX live broker context facts.

QA-only local MT5/DWX context writer. The daemon writes normalised facts into
vanguard_context_* tables. It does not approve, block, or declare staleness;
V6 reads these facts and applies JSON-configured health rules.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter
from vanguard.helpers.clock import iso_utc, now_utc
from vanguard.helpers.db import connect_wal
from vanguard.selection.crypto_symbol_state import build_crypto_symbol_state
from vanguard.selection.forex_pair_state import build_forex_pair_state

logger = logging.getLogger("dwx_live_context")

DEFAULT_RUNTIME_CONFIG = _REPO_ROOT / "config" / "vanguard_runtime.json"
DEFAULT_DB = _REPO_ROOT / "data" / "vanguard_universe.db"
DEFAULT_COVERAGE_REPORT = Path("/tmp/dwx_live_context_coverage.json")
DEFAULT_DWX_PATH = (
    Path.home()
    / "Library/Application Support/net.metaquotes.wine.metatrader5"
    / "drive_c/Program Files/MetaTrader 5/MQL5/Files"
)

CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS vanguard_context_quote_latest (
    profile_id        TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    symbol            TEXT NOT NULL,
    broker_symbol     TEXT,
    bid               REAL,
    ask               REAL,
    mid               REAL,
    spread_price      REAL,
    spread_pips       REAL,
    spread_rt         REAL,
    quote_ts_utc      TEXT,
    received_ts_utc   TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT,
    PRIMARY KEY (profile_id, source, symbol)
);

CREATE TABLE IF NOT EXISTS vanguard_context_quote_history (
    ingest_run_id     TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    symbol            TEXT NOT NULL,
    broker_symbol     TEXT,
    bid               REAL,
    ask               REAL,
    mid               REAL,
    spread_price      REAL,
    spread_pips       REAL,
    spread_rt         REAL,
    quote_ts_utc      TEXT,
    received_ts_utc   TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_context_quote_history_symbol_ts
    ON vanguard_context_quote_history(profile_id, symbol, received_ts_utc);

CREATE TABLE IF NOT EXISTS vanguard_context_account_latest (
    profile_id        TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT NOT NULL DEFAULT '',
    account_name      TEXT,
    currency          TEXT,
    balance           REAL,
    equity            REAL,
    free_margin       REAL,
    margin_used       REAL,
    margin_level      REAL,
    leverage          INTEGER,
    received_ts_utc   TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT,
    PRIMARY KEY (profile_id, source, account_number)
);

CREATE TABLE IF NOT EXISTS vanguard_context_account_history (
    ingest_run_id     TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    account_name      TEXT,
    currency          TEXT,
    balance           REAL,
    equity            REAL,
    free_margin       REAL,
    margin_used       REAL,
    margin_level      REAL,
    leverage          INTEGER,
    received_ts_utc   TEXT NOT NULL,
    source            TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT
);

CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
    profile_id        TEXT NOT NULL,
    source            TEXT NOT NULL,
    ticket            TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    symbol            TEXT,
    direction         TEXT,
    lots              REAL,
    entry_price       REAL,
    current_price     REAL,
    sl                REAL,
    tp                REAL,
    floating_pnl      REAL,
    swap              REAL,
    open_time_utc     TEXT,
    holding_minutes   REAL,
    received_ts_utc   TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT,
    PRIMARY KEY (profile_id, source, ticket)
);

CREATE TABLE IF NOT EXISTS vanguard_context_positions_history (
    ingest_run_id     TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    source            TEXT NOT NULL,
    ticket            TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    symbol            TEXT,
    direction         TEXT,
    lots              REAL,
    entry_price       REAL,
    current_price     REAL,
    sl                REAL,
    tp                REAL,
    floating_pnl      REAL,
    swap              REAL,
    open_time_utc     TEXT,
    holding_minutes   REAL,
    received_ts_utc   TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_context_positions_history_ticket_ts
    ON vanguard_context_positions_history(profile_id, source, ticket, received_ts_utc);

CREATE TABLE IF NOT EXISTS vanguard_context_orders_latest (
    profile_id        TEXT NOT NULL,
    source            TEXT NOT NULL,
    ticket            TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    symbol            TEXT,
    order_type        TEXT,
    direction         TEXT,
    lots              REAL,
    price             REAL,
    sl                REAL,
    tp                REAL,
    state             TEXT,
    received_ts_utc   TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT,
    PRIMARY KEY (profile_id, source, ticket)
);

CREATE TABLE IF NOT EXISTS vanguard_context_orders_history (
    ingest_run_id     TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    source            TEXT NOT NULL,
    ticket            TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    symbol            TEXT,
    order_type        TEXT,
    direction         TEXT,
    lots              REAL,
    price             REAL,
    sl                REAL,
    tp                REAL,
    state             TEXT,
    received_ts_utc   TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_context_orders_history_ticket_ts
    ON vanguard_context_orders_history(profile_id, source, ticket, received_ts_utc);

CREATE TABLE IF NOT EXISTS vanguard_context_symbol_specs (
    broker            TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    broker_symbol     TEXT,
    min_lot           REAL,
    max_lot           REAL,
    lot_step          REAL,
    pip_size          REAL,
    tick_size         REAL,
    tick_value        REAL,
    contract_size     REAL,
    trade_allowed     INTEGER,
    source            TEXT,
    updated_ts_utc    TEXT,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT,
    PRIMARY KEY (broker, profile_id, symbol)
);

CREATE TABLE IF NOT EXISTS vanguard_context_ingest_runs (
    ingest_run_id     TEXT PRIMARY KEY,
    profile_id        TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    source            TEXT NOT NULL,
    context_source_id TEXT,
    started_ts_utc    TEXT NOT NULL,
    finished_ts_utc   TEXT NOT NULL,
    symbols_requested INTEGER DEFAULT 0,
    quotes_written    INTEGER DEFAULT 0,
    account_written   INTEGER DEFAULT 0,
    positions_written INTEGER DEFAULT 0,
    orders_written    INTEGER DEFAULT 0,
    specs_written     INTEGER DEFAULT 0,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    coverage_json     TEXT
);
"""


def _load_runtime(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _normalise_symbol(value: Any) -> str:
    clean = str(value or "").replace("/", "").upper().strip()
    return clean.split(".", 1)[0]


def _default_instrument_scope(cfg: dict[str, Any]) -> str:
    for profile in cfg.get("profiles") or []:
        scope = str(profile.get("instrument_scope") or "").strip()
        if scope:
            return scope
    universes = cfg.get("universes") or {}
    return str(next(iter(universes), ""))


def _active_profiles(cfg: dict[str, Any], source_cfg: dict[str, Any], profile_id: str | None) -> list[dict[str, Any]]:
    wanted = {profile_id} if profile_id else set()
    source_profiles = source_cfg.get("profiles") or {}
    profiles: list[dict[str, Any]] = []
    for profile in cfg.get("profiles") or []:
        pid = str(profile.get("id") or "")
        if not pid or (wanted and pid not in wanted):
            continue
        if not profile.get("is_active", True):
            continue
        if str(profile.get("context_source_id") or "") != str(source_cfg.get("_source_id") or ""):
            continue
        if pid in source_profiles and not (source_profiles.get(pid) or {}).get("enabled", True):
            continue
        profiles.append(dict(profile))
    if profiles:
        return profiles
    fallback_scope = _default_instrument_scope(cfg)
    if profile_id:
        return [{
            "id": profile_id,
            "instrument_scope": fallback_scope,
            "context_source_id": source_cfg.get("_source_id"),
        }]
    for pid, pcfg in source_profiles.items():
        if (pcfg or {}).get("enabled", True):
            return [{
                "id": str(pid),
                "instrument_scope": fallback_scope,
                "context_source_id": source_cfg.get("_source_id"),
            }]
    return []


def _universe_symbols(cfg: dict[str, Any], universe_id: str | None, asset_class: str) -> list[str]:
    universes = cfg.get("universes") or {}
    universe = universes.get(str(universe_id or "")) if universe_id else None
    if universe is None and len(universes) == 1:
        universe = next(iter(universes.values()))
    if not isinstance(universe, dict):
        return []
    symbol_map = universe.get("symbols") if isinstance(universe.get("symbols"), dict) else universe
    return [_normalise_symbol(sym) for sym in symbol_map.get(asset_class, []) or [] if _normalise_symbol(sym)]


def _load_symbols_for_context_source(
    cfg: dict[str, Any],
    source_cfg: dict[str, Any],
    profile_id: str | None,
) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    primary_for = {str(v).lower() for v in source_cfg.get("primary_for", ["forex"]) or ["forex"]}
    symbols: set[str] = set()
    asset_class_by_symbol: dict[str, str] = {}
    by_asset: dict[str, set[str]] = {asset: set() for asset in primary_for}
    for profile in _active_profiles(cfg, source_cfg, profile_id):
        universe_id = profile.get("instrument_scope") or profile.get("universe_id")
        for asset_class in sorted(primary_for):
            for symbol in _universe_symbols(cfg, str(universe_id) if universe_id else None, asset_class):
                symbols.add(symbol)
                asset_class_by_symbol.setdefault(symbol, asset_class)
                by_asset.setdefault(asset_class, set()).add(symbol)
    return (
        sorted(symbols),
        asset_class_by_symbol,
        {asset: sorted(values) for asset, values in by_asset.items() if values},
    )


def _classify_symbol_from_runtime(cfg: dict[str, Any], symbol: str) -> str | None:
    target = _normalise_symbol(symbol)
    for universe in (cfg.get("universes") or {}).values():
        symbol_map = universe.get("symbols") if isinstance(universe.get("symbols"), dict) else universe
        for asset_class, values in (symbol_map or {}).items():
            if target in {_normalise_symbol(value) for value in values or []}:
                return str(asset_class).lower()
    return None


def _resolve_db_path(cfg: dict[str, Any], explicit: str | None) -> str:
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    db_cfg = cfg.get("database") or {}
    for key in ("shadow_db_path", "path", "db_path"):
        if db_cfg.get(key):
            return str(Path(str(db_cfg[key])).expanduser().resolve())
    return str(DEFAULT_DB)


def _resolve_context_source(
    cfg: dict[str, Any],
    context_source_id: str | None,
) -> tuple[str, dict[str, Any]]:
    source_id = context_source_id or str((cfg.get("v6_health") or {}).get("default_context_source_id") or "")
    if not source_id:
        for profile in cfg.get("profiles") or []:
            if str(profile.get("is_active") or "").strip().lower() not in {"1", "true", "yes", "on"}:
                continue
            source_id = str(profile.get("context_source_id") or "")
            if source_id:
                break
    if not source_id:
        sources = cfg.get("context_sources") or {}
        source_id = next(iter(sources), "mt5_local")
    source_cfg = dict((cfg.get("context_sources") or {}).get(source_id) or {})
    if not source_cfg:
        # Backwards-compatible fallback for older runtime files.
        mt5_cfg = (cfg.get("data_sources") or {}).get("mt5_local") or {}
        source_cfg = {
            "enabled": True,
            "source_type": "dwx_mt5",
            "source": "mt5_dwx",
            "broker": str(mt5_cfg.get("broker") or "mt5"),
            "dwx_files_path": mt5_cfg.get("dwx_files_path") or str(DEFAULT_DWX_PATH),
            "symbol_suffix": ".x" if mt5_cfg.get("symbol_suffix") is None else mt5_cfg.get("symbol_suffix"),
            "profiles": {},
        }
    source_cfg["_source_id"] = source_id
    return source_id, source_cfg


def _resolve_profile_id(args_profile: str | None, source_cfg: dict[str, Any]) -> str:
    if args_profile:
        return args_profile
    profiles = source_cfg.get("profiles") or {}
    for profile_id, profile_cfg in profiles.items():
        if profile_cfg.get("enabled", True):
            return str(profile_id)
    return ""


def _resolve_dwx_path(source_cfg: dict[str, Any], explicit: str | None) -> str:
    if explicit:
        return str(Path(explicit).expanduser())
    return str(Path(str(source_cfg.get("dwx_files_path") or DEFAULT_DWX_PATH)).expanduser())


def _resolve_suffix(source_cfg: dict[str, Any], explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    return str(source_cfg.get("symbol_suffix", ".x") or "")


def _suffix_for_asset_class(source_cfg: dict[str, Any], asset_class: str, default_suffix: str) -> str:
    by_asset = source_cfg.get("symbol_suffix_by_asset_class") or {}
    return str(by_asset.get(asset_class) if asset_class in by_asset else default_suffix)


def _resolve_symbol_suffix_map(
    source_cfg: dict[str, Any],
    symbols: list[str],
    asset_class_by_symbol: dict[str, str],
    default_suffix: str,
) -> dict[str, str]:
    return {
        symbol: _suffix_for_asset_class(source_cfg, asset_class_by_symbol.get(symbol, ""), default_suffix)
        for symbol in symbols
    }


def _profile_account_number(source_cfg: dict[str, Any], profile_id: str) -> str:
    profile_cfg = (source_cfg.get("profiles") or {}).get(profile_id) or {}
    return str(profile_cfg.get("account_number") or "")


def _pip_size(symbol: str) -> float:
    return 0.01 if symbol.upper().endswith("JPY") else 0.0001


def _broker_symbol(symbol: str, suffix: str) -> str:
    clean = _normalise_symbol(symbol)
    if suffix and not clean.upper().endswith(suffix.upper()):
        return f"{clean}{suffix}"
    return clean


def _utc_from_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return None


def ensure_schema(db_path: str) -> None:
    with connect_wal(db_path) as conn:
        conn.executescript(CONTEXT_SCHEMA)
        conn.commit()


def _insert_dict(conn, table: str, row: dict[str, Any], replace: bool = False) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(["?"] * len(cols))
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    conn.execute(
        f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )


def _quote_row(
    *,
    ingest_run_id: str,
    profile_id: str,
    broker: str,
    account_number: str,
    source: str,
    symbol: str,
    quote: dict[str, Any] | None,
    quote_ts: datetime | None,
    received_ts: datetime,
    suffix: str,
    asset_class: str,
) -> dict[str, Any]:
    bid = float((quote or {}).get("bid") or 0.0)
    ask = float((quote or {}).get("ask") or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else None
    spread_price = ask - bid if bid > 0 and ask > 0 else None
    pip_size = _pip_size(symbol) if asset_class == "forex" else None
    spread_pips = spread_price / pip_size if pip_size and spread_price is not None else None
    spread_rt = (2.0 * spread_price / mid) if mid and spread_price is not None else None
    status = "OK" if quote else "MISSING"
    row = {
        "ingest_run_id": ingest_run_id,
        "profile_id": profile_id,
        "broker": broker,
        "account_number": account_number,
        "symbol": symbol,
        "broker_symbol": _broker_symbol(symbol, suffix),
        "bid": bid if quote else None,
        "ask": ask if quote else None,
        "mid": mid,
        "spread_price": spread_price,
        "spread_pips": spread_pips,
        "spread_rt": spread_rt,
        "quote_ts_utc": iso_utc(quote_ts) if quote_ts else None,
        "received_ts_utc": iso_utc(received_ts),
        "source": source,
        "source_status": status,
        "source_error": None if quote else "quote_not_returned_by_source",
        "raw_payload_json": json.dumps({"asset_class": asset_class, "quote": quote or {}}, sort_keys=True),
    }
    return row


def _raw_account_from_client(adapter: MT5DWXAdapter) -> dict[str, Any]:
    client = getattr(adapter, "_client", None)
    if client is None:
        return {}
    try:
        return dict(client.account_info or {})
    except Exception:  # noqa: BLE001
        return {}


def _account_row(
    *,
    ingest_run_id: str,
    profile_id: str,
    broker: str,
    account_number: str,
    source: str,
    adapter: MT5DWXAdapter,
    received_ts: datetime,
) -> dict[str, Any]:
    raw = _raw_account_from_client(adapter)
    normalised = adapter.get_account_info()
    raw_account_number = str(raw.get("number") or raw.get("login") or account_number or "")
    status = "OK" if raw or normalised else "MISSING"
    return {
        "ingest_run_id": ingest_run_id,
        "profile_id": profile_id,
        "broker": broker,
        "account_number": raw_account_number,
        "account_name": str(raw.get("name") or ""),
        "currency": str(raw.get("currency") or ""),
        "balance": float(normalised.get("balance") or 0.0),
        "equity": float(normalised.get("equity") or 0.0),
        "free_margin": float(normalised.get("free_margin") or 0.0),
        "margin_used": _float_or_none(raw.get("margin") or raw.get("margin_used")),
        "margin_level": _float_or_none(raw.get("margin_level") or raw.get("marginLevel")),
        "leverage": int(normalised.get("leverage") or raw.get("leverage") or 0),
        "received_ts_utc": iso_utc(received_ts),
        "source": source,
        "source_status": status,
        "source_error": None if status == "OK" else "account_not_returned_by_source",
        "raw_payload_json": json.dumps(raw or normalised, sort_keys=True, default=str),
    }


def _position_rows(
    *,
    ingest_run_id: str,
    profile_id: str,
    broker: str,
    account_number: str,
    source: str,
    adapter: MT5DWXAdapter,
    received_ts: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticket, pos in adapter.get_open_positions().items():
        lots = float(pos.get("lots") or 0.0)
        entry_price = float(pos.get("open_price") or 0.0)
        raw_type = str(pos.get("type") or "").lower()
        direction = "LONG" if raw_type == "buy" else "SHORT" if raw_type == "sell" else raw_type.upper()
        open_time = pos.get("open_time")
        holding_minutes = None
        if isinstance(open_time, datetime):
            holding_minutes = (received_ts - open_time).total_seconds() / 60.0
        symbol = str(pos.get("symbol") or "").replace("/", "").upper()
        rows.append(
            {
                "ingest_run_id": ingest_run_id,
                "profile_id": profile_id,
                "source": source,
                "ticket": str(ticket),
                "broker": broker,
                "account_number": account_number,
                "symbol": symbol,
                "direction": direction,
                "lots": lots,
                "entry_price": entry_price,
                "current_price": None,
                "sl": _float_or_none(pos.get("sl")),
                "tp": _float_or_none(pos.get("tp")),
                "floating_pnl": _float_or_none(pos.get("pnl")),
                "swap": _float_or_none(pos.get("swap")),
                "open_time_utc": iso_utc(open_time) if isinstance(open_time, datetime) else None,
                "holding_minutes": holding_minutes,
                "received_ts_utc": iso_utc(received_ts),
                "source_status": "OK",
                "source_error": None,
                "raw_payload_json": json.dumps(pos, sort_keys=True, default=str),
            }
        )
    return rows


def _symbol_spec_rows(
    *,
    profile_id: str,
    broker: str,
    source: str,
    symbols: list[str],
    asset_class_by_symbol: dict[str, str],
    suffix_map: dict[str, str],
    updated_ts: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        asset_class = asset_class_by_symbol.get(symbol, "")
        suffix = suffix_map.get(symbol, "")
        rows.append(
            {
                "broker": broker,
                "profile_id": profile_id,
                "symbol": symbol,
                "broker_symbol": _broker_symbol(symbol, suffix),
                "min_lot": None,
                "max_lot": None,
                "lot_step": None,
                "pip_size": _pip_size(symbol) if asset_class == "forex" else None,
                "tick_size": None,
                "tick_value": None,
                "contract_size": None,
                "trade_allowed": None,
                "source": source,
                "updated_ts_utc": iso_utc(updated_ts),
                "source_status": "PARTIAL",
                "source_error": "dwx_symbol_specs_not_available; static broker symbol only",
                "raw_payload_json": json.dumps(
                    {"asset_class": asset_class, "broker_symbol": _broker_symbol(symbol, suffix)},
                    sort_keys=True,
                ),
            }
        )
    return rows


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def persist_snapshot(
    *,
    db_path: str,
    profile_id: str,
    source: str,
    ingest_run: dict[str, Any],
    quote_rows: list[dict[str, Any]],
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    specs: list[dict[str, Any]],
) -> None:
    with connect_wal(db_path) as conn:
        for row in quote_rows:
            latest = dict(row)
            latest.pop("ingest_run_id", None)
            _insert_dict(conn, "vanguard_context_quote_latest", latest, replace=True)
            _insert_dict(conn, "vanguard_context_quote_history", row)

        account_latest = dict(account)
        account_latest.pop("ingest_run_id", None)
        _insert_dict(conn, "vanguard_context_account_latest", account_latest, replace=True)
        _insert_dict(conn, "vanguard_context_account_history", account)

        conn.execute(
            "DELETE FROM vanguard_context_positions_latest WHERE profile_id = ? AND source = ?",
            (profile_id, source),
        )
        conn.execute(
            "DELETE FROM vanguard_context_orders_latest WHERE profile_id = ? AND source = ?",
            (profile_id, source),
        )
        for row in positions:
            latest = dict(row)
            latest.pop("ingest_run_id", None)
            _insert_dict(conn, "vanguard_context_positions_latest", latest, replace=True)
            _insert_dict(conn, "vanguard_context_positions_history", row)

        for row in specs:
            _insert_dict(conn, "vanguard_context_symbol_specs", row, replace=True)

        _insert_dict(conn, "vanguard_context_ingest_runs", ingest_run, replace=True)
        conn.commit()


def persist_ingest_error(
    *,
    db_path: str,
    profile_id: str,
    source_id: str,
    source: str,
    broker: str,
    account_number: str,
    started_ts: datetime,
    error: str,
) -> None:
    finished = now_utc()
    run = {
        "ingest_run_id": f"{profile_id}:{source}:{int(finished.timestamp() * 1000)}",
        "profile_id": profile_id,
        "broker": broker,
        "account_number": account_number,
        "source": source,
        "context_source_id": source_id,
        "started_ts_utc": iso_utc(started_ts),
        "finished_ts_utc": iso_utc(finished),
        "symbols_requested": 0,
        "quotes_written": 0,
        "account_written": 0,
        "positions_written": 0,
        "orders_written": 0,
        "specs_written": 0,
        "source_status": "ERROR",
        "source_error": error,
        "coverage_json": json.dumps({"error": error}, sort_keys=True),
    }
    with connect_wal(db_path) as conn:
        _insert_dict(conn, "vanguard_context_ingest_runs", run, replace=True)
        conn.commit()


def collect_once(
    *,
    adapter: MT5DWXAdapter,
    db_path: str,
    profile_id: str,
    context_source_id: str,
    source_cfg: dict[str, Any],
    symbols: list[str],
    asset_class_by_symbol: dict[str, str],
    suffix_map: dict[str, str],
    subscribe_sleep_sec: float,
    coverage_report: Path,
) -> dict[str, Any]:
    started_ts = now_utc()
    received_ts = started_ts
    source = str(source_cfg.get("source") or "mt5_dwx")
    broker = str(source_cfg.get("broker") or "gft")
    account_number = _profile_account_number(source_cfg, profile_id)
    ingest_run_id = f"{profile_id}:{source}:{int(started_ts.timestamp() * 1000)}"
    market_file = Path(adapter.dwx_files_path) / "DWX" / "DWX_Market_Data.txt"

    adapter.subscribe_ticks(symbols)
    if subscribe_sleep_sec > 0:
        time.sleep(subscribe_sleep_sec)
    received_ts = now_utc()
    quote_ts = _utc_from_mtime(market_file)

    source_status = "OK"
    source_error: str | None = None
    try:
        quotes = adapter.get_live_quotes(symbols)
    except Exception as exc:  # noqa: BLE001
        quotes = {}
        source_status = "ERROR"
        source_error = f"quote_fetch_failed: {exc}"

    quote_rows = [
        _quote_row(
            ingest_run_id=ingest_run_id,
            profile_id=profile_id,
            broker=broker,
            account_number=account_number,
            source=source,
            symbol=sym,
            quote=quotes.get(sym),
            quote_ts=quote_ts,
            received_ts=received_ts,
            suffix=suffix_map.get(sym, ""),
            asset_class=asset_class_by_symbol.get(sym, ""),
        )
        for sym in symbols
    ]

    try:
        account = _account_row(
            ingest_run_id=ingest_run_id,
            profile_id=profile_id,
            broker=broker,
            account_number=account_number,
            source=source,
            adapter=adapter,
            received_ts=received_ts,
        )
    except Exception as exc:  # noqa: BLE001
        source_status = "ERROR"
        source_error = f"account_fetch_failed: {exc}"
        account = {
            "ingest_run_id": ingest_run_id,
            "profile_id": profile_id,
            "broker": broker,
            "account_number": account_number,
            "account_name": "",
            "currency": "",
            "balance": None,
            "equity": None,
            "free_margin": None,
            "margin_used": None,
            "margin_level": None,
            "leverage": None,
            "received_ts_utc": iso_utc(received_ts),
            "source": source,
            "source_status": "ERROR",
            "source_error": str(exc),
            "raw_payload_json": "{}",
        }

    try:
        positions = _position_rows(
            ingest_run_id=ingest_run_id,
            profile_id=profile_id,
            broker=broker,
            account_number=account_number,
            source=source,
            adapter=adapter,
            received_ts=received_ts,
        )
    except Exception as exc:  # noqa: BLE001
        positions = []
        source_status = "ERROR"
        source_error = f"positions_fetch_failed: {exc}"

    specs = _symbol_spec_rows(
        profile_id=profile_id,
        broker=broker,
        source=source,
        symbols=symbols,
        asset_class_by_symbol=asset_class_by_symbol,
        suffix_map=suffix_map,
        updated_ts=received_ts,
    )
    quotes_written = sum(1 for row in quote_rows if row["source_status"] == "OK")
    symbols_by_asset: dict[str, list[str]] = {}
    for symbol in symbols:
        symbols_by_asset.setdefault(asset_class_by_symbol.get(symbol, "unknown"), []).append(symbol)
    quotes_written_by_asset = {
        asset: sum(
            1
            for row in quote_rows
            if row["source_status"] == "OK" and asset_class_by_symbol.get(row["symbol"], "unknown") == asset
        )
        for asset in symbols_by_asset
    }
    coverage = {
        "received_ts_utc": iso_utc(received_ts),
        "profile_id": profile_id,
        "context_source_id": context_source_id,
        "source": source,
        "broker": broker,
        "account_number": account.get("account_number") or account_number,
        "symbols_requested": len(symbols),
        "symbols_by_asset_class": {asset: sorted(values) for asset, values in symbols_by_asset.items()},
        "quotes_written": quotes_written,
        "quotes_written_by_asset_class": quotes_written_by_asset,
        "quote_symbols_missing": [row["symbol"] for row in quote_rows if row["source_status"] != "OK"],
        "account_written": 1 if account.get("source_status") == "OK" else 0,
        "positions_written": len(positions),
        "orders_written": 0,
        "specs_written": len(specs),
        "quote_file_ts_utc": iso_utc(quote_ts) if quote_ts else None,
        "source_status": source_status,
        "source_error": source_error,
        "db_path": db_path,
    }
    ingest_run = {
        "ingest_run_id": ingest_run_id,
        "profile_id": profile_id,
        "broker": broker,
        "account_number": str(account.get("account_number") or account_number),
        "source": source,
        "context_source_id": context_source_id,
        "started_ts_utc": iso_utc(started_ts),
        "finished_ts_utc": iso_utc(received_ts),
        "symbols_requested": len(symbols),
        "quotes_written": quotes_written,
        "account_written": coverage["account_written"],
        "positions_written": len(positions),
        "orders_written": 0,
        "specs_written": len(specs),
        "source_status": source_status,
        "source_error": source_error,
        "coverage_json": json.dumps(coverage, sort_keys=True, default=str),
    }

    persist_snapshot(
        db_path=db_path,
        profile_id=profile_id,
        source=source,
        ingest_run=ingest_run,
        quote_rows=quote_rows,
        account=account,
        positions=positions,
        specs=specs,
    )
    try:
        if symbols_by_asset.get("forex"):
            pair_state_rows = build_forex_pair_state(
                db_path=db_path,
                cycle_ts_utc=iso_utc(received_ts),
                profile_ids=[profile_id],
                symbols=symbols_by_asset["forex"],
            )
            coverage["forex_pair_state_rows"] = len(pair_state_rows)
            coverage["forex_pair_state_ok"] = sum(1 for row in pair_state_rows if row.get("source_status") == "OK")
    except Exception as exc:  # noqa: BLE001
        logger.warning("forex pair-state build failed: %s", exc)
        coverage["forex_pair_state_error"] = str(exc)
    try:
        if symbols_by_asset.get("crypto"):
            crypto_state_rows = build_crypto_symbol_state(
                db_path=db_path,
                cycle_ts_utc=iso_utc(received_ts),
                profile_ids=[profile_id],
                symbols=symbols_by_asset["crypto"],
            )
            coverage["crypto_symbol_state_rows"] = len(crypto_state_rows)
            coverage["crypto_symbol_state_ok"] = sum(1 for row in crypto_state_rows if row.get("source_status") == "OK")
    except Exception as exc:  # noqa: BLE001
        logger.warning("crypto symbol-state build failed: %s", exc)
        coverage["crypto_symbol_state_error"] = str(exc)
    coverage_report.parent.mkdir(parents=True, exist_ok=True)
    coverage_report.write_text(json.dumps(coverage, indent=2, sort_keys=True, default=str))
    return coverage


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persist MT5/DWX live broker context facts into Vanguard QA DB.")
    parser.add_argument("--runtime-config", default=str(DEFAULT_RUNTIME_CONFIG))
    parser.add_argument("--db", default=None)
    parser.add_argument("--context-source-id", default=None)
    parser.add_argument("--dwx-files-path", default=None)
    parser.add_argument("--symbol-suffix", default=None)
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols. Default: runtime forex universe.")
    parser.add_argument("--interval-sec", type=float, default=5.0)
    parser.add_argument("--cycles", type=int, default=0, help="0 means run forever unless --once is set.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--subscribe-sleep-sec", type=float, default=2.0)
    parser.add_argument("--coverage-report", default=str(DEFAULT_COVERAGE_REPORT))
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = _load_runtime(Path(args.runtime_config).expanduser())
    context_source_id, source_cfg = _resolve_context_source(cfg, args.context_source_id)
    profile_id = _resolve_profile_id(args.profile_id, source_cfg)
    source = str(source_cfg.get("source") or "mt5_dwx")
    broker = str(source_cfg.get("broker") or "gft")
    account_number = _profile_account_number(source_cfg, profile_id)
    db_path = _resolve_db_path(cfg, args.db)
    dwx_path = _resolve_dwx_path(source_cfg, args.dwx_files_path)
    suffix = _resolve_suffix(source_cfg, args.symbol_suffix)
    loaded_symbols, asset_class_by_symbol, symbols_by_asset = _load_symbols_for_context_source(
        cfg,
        source_cfg,
        profile_id,
    )
    symbols = [_normalise_symbol(s) for s in str(args.symbols).split(",") if _normalise_symbol(s)] if args.symbols else loaded_symbols
    if args.symbols:
        asset_class_by_symbol = {
            symbol: asset_class_by_symbol.get(symbol) or _classify_symbol_from_runtime(cfg, symbol) or "unknown"
            for symbol in symbols
        }
        symbols_by_asset = {}
        for symbol, asset_class in asset_class_by_symbol.items():
            symbols_by_asset.setdefault(asset_class, []).append(symbol)
    if not symbols:
        logger.error("No symbols found. Provide --symbols or configure runtime universes/context_sources.primary_for.")
        return 2
    suffix_map = _resolve_symbol_suffix_map(source_cfg, symbols, asset_class_by_symbol, suffix)

    ensure_schema(db_path)

    adapter = MT5DWXAdapter(
        dwx_files_path=dwx_path,
        db_path=db_path,
        symbol_suffix=suffix,
        symbol_suffix_map=suffix_map,
    )
    started_ts = now_utc()
    if not adapter.connect():
        error = "DWX connection failed. Is MT5 running with DWX EA active?"
        logger.error(error)
        persist_ingest_error(
            db_path=db_path,
            profile_id=profile_id,
            source_id=context_source_id,
            source=source,
            broker=broker,
            account_number=account_number,
            started_ts=started_ts,
            error=error,
        )
        return 1

    logger.info(
        "DWX context daemon started profile_id=%s context_source_id=%s source=%s symbols=%d assets=%s db=%s",
        profile_id,
        context_source_id,
        source,
        len(symbols),
        {asset: len(values) for asset, values in symbols_by_asset.items()},
        db_path,
    )
    count = 0
    try:
        while True:
            count += 1
            coverage = collect_once(
                adapter=adapter,
                db_path=db_path,
                profile_id=profile_id,
                context_source_id=context_source_id,
                source_cfg=source_cfg,
                symbols=symbols,
                asset_class_by_symbol=asset_class_by_symbol,
                suffix_map=suffix_map,
                subscribe_sleep_sec=args.subscribe_sleep_sec,
                coverage_report=Path(args.coverage_report),
            )
            logger.info(
                "coverage profile=%s quotes=%d/%d account=%d positions=%d orders=%d status=%s report=%s",
                profile_id,
                coverage["quotes_written"],
                coverage["symbols_requested"],
                coverage["account_written"],
                coverage["positions_written"],
                coverage["orders_written"],
                coverage["source_status"],
                args.coverage_report,
            )
            if args.once or (args.cycles and count >= args.cycles):
                break
            time.sleep(max(0.1, float(args.interval_sec)))
    finally:
        adapter.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
