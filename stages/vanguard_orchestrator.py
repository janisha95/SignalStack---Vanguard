"""
vanguard_orchestrator.py — V7 Session Lifecycle Orchestrator.

Runs the Vanguard pipeline (V3→V5→V6) every 5 minutes during
market hours and optionally executes trades via SignalStack.

Session window: 09:35–15:25 ET (configurable)
Cycle cadence:  every 5 minutes, aligned to bar boundaries + 10s buffer
EOD flatten:    15:50 ET for must_close_eod=1 accounts

Execution modes:
  manual — writes FORWARD_TRACKED only (no external orders)
  live   — calls live execution bridges
  test   — no external orders and no DB/session/execution-log writes

CLI:
  python3 stages/vanguard_orchestrator.py                          # default (mode=manual)
  python3 stages/vanguard_orchestrator.py --execution-mode manual
  python3 stages/vanguard_orchestrator.py --execution-mode test --single-cycle
  python3 stages/vanguard_orchestrator.py --execution-mode live
  python3 stages/vanguard_orchestrator.py --single-cycle --dry-run
  python3 stages/vanguard_orchestrator.py --status
  python3 stages/vanguard_orchestrator.py --flatten-now

Location: ~/SS/Vanguard/stages/vanguard_orchestrator.py
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict, deque
import fcntl
import html
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.clock import now_et, now_utc, iso_utc, round_down_5m, utc_to_et
from vanguard.helpers.account_truth import fetch_latest_context_account_row
from vanguard.config.runtime_config import (
    _DEFAULT_CONFIG_PATH,
    binding_allows_legacy,
    get_data_sources_config,
    get_execution_config,
    get_market_data_config,
    get_runtime_config,
    get_shadow_gate_config,
    get_shadow_db_path,
    is_replay_from_source_db,
    load_runtime_config,
    resolve_exit_policy_id,
    resolve_market_data_source_id,
    resolve_profile_lane_binding,
)
from vanguard.helpers.bars import aggregate_1m_to_5m, aggregate_1m_to_timeframe, aggregate_5m_to_1h, parse_utc
from vanguard.helpers.db import VanguardDB, checkpoint_wal_truncate, sqlite_conn, warn_if_large_wal
from vanguard.helpers.eod_flatten import check_eod_action, get_positions_to_flatten
from vanguard.helpers.universe_builder import classify_asset_class, materialize_universe_members
from vanguard.data_adapters.alpaca_adapter import AlpacaAdapter, AlpacaWSAdapter, get_active_equity_symbols
from vanguard.data_adapters.ibkr_adapter import IBKRAdapter, IBKRStreamingAdapter
from vanguard.data_adapters.twelvedata_adapter import TwelveDataAdapter, load_from_config as load_twelvedata_adapter
from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter
from vanguard.execution.signalstack_adapter import SignalStackAdapter
from vanguard.execution.telegram_alerts import TelegramAlerts
from vanguard.execution.trade_journal import (
    ensure_table as _journal_ensure_table,
    insert_approval_row as _journal_insert_approval,
    update_submitted as _journal_update_submitted,
    update_filled as _journal_update_filled,
    update_rejected_by_broker as _journal_update_rejected,
    update_skipped_before_submit as _journal_update_skipped_before_submit,
)
from vanguard.execution.forward_checkpoints import (
    ensure_table as _forward_checkpoints_ensure_table,
    refresh_checkpoints as _forward_checkpoints_refresh,
)
from vanguard.execution.signal_decision_log import (
    ensure_table as _signal_decision_log_ensure_table,
    insert_decision as _signal_decision_insert,
    update_decision as _signal_decision_update,
)
from vanguard.execution.signal_forward_checkpoints import (
    ensure_table as _signal_forward_checkpoints_ensure_table,
    refresh_checkpoints as _signal_forward_checkpoints_refresh,
)
from vanguard.execution.timeout_policy import (
    get_policy_for_session as _get_timeout_policy_for_session,
    normalize_session_bucket as _normalize_timeout_session_bucket,
    refresh_timeout_policy_data as _refresh_timeout_policy_data,
)
from vanguard.risk.risky_policy_adapter import (
    compile_effective_risk_policy as _compile_effective_risk_policy,
    load_risk_rules as _load_risk_rules,
)
from scripts.dwx_live_context_daemon import (
    collect_once as _dwx_collect_context_once,
    ensure_schema as _ensure_dwx_context_schema,
    _load_symbols_for_context_source as _dwx_load_symbols_for_context_source,
    _resolve_suffix as _dwx_resolve_default_suffix,
    _resolve_symbol_suffix_map as _dwx_resolve_symbol_suffix_map,
)
from scripts.metaapi_live_context_daemon import (
    collect_once as _metaapi_collect_context_once,
)
from vanguard.execution.metaapi_context_adapter import MetaApiContextAdapter
from vanguard.execution.metaapi_client import MetaApiUnavailable
from vanguard.analytics.cycle_audit import refresh_cycle_audit as _refresh_cycle_audit

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_orchestrator")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DB_PATH = get_shadow_db_path()
_IBKR_INTRADAY_DB = str(_REPO_ROOT / "data" / "ibkr_intraday.db")
_EXEC_CFG_PATH = _REPO_ROOT / "config" / "vanguard_execution_config.json"
_CYCLE_TRACE_PATH = _REPO_ROOT / "data" / "operator_logs" / "vanguard_cycle_trace.jsonl"
_LOOP_LOCK_FH = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CYCLE_MINUTES         = 5           # pipeline cadence
CYCLE_BUFFER_SECONDS  = 10          # extra buffer after bar open
MAX_CONSECUTIVE_FAILS = 3           # abort after N consecutive failures
DEFAULT_SESSION_START = "09:35"
DEFAULT_SESSION_END   = "15:25"
DEFAULT_EOD_FLATTEN   = "15:50"
DEFAULT_NO_NEW_AFTER  = "15:45"
UNIVERSE_REFRESH_TIMES = {"09:35", "12:00", "15:00"}
TELEGRAM_CHUNK_LIMIT = int(get_runtime_config().get("telegram", {}).get("max_message_chars", 3500))
_MTF_INTRADAY_MINUTES = (10, 15, 30)
_V1_PRECOMPUTE_SERVICE_NAME = "v1_precompute"


def _infer_model_family(model_source: Any) -> Optional[str]:
    value = str(model_source or "").strip().lower()
    if not value:
        return None
    if "forward_return" in value:
        return "forward_return"
    if "raw_return" in value:
        return "raw_return"
    if "classification" in value or "classifier" in value:
        return "classification"
    if "regressor" in value or "ridge" in value or "lgbm" in value:
        return "regressor"
    return value


def _load_asset_session_windows() -> dict[str, dict[str, Any]]:
    sessions = (get_market_data_config().get("sessions") or {})
    resolved: dict[str, dict[str, Any]] = {}
    for asset_class, session in sessions.items():
        resolved[str(asset_class).lower()] = {
            "days": set(session.get("days") or []),
            "start": session.get("start"),
            "end": session.get("end"),
        }
    return resolved or {
        "equity": {"days": {0, 1, 2, 3, 4}, "start": "09:30", "end": "16:00"},
        "index": {"days": {0, 1, 2, 3, 4}, "start": "09:30", "end": "16:00"},
        "forex": {"days": {6, 0, 1, 2, 3, 4}, "start": "17:00", "end": "17:00"},
        "crypto": {"days": {0, 1, 2, 3, 4, 5, 6}, "start": None, "end": None},
    }


_ASSET_SESSION_WINDOWS = _load_asset_session_windows()

# ---------------------------------------------------------------------------
# Session log schema
# ---------------------------------------------------------------------------

_CREATE_SESSION_LOG = """
CREATE TABLE IF NOT EXISTS vanguard_session_log (
    date            TEXT PRIMARY KEY,
    session_start   TEXT,
    session_end     TEXT,
    total_cycles    INTEGER DEFAULT 0,
    failed_cycles   INTEGER DEFAULT 0,
    trades_placed   INTEGER DEFAULT 0,
    forward_tracked INTEGER DEFAULT 0,
    execution_mode  TEXT,
    status          TEXT DEFAULT 'running',
    notes           TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_execution_mode(mode: str | None) -> str:
    """Map legacy aliases onto the canonical manual/live/test execution modes."""
    normalized = str(mode or "manual").lower().strip()
    if normalized in {"off", "paper"}:
        return "manual"
    return normalized


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    """Parse "HH:MM" into (hour, minute)."""
    h, m = time_str.split(":")
    return int(h), int(m)


def _canonical_symbol(symbol: str | None) -> str:
    return str(symbol or "").upper().replace("/", "").replace(" ", "")


def _expand_mt5_asset_classes(asset_classes: set[str]) -> set[str]:
    expanded = {str(asset_class or "").lower() for asset_class in asset_classes}
    if "commodity" in expanded:
        expanded.update({"metal", "energy", "agriculture"})
    return expanded


def _is_asset_session_active(asset_class: str, et: datetime) -> bool:
    """Return True when the asset class market session is active in ET."""
    normalized = str(asset_class or "").lower().strip()
    session = _ASSET_SESSION_WINDOWS.get(normalized)
    if not session:
        return False
    if session["start"] is None or session["end"] is None:
        return True

    start_h, start_m = _parse_hhmm(session["start"])
    end_h, end_m = _parse_hhmm(session["end"])
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    now_minutes = et.hour * 60 + et.minute
    allowed_days = session["days"]

    if normalized == "forex" and start_minutes == end_minutes:
        if et.weekday() in {0, 1, 2, 3}:
            return True
        if et.weekday() == 4:
            return now_minutes < end_minutes
        if et.weekday() == 6:
            return now_minutes >= start_minutes
        return False

    if start_minutes < end_minutes:
        return et.weekday() in allowed_days and start_minutes <= now_minutes < end_minutes

    if now_minutes >= start_minutes:
        return et.weekday() in allowed_days
    previous_day = (et.weekday() - 1) % 7
    return previous_day in allowed_days


def _acquire_loop_lock(db_path: str) -> bool:
    """Prevent duplicate --loop orchestrators from running against the same DB."""
    global _LOOP_LOCK_FH

    lock_path = Path(db_path).with_suffix(".vanguard_loop.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _LOOP_LOCK_FH = open(lock_path, "w")
    try:
        fcntl.flock(_LOOP_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error(
            "Another Vanguard loop already holds %s; exiting to prevent duplicate Telegrams.",
            lock_path,
        )
        _LOOP_LOCK_FH.close()
        _LOOP_LOCK_FH = None
        return False

    _LOOP_LOCK_FH.seek(0)
    _LOOP_LOCK_FH.truncate()
    _LOOP_LOCK_FH.write(f"pid={os.getpid()} started_at={datetime.now().isoformat()}\n")
    _LOOP_LOCK_FH.flush()
    logger.info("Acquired Vanguard loop lock: %s", lock_path)
    return True


def _load_exec_config() -> dict:
    """Load execution config from the canonical QA runtime snapshot."""
    cfg = get_execution_config()
    if cfg:
        return cfg
    with open(_EXEC_CFG_PATH) as fh:
        return json.load(fh)


def _load_accounts(db_path: str = _DB_PATH) -> list[dict]:
    """Load active account profiles.

    JSON runtime config profiles[] is authoritative. The legacy
    account_profiles DB table is used only when the JSON list is empty.
    """
    from vanguard.config.runtime_config import get_profiles_config

    json_profiles = [
        p for p in get_profiles_config()
        if str(p.get("is_active", "")).strip().lower() in {"1", "true", "yes", "on"}
    ]
    if json_profiles:
        accounts = []
        for p in json_profiles:
            accounts.append({
                "id":               str(p.get("id") or ""),
                "name":             str(p.get("name") or p.get("display_name") or p.get("id") or ""),
                "is_active":        1,
                "policy_id":        str(p.get("policy_id") or ""),
                "risk_profile_id":  str(p.get("risk_profile_id") or ""),
                "account_size":     p.get("account_size", 0),
                "instrument_scope": str(p.get("instrument_scope") or ""),
                "context_source_id": str(p.get("context_source_id") or ""),
                "context_health_mode": str(p.get("context_health_mode") or ""),
                "execution_mode":   str(p.get("execution_mode") or ""),
                "execution_mode_by_asset_class": dict(p.get("execution_mode_by_asset_class") or {}),
                "asset_lanes":      dict(p.get("asset_lanes") or {}),
                "live_allowed_asset_classes": [
                    str(asset).strip()
                    for asset in (p.get("live_allowed_asset_classes") or [])
                    if str(asset).strip()
                ],
                "environment":      str(p.get("environment") or "prod"),
                "execution_bridge": str(p.get("execution_bridge") or ""),
                "must_close_eod":   int(p.get("must_close_eod") or 0),
                "disabled":         0,
            })
        logger.info(
            "_load_accounts: loaded %d active profile(s) from JSON config: %s",
            len(accounts), [a["id"] for a in accounts],
        )
        return accounts

    # Fallback: legacy DB table (used only when JSON profiles[] is empty)
    logger.warning("_load_accounts: JSON profiles empty — falling back to DB account_profiles table")
    try:
        with sqlite_conn(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM account_profiles WHERE is_active = 1"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning(f"Could not load account profiles: {exc}")
        return []


def _resolve_active_mt5_local_config(runtime_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the live MT5/DWX config from active profiles before legacy fallback."""
    cfg = runtime_cfg or get_runtime_config()
    profiles = cfg.get("profiles") or []
    context_sources = cfg.get("context_sources") or {}

    for profile in profiles:
        if str(profile.get("is_active") or "").strip().lower() not in {"1", "true", "yes", "on"}:
            continue
        bridge = str(profile.get("execution_bridge") or "")
        source_id = str(profile.get("context_source_id") or "")
        if not bridge.startswith("mt5_local") or not source_id:
            continue
        source_cfg = dict(context_sources.get(source_id) or {})
        if not source_cfg or not bool(source_cfg.get("enabled", False)):
            continue
        source_cfg["_source_id"] = source_id
        source_cfg["_profile_id"] = str(profile.get("id") or "")
        return source_cfg

    # Keep V1 off DWX unless an active profile explicitly requires mt5_local.
    return {}


def _ensure_execution_log(db_path: str = _DB_PATH) -> None:
    """Ensure execution_log table exists (matches trade_desk.py schema)."""
    create_sql = """
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
    with sqlite_conn(db_path) as con:
        con.execute(create_sql)
        con.commit()


def _ensure_execution_attempts(db_path: str = _DB_PATH) -> None:
    """Ensure the canonical per-approved execution-attempt fact table exists."""
    create_sql = """
    CREATE TABLE IF NOT EXISTS vanguard_execution_attempts (
        attempt_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_ts_utc          TEXT NOT NULL,
        profile_id            TEXT NOT NULL,
        decision_stream_id    TEXT,
        trade_id              TEXT,
        symbol                TEXT NOT NULL,
        direction             TEXT NOT NULL,
        execution_mode        TEXT NOT NULL,
        execution_bridge      TEXT NOT NULL,
        attempt_status        TEXT NOT NULL,
        reason_code           TEXT,
        broker_order_id       TEXT,
        broker_position_id    TEXT,
        response_json         TEXT NOT NULL DEFAULT '{}',
        created_at_utc        TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_execution_attempts_cycle_profile
        ON vanguard_execution_attempts(cycle_ts_utc, profile_id, symbol, direction);
    CREATE INDEX IF NOT EXISTS idx_execution_attempts_trade
        ON vanguard_execution_attempts(trade_id, created_at_utc);
    """
    with sqlite_conn(db_path) as con:
        con.executescript(create_sql)
        con.commit()


def _write_execution_row(
    symbol: str,
    direction: str,
    tier: str,
    shares: int,
    entry_price: Optional[float],
    stop_loss: Optional[float],
    take_profit: Optional[float],
    status: str,
    response_json: dict,
    account: Optional[str] = None,
    source_scores: Optional[dict] = None,
    tags: Optional[list] = None,
    notes: Optional[str] = None,
    db_path: str = _DB_PATH,
) -> Optional[int]:
    """Write one row to execution_log (canonical table). Returns new row id."""
    try:
        execution_fee = max(0.01 * shares, 0.0)
        with sqlite_conn(db_path) as con:
            cur = con.execute(
                """
                INSERT INTO execution_log
                    (symbol, direction, tier, shares, entry_price, stop_loss, take_profit,
                     order_type, status, signalstack_response, account, source_scores,
                     tags, notes, execution_fee)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol, direction, tier, shares,
                    entry_price, stop_loss, take_profit,
                    "market", status,
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
        logger.error(f"Failed to write execution_log: {exc}")
        return None


def _write_execution_attempt(
    *,
    cycle_ts_utc: str,
    profile_id: str,
    decision_stream_id: str | None,
    trade_id: str | None,
    symbol: str,
    direction: str,
    execution_mode: str,
    execution_bridge: str,
    attempt_status: str,
    reason_code: str | None = None,
    broker_order_id: str | None = None,
    broker_position_id: str | None = None,
    response_json: dict[str, Any] | None = None,
    db_path: str = _DB_PATH,
) -> Optional[int]:
    """Write one final execution-attempt outcome for an approved trade candidate."""
    try:
        with sqlite_conn(db_path) as con:
            cur = con.execute(
                """
                INSERT INTO vanguard_execution_attempts
                    (cycle_ts_utc, profile_id, decision_stream_id, trade_id, symbol, direction,
                     execution_mode, execution_bridge, attempt_status, reason_code,
                     broker_order_id, broker_position_id, response_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_ts_utc,
                    profile_id,
                    decision_stream_id,
                    trade_id,
                    symbol,
                    direction,
                    execution_mode,
                    execution_bridge,
                    attempt_status,
                    reason_code,
                    broker_order_id,
                    broker_position_id,
                    json.dumps(response_json or {}, sort_keys=True, default=str),
                ),
            )
            con.commit()
            return cur.lastrowid
    except Exception as exc:
        logger.error("Failed to write vanguard_execution_attempts: %s", exc)
        return None


def _get_approved_rows(db_path: str = _DB_PATH, cycle_ts: str | None = None) -> list[dict]:
    """Load APPROVED rows from vanguard_tradeable_portfolio, scoped to cycle when provided."""
    try:
        with sqlite_conn(db_path) as con:
            con.row_factory = sqlite3.Row
            latest_cycle = cycle_ts
            if not latest_cycle:
                row = con.execute(
                    "SELECT MAX(cycle_ts_utc) AS latest FROM vanguard_tradeable_portfolio"
                ).fetchone()
                if not row or not row["latest"]:
                    return []
                latest_cycle = row["latest"]
            rows = con.execute(
                """
                SELECT * FROM vanguard_tradeable_portfolio
                WHERE cycle_ts_utc = ? AND status = 'APPROVED'
                ORDER BY account_id, symbol
                """,
                (latest_cycle,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning(f"Could not load approved rows: {exc}")
        return []


def _get_tradeable_rows(db_path: str = _DB_PATH, cycle_ts: str | None = None) -> list[dict]:
    """Load all tradeable-portfolio rows for a cycle."""
    try:
        with sqlite_conn(db_path) as con:
            con.row_factory = sqlite3.Row
            latest_cycle = cycle_ts
            if not latest_cycle:
                row = con.execute(
                    "SELECT MAX(cycle_ts_utc) AS latest FROM vanguard_tradeable_portfolio"
                ).fetchone()
                if not row or not row["latest"]:
                    return []
                latest_cycle = row["latest"]
            rows = con.execute(
                """
                SELECT * FROM vanguard_tradeable_portfolio
                WHERE cycle_ts_utc = ?
                ORDER BY account_id, symbol, direction
                """,
                (latest_cycle,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("Could not load tradeable rows: %s", exc)
        return []


# ---------------------------------------------------------------------------
# VanguardOrchestrator
# ---------------------------------------------------------------------------


class VanguardOrchestrator:
    """
    V7 Session Lifecycle Orchestrator.

    Runs V3→V5→V6 every 5 minutes during market hours.
    Writes results to execution_log (canonical table — NOT vanguard_execution_log).
    """

    def __init__(
        self,
        execution_mode: str = "manual",
        dry_run: bool = False,
        db_path: str = _DB_PATH,
        session_start: str = DEFAULT_SESSION_START,
        session_end: str = DEFAULT_SESSION_END,
        eod_flatten_time: str = DEFAULT_EOD_FLATTEN,
        no_new_positions_after: str = DEFAULT_NO_NEW_AFTER,
        force_regime: str | None = None,
        equity_data_source: str = "auto",
        force_assets: str | None = None,
        cycle_interval_seconds: int = CYCLE_MINUTES * 60,
        cycle_offset_seconds: int = 0,
        force_cycle_ts: str | None = None,
        no_telegram: bool = False,
        _now_et_fn: Optional[Callable[[], datetime]] = None,
        _now_utc_fn: Optional[Callable[[], datetime]] = None,
    ):
        boot_t0 = time.time()
        execution_mode = _normalize_execution_mode(execution_mode)
        if dry_run:
            execution_mode = "test"
        if execution_mode not in ("manual", "live", "test"):
            raise ValueError(
                f"Invalid execution_mode: {execution_mode!r}. Use manual/live/test"
            )

        self.execution_mode         = execution_mode
        self.dry_run                = bool(dry_run or execution_mode == "test")
        self.db_path                = db_path
        self.session_start          = session_start
        self.session_end            = session_end
        self.eod_flatten_time       = eod_flatten_time
        self.no_new_positions_after = no_new_positions_after
        self.force_regime           = force_regime.upper() if force_regime else None
        self.equity_data_source     = str(equity_data_source or "auto").lower()
        self._force_cycle_ts        = str(force_cycle_ts).strip() if force_cycle_ts else None
        self._no_telegram           = bool(no_telegram)
        self._force_assets = {
            asset.strip().lower()
            for asset in str(force_assets or "").split(",")
            if asset.strip()
        }
        self.cycle_interval_seconds = max(int(cycle_interval_seconds), 1)
        self.cycle_offset_seconds = max(int(cycle_offset_seconds), 0) % (CYCLE_MINUTES * 60)

        # Injectable time functions for testability
        self._now_et  = _now_et_fn  if _now_et_fn  is not None else now_et
        self._now_utc = _now_utc_fn if _now_utc_fn is not None else now_utc

        # Runtime state
        self._running              = False
        self._session_date: str    = ""
        self._session_start_ts: Optional[datetime] = None
        self._total_cycles         = 0
        self._failed_cycles        = 0
        self._consecutive_failures = 0
        self._trades_placed        = 0
        self._forward_tracked      = 0
        self._shutdown_requested   = False
        self._last_universe_refresh: str | None = None
        self.last_cycle_duration:  float = 0.0
        self._cycle_trace_path = _CYCLE_TRACE_PATH
        self._active_cycle_trace: dict[str, Any] | None = None
        self._shortlist_direction_history = defaultdict(lambda: deque(maxlen=5))
        self._current_resolved_universe = None  # set each cycle by resolve_universe_for_cycle

        # Phase 2a: load + validate runtime config (kill switch on failure)
        try:
            from vanguard.accounts.runtime_config import ConfigValidationError, load_runtime_config as _load_p2a_cfg
            self._runtime_config = _load_p2a_cfg()
        except Exception as exc:
            logger.error("Phase 2a config load failed: %s — orchestrator will start with no Phase 2a config", exc)
            self._runtime_config = None

        # Phase 5: track config file mtime for live reload
        self._config_path = Path(_DEFAULT_CONFIG_PATH)
        try:
            self._config_mtime: float = self._config_path.stat().st_mtime if self._config_path.exists() else 0.0
        except OSError:
            self._config_mtime = 0.0
        self._phase5_reload_flag = Path("/tmp/vanguard_config_reload")

        # Config + adapters
        t = time.time()
        try:
            self._exec_cfg = _load_exec_config()
        except Exception as exc:
            logger.warning(f"Could not load execution config: {exc} — using defaults")
            self._exec_cfg = {"signalstack": {}, "telegram": {}, "defaults": {}}
        logger.info("[BOOT] load_exec_config took %.2fs", time.time() - t)

        t = time.time()
        self._ss_adapter = self._init_signalstack()
        logger.info("[BOOT] init_signalstack took %.2fs", time.time() - t)

        t = time.time()
        self._telegram   = self._init_telegram()
        if self._no_telegram:
            self._telegram = None
            logger.info("[BOOT] --no-telegram: Telegram disabled")
        logger.info("[BOOT] init_telegram took %.2fs", time.time() - t)

        t = time.time()
        self._accounts   = _load_accounts(db_path)
        logger.info("[BOOT] load_accounts took %.2fs", time.time() - t)

        # === ASSET-CLASS AWARE SESSION WINDOW (additive override) ===
        # If any active account has a forex-capable scope, polling follows asset
        # session rules instead of a profile-specific window.
        runtime_universes = (self._runtime_config or {}).get("universes") or {}
        has_forex_scope = False
        for account in self._accounts:
            scope_id = str(account.get("instrument_scope") or "")
            scope_cfg = runtime_universes.get(scope_id) or {}
            scope_symbols = scope_cfg.get("symbols") if isinstance(scope_cfg.get("symbols"), dict) else {}
            if scope_id == "forex_cfd" or ((scope_symbols or {}).get("forex")):
                has_forex_scope = True
                break
        if has_forex_scope:
            logger.info(
                "Profile-driven session overrides are disabled; polling now follows asset-class market sessions"
            )
        # Asset-class session gates now own polling eligibility; restore the
        # CLI-configured window values so account profiles do not override timing.
        self.session_start = session_start
        self.session_end = session_end

        t = time.time()
        self._vg_db      = VanguardDB(db_path)
        logger.info("[BOOT] VanguardDB init took %.2fs", time.time() - t)

        self._alpaca_adapter: Optional[AlpacaWSAdapter] = None
        self._alpaca_task: Optional[threading.Thread] = None
        self._alpaca_rest: Optional[AlpacaAdapter] = None
        self._td_adapter: Optional[TwelveDataAdapter] = None
        self._ibkr_adapter: Optional[IBKRStreamingAdapter] = None
        self._mt5_adapter: Optional[MT5DWXAdapter] = None

        logger.info(
            "VanguardOrchestrator init: mode=%s, dry_run=%s, session=%s–%s ET, equity_source=%s, accounts=%s",
            execution_mode, dry_run, self.session_start, self.session_end, self.equity_data_source,
            [a["id"] for a in self._accounts],
        )
        logger.info("[BOOT] __init__ total took %.2fs", time.time() - boot_t0)

    def _ensure_cycle_trace_parent(self) -> None:
        self._cycle_trace_path.parent.mkdir(parents=True, exist_ok=True)

    def _start_cycle_trace(self, cycle_ts: str) -> None:
        runner_cfg = self._load_execution_policy_runner_config("forex")
        self._active_cycle_trace = {
            "record_type": "cycle_trace",
            "cycle_ts_utc": str(cycle_ts or ""),
            "started_at_utc": iso_utc(self._now_utc()),
            "execution_mode": self.execution_mode,
            "dry_run": bool(self.dry_run),
            "config_version": str((self._runtime_config or {}).get("config_version") or ""),
            "profiles": [str(account.get("id") or "") for account in (self._accounts or [])],
            "live_policy_id": str(runner_cfg.get("live_policy_id") or ""),
            "throughput_mode": self._throughput_mode_summary(),
            "tactical_bans": self._tactical_bans_summary("forex"),
            "stage_timings": {},
            "pipeline": {},
            "execution": {},
            "shadow_eval": {},
            "telegram_events": [],
        }

    def _record_cycle_trace_result(self, result: dict[str, Any]) -> None:
        if not self._active_cycle_trace:
            return
        self._active_cycle_trace["pipeline"] = {
            "status": str(result.get("status") or ""),
            "v1_cache_used": bool(result.get("v1_cache_used")),
            "v1_source_health": result.get("v1_source_health"),
            "v2_rows": result.get("v2_rows"),
            "v2_survivors": result.get("v2_survivors"),
            "v3_symbols": result.get("v3_symbols"),
            "v4b_status": result.get("v4b_status"),
            "v4b_rows": result.get("v4b_rows"),
            "v5_status": result.get("v5_status"),
            "v5_candidates": result.get("v5_candidates"),
            "v6_status": result.get("v6_status"),
            "v6_rows": result.get("v6_rows"),
            "approved": result.get("approved"),
        }
        self._active_cycle_trace["stage_timings"] = dict(result.get("stage_timings") or {})
        if result.get("context_refresh"):
            self._active_cycle_trace["context_refresh"] = result.get("context_refresh")

    def _record_cycle_trace_execution(self, cycle_ts: str, summary: dict[str, Any]) -> None:
        if not self._active_cycle_trace:
            return
        if str(self._active_cycle_trace.get("cycle_ts_utc") or "") != str(cycle_ts or ""):
            return
        self._active_cycle_trace["execution"] = summary

    def _record_cycle_trace_shadow(self, cycle_ts: str, summary: dict[str, Any]) -> None:
        if not self._active_cycle_trace:
            return
        if str(self._active_cycle_trace.get("cycle_ts_utc") or "") != str(cycle_ts or ""):
            return
        self._active_cycle_trace["shadow_eval"] = dict(summary or {})

    def _record_cycle_trace_error(self, exc: Exception) -> None:
        if not self._active_cycle_trace:
            return
        self._active_cycle_trace["error"] = str(exc)

    def _upsert_orchestrator_service_state(
        self,
        *,
        status: str,
        cycle_ts_utc: str | None = None,
        error: Exception | str | None = None,
    ) -> None:
        try:
            now_ts = iso_utc(self._now_utc())
            details: dict[str, Any] = {
                "execution_mode": str(self.execution_mode or ""),
            }
            if cycle_ts_utc:
                details["cycle_ts_utc"] = str(cycle_ts_utc)
            if error is not None:
                details["last_error"] = str(error)
            self._vg_db.upsert_service_state(
                "orchestrator",
                status=str(status or "OK"),
                updated_ts_utc=now_ts,
                last_success_ts_utc=now_ts if str(status or "").upper() == "OK" else None,
                last_error_ts_utc=now_ts if error is not None else None,
                pid=os.getpid(),
                details=details,
            )
        except Exception as _svc_exc:  # noqa: BLE001
            logger.debug("orchestrator service-state upsert failed: %s", _svc_exc)

    def _record_cycle_trace_telegram(self, message: str, message_type: str | None, chunk_count: int) -> None:
        if not self._active_cycle_trace:
            return
        preview = str(message or "").strip().replace("\n", " ")
        self._active_cycle_trace.setdefault("telegram_events", []).append(
            {
                "ts_utc": iso_utc(self._now_utc()),
                "message_type": str(message_type or ""),
                "chars": len(message or ""),
                "chunks": int(chunk_count),
                "preview": preview[:240],
            }
        )

    def _flush_cycle_trace(self, *, status: str) -> None:
        if not self._active_cycle_trace:
            return
        trace = dict(self._active_cycle_trace)
        trace["final_status"] = str(status or "")
        trace["finished_at_utc"] = iso_utc(self._now_utc())
        self._ensure_cycle_trace_parent()
        with self._cycle_trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trace, default=str))
            fh.write("\n")
        self._active_cycle_trace = None

    # ------------------------------------------------------------------
    # Startup validation
    # ------------------------------------------------------------------

    def _validate_profiles(self) -> list[dict]:
        """Startup check: verify active account profiles exist. Raises if none.

        JSON runtime config profiles[] is authoritative.
        """
        # Re-use _load_accounts which already reads from JSON (with DB fallback)
        profiles = _load_accounts(self.db_path)

        if not profiles:
            msg = "[V7] FATAL: No active account profiles found. Cannot execute trades."
            logger.error(msg)
            self._send_telegram(f"🔴 {msg}", message_type="ERROR")
            raise RuntimeError(msg)

        logger.info("[V7] %d active profile(s):", len(profiles))
        for p in profiles:
            logger.info("  %s (env=%s)", p["id"], p.get("environment", "?"))
        return profiles

    def _verify_data_sources(self) -> None:
        """Startup check: verify data adapters have keys and DB has bars."""
        runtime_cfg = get_runtime_config()
        data_sources_cfg = get_data_sources_config()
        checks: list[tuple[str, str]] = []

        if bool((data_sources_cfg.get("alpaca") or {}).get("enabled", False)):
            alpaca_key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_KEY")
            checks.append(("Alpaca", "OK" if alpaca_key else "NO API KEY"))

        if bool((data_sources_cfg.get("twelvedata") or {}).get("enabled", False)):
            td_key = (
                os.environ.get("TWELVE_DATA_API_KEY")
                or os.environ.get("TWELVEDATA_API_KEY")
            )
            checks.append(("TwelveData", "OK" if td_key else "NO API KEY"))

        if bool((data_sources_cfg.get("ibkr") or {}).get("enabled", False)) or self.equity_data_source == "ibkr":
            try:
                if self._ibkr_adapter:
                    ibkr_ok = self._ibkr_adapter.is_connected()
                else:
                    ibkr = IBKRAdapter(client_id=10)
                    ibkr_ok = ibkr.connect()
                    ibkr.disconnect()
                checks.append(("IBKR", "OK" if ibkr_ok else "NOT CONNECTED"))
            except Exception as exc:
                checks.append(("IBKR", f"ERROR: {exc}"))

        if bool((data_sources_cfg.get("mt5_local") or {}).get("enabled", False)):
            try:
                mt5_ok = bool(self._mt5_adapter and self._mt5_adapter.is_connected())
                checks.append(("MT5_DWX", "OK" if mt5_ok else "NOT CONNECTED"))
            except Exception as exc:
                checks.append(("MT5_DWX", f"ERROR: {exc}"))

        try:
            with sqlite_conn(self.db_path) as con:
                bar_count = con.execute(
                    "SELECT COUNT(*) FROM vanguard_bars_1m"
                ).fetchone()[0]
            checks.append(("DB", f"OK ({bar_count:,} 1m bars)"))
        except Exception as exc:
            checks.append(("DB", f"ERROR: {exc}"))

        for name, status in checks:
            logger.info("[V7] Data source %s: %s", name, status)

        failures = [c for c in checks if "ERROR" in c[1] or "NO API KEY" in c[1]]
        if failures:
            logger.warning("[V7] %d data source issue(s): %s", len(failures), failures)

    def _check_cycle_lag(self, cycle_start: float) -> None:
        """Record cycle duration; warn and alert via Telegram if over threshold."""
        elapsed = time.time() - cycle_start
        self.last_cycle_duration = elapsed
        max_lag = self._exec_cfg.get("cycle_timeout_seconds", 120)
        if elapsed > max_lag:
            msg = f"[V7] CYCLE LAG: {elapsed:.0f}s exceeds {max_lag}s limit"
            logger.warning(msg)
            self._send_telegram(f"⚠️ {msg}", message_type="EXECUTION_RUN")
        else:
            logger.info("[V7] Cycle completed in %.1fs", elapsed)

    def _get_v1_precompute_config(self) -> dict[str, Any]:
        runtime_cfg = (self._runtime_config or {}).get("runtime") or {}
        cfg = dict(runtime_cfg.get("v1_precompute") or {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("freshness_seconds", max(int(self.cycle_interval_seconds * 2), 300))
        cfg.setdefault("interval_seconds", self.cycle_interval_seconds)
        return cfg

    def _resolve_runtime_universe_for_cycle(self, cycle_ts: str) -> list[str] | None:
        in_scope_symbols: list[str] | None = None
        self._current_resolved_universe = None
        if self._runtime_config is None:
            return None
        try:
            from vanguard.accounts.runtime_universe import resolve_universe_for_cycle
            from datetime import timezone as _tz

            cycle_dt = parse_utc(cycle_ts).replace(tzinfo=_tz.utc)
            self._current_resolved_universe = resolve_universe_for_cycle(
                self._runtime_config, cycle_dt, force_assets=set(self._force_assets)
            )
            ru = self._current_resolved_universe
            logger.info(
                "[Phase2a] Resolved universe: mode=%s profiles=%s classes=%s symbols=%d",
                ru.mode, ru.active_profile_ids, ru.expected_asset_classes, len(ru.in_scope_symbols),
            )
            self._write_resolved_universe_log(ru)
            if ru.mode == "enforce":
                in_scope_symbols = ru.in_scope_symbols
            else:
                logger.info(
                    "[Phase2a] OBSERVE mode — pipeline unconstrained. "
                    "WOULD have filtered to %d symbols: %s",
                    len(ru.in_scope_symbols), ru.in_scope_symbols[:10],
                )
        except Exception as exc:
            logger.warning("[Phase2a] Universe resolver failed: %s — continuing without filter", exc)
        return in_scope_symbols

    def _build_v1_precompute_details(self, cycle_ts: str, in_scope_symbols: list[str] | None, v1_result: dict[str, int]) -> dict[str, Any]:
        ru = getattr(self, "_current_resolved_universe", None)
        expected_asset_classes = list(getattr(ru, "expected_asset_classes", []) or [])
        return {
            "cycle_ts_utc": cycle_ts,
            "resolved_mode": getattr(ru, "mode", None),
            "expected_asset_classes": expected_asset_classes,
            "in_scope_symbol_count": len(getattr(ru, "in_scope_symbols", []) or []),
            "in_scope_symbols": list(getattr(ru, "in_scope_symbols", []) or []),
            "enforced_symbols": list(in_scope_symbols or []),
            "primary_sources": self._resolved_primary_sources_for_asset_classes(expected_asset_classes),
            "runtime_details": dict(getattr(self, "_last_v1_runtime_details", {}) or {}),
            "v1_result": dict(v1_result),
        }

    def _load_v1_precompute_state(self) -> dict[str, Any] | None:
        try:
            return self._vg_db.get_service_state(_V1_PRECOMPUTE_SERVICE_NAME)
        except Exception as exc:
            logger.warning("[V1 cache] failed to load service state: %s", exc)
            return None

    def _is_v1_precompute_state_fresh(self, state: dict[str, Any] | None) -> bool:
        if not state or str(state.get("status") or "").lower() != "ok":
            return False
        cfg = self._get_v1_precompute_config()
        last_success_ts = str(state.get("last_success_ts_utc") or "").strip()
        if not last_success_ts:
            return False
        try:
            age_s = int((self._now_utc() - parse_utc(last_success_ts)).total_seconds())
        except Exception:
            return False
        if age_s > int(cfg.get("freshness_seconds") or 0):
            return False
        details = state.get("details") or {}
        cached_symbols = [str(symbol).upper() for symbol in (details.get("in_scope_symbols") or [])]
        current_ru = getattr(self, "_current_resolved_universe", None)
        current_symbols = [str(symbol).upper() for symbol in (getattr(current_ru, "in_scope_symbols", []) or [])]
        if cached_symbols != current_symbols:
            return False
        cached_asset_classes = sorted(str(asset).lower() for asset in (details.get("expected_asset_classes") or []))
        current_asset_classes = sorted(str(asset).lower() for asset in (getattr(current_ru, "expected_asset_classes", []) or []))
        if cached_asset_classes != current_asset_classes:
            return False
        cached_mode = str(details.get("resolved_mode") or "")
        current_mode = str(getattr(current_ru, "mode", "") or "")
        if cached_mode != current_mode:
            return False
        cached_sources = {
            str(asset).lower(): str(source)
            for asset, source in ((details.get("primary_sources") or {}).items())
        }
        current_sources = self._resolved_primary_sources_for_asset_classes(current_asset_classes)
        if cached_sources != current_sources:
            return False
        return isinstance(details.get("v1_result"), dict)

    def _resolved_primary_sources_for_asset_classes(self, asset_classes: list[str] | set[str]) -> dict[str, str]:
        runtime_cfg = self._runtime_config or get_runtime_config()
        sources: dict[str, str] = {}
        for asset_class in sorted({str(asset).strip().lower() for asset in (asset_classes or []) if str(asset).strip()}):
            source_id = resolve_market_data_source_id(asset_class, runtime_config=runtime_cfg)
            if source_id:
                sources[asset_class] = str(source_id)
        return sources

    def _active_profile_ids(self) -> list[str]:
        ru = getattr(self, "_current_resolved_universe", None)
        active_from_cycle = [str(profile_id) for profile_id in (getattr(ru, "active_profile_ids", []) or []) if str(profile_id).strip()]
        if active_from_cycle:
            return active_from_cycle
        return [str(account.get("id") or "") for account in (self._accounts or []) if str(account.get("id") or "").strip()]

    def _get_active_runtime_symbols_for_asset_class(self, asset_class: str) -> list[str]:
        normalized_asset = str(asset_class or "").strip().lower()
        if not normalized_asset:
            return []
        ru = getattr(self, "_current_resolved_universe", None)
        if ru is not None:
            scoped = [
                _canonical_symbol(symbol)
                for symbol in (getattr(ru, "in_scope_symbols", []) or [])
                if classify_asset_class(_canonical_symbol(symbol)) == normalized_asset
            ]
            return sorted({symbol for symbol in scoped if symbol})

        symbols: set[str] = set()
        for profile_id in self._active_profile_ids():
            symbols.update(self._get_runtime_universe_symbols(profile_id, normalized_asset))
        return sorted(symbols)

    def _build_mt5_asset_coverage(self, asset_class: str) -> dict[str, Any] | None:
        normalized_asset = str(asset_class or "").strip().lower()
        runtime_cfg = self._runtime_config or get_runtime_config()
        if resolve_market_data_source_id(normalized_asset, runtime_config=runtime_cfg) != "mt5_local":
            return None

        expected_symbols = self._get_active_runtime_symbols_for_asset_class(normalized_asset)
        if not expected_symbols:
            return None

        mt5_details = dict(((getattr(self, "_last_v1_runtime_details", {}) or {}).get("mt5_poll_details") or {}))
        requested = {_canonical_symbol(symbol) for symbol in (mt5_details.get("requested_symbols") or []) if _canonical_symbol(symbol)}
        successful = {_canonical_symbol(symbol) for symbol in (mt5_details.get("successful_symbols") or []) if _canonical_symbol(symbol)}
        failed = {_canonical_symbol(symbol) for symbol in (mt5_details.get("failed_symbols") or []) if _canonical_symbol(symbol)}
        timed_out = {_canonical_symbol(symbol) for symbol in (mt5_details.get("timed_out_symbols") or []) if _canonical_symbol(symbol)}
        active_profile_ids = self._active_profile_ids()
        active_source_names = sorted(
            {
                str((((runtime_cfg.get("context_sources") or {}).get(str(profile.get("context_source_id") or "")) or {}).get("source") or "")).strip()
                for profile in (self._accounts or [])
                if str(profile.get("id") or "") in active_profile_ids
            }
        )
        active_source_names = [name for name in active_source_names if name]

        quote_symbols: set[str] = set()
        spec_symbols: set[str] = set()
        placeholders_profiles = ",".join("?" for _ in active_profile_ids)
        placeholders_symbols = ",".join("?" for _ in expected_symbols)
        placeholders_sources = ",".join("?" for _ in active_source_names)
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            try:
                source_clause = f"AND source IN ({placeholders_sources})" if active_source_names else ""
                quote_rows = con.execute(
                    f"""
                    SELECT symbol
                    FROM vanguard_context_quote_latest
                    WHERE profile_id IN ({placeholders_profiles})
                      AND symbol IN ({placeholders_symbols})
                      {source_clause}
                      AND UPPER(COALESCE(source_status, '')) = 'OK'
                    """,
                    [*active_profile_ids, *expected_symbols, *active_source_names],
                ).fetchall()
                quote_symbols = {_canonical_symbol(row["symbol"]) for row in quote_rows if row["symbol"]}
            except sqlite3.OperationalError:
                quote_symbols = set()
            try:
                source_clause = f"AND source IN ({placeholders_sources})" if active_source_names else ""
                spec_rows = con.execute(
                    f"""
                    SELECT symbol
                    FROM vanguard_context_symbol_specs
                    WHERE profile_id IN ({placeholders_profiles})
                      AND symbol IN ({placeholders_symbols})
                      {source_clause}
                      AND UPPER(COALESCE(source_status, '')) = 'OK'
                    """,
                    [*active_profile_ids, *expected_symbols, *active_source_names],
                ).fetchall()
                spec_symbols = {_canonical_symbol(row["symbol"]) for row in spec_rows if row["symbol"]}
            except sqlite3.OperationalError:
                spec_symbols = set()

        symbol_details: list[dict[str, str]] = []
        status_counts: Counter[str] = Counter()
        for symbol in expected_symbols:
            if symbol in timed_out:
                status = "mt5_timeout"
            elif symbol not in requested or symbol in failed or symbol not in successful:
                status = "mt5_missing_bar_source"
            elif symbol not in quote_symbols:
                status = "mt5_context_missing"
            elif symbol not in spec_symbols:
                status = "mt5_spec_missing"
            else:
                status = "mt5_ok"
            status_counts[status] += 1
            symbol_details.append({"symbol": symbol, "status": status})

        return {
            "asset_class": normalized_asset,
            "expected_symbols": expected_symbols,
            "requested_symbols": sorted(requested),
            "successful_symbols": sorted(successful),
            "failed_symbols": sorted(failed),
            "timed_out_symbols": sorted(timed_out),
            "status_counts": dict(status_counts),
            "symbols": symbol_details,
        }

    def _attach_mt5_coverage_details(self, result: dict[str, Any]) -> None:
        runtime_cfg = self._runtime_config or get_runtime_config()
        coverage: dict[str, Any] = {}
        asset_classes = ("crypto", "metal")
        if self._force_assets:
            forced_mt5_assets = tuple(
                asset_class
                for asset_class in asset_classes
                if asset_class in self._force_assets
            )
            if forced_mt5_assets:
                asset_classes = forced_mt5_assets
        for asset_class in asset_classes:
            if resolve_market_data_source_id(asset_class, runtime_config=runtime_cfg) != "mt5_local":
                continue
            details = self._build_mt5_asset_coverage(asset_class)
            if not details:
                continue
            coverage[asset_class] = details
            logger.info("[MT5 COVERAGE] %s %s", asset_class.upper(), details.get("status_counts"))
        if coverage:
            result["mt5_coverage"] = coverage

    # ------------------------------------------------------------------
    # Adapter init
    # ------------------------------------------------------------------

    def _init_signalstack(self) -> Optional[SignalStackAdapter]:
        ss  = self._exec_cfg.get("signalstack", {})
        return SignalStackAdapter(
            webhook_url="",
            timeout=ss.get("timeout_seconds", 5),
            max_retries=ss.get("max_retries", 2),
            retry_delay=ss.get("retry_delay_seconds", 1.0),
            log_payloads=ss.get("log_payloads", True),
        )

    def _get_account_profile(self, account_id: str) -> dict[str, Any] | None:
        for account in self._accounts:
            if str(account.get("id")) == str(account_id):
                return account
        return None

    def _load_risky_policy_for_account(self, account_id: str) -> dict[str, Any]:
        profile = self._get_account_profile(account_id) or {}
        if not profile:
            return {}
        try:
            risk_rules = _load_risk_rules()
            return _compile_effective_risk_policy(
                profile=profile,
                runtime_config=self._runtime_config or get_runtime_config(),
                risk_rules=risk_rules,
            )
        except Exception as exc:
            logger.debug("Could not resolve Risky policy for %s: %s", account_id, exc)
            return {}

    def _get_reentry_cooldown_minutes(self, account_id: str) -> int:
        policy = self._load_risky_policy_for_account(account_id)
        position_limits = policy.get("position_limits") or {}
        return int(position_limits.get("reentry_cooldown_minutes", 120) or 120)

    def _get_thesis_display_window_minutes(self, account_id: str) -> int:
        policy = self._load_risky_policy_for_account(account_id)
        position_limits = policy.get("position_limits") or {}
        return int(position_limits.get("thesis_display_window_minutes", 30) or 30)

    def _get_thesis_display_consecutive_gap_seconds(self) -> int:
        return max(int(self.cycle_interval_seconds * 1.5), 60)

    def _session_label_for_cycle_ts(self, cycle_ts: str) -> str:
        try:
            et = utc_to_et(parse_utc(cycle_ts))
        except Exception:
            return "Unknown"
        hhmm = et.hour * 60 + et.minute
        if hhmm >= 17 * 60 or hhmm < 3 * 60:
            return "Tokyo"
        if hhmm < 8 * 60:
            return "London"
        if hhmm < 12 * 60:
            return "London/NY"
        if hhmm < 17 * 60:
            return "New York"
        return "Overnight"

    def _format_exit_by_window(self, cycle_ts: str) -> str:
        try:
            approved_dt = parse_utc(cycle_ts)
            exit_90 = utc_to_et(approved_dt + timedelta(minutes=90)).strftime("%H:%M ET")
            exit_120 = utc_to_et(approved_dt + timedelta(minutes=120)).strftime("%H:%M ET")
            return f"{exit_90} — {exit_120}"
        except Exception:
            return "-"

    def _timeout_policy_for_cycle_ts(self, cycle_ts: str, asset_class: str = "forex") -> dict[str, Any]:
        if str(asset_class or "").lower() != "forex":
            return {}
        session_label = self._session_label_for_cycle_ts(cycle_ts)
        session_bucket = _normalize_timeout_session_bucket(session_label)
        policy = _get_timeout_policy_for_session(session_bucket) or {}
        if not policy:
            return {}
        approved_dt = parse_utc(cycle_ts)
        timeout_dt = approved_dt + timedelta(minutes=int(policy["timeout_minutes"]))
        return {
            "session_bucket": session_bucket,
            "timeout_minutes": int(policy["timeout_minutes"]),
            "dedupe_minutes": int(policy["dedupe_minutes"]),
            "timeout_at_utc": iso_utc(timeout_dt),
            "timeout_label": utc_to_et(timeout_dt).strftime("%H:%M ET"),
        }

    def _build_timeout_policy_header(self, cycle_ts: str) -> str:
        policies = [
            ("London", _get_timeout_policy_for_session("london")),
            ("NY", _get_timeout_policy_for_session("ny")),
            ("Asian", _get_timeout_policy_for_session("asian")),
        ]
        if policies and all(
            (not policy) or int(policy.get("dedupe_minutes") or 0) <= 0
            for _label, policy in policies
        ):
            return ""
        parts = [
            f"{label} {int(policy['timeout_minutes'])}m (dedupe {int(policy['dedupe_minutes'])})"
            for label, policy in policies
            if policy
        ]
        if not parts:
            return ""
        return "Session Policy: " + " | ".join(parts)

    def _load_selection_telegram_config(self) -> dict[str, Any]:
        override = getattr(self, "_selection_telegram_config_override", None)
        if isinstance(override, dict):
            return override
        cached = getattr(self, "_selection_telegram_config_cache", None)
        if isinstance(cached, dict):
            return cached
        cfg_path = _REPO_ROOT / "config" / "vanguard_v5_selection.json"
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except Exception as exc:
            logger.debug("Could not load selection telegram config from %s: %s", cfg_path, exc)
            loaded = {}
        self._selection_telegram_config_cache = loaded
        return loaded

    def _build_selection_contract_summary(self) -> dict[str, str]:
        cfg = self._load_selection_telegram_config()
        defaults = cfg.get("defaults") or {}
        pocket = defaults.get("pocket_filter") or {}
        if not pocket or not bool(pocket.get("enabled")):
            return {}

        asset_classes = [
            str(asset).strip().lower()
            for asset in (pocket.get("apply_to_asset_classes") or ["forex"])
            if str(asset).strip()
        ]
        asset_scope = "Forex only" if asset_classes == ["forex"] else "Assets " + "/".join(asset_classes)
        require_primary = pocket.get("require_dedupe_primary")
        require_primary_text = f"primary{int(require_primary)}" if str(require_primary or "").strip() else "primary dedupe off"
        allowed_pairs = {
            (str(direction).upper(), str(session).lower())
            for direction, session in (pocket.get("allowed_session_directions") or [])
            if isinstance(direction, str) and isinstance(session, str)
        }
        strict_pairs = {
            ("LONG", "ny"),
            ("LONG", "overlap"),
            ("SHORT", "london"),
            ("SHORT", "overlap"),
        }
        if not pocket.get("enabled"):
            return {
                "regime": "Regime: Pocket disabled",
                "eligibility": f"Eligibility: {asset_scope} | {require_primary_text}",
            }
        if allowed_pairs == strict_pairs:
            return {
                "regime": "Regime: STRICT POCKET",
                "eligibility": f"Eligibility: {asset_scope} | LONG ny/overlap | SHORT london/overlap | {require_primary_text}",
            }
        if not allowed_pairs:
            return {
                "regime": "Regime: PRIMARY-ONLY",
                "eligibility": f"Eligibility: {asset_scope} | any session/direction | {require_primary_text}",
            }
        allowed_display = ", ".join(
            f"{direction} {session}"
            for direction, session in sorted(allowed_pairs, key=lambda item: (item[1], item[0]))
        )
        return {
            "regime": "Regime: CUSTOM POCKET",
            "eligibility": f"Eligibility: {asset_scope} | {allowed_display} | {require_primary_text}",
        }

    def _load_thesis_display_state(
        self,
        cycle_ts: str,
        candidate_keys: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        if not candidate_keys:
            return {}

        cycle_dt = parse_utc(cycle_ts)
        requested = {
            (str(profile_id), str(symbol).upper(), str(direction).upper())
            for profile_id, symbol, direction in candidate_keys
        }
        profile_ids = sorted({key[0] for key in requested if key[0]})
        if not profile_ids:
            return {}

        display_window_by_profile = {
            profile_id: self._get_thesis_display_window_minutes(profile_id)
            for profile_id in profile_ids
        }
        max_display_window = max(display_window_by_profile.values() or [30])
        cutoff_iso = (cycle_dt - timedelta(minutes=max_display_window)).strftime("%Y-%m-%dT%H:%M:%SZ")

        broker_open_keys: set[tuple[str, str, str]] = set()
        try:
            placeholders = ",".join("?" for _ in profile_ids)
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                position_rows = con.execute(
                    f"""
                    SELECT profile_id, symbol, direction
                    FROM (
                        SELECT profile_id,
                               symbol,
                               direction,
                               ticket,
                               ROW_NUMBER() OVER (
                                   PARTITION BY profile_id, ticket
                                   ORDER BY received_ts_utc DESC
                               ) AS rn
                        FROM vanguard_context_positions_latest
                        WHERE profile_id IN ({placeholders})
                          AND source_status = 'OK'
                    )
                    WHERE rn = 1
                    """,
                    profile_ids,
                ).fetchall()
                for row in position_rows:
                    broker_open_keys.add(
                        (
                            str(row["profile_id"] or ""),
                            str(row["symbol"] or "").upper(),
                            str(row["direction"] or "").upper(),
                        )
                    )

                journal_rows = con.execute(
                    f"""
                    SELECT profile_id, symbol, side, status, approved_cycle_ts_utc, filled_at_utc, closed_at_utc
                    FROM vanguard_trade_journal
                    WHERE profile_id IN ({placeholders})
                      AND approved_cycle_ts_utc >= ?
                    ORDER BY approved_cycle_ts_utc DESC
                    """,
                    [*profile_ids, cutoff_iso],
                ).fetchall()
        except Exception as exc:
            logger.debug("Could not load thesis display state: %s", exc)
            return {
                key: {
                    "thesis_state": "NEW",
                    "reentry_blocked": False,
                }
                for key in requested
            }

        state_map = {
            key: {
                "thesis_state": "NEW",
                "reentry_blocked": False,
            }
            for key in requested
        }

        for key in broker_open_keys:
            if key not in state_map:
                continue
            state_map[key]["thesis_state"] = "OPEN"
            state_map[key]["reentry_blocked"] = True

        history_by_symbol: dict[tuple[str, str], list[tuple[str, datetime]]] = {}
        for row in journal_rows:
            symbol_key = (
                str(row["profile_id"] or ""),
                str(row["symbol"] or "").upper(),
            )
            row_dt = parse_utc(str(row["approved_cycle_ts_utc"] or cycle_ts))
            history_by_symbol.setdefault(symbol_key, []).append(
                (
                    str(row["side"] or "").upper(),
                    row_dt,
                )
            )

        consecutive_gap_seconds = self._get_thesis_display_consecutive_gap_seconds()
        for key in state_map:
            if key in broker_open_keys:
                continue
            profile_id, symbol, direction = key
            display_window = display_window_by_profile.get(profile_id, 30)
            recent_rows = history_by_symbol.get((profile_id, symbol), [])
            count = 0
            prev_dt = cycle_dt
            for row_side, row_dt in recent_rows:
                gap_seconds = (prev_dt - row_dt).total_seconds()
                if gap_seconds > display_window * 60:
                    break
                if gap_seconds > consecutive_gap_seconds:
                    break
                if row_side != direction:
                    break
                count += 1
                prev_dt = row_dt
            if count > 0:
                state_map[key]["thesis_state"] = f"SEEN_RECENTLY x{count}"

        return state_map

    def _get_max_open_positions(self, account_id: str) -> int:
        policy = self._load_risky_policy_for_account(account_id)
        position_limits = policy.get("position_limits") or {}
        return max(int(position_limits.get("max_open_positions", 0) or 0), 0)

    def _scheduled_flatten_config_for_account(self, account_id: str) -> dict[str, Any] | None:
        profile = self._get_account_profile(account_id) or {}
        cfg = ((self._runtime_config.get("position_manager") or {}).get("scheduled_flatten") or {})
        if not bool(cfg.get("enabled", False)):
            return None
        bridge = str(profile.get("execution_bridge") or "").lower()
        # Keep entry blocking aligned with lifecycle_daemon scheduled flatten:
        # both MetaApi and local MT5 profiles should stop opening new trades
        # once the flatten window has started.
        if not (bridge.startswith("mt5_local") or bridge.startswith("metaapi")):
            return None
        eligible = {
            str(profile_id).strip()
            for profile_id in (cfg.get("eligible_profiles") or [])
            if str(profile_id).strip()
        }
        profile_id = str(profile.get("id") or account_id or "")
        if eligible and profile_id not in eligible:
            return None
        start_et = str(cfg.get("start_et") or "").strip()
        end_et = str(cfg.get("end_et") or "").strip()
        if not start_et or not end_et:
            return None
        return cfg

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        hour_str, minute_str = str(value).split(":", 1)
        return (
            max(0, min(23, int(hour_str))),
            max(0, min(59, int(minute_str))),
        )

    def _scheduled_flatten_window_active(self, account_id: str, et: datetime | None = None) -> bool:
        cfg = self._scheduled_flatten_config_for_account(account_id)
        if cfg is None:
            return False
        now_local = et or self._now_et()
        allowed_days = {
            int(day)
            for day in (cfg.get("days") or [])
            if str(day).strip() not in {"", "None"}
        }
        if allowed_days and now_local.weekday() not in allowed_days:
            return False
        start_h, start_m = self._parse_hhmm(str(cfg.get("start_et")))
        end_h, end_m = self._parse_hhmm(str(cfg.get("end_et")))
        current_minutes = now_local.hour * 60 + now_local.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        return start_minutes <= current_minutes <= end_minutes

    def _scheduled_flatten_window_note(self, account_id: str, et: datetime | None = None) -> str:
        cfg = self._scheduled_flatten_config_for_account(account_id)
        if cfg is None:
            return ""
        start_et = str(cfg.get("start_et") or "").strip()
        end_et = str(cfg.get("end_et") or "").strip()
        if not start_et or not end_et:
            return ""
        note = f"Intentional liquidity guard: no new entries {start_et}–{end_et} ET"
        if self._scheduled_flatten_window_active(account_id, et=et):
            return f"{note} (active now)"
        return note

    def _active_routing_pause_note(self, et: datetime | None = None) -> str:
        now_local = et or self._now_et()
        for account in self._accounts or []:
            account_id = str(account.get("id") or "")
            if not account_id:
                continue
            note = self._scheduled_flatten_window_note(account_id, et=now_local)
            if note and self._scheduled_flatten_window_active(account_id, et=now_local):
                return note
        return ""

    @staticmethod
    def _pip_size_for_symbol(symbol: str) -> float:
        return 0.01 if str(symbol or "").upper().endswith("JPY") else 0.0001

    def _price_delta_to_pips(self, symbol: str, from_price: Any, to_price: Any) -> float | None:
        try:
            start = float(from_price)
            end = float(to_price)
        except Exception:
            return None
        pip_size = self._pip_size_for_symbol(symbol)
        if pip_size <= 0:
            return None
        return abs(end - start) / pip_size

    def _load_context_positions_snapshot(self, profile_id: str) -> list[dict[str, Any]]:
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    """
                    SELECT ticket,
                           symbol,
                           direction,
                           lots,
                           floating_pnl,
                           holding_minutes,
                           open_time_utc,
                           received_ts_utc
                    FROM (
                        SELECT ticket,
                               symbol,
                               direction,
                               lots,
                               floating_pnl,
                               holding_minutes,
                               open_time_utc,
                               received_ts_utc,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ticket
                                   ORDER BY received_ts_utc DESC
                               ) AS rn
                        FROM vanguard_context_positions_latest
                        WHERE profile_id = ?
                          AND source_status = 'OK'
                    )
                    WHERE rn = 1
                    """,
                    (str(profile_id or ""),),
                ).fetchall()
        except Exception:
            return []
        snapshot: list[dict[str, Any]] = []
        for row in rows:
            snapshot.append(
                {
                    "ticket": str(row["ticket"] or ""),
                    "broker_position_id": str(row["ticket"] or ""),
                    "symbol": str(row["symbol"] or "").upper(),
                    "direction": str(row["direction"] or "").upper(),
                    "qty": row["lots"],
                    "unrealized_pnl": row["floating_pnl"],
                    "holding_minutes": row["holding_minutes"],
                    "open_time_utc": row["open_time_utc"],
                    "received_ts_utc": row["received_ts_utc"],
                }
            )
        return snapshot

    def _load_open_journal_positions_snapshot(self, profile_id: str) -> list[dict[str, Any]]:
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    """
                    SELECT trade_id,
                           broker_position_id,
                           symbol,
                           side,
                           COALESCE(fill_qty, approved_qty) AS qty,
                           COALESCE(realized_pnl, 0.0) AS realized_pnl,
                           approved_cycle_ts_utc,
                           submitted_at_utc,
                           filled_at_utc
                    FROM vanguard_trade_journal
                    WHERE profile_id = ?
                      AND (
                            status = 'OPEN'
                            OR (
                                status = 'PENDING_FILL'
                                AND COALESCE(submitted_at_utc, filled_at_utc, '') <> ''
                            )
                      )
                    ORDER BY approved_cycle_ts_utc DESC
                    """,
                    (str(profile_id or ""),),
                ).fetchall()
        except Exception:
            return []
        snapshot: list[dict[str, Any]] = []
        for row in rows:
            snapshot.append(
                {
                    "ticket": str(row["broker_position_id"] or row["trade_id"] or ""),
                    "broker_position_id": str(row["broker_position_id"] or row["trade_id"] or ""),
                    "symbol": str(row["symbol"] or "").upper(),
                    "direction": str(row["side"] or "").upper(),
                    "qty": row["qty"],
                    "unrealized_pnl": row["realized_pnl"],
                    "open_time_utc": row["filled_at_utc"] or row["submitted_at_utc"] or row["approved_cycle_ts_utc"],
                    "received_ts_utc": row["filled_at_utc"] or row["submitted_at_utc"] or row["approved_cycle_ts_utc"],
                }
            )
        return snapshot

    def _serialize_incumbent_snapshot(
        self,
        positions: list[dict[str, Any]],
        *,
        timeout_policy: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        timeout_minutes = int((timeout_policy or {}).get("timeout_minutes") or 0)
        serialized: list[dict[str, Any]] = []
        for position in positions:
            age_minutes = position.get("holding_minutes")
            try:
                age_value = float(age_minutes) if age_minutes is not None else None
            except Exception:
                age_value = None
            timeout_remaining = None
            if timeout_minutes > 0 and age_value is not None:
                timeout_remaining = max(timeout_minutes - age_value, 0.0)
            serialized.append(
                {
                    "ticket": str(position.get("ticket") or position.get("broker_position_id") or ""),
                    "broker_position_id": str(position.get("broker_position_id") or position.get("ticket") or ""),
                    "symbol": str(position.get("symbol") or "").upper(),
                    "direction": str(position.get("direction") or position.get("side") or "").upper(),
                    "qty": position.get("qty"),
                    "unrealized_pnl": position.get("unrealized_pnl") if position.get("unrealized_pnl") is not None else position.get("pnl"),
                    "holding_minutes": age_value,
                    "timeout_remaining_minutes": timeout_remaining,
                }
            )
        return serialized

    def _refresh_forward_checkpoints(self, trade_ids: list[str] | None = None) -> None:
        if self._is_test_execution_mode():
            return
        try:
            _forward_checkpoints_ensure_table(self.db_path)
            _forward_checkpoints_refresh(self.db_path, trade_ids=trade_ids)
            # Timeout-policy summaries and shell replays are analytics/backfill work.
            # Skipping them in the live loop keeps pre-cycle maintenance bounded.
            _refresh_timeout_policy_data(
                self.db_path,
                trade_ids=trade_ids,
                refresh_summary_flag=False,
                refresh_shell_replays_flag=False,
            )
        except Exception as exc:
            logger.debug("forward checkpoint refresh skipped: %s", exc)

    def _refresh_signal_forward_checkpoints(self, decision_ids: list[str] | None = None) -> None:
        if self._is_test_execution_mode():
            return
        try:
            _signal_forward_checkpoints_ensure_table(self.db_path)
            _signal_forward_checkpoints_refresh(self.db_path, decision_ids=decision_ids)
        except Exception as exc:
            logger.debug("signal forward checkpoint refresh skipped: %s", exc)

    def _get_runtime_universe_symbols(self, account_id: str, asset_class: str) -> list[str]:
        profile = self._get_account_profile(account_id) or {}
        scope_id = str(profile.get("instrument_scope") or "")
        runtime_cfg = self._runtime_config or get_runtime_config()
        scope_cfg = ((runtime_cfg.get("universes") or {}).get(scope_id) or {})
        symbols = ((scope_cfg.get("symbols") or {}).get(str(asset_class or "").lower()) or [])
        return [str(symbol).upper() for symbol in symbols if str(symbol).strip()]

    def _log_context_state_diagnostics(self, cycle_ts: str) -> None:
        runtime_cfg = self._runtime_config or get_runtime_config()
        diag_cfg = ((runtime_cfg.get("runtime") or {}).get("context_state_diagnostics") or {})
        if not bool(diag_cfg.get("enabled", False)):
            return

        fresh_seconds = {
            "forex": int(diag_cfg.get("forex_fresh_seconds") or 180),
            "crypto": int(diag_cfg.get("crypto_fresh_seconds") or 120),
            "equity": int(diag_cfg.get("equity_fresh_seconds") or 180),
        }
        now_dt = now_utc()

        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            for profile in self._accounts or []:
                profile_id = str(profile.get("id") or "")
                if not profile_id:
                    continue
                source_id = str(profile.get("context_source_id") or "")
                source_cfg = ((runtime_cfg.get("context_sources") or {}).get(source_id) or {})
                context_source = str(source_cfg.get("source") or "").strip()
                for asset_class, state_table in (
                    ("forex", "vanguard_forex_pair_state"),
                    ("crypto", "vanguard_crypto_symbol_state"),
                    ("equity", "vanguard_equity_symbol_state"),
                ):
                    expected_symbols = self._get_runtime_universe_symbols(profile_id, asset_class)
                    if not expected_symbols:
                        continue
                    placeholders = ",".join("?" for _ in expected_symbols)
                    try:
                        source_clause = "AND source = ?" if context_source else ""
                        params = [profile_id, *expected_symbols]
                        if context_source:
                            params.append(context_source)
                        quote_rows = con.execute(
                            f"""
                            SELECT symbol,
                                   COALESCE(received_ts_utc, quote_ts_utc) AS effective_quote_ts_utc,
                                   source_status
                            FROM vanguard_context_quote_latest
                            WHERE profile_id = ?
                              AND symbol IN ({placeholders})
                              {source_clause}
                            """,
                            params,
                        ).fetchall()
                    except sqlite3.OperationalError as exc:
                        logger.info(
                            "[DIAG] skipping context diagnostics for profile=%s asset=%s: %s",
                            profile_id,
                            asset_class,
                            exc,
                        )
                        continue
                    quote_by_symbol = {str(row["symbol"]).upper(): row for row in quote_rows}
                    fresh = 0
                    stale = 0
                    missing = 0
                    latest_age_s: int | None = None
                    for symbol in expected_symbols:
                        row = quote_by_symbol.get(symbol)
                        if row is None:
                            missing += 1
                            continue
                        quote_dt = (
                            parse_utc(str(row["effective_quote_ts_utc"]))
                            if row["effective_quote_ts_utc"]
                            else None
                        )
                        age_s = int((now_dt - quote_dt).total_seconds()) if quote_dt else None
                        if age_s is not None:
                            latest_age_s = age_s if latest_age_s is None else min(latest_age_s, age_s)
                        is_fresh = (
                            str(row["source_status"] or "").upper() == "OK"
                            and age_s is not None
                            and age_s <= fresh_seconds[asset_class]
                        )
                        if is_fresh:
                            fresh += 1
                        else:
                            stale += 1

                    try:
                        state_row = con.execute(
                            f"""
                            SELECT
                                COUNT(*) AS row_count,
                                SUM(CASE WHEN UPPER(COALESCE(source_status, '')) = 'OK' THEN 1 ELSE 0 END) AS ok_count
                            FROM {state_table}
                            WHERE profile_id = ?
                              AND cycle_ts_utc = ?
                              AND symbol IN ({placeholders})
                            """,
                            [profile_id, cycle_ts, *expected_symbols],
                        ).fetchone()
                        latest_state_cycle = con.execute(
                            f"""
                            SELECT MAX(cycle_ts_utc) AS latest_cycle
                            FROM {state_table}
                            WHERE profile_id = ?
                              AND symbol IN ({placeholders})
                            """,
                            [profile_id, *expected_symbols],
                        ).fetchone()
                    except sqlite3.OperationalError as exc:
                        logger.info(
                            "[DIAG] skipping state diagnostics for profile=%s asset=%s: %s",
                            profile_id,
                            asset_class,
                            exc,
                        )
                        state_row = {"row_count": 0, "ok_count": 0}
                        latest_state_cycle = {"latest_cycle": None}
                    logger.info(
                        "[DIAG] profile=%s asset=%s expected=%d quotes fresh=%d stale=%d missing=%d latest_quote_age_s=%s state_rows=%d state_ok=%d latest_state_cycle=%s",
                        profile_id,
                        asset_class,
                        len(expected_symbols),
                        fresh,
                        stale,
                        missing,
                        latest_age_s if latest_age_s is not None else "-",
                        int(state_row["row_count"] or 0),
                        int(state_row["ok_count"] or 0),
                        latest_state_cycle["latest_cycle"] if latest_state_cycle and latest_state_cycle["latest_cycle"] else "-",
                    )

    def _log_v6_diagnostics(self, cycle_ts: str) -> None:
        runtime_cfg = self._runtime_config or get_runtime_config()
        diag_cfg = ((runtime_cfg.get("runtime") or {}).get("context_state_diagnostics") or {})
        if not bool(diag_cfg.get("enabled", False)):
            return

        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            portfolio_rows = con.execute(
                """
                SELECT account_id, symbol, status, rejection_reason, v6_state, v6_reasons_json
                FROM vanguard_tradeable_portfolio
                WHERE cycle_ts_utc = ?
                """,
                (cycle_ts,),
            ).fetchall()
            risky_rows = con.execute(
                """
                SELECT profile_id, final_verdict, final_reason_codes_json
                FROM vanguard_risky_decisions
                WHERE cycle_ts_utc = ?
                """,
                (cycle_ts,),
            ).fetchall()

        if not portfolio_rows and not risky_rows:
            logger.info("[DIAG] V6 cycle=%s no risky/portfolio rows", cycle_ts)
            return

        status_counts: Counter[str] = Counter()
        v6_state_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        for row in portfolio_rows:
            status_counts[str(row["status"] or "UNKNOWN")] += 1
            v6_state_counts[str(row["v6_state"] or "UNKNOWN")] += 1
            if row["rejection_reason"]:
                reason_counts[str(row["rejection_reason"])] += 1
            try:
                for item in json.loads(str(row["v6_reasons_json"] or "[]")):
                    code = str((item or {}).get("code") or "").strip()
                    if code:
                        reason_counts[code] += 1
            except Exception:
                pass

        risky_verdict_counts: Counter[str] = Counter()
        for row in risky_rows:
            risky_verdict_counts[str(row["final_verdict"] or "UNKNOWN")] += 1
            try:
                for code in json.loads(str(row["final_reason_codes_json"] or "[]")):
                    code_str = str(code or "").strip()
                    if code_str:
                        reason_counts[code_str] += 1
            except Exception:
                pass

        logger.info(
            "[DIAG] V6 cycle=%s portfolio=%s risky=%s reasons=%s",
            cycle_ts,
            dict(status_counts),
            dict(risky_verdict_counts),
            dict(reason_counts.most_common(6)),
        )

    def _has_gft_accounts(self) -> bool:
        """True if any active account is a GFT account."""
        return any(
            str(account.get("id") or "").lower().startswith("gft_")
            for account in self._accounts
        )

    def _is_manual_execution_mode(self) -> bool:
        """True when execution should only forward-track and never submit external orders."""
        return str(self.execution_mode or "manual").lower() in {"manual", "test"}

    def _is_test_execution_mode(self) -> bool:
        """True when the run should not persist any DB/session/execution-log writes."""
        return str(self.execution_mode or "manual").lower() == "test"

    @staticmethod
    def _profile_execution_mode_for_asset(profile: dict[str, Any] | None, asset_class: str | None) -> str:
        profile = profile or {}
        asset = str(asset_class or "").lower().strip()
        lanes = profile.get("asset_lanes") or {}
        if isinstance(lanes, dict):
            lane = lanes.get(asset) or {}
            if isinstance(lane, dict):
                lane_mode = str(lane.get("execution_mode") or "").lower().strip()
                if lane_mode == "forward_track_only":
                    return "manual"
                if lane_mode in {"auto", "manual", "off", "paper", "test"}:
                    return lane_mode
        overrides = profile.get("execution_mode_by_asset_class") or {}
        if isinstance(overrides, dict):
            override = str(overrides.get(asset) or "").lower().strip()
            if override == "forward_track_only":
                return "manual"
            if override in {"auto", "manual", "off", "paper", "test"}:
                return override
        mode = str(profile.get("execution_mode") or "").lower().strip()
        if mode == "forward_track_only":
            return "manual"
        return mode

    @staticmethod
    def _profile_lane_for_asset(profile: dict[str, Any] | None, asset_class: str | None) -> dict[str, Any]:
        profile = profile or {}
        asset = str(asset_class or "").lower().strip()
        lanes = profile.get("asset_lanes") or {}
        if not isinstance(lanes, dict):
            return {}
        lane = lanes.get(asset) or {}
        return lane if isinstance(lane, dict) else {}

    def _lifecycle_policy_id_for_candidate(
        self,
        asset_class: str | None,
        direction: str | None,
        session_bucket: str | None,
    ) -> str | None:
        return resolve_exit_policy_id(
            {},
            asset_class,
            runtime_cfg=self._runtime_config,
            direction=direction,
            session_bucket=session_bucket,
            prefer_lane_binding=False,
            apply_staged_forex_override=True,
        )

    def _load_execution_policy_runner_config(self, asset_class: str | None) -> dict[str, Any]:
        """Resolve the execution-policy-runner binding for this asset class.

        Precedence (new):
          1. position_manager.execution_policy_runner.by_asset_class[<asset>]
             — per-asset {live_policy_id, shadow_policy_ids} map. Lets each
             asset (forex / crypto / metal / equity) carry its own LIVE gate
             and SHADOW A/B set.
          2. Legacy flat shape: asset_classes[] + top-level live_policy_id +
             shadow_policy_ids[]. Back-compat for configs that haven't migrated
             to by_asset_class yet. Behaves identically to the old code path.

        Shared across both: `policies` map (referenced by id) and
        `telegram_summary` flag.
        """
        asset = str(asset_class or "").lower().strip()
        pm_cfg = ((self._runtime_config or {}).get("position_manager") or {})
        runner = pm_cfg.get("execution_policy_runner") or {}
        if not isinstance(runner, dict) or not bool(runner.get("enabled")):
            return {"enabled": False}
        policies = runner.get("policies") or {}
        if not isinstance(policies, dict):
            return {"enabled": False}

        # --- Precedence 1: per-asset map --------------------------------
        by_asset = runner.get("by_asset_class") or {}
        live_policy_id: str = ""
        shadow_policy_ids: list[str] = []
        source: str = "execution_policy_runner.flat"
        if isinstance(by_asset, dict) and asset and asset in {
            str(k or "").lower().strip() for k in by_asset.keys()
        }:
            entry = next(
                (v for k, v in by_asset.items() if str(k or "").lower().strip() == asset),
                None,
            ) or {}
            live_policy_id = str(entry.get("live_policy_id") or "").strip()
            shadow_policy_ids = [
                str(value or "").strip()
                for value in (entry.get("shadow_policy_ids") or [])
                if str(value or "").strip()
            ]
            source = f"by_asset_class[{asset}]"
        else:
            # --- Precedence 2: legacy flat shape ------------------------
            allowed_assets = {
                str(value or "").lower().strip()
                for value in (runner.get("asset_classes") or [])
                if str(value or "").strip()
            }
            if allowed_assets and asset not in allowed_assets:
                return {"enabled": False}
            live_policy_id = str(runner.get("live_policy_id") or "").strip()
            shadow_policy_ids = [
                str(value or "").strip()
                for value in (runner.get("shadow_policy_ids") or [])
                if str(value or "").strip()
            ]

        policy_order: list[str] = []
        if live_policy_id and live_policy_id in policies:
            policy_order.append(live_policy_id)
        for policy_id in shadow_policy_ids:
            if policy_id in policies and policy_id not in policy_order:
                policy_order.append(policy_id)
        if not policy_order:
            return {"enabled": False}
        return {
            "enabled": True,
            "live_policy_id": live_policy_id,
            "shadow_policy_ids": shadow_policy_ids,
            "policy_order": policy_order,
            "policies": policies,
            "telegram_summary": bool(runner.get("telegram_summary", True)),
            "binding_source": source,
        }

    def _load_tactical_ban_rules(self, asset_class: str | None) -> list[dict[str, str]]:
        asset = str(asset_class or "").lower().strip()
        pm_cfg = ((self._runtime_config or {}).get("position_manager") or {})
        cfg = pm_cfg.get("tactical_bans") or {}
        if not bool(cfg.get("enabled")):
            return []
        rules: list[dict[str, str]] = []
        for raw_rule in (cfg.get("rules") or []):
            if not isinstance(raw_rule, dict):
                continue
            rule_asset = str(raw_rule.get("asset_class") or "").lower().strip()
            if rule_asset and asset and rule_asset != asset:
                continue
            symbol = str(raw_rule.get("symbol") or "").upper().strip()
            side = str(raw_rule.get("side") or "").upper().strip()
            if not symbol or side not in {"LONG", "SHORT"}:
                continue
            rules.append(
                {
                    "asset_class": rule_asset or asset,
                    "symbol": symbol,
                    "side": side,
                    "reason": str(raw_rule.get("reason") or "tactical_ban").strip() or "tactical_ban",
                }
            )
        return rules

    def _match_tactical_ban(
        self,
        *,
        asset_class: str | None,
        symbol: str | None,
        direction: str | None,
    ) -> dict[str, str] | None:
        sym = str(symbol or "").upper().strip()
        side = str(direction or "").upper().strip()
        for rule in self._load_tactical_ban_rules(asset_class):
            if rule["symbol"] == sym and rule["side"] == side:
                return rule
        return None

    def _load_throughput_control_config(self) -> dict[str, Any]:
        pm_cfg = ((self._runtime_config or {}).get("position_manager") or {})
        cfg = pm_cfg.get("throughput_control") or {}
        if not bool(cfg.get("enabled")):
            return {"enabled": False}
        return {
            "enabled": True,
            "default_max_new_entries_per_cycle": max(int(cfg.get("default_max_new_entries_per_cycle") or 1), 1),
            "allow_second_entry_if": dict(cfg.get("allow_second_entry_if") or {}),
        }

    def _load_cost_aware_routing_config(self) -> dict[str, Any]:
        pm_cfg = ((self._runtime_config or {}).get("position_manager") or {})
        cfg = pm_cfg.get("cost_aware_routing") or {}
        if not bool(cfg.get("enabled")):
            return {"enabled": False}
        return {
            "enabled": True,
            "allow_on_missing_metrics": bool(cfg.get("allow_on_missing_metrics", False)),
            "max_spread_bps": float(cfg.get("max_spread_bps") or 8.0),
            "min_predicted_move_bps": float(cfg.get("min_predicted_move_bps") or 6.0),
            "min_after_cost_bps": float(cfg.get("min_after_cost_bps") or 2.0),
            "min_edge_to_cost_ratio": float(cfg.get("min_edge_to_cost_ratio") or 1.5),
        }

    def _candidate_cost_routing_state(
        self,
        row: dict[str, Any],
        metrics: dict[str, Any] | None,
        cfg: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = cfg or self._load_cost_aware_routing_config()
        metrics = metrics or {}
        spread_bps_raw = metrics.get("spread_bps")
        cost_bps_raw = metrics.get("cost_bps")
        predicted_move_bps_raw = metrics.get("predicted_move_bps")
        after_cost_bps_raw = metrics.get("after_cost_bps")
        raw_metrics_json = metrics.get("metrics_json")
        metrics_payload: dict[str, Any] = {}
        if isinstance(raw_metrics_json, dict):
            metrics_payload = raw_metrics_json
        elif raw_metrics_json not in (None, ""):
            try:
                parsed = json.loads(str(raw_metrics_json))
                if isinstance(parsed, dict):
                    metrics_payload = parsed
            except Exception:
                metrics_payload = {}
        pip_size = metrics_payload.get("pip_size")
        live_mid = metrics_payload.get("live_mid")
        spread_pips = metrics_payload.get("spread_pips")
        cost_pips = metrics_payload.get("cost_pips")
        spread_bps = float(spread_bps_raw) if spread_bps_raw not in (None, "") else None
        cost_bps = float(cost_bps_raw) if cost_bps_raw not in (None, "") else None
        predicted_move_bps = float(predicted_move_bps_raw) if predicted_move_bps_raw not in (None, "") else None
        after_cost_bps = float(after_cost_bps_raw) if after_cost_bps_raw not in (None, "") else None
        if spread_bps is None and spread_pips not in (None, "") and pip_size not in (None, "") and live_mid not in (None, "", 0):
            try:
                spread_bps = (float(spread_pips) * float(pip_size) / float(live_mid)) * 10_000.0
            except Exception:
                spread_bps = None
        if cost_bps is None and cost_pips not in (None, "") and pip_size not in (None, "") and live_mid not in (None, "", 0):
            try:
                cost_bps = (float(cost_pips) * float(pip_size) / float(live_mid)) * 10_000.0
            except Exception:
                cost_bps = None
        if predicted_move_bps is None:
            predicted_return = metrics.get("predicted_return", row.get("predicted_return"))
            if predicted_return not in (None, ""):
                try:
                    predicted_move_bps = abs(float(predicted_return)) * 10_000.0
                except Exception:
                    predicted_move_bps = None
        if after_cost_bps is None and predicted_move_bps is not None and cost_bps is not None:
            after_cost_bps = predicted_move_bps - cost_bps
        metrics_available = (
            spread_bps is not None
            and cost_bps is not None
            and predicted_move_bps is not None
            and after_cost_bps is not None
        )
        edge_to_cost_ratio = None
        if predicted_move_bps is not None and cost_bps is not None:
            if cost_bps > 0:
                edge_to_cost_ratio = predicted_move_bps / cost_bps
            else:
                edge_to_cost_ratio = float("inf")
        state: dict[str, Any] = {
            "enabled": bool(cfg.get("enabled")),
            "metrics_available": metrics_available,
            "spread_bps": spread_bps,
            "cost_bps": cost_bps,
            "predicted_move_bps": predicted_move_bps,
            "after_cost_bps": after_cost_bps,
            "edge_to_cost_ratio": edge_to_cost_ratio,
            "passes": True,
            "reason_code": "COST_ROUTING_DISABLED" if not cfg.get("enabled") else "OK",
            "reason_json": {},
        }
        if not cfg.get("enabled"):
            return state
        if not metrics_available:
            state["passes"] = bool(cfg.get("allow_on_missing_metrics"))
            state["reason_code"] = "COST_METRICS_MISSING" if not state["passes"] else "COST_METRICS_BYPASS"
            state["reason_json"] = {
                "allow_on_missing_metrics": bool(cfg.get("allow_on_missing_metrics")),
                "spread_bps": spread_bps,
                "cost_bps": cost_bps,
                "predicted_move_bps": predicted_move_bps,
                "after_cost_bps": after_cost_bps,
                "metrics_json_present": bool(metrics_payload),
            }
            return state
        if spread_bps is not None and spread_bps > float(cfg.get("max_spread_bps") or 8.0):
            state["passes"] = False
            state["reason_code"] = "COST_SPREAD_TOO_WIDE"
        elif predicted_move_bps is not None and predicted_move_bps < float(cfg.get("min_predicted_move_bps") or 6.0):
            state["passes"] = False
            state["reason_code"] = "COST_PREDICTED_MOVE_TOO_SMALL"
        elif after_cost_bps is not None and after_cost_bps < float(cfg.get("min_after_cost_bps") or 2.0):
            state["passes"] = False
            state["reason_code"] = "COST_AFTER_COST_EDGE_TOO_SMALL"
        elif (
            edge_to_cost_ratio is not None
            and edge_to_cost_ratio != float("inf")
            and edge_to_cost_ratio < float(cfg.get("min_edge_to_cost_ratio") or 1.5)
        ):
            state["passes"] = False
            state["reason_code"] = "COST_EDGE_TO_COST_TOO_LOW"
        state["reason_json"] = {
            "spread_bps": spread_bps,
            "cost_bps": cost_bps,
            "predicted_move_bps": predicted_move_bps,
            "after_cost_bps": after_cost_bps,
            "edge_to_cost_ratio": edge_to_cost_ratio,
            "score_unit": metrics_payload.get("score_unit"),
            "max_spread_bps": float(cfg.get("max_spread_bps") or 8.0),
            "min_predicted_move_bps": float(cfg.get("min_predicted_move_bps") or 6.0),
            "min_after_cost_bps": float(cfg.get("min_after_cost_bps") or 2.0),
            "min_edge_to_cost_ratio": float(cfg.get("min_edge_to_cost_ratio") or 1.5),
        }
        return state

    def _candidate_priority_sort_key(
        self,
        row: dict[str, Any],
        metrics: dict[str, Any] | None,
    ) -> tuple[Any, ...]:
        metrics = metrics or {}
        cost_cfg = self._load_cost_aware_routing_config()
        cost_state = self._candidate_cost_routing_state(row, metrics, cost_cfg)
        direction = str(row.get("direction") or "").upper()
        conviction = metrics.get("conviction", row.get("conviction"))
        strength = self._directional_conviction_strength(direction, conviction)
        top_rank_streak = int(metrics.get("top_rank_streak") or 0)
        selection_rank = int(metrics.get("selection_rank") or row.get("rank") or 999999)
        flip_count_window = int(metrics.get("flip_count_window") or 999999)
        edge_score = float(row.get("edge_score") or 0.0)
        predicted_return = abs(float(metrics.get("predicted_return") or row.get("predicted_return") or 0.0))
        symbol = str(row.get("symbol") or "").upper()
        edge_to_cost_ratio = cost_state.get("edge_to_cost_ratio")
        after_cost_bps = cost_state.get("after_cost_bps")
        return (
            -(float(edge_to_cost_ratio) if edge_to_cost_ratio not in (None, float("inf")) else 999999.0 if edge_to_cost_ratio == float("inf") else -1.0),
            -(float(after_cost_bps) if after_cost_bps is not None else -999999.0),
            -(float(strength) if strength is not None else -1.0),
            -top_rank_streak,
            selection_rank,
            flip_count_window,
            -edge_score,
            -predicted_return,
            symbol,
        )

    def _candidate_allows_second_entry(
        self,
        row: dict[str, Any],
        metrics: dict[str, Any] | None,
        cfg: dict[str, Any] | None,
    ) -> bool:
        cfg = cfg or {}
        allow_cfg = dict(cfg.get("allow_second_entry_if") or {})
        min_strength = float(allow_cfg.get("min_directional_conviction") or 0.82)
        min_top_rank = int(allow_cfg.get("min_top_rank_streak") or 2)
        metrics = metrics or {}
        strength = self._directional_conviction_strength(
            str(row.get("direction") or "").upper(),
            metrics.get("conviction", row.get("conviction")),
        )
        top_rank_streak = int(metrics.get("top_rank_streak") or 0)
        return strength is not None and strength >= min_strength and top_rank_streak >= min_top_rank

    def _tactical_bans_summary(self, asset_class: str | None) -> str:
        rules = self._load_tactical_ban_rules(asset_class)
        if not rules:
            return "none"
        return ", ".join(f"{rule['symbol']} {rule['side']}" for rule in rules)

    def _throughput_mode_summary(self) -> str:
        cfg = self._load_throughput_control_config()
        if not cfg.get("enabled"):
            return "unlimited"
        default_max = int(cfg.get("default_max_new_entries_per_cycle") or 1)
        allow_cfg = dict(cfg.get("allow_second_entry_if") or {})
        min_strength = float(allow_cfg.get("min_directional_conviction") or 0.82)
        min_top_rank = int(allow_cfg.get("min_top_rank_streak") or 2)
        return (
            f"top{default_max}"
            f"+top2_if_strong(conv>={min_strength:.2f},top_rank>={min_top_rank})"
        )

    def _cost_aware_routing_summary(self) -> str:
        cfg = self._load_cost_aware_routing_config()
        if not cfg.get("enabled"):
            return "off"
        return (
            "on "
            f"allow_missing={'yes' if cfg.get('allow_on_missing_metrics') else 'no'} "
            f"max_spread={float(cfg.get('max_spread_bps') or 0.0):.1f}bp "
            f"min_move={float(cfg.get('min_predicted_move_bps') or 0.0):.1f}bp "
            f"min_after_cost={float(cfg.get('min_after_cost_bps') or 0.0):.1f}bp "
            f"min_ratio={float(cfg.get('min_edge_to_cost_ratio') or 0.0):.2f}x"
        )

    def _live_policy_recent_audit(
        self,
        *,
        profile_id: str,
        asset_class: str = "forex",
        hours: int = 24,
    ) -> dict[str, Any]:
        runner_cfg = self._load_execution_policy_runner_config(asset_class)
        live_policy_id = str(runner_cfg.get("live_policy_id") or "").strip()
        if not live_policy_id:
            return {"live_policy_id": None, "decision_counts": {}, "reason_counts": {}}
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                decision_rows = con.execute(
                    """
                    SELECT execution_decision, COUNT(*) AS n
                    FROM vanguard_signal_decision_log
                    WHERE profile_id = ?
                      AND asset_class = ?
                      AND execution_policy_mode = 'live'
                      AND execution_policy_id = ?
                      AND created_at_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                    GROUP BY execution_decision
                    ORDER BY n DESC, execution_decision ASC
                    """,
                    (profile_id, asset_class, live_policy_id, f"-{int(hours)} hours"),
                ).fetchall()
                reason_rows = con.execute(
                    """
                    SELECT decision_reason_code, COUNT(*) AS n
                    FROM vanguard_signal_decision_log
                    WHERE profile_id = ?
                      AND asset_class = ?
                      AND execution_policy_mode = 'live'
                      AND execution_policy_id = ?
                      AND created_at_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
                    GROUP BY decision_reason_code
                    ORDER BY n DESC, decision_reason_code ASC
                    LIMIT 6
                    """,
                    (profile_id, asset_class, live_policy_id, f"-{int(hours)} hours"),
                ).fetchall()
            return {
                "live_policy_id": live_policy_id,
                "decision_counts": {
                    str(r["execution_decision"]): int(r["n"] or 0) for r in decision_rows
                },
                "reason_counts": {
                    str(r["decision_reason_code"] or ""): int(r["n"] or 0) for r in reason_rows
                },
            }
        except sqlite3.Error:
            return {"live_policy_id": live_policy_id, "decision_counts": {}, "reason_counts": {}}

    def _build_live_policy_contract_telegram(self, *, asset_class: str = "forex") -> str:
        pm_cfg = ((self._runtime_config or {}).get("position_manager") or {})
        runner_cfg = self._load_execution_policy_runner_config(asset_class)
        policies = dict(pm_cfg.get("policies") or {})
        shadow_ids = [str(v) for v in (runner_cfg.get("shadow_policy_ids") or []) if str(v)]
        shadow_bits = []
        for policy_id in shadow_ids:
            spec = dict(((runner_cfg.get("policies") or {}).get(policy_id) or {}))
            kind = str(spec.get("kind") or "?")
            shadow_bits.append(f"{policy_id}({kind})")
        active_bindings = self._active_telegram_bindings(asset_class)
        if active_bindings:
            policy_to_profiles: dict[str, list[str]] = {}
            for profile_id, binding in active_bindings:
                policy_id = str(binding.get("execution_policy_id") or runner_cfg.get("live_policy_id") or "").strip()
                if not policy_id:
                    policy_id = "-"
                policy_to_profiles.setdefault(policy_id, []).append(profile_id)
        else:
            fallback_policy = str(runner_cfg.get("live_policy_id") or "").strip() or "-"
            fallback_profile = str(self._accounts[0].get("id") or "ftmo_demo_100k") if self._accounts else "ftmo_demo_100k"
            policy_to_profiles = {fallback_policy: [fallback_profile]}

        policy_lines: list[str] = []
        for policy_id, profile_ids in policy_to_profiles.items():
            live_policy = dict(((runner_cfg.get("policies") or {}).get(policy_id) or {}))
            lifecycle_policy = dict((policies.get(policy_id) or {}))
            decision_counts: dict[str, int] = {}
            reason_counts: dict[str, int] = {}
            for profile_id in profile_ids:
                audit = self._live_policy_recent_audit(
                    profile_id=profile_id,
                    asset_class=asset_class,
                    hours=24,
                )
                for key, value in (audit.get("decision_counts") or {}).items():
                    decision_counts[str(key)] = int(decision_counts.get(str(key), 0)) + int(value or 0)
                for key, value in (audit.get("reason_counts") or {}).items():
                    reason_counts[str(key)] = int(reason_counts.get(str(key), 0)) + int(value or 0)

            decision_summary = " ".join(f"{k}={v}" for k, v in sorted(decision_counts.items())) or "-"
            reason_summary = " ".join(f"{k}={v}" for k, v in sorted(reason_counts.items())) or "-"
            gate_bits = [
                f"min_conviction={live_policy.get('min_conviction')}",
                f"min_top_rank={live_policy.get('min_top_rank_streak')}",
            ]
            if live_policy.get("max_global_cycle_rank_abs_pred") is not None:
                gate_bits.append(f"max_cycle_rank_abs_pred={live_policy.get('max_global_cycle_rank_abs_pred')}")
            if live_policy.get("min_abs_predicted_return") is not None:
                gate_bits.append(f"min_abs_pred={live_policy.get('min_abs_predicted_return')}")
            gate_bits.extend(
                [
                    f"max_flip={live_policy.get('max_flip_count_window')}",
                    f"session={live_policy.get('session_bucket') or 'all'}",
                    f"direction={live_policy.get('direction') or 'SYM'}",
                    f"checkpoint={live_policy.get('checkpoint_minutes') or 'default'}",
                ]
            )
            exit_summary = (
                f"bar={int(lifecycle_policy.get('bar_interval_minutes') or 5)}m "
                f"loss_review={int(lifecycle_policy.get('loss_review_interval_minutes') or 0)}m "
                f"close_below=${lifecycle_policy.get('close_below_pnl_dollars')} "
                f"hard_close={int(lifecycle_policy.get('hard_close_bars') or 0) * int(lifecycle_policy.get('bar_interval_minutes') or 5)}m "
                f"cooldown={int(lifecycle_policy.get('action_cooldown_seconds') or 0)}s "
                f"retries={int(lifecycle_policy.get('max_retry_count_per_action') or 0)}"
            )
            profile_names = ", ".join(self._profile_display_label(profile_id) for profile_id in profile_ids)
            policy_lines.extend(
                [
                    f"live={policy_id} profiles={profile_names}",
                    f"kind={live_policy.get('kind') or '-'}",
                    f"gate: {' '.join(gate_bits)}",
                    f"exit: {exit_summary}",
                    f"24h live decisions: {decision_summary}",
                    f"24h top reasons: {reason_summary}",
                    "",
                ]
            )
        return (
            f"📐 <b>[{self._telegram_env_label()}] LIVE POLICY CONTRACT</b>\n"
            f"asset={str(asset_class).upper()} binding={runner_cfg.get('binding_source') or '-'}\n"
            f"shadows: {', '.join(shadow_bits) if shadow_bits else 'none'}\n"
            f"bans: {self._tactical_bans_summary(asset_class)}\n"
            f"throughput: {self._throughput_mode_summary()}\n"
            f"cost_aware: {self._cost_aware_routing_summary()}\n"
            f"timeout_auto_close={'on' if bool((pm_cfg.get('timeout_auto_close') or {}).get('enabled')) else 'off'} "
            f"skip_legacy={'yes' if bool((pm_cfg.get('timeout_auto_close') or {}).get('skip_legacy_exit_rules')) else 'no'}\n"
            f"{chr(10).join(policy_lines).rstrip()}"
        )

    def _evaluate_execution_policy(
        self,
        policy_id: str,
        policy_spec: dict[str, Any] | None,
        *,
        row: dict[str, Any],
        asset_class: str | None,
        direction: str | None,
        session_bucket: str | None,
        candidate_metrics: dict[str, Any] | None,
    ) -> dict[str, Any]:
        spec = policy_spec or {}
        kind = str(spec.get("kind") or "").strip().lower()
        metrics = candidate_metrics or {}
        asset = str(asset_class or "").lower().strip()
        side = str(direction or "").upper().strip()
        session = str(session_bucket or "").lower().strip()
        conviction_raw = row.get("conviction")
        if conviction_raw is None:
            conviction_raw = metrics.get("conviction")
        conviction = float(conviction_raw) if conviction_raw not in (None, "") else None
        top_rank_streak = int(metrics.get("top_rank_streak") or 0)
        flip_count_window = int(metrics.get("flip_count_window") or 0)
        global_cycle_rank_abs_pred = (
            int(metrics.get("global_cycle_rank_abs_pred"))
            if metrics.get("global_cycle_rank_abs_pred") not in (None, "")
            else None
        )
        predicted_return_raw = metrics.get("predicted_return", row.get("predicted_return"))
        predicted_return = (
            float(predicted_return_raw) if predicted_return_raw not in (None, "") else None
        )
        abs_predicted_return = abs(predicted_return) if predicted_return is not None else None
        target_horizon_bars = int(row.get("target_horizon_bars") or metrics.get("target_horizon_bars") or 0)
        reason_json = {
            "policy_kind": kind,
            "conviction": conviction,
            "top_rank_streak": top_rank_streak,
            "flip_count_window": flip_count_window,
            "global_cycle_rank_abs_pred": global_cycle_rank_abs_pred,
            "predicted_return": predicted_return,
            "abs_predicted_return": abs_predicted_return,
            "target_horizon_bars": target_horizon_bars,
            "session_bucket": session or None,
            "direction": side or None,
        }
        if str(spec.get("asset_class") or "").lower().strip() not in {"", asset}:
            return {
                "approved": False,
                "reason_code": "POLICY_ASSET_MISMATCH",
                "reason_json": {**reason_json, "required_asset_class": spec.get("asset_class")},
            }
        if kind == "baseline":
            return {
                "approved": True,
                "reason_code": "POLICY_BASELINE_APPROVED",
                "reason_json": {**reason_json, "gate_policy": str(row.get("gate_policy") or "") or None},
            }
        if kind == "conviction_top_rank":
            min_conviction = float(spec.get("min_conviction") or 0.0)
            min_top_rank = int(spec.get("min_top_rank_streak") or 1)
            approved = conviction is not None and conviction >= min_conviction and top_rank_streak >= min_top_rank
            return {
                "approved": approved,
                "reason_code": "POLICY_APPROVED" if approved else "POLICY_FILTER_NOT_MET",
                "reason_json": {
                    **reason_json,
                    "min_conviction": min_conviction,
                    "min_top_rank_streak": min_top_rank,
                },
            }
        if kind == "conviction_top_rank_directional":
            # Direction-aware mirror of conviction_top_rank: admits strong SHORT
            # conviction (conviction <= 1 - min_conviction) alongside strong LONG
            # (conviction >= min_conviction). Observational use recommended until
            # SHORT-side sample is validated independently of LONG.
            min_conviction = float(spec.get("min_conviction") or 0.0)
            min_top_rank = int(spec.get("min_top_rank_streak") or 1)
            directional_conviction = (
                max(conviction, 1.0 - conviction) if conviction is not None else None
            )
            approved = (
                directional_conviction is not None
                and directional_conviction >= min_conviction
                and top_rank_streak >= min_top_rank
            )
            min_abs_predicted_return = (
                float(spec.get("min_abs_predicted_return"))
                if spec.get("min_abs_predicted_return") not in (None, "")
                else None
            )
            max_global_cycle_rank_abs_pred = (
                int(spec.get("max_global_cycle_rank_abs_pred"))
                if spec.get("max_global_cycle_rank_abs_pred") not in (None, "")
                else None
            )
            if min_abs_predicted_return is not None:
                approved = approved and abs_predicted_return is not None and abs_predicted_return >= min_abs_predicted_return
            if max_global_cycle_rank_abs_pred is not None:
                approved = (
                    approved
                    and global_cycle_rank_abs_pred is not None
                    and global_cycle_rank_abs_pred <= max_global_cycle_rank_abs_pred
                )
            return {
                "approved": approved,
                "reason_code": "POLICY_APPROVED" if approved else "POLICY_FILTER_NOT_MET",
                "reason_json": {
                    **reason_json,
                    "min_conviction": min_conviction,
                    "min_top_rank_streak": min_top_rank,
                    "directional_conviction": directional_conviction,
                    "min_abs_predicted_return": min_abs_predicted_return,
                    "max_global_cycle_rank_abs_pred": max_global_cycle_rank_abs_pred,
                    "conviction_direction": (
                        "LONG" if (conviction is not None and conviction >= 0.5)
                        else "SHORT" if conviction is not None
                        else None
                    ),
                    "gate_shape": "MAX(c, 1-c) >= min_conviction",
                },
            }
        if kind == "conviction_short_top_rank":
            min_conviction = float(spec.get("min_conviction") or 0.0)
            max_short_conviction = 1.0 - min_conviction
            min_top_rank = int(spec.get("min_top_rank_streak") or 1)
            approved = (
                conviction is not None
                and conviction <= max_short_conviction
                and top_rank_streak >= min_top_rank
            )
            return {
                "approved": approved,
                "reason_code": "POLICY_APPROVED" if approved else "POLICY_FILTER_NOT_MET",
                "reason_json": {
                    **reason_json,
                    "min_conviction": min_conviction,
                    "max_short_conviction": max_short_conviction,
                    "min_top_rank_streak": min_top_rank,
                    "gate_shape": "conviction <= 1 - min_conviction",
                },
            }
        if kind == "conviction_flip":
            min_conviction = float(spec.get("min_conviction") or 0.0)
            max_flip = int(spec.get("max_flip_count_window") or 0)
            approved = conviction is not None and conviction >= min_conviction and flip_count_window <= max_flip
            return {
                "approved": approved,
                "reason_code": "POLICY_APPROVED" if approved else "POLICY_FILTER_NOT_MET",
                "reason_json": {
                    **reason_json,
                    "min_conviction": min_conviction,
                    "max_flip_count_window": max_flip,
                },
            }
        if kind == "asian_long_240":
            required_session = str(spec.get("session_bucket") or "asian").lower().strip()
            required_direction = str(spec.get("direction") or "LONG").upper().strip()
            checkpoint_minutes = int(spec.get("checkpoint_minutes") or 240)
            approved = session == required_session and side == required_direction
            return {
                "approved": approved,
                "reason_code": "POLICY_APPROVED" if approved else "POLICY_FILTER_NOT_MET",
                "reason_json": {
                    **reason_json,
                    "required_session_bucket": required_session,
                    "required_direction": required_direction,
                    "checkpoint_minutes": checkpoint_minutes,
                },
            }
        if kind == "pocket_matrix_long_only_v1":
            # Slot-4-only pocket-matrix policy. LONG-only, family-aware (USD/JPY_X/CROSS),
            # conviction-banded, session-restricted. Rejection emits precise reason codes.
            sym = str(row.get("symbol") or metrics.get("symbol") or "").upper()
            if sym.endswith("JPY"):
                family = "JPY_X"
            elif sym.startswith("USD") or sym.endswith("USD"):
                family = "USD"
            elif sym:
                family = "CROSS"
            else:
                family = None

            allowed_pockets = spec.get("allowed_pockets") or []

            # Hard rejects in precedence order.
            if side != "LONG":
                return {
                    "approved": False,
                    "reason_code": "POLICY_SHORT_NOT_ALLOWED",
                    "reason_json": {
                        **reason_json,
                        "policy_id": policy_id,
                        "family": family,
                        "symbol": sym,
                    },
                }
            allowed_sessions = {
                str(s).lower().strip()
                for pocket in allowed_pockets
                for s in (
                    [pocket.get("session")] if isinstance(pocket.get("session"), str)
                    else (pocket.get("session") or [])
                )
            } or {"asian", "overlap"}
            if session not in allowed_sessions:
                return {
                    "approved": False,
                    "reason_code": "POLICY_SESSION_NOT_ALLOWED",
                    "reason_json": {
                        **reason_json,
                        "policy_id": policy_id,
                        "family": family,
                        "symbol": sym,
                        "allowed_sessions": sorted(allowed_sessions),
                    },
                }
            if family is None:
                return {
                    "approved": False,
                    "reason_code": "POLICY_FAMILY_CLASSIFICATION_MISSING",
                    "reason_json": {
                        **reason_json,
                        "policy_id": policy_id,
                        "symbol": sym,
                    },
                }

            # Match against allowed pockets. A pocket matches if family+session+direction
            # match AND conviction is in [min_conv, max_conv).
            family_session_match = False
            for pocket in allowed_pockets:
                p_family = str(pocket.get("family") or "").upper().strip()
                p_dir = str(pocket.get("direction") or "LONG").upper().strip()
                p_sessions = pocket.get("session")
                if isinstance(p_sessions, str):
                    p_session_set = {p_sessions.lower().strip()}
                else:
                    p_session_set = {str(s).lower().strip() for s in (p_sessions or [])}
                if p_family != family:
                    continue
                if p_dir != side:
                    continue
                if session not in p_session_set:
                    continue
                family_session_match = True
                p_min_conv = float(pocket.get("min_conv", 0.0) or 0.0)
                p_max_conv = pocket.get("max_conv")
                p_max_conv = float(p_max_conv) if p_max_conv not in (None, "") else 1.0
                if conviction is None:
                    continue
                # Inclusive on min, exclusive on max — except when max == 1.0 (catch top tail).
                if conviction >= p_min_conv and (
                    conviction < p_max_conv or (p_max_conv >= 1.0 and conviction <= 1.0)
                ):
                    return {
                        "approved": True,
                        "reason_code": "POLICY_APPROVED",
                        "reason_json": {
                            **reason_json,
                            "policy_id": policy_id,
                            "matched_pocket": {
                                "family": p_family,
                                "session": session,
                                "direction": p_dir,
                                "min_conv": p_min_conv,
                                "max_conv": p_max_conv,
                            },
                            "symbol": sym,
                            "family": family,
                        },
                    }

            # No conviction-band match: distinguish "no family/session match" from
            # "family/session matched but conviction outside all allowed bands".
            if family_session_match:
                return {
                    "approved": False,
                    "reason_code": "POLICY_CONVICTION_BUCKET_NOT_ALLOWED",
                    "reason_json": {
                        **reason_json,
                        "policy_id": policy_id,
                        "family": family,
                        "symbol": sym,
                        "conviction_observed": conviction,
                    },
                }
            return {
                "approved": False,
                "reason_code": "POLICY_POCKET_NOT_ALLOWED",
                "reason_json": {
                    **reason_json,
                    "policy_id": policy_id,
                    "family": family,
                    "symbol": sym,
                },
            }
        return {
            "approved": False,
            "reason_code": "POLICY_UNKNOWN",
            "reason_json": {**reason_json, "policy_id": policy_id},
        }

    def _build_execution_policy_summary_telegram(
        self,
        cycle_ts: str,
        summary: dict[str, Any] | None,
    ) -> str | None:
        if not summary:
            return None
        lines = [
            f"🧪 <b>[{self._telegram_env_label()}] EXECUTION POLICIES</b> — {html.escape(str(cycle_ts or '')[:16])}",
            f"mode={html.escape(str(self.execution_mode or '').upper())}",
        ]
        live = summary.get("live") or {}
        live_policy_id = str(live.get("policy_id") or "").strip()
        if live_policy_id:
            lines.append(
                "LIVE "
                f"{html.escape(live_policy_id)}: approved={int(live.get('approved') or 0)} "
                f"executed={int(live.get('executed') or 0)} "
                f"filled={int(live.get('filled') or 0)} "
                f"failed={int(live.get('failed') or 0)} "
                f"forward={int(live.get('forward_tracked') or 0)} "
                f"rejected={int(live.get('rejected') or 0)}"
            )
        shadow = summary.get("shadow") or {}
        if shadow:
            lines.append("")
            lines.append("SHADOW")
            for policy_id, counts in shadow.items():
                lines.append(
                    f"{html.escape(str(policy_id))}: approved={int((counts or {}).get('approved') or 0)} "
                    f"rejected={int((counts or {}).get('rejected') or 0)}"
                )
        return "\n".join(lines)

    def _build_live_trade_shortlist_telegram(
        self,
        cycle_ts: str,
        summary: dict[str, Any] | None = None,
    ) -> str | None:
        visible_assets = [str(asset or "").lower() for asset in self._telegram_visible_asset_classes()]
        asset_class = "forex" if "forex" in visible_assets else str((visible_assets[0] if visible_assets else "forex") or "forex").lower()
        active_profile_ids = self._active_telegram_profile_ids(asset_class)
        if not active_profile_ids:
            active_profile_ids = [
                str(account.get("id") or "")
                for account in (self._accounts or [])
                if str(account.get("id") or "").strip()
            ]
        if not active_profile_ids:
            return None

        active_bindings = self._active_telegram_bindings(asset_class)
        runner_cfg = self._load_execution_policy_runner_config(asset_class)
        policy_to_profile_ids: dict[str, list[str]] = defaultdict(list)
        for profile_id, binding in active_bindings:
            policy_id = str(binding.get("execution_policy_id") or "").strip()
            if not policy_id:
                continue
            policy_to_profile_ids[policy_id].append(str(profile_id or "").strip())
        if not policy_to_profile_ids:
            summary_policy_id = str(((summary or {}).get("live") or {}).get("policy_id") or runner_cfg.get("live_policy_id") or "").strip()
            if summary_policy_id:
                policy_to_profile_ids[summary_policy_id] = list(active_profile_ids)
        if not policy_to_profile_ids:
            return None
        cycle_audit_rows: list[dict[str, Any]] = []

        placeholders = ",".join("?" for _ in active_profile_ids) or "''"
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                live_policy_rows = [
                    dict(row)
                    for row in con.execute(
                        f"""
                        SELECT profile_id,
                               symbol,
                               direction,
                               execution_policy_id,
                               execution_decision,
                               decision_reason_code,
                               selection_rank,
                               entry_price,
                               lot_size,
                               edge_score,
                               predicted_return,
                               decision_reason_json
                        FROM vanguard_signal_decision_log
                        WHERE cycle_ts_utc = ?
                          AND profile_id IN ({placeholders})
                          AND execution_policy_mode = 'live'
                        ORDER BY selection_rank IS NULL, selection_rank, symbol
                        """,
                        [cycle_ts, *active_profile_ids],
                    )
                ]
                pre_live_rows = [
                    dict(row)
                    for row in con.execute(
                        f"""
                        SELECT profile_id,
                               symbol,
                               direction,
                               execution_decision,
                               decision_reason_code,
                               selection_rank,
                               entry_price,
                               lot_size,
                               edge_score,
                               predicted_return,
                               decision_reason_json
                        FROM vanguard_signal_decision_log
                        WHERE cycle_ts_utc = ?
                          AND profile_id IN ({placeholders})
                          AND execution_policy_mode IS NULL
                          AND execution_decision IN ('SKIPPED_SELECTION_CAP', 'BRIDGE_FORWARD_TRACKED', 'EXECUTION_FAILED')
                        ORDER BY predicted_return DESC, symbol
                        """,
                        [cycle_ts, *active_profile_ids],
                    )
                ]
                v5_rank_lookup: dict[tuple[str, str, str, str], int | None] = {}
                try:
                    for row in con.execute(
                        f"""
                        SELECT cycle_ts_utc,
                               profile_id,
                               symbol,
                               direction,
                               global_cycle_rank_abs_pred,
                               top_rank_streak,
                               metrics_json
                        FROM vanguard_v5_selection
                        WHERE cycle_ts_utc = ?
                          AND profile_id IN ({placeholders})
                          AND asset_class = ?
                        """,
                        [cycle_ts, *active_profile_ids, asset_class],
                    ):
                        v5_rank_lookup[
                            (
                                str(row["cycle_ts_utc"] or ""),
                                str(row["profile_id"] or ""),
                                str(row["symbol"] or "").upper(),
                                str(row["direction"] or "").upper(),
                            )
                        ] = {
                            "global_cycle_rank_abs_pred": (
                                None if row["global_cycle_rank_abs_pred"] is None else int(row["global_cycle_rank_abs_pred"])
                            ),
                            "top_rank_streak": int(row["top_rank_streak"] or 0),
                            **(
                                json.loads(row["metrics_json"] or "{}")
                                if row["metrics_json"] not in (None, "")
                                else {}
                            ),
                        }
                except Exception:
                    v5_rank_lookup = {}
                try:
                    cycle_audit_rows = [
                        dict(row)
                        for row in con.execute(
                            f"""
                            SELECT profile_id,
                                   execution_policy_id,
                                   tradeability_rows,
                                   tradeability_pass_rows,
                                   live_policy_filtered_rows,
                                   route_candidate_rows,
                                   selected_rows,
                                   portfolio_approved_rows,
                                   decision_rows,
                                   selection_reasons_json,
                                   live_policy_failed_checks_json
                            FROM vanguard_cycle_audit
                            WHERE cycle_ts_utc = ?
                              AND profile_id IN ({placeholders})
                              AND asset_class = ?
                            """,
                            [cycle_ts, *active_profile_ids, asset_class],
                        )
                    ]
                except Exception:
                    cycle_audit_rows = []
        except Exception as exc:
            logger.warning("Could not build live trade shortlist Telegram: %s", exc)
            return None

        live_rows = [row for row in live_policy_rows if str(row.get("execution_decision") or "").upper() == "EXECUTED"]
        for row in pre_live_rows:
            key = (
                cycle_ts,
                str(row.get("profile_id") or ""),
                str(row.get("symbol") or "").upper(),
                str(row.get("direction") or "").upper(),
            )
        display_n = int(((self._runtime_config or {}).get("runtime") or {}).get("shortlist_display_top_n") or 10)

        def _bucket(row: dict[str, Any]) -> str:
            reason = str(row.get("decision_reason_code") or "").upper()
            decision = str(row.get("execution_decision") or "").upper()
            if decision == "EXECUTED":
                return "TAKEN_LIVE"
            if reason == "TACTICAL_BAN":
                return "BANNED"
            if reason in {
                "BRIDGE_FORWARD_TRACKED",
                "SCHEDULED_FLATTEN_WINDOW",
                "LIVE_ASSET_DISABLED",
                "ZERO_SIZING",
                "PROFILE_SELECTION_CAP",
                "PROFILE_DIRECTION_SELECTION_CAP",
                "ASSET_CLASS_SELECTION_CAP",
                "BASE_CURRENCY_SELECTION_CAP",
                "SKIPPED_DUPLICATE_SYMBOL_OPEN",
                "DUPLICATE_SYMBOL_OPEN",
                "THROUGHPUT_LIMIT",
            }:
                return "ACTIONABLE_NOT_ROUTED"
            if decision == "EXECUTION_FAILED":
                return "FAILED"
            return decision or "OTHER"

        def _fmt_price(value: Any) -> str:
            try:
                val = float(value)
            except Exception:
                return "-"
            if val >= 100:
                return f"{val:.3f}"
            return f"{val:.5f}"

        def _fmt_pred(row: dict[str, Any]) -> str:
            pred = row.get("predicted_return")
            try:
                return f"{float(pred):+.4f}"
            except Exception:
                return "-"

        def _pre_live_block_label(row: dict[str, Any]) -> str:
            reason = str(row.get("decision_reason_code") or row.get("execution_decision") or "").upper()
            labels = {
                "PROFILE_DIRECTION_SELECTION_CAP": "BLOCKED: direction cap already full",
                "PROFILE_SELECTION_CAP": "BLOCKED: profile cap already full",
                "ASSET_CLASS_SELECTION_CAP": "BLOCKED: asset-class cap already full",
                "BASE_CURRENCY_SELECTION_CAP": "BLOCKED: base-currency cap already full",
                "BLOCKED_ACCOUNT_HEADROOM": "BLOCKED: account headroom too low",
                "SKIPPED_DUPLICATE_SYMBOL_OPEN": "BLOCKED: symbol already open",
                "DUPLICATE_SYMBOL_OPEN": "BLOCKED: symbol already open",
                "BRIDGE_FORWARD_TRACKED": "BLOCKED: forward-tracked only",
                "EXECUTION_FAILED": "BLOCKED: execution failed",
            }
            return labels.get(reason, f"BLOCKED: {reason.lower().replace('_', ' ')}")

        def _is_manual_candidate(row: dict[str, Any]) -> bool:
            reason = str(row.get("decision_reason_code") or row.get("execution_decision") or "").upper()
            if reason not in {
                "PROFILE_DIRECTION_SELECTION_CAP",
                "PROFILE_SELECTION_CAP",
                "ASSET_CLASS_SELECTION_CAP",
                "BASE_CURRENCY_SELECTION_CAP",
                "BLOCKED_ACCOUNT_HEADROOM",
            }:
                return False
            try:
                pred = abs(float(row.get("predicted_return")))
            except Exception:
                return False
            global_rank = row.get("global_cycle_rank_abs_pred")
            try:
                rank_ok = True if max_global_rank is None else (
                    global_rank is not None and int(global_rank) <= int(max_global_rank)
                )
            except Exception:
                rank_ok = False
            try:
                mag_ok = True if min_abs_pred is None else pred >= float(min_abs_pred)
            except Exception:
                mag_ok = False
            return bool(rank_ok and mag_ok)

        def _pre_live_manual_gate_bits(row: dict[str, Any]) -> str:
            bits: list[str] = []
            global_rank = row.get("global_cycle_rank_abs_pred")
            try:
                pred = abs(float(row.get("predicted_return")))
            except Exception:
                pred = None
            try:
                if global_rank is not None and max_global_rank is not None:
                    if int(global_rank) > int(max_global_rank):
                        bits.append(f"rank {int(global_rank)} > {int(max_global_rank)}")
            except Exception:
                pass
            try:
                if pred is not None and min_abs_pred is not None and pred < float(min_abs_pred):
                    bits.append(f"|pred| {pred:.4f} < {float(min_abs_pred):.2f}")
            except Exception:
                pass
            return "  ".join(bits)

        def _live_policy_block_label(row: dict[str, Any]) -> str:
            payload = {}
            try:
                payload = json.loads(row.get("decision_reason_json") or "{}")
            except Exception:
                payload = {}

            global_rank = payload.get("global_cycle_rank_abs_pred")
            max_rank = payload.get("max_global_cycle_rank_abs_pred")
            abs_pred = payload.get("abs_predicted_return")
            min_abs_pred_payload = payload.get("min_abs_predicted_return")

            rank_failed = False
            mag_failed = False
            try:
                if global_rank is not None and max_rank is not None:
                    rank_failed = int(global_rank) > int(max_rank)
            except Exception:
                rank_failed = False
            try:
                if abs_pred is not None and min_abs_pred_payload is not None:
                    mag_failed = float(abs_pred) < float(min_abs_pred_payload)
            except Exception:
                mag_failed = False

            if rank_failed and mag_failed:
                return "FAILED POLICY: not top-3 and predicted move too small"
            if rank_failed:
                return "FAILED POLICY: not top-3 in cycle"
            if mag_failed:
                return "FAILED POLICY: predicted move below threshold"
            return "FAILED POLICY"

        def _live_policy_requirement_bits(row: dict[str, Any]) -> str:
            payload = {}
            try:
                payload = json.loads(row.get("decision_reason_json") or "{}")
            except Exception:
                payload = {}

            bits: list[str] = []
            if payload.get("global_cycle_rank_abs_pred") is not None and payload.get("max_global_cycle_rank_abs_pred") is not None:
                try:
                    current_rank = int(payload["global_cycle_rank_abs_pred"])
                    max_rank = int(payload["max_global_cycle_rank_abs_pred"])
                    if current_rank > max_rank:
                        bits.append(f"rank {current_rank} > {max_rank}")
                except Exception:
                    pass
            if payload.get("abs_predicted_return") is not None and payload.get("min_abs_predicted_return") is not None:
                try:
                    abs_pred = float(payload["abs_predicted_return"])
                    min_required = float(payload["min_abs_predicted_return"])
                    if abs_pred < min_required:
                        bits.append(f"|pred| {abs_pred:.4f} < {min_required:.2f}")
                except Exception:
                    pass
            return "  ".join(bits)

        def _annotate_with_v5_metrics(row: dict[str, Any]) -> dict[str, Any]:
            key = (
                cycle_ts,
                str(row.get("profile_id") or ""),
                str(row.get("symbol") or "").upper(),
                str(row.get("direction") or "").upper(),
            )
            metrics = dict(v5_rank_lookup.get(key) or {})
            row = dict(row)
            row["_v5_metrics"] = metrics
            row["global_cycle_rank_abs_pred"] = metrics.get("global_cycle_rank_abs_pred")
            return row

        pre_live_rows = [_annotate_with_v5_metrics(row) for row in pre_live_rows]

        def _passes_live_policy(row: dict[str, Any], live_policy_id: str, live_policy_cfg: dict[str, Any]) -> bool:
            metrics = dict(row.get("_v5_metrics") or {})
            eval_row = {
                "conviction": metrics.get("conviction"),
                "predicted_return": row.get("predicted_return"),
                "target_horizon_bars": metrics.get("target_horizon_bars"),
            }
            result = self._evaluate_execution_policy(
                live_policy_id,
                live_policy_cfg,
                row=eval_row,
                asset_class=asset_class,
                direction=row.get("direction"),
                session_bucket=metrics.get("session_bucket"),
                candidate_metrics=metrics,
            )
            row["_policy_eval"] = result
            return bool(result.get("approved"))

        cap_reason_codes = {
            "PROFILE_DIRECTION_SELECTION_CAP",
            "PROFILE_SELECTION_CAP",
            "ASSET_CLASS_SELECTION_CAP",
            "BASE_CURRENCY_SELECTION_CAP",
            "BLOCKED_ACCOUNT_HEADROOM",
        }
        lines = [
            f"🎯 <b>[{self._telegram_env_label()}] LIVE TRADE SHORTLIST</b> — {html.escape(cycle_ts[:16])}",
            (
                f"policy={html.escape(next(iter(policy_to_profile_ids.keys())))}"
                if len(policy_to_profile_ids) == 1
                else "policies=" + " | ".join(
                    f"{html.escape(policy_id)} x{len(profile_ids)}"
                    for policy_id, profile_ids in sorted(policy_to_profile_ids.items())
                )
            ),
            f"tactical_bans={html.escape(self._tactical_bans_summary(asset_class))}",
            f"routing_limit={html.escape(self._throughput_mode_summary())}",
            self._build_session_status_block(asset_class=asset_class, profile_ids=active_profile_ids),
            "",
        ]
        def _append_rows(title: str, rows_in: list[dict[str, Any]], formatter) -> None:
            lines.append(f"━━ {title} ━━")
            if rows_in:
                for row in rows_in[:display_n]:
                    for line in formatter(row):
                        lines.append(line)
            else:
                lines.append("none")
            lines.append("")

        section_labels = {
            "ACTIONABLE_NOT_ROUTED": "ACTIONABLE NOT ROUTED",
            "TAKEN_LIVE": "TAKEN LIVE",
            "BANNED": "BANNED",
            "FAILED": "FAILED",
            "OTHER": "OTHER",
        }

        def _fmt_executed_row(row: dict[str, Any]) -> list[str]:
            symbol = str(row.get("symbol") or "").upper()
            direction = str(row.get("direction") or "").upper()
            emoji = "🟢" if direction == "LONG" else "🔴"
            qty = row.get("lot_size")
            qty_text = "-" if qty is None else f"{float(qty):.2f}"
            payload = {}
            try:
                payload = json.loads(row.get("decision_reason_json") or "{}")
            except Exception:
                payload = {}
            rank = payload.get("global_cycle_rank_abs_pred")
            if rank is None:
                key = (
                    cycle_ts,
                    str(row.get("profile_id") or ""),
                    symbol,
                    direction,
                )
                rank = (v5_rank_lookup.get(key) or {}).get("global_cycle_rank_abs_pred")
            rank_text = "-" if rank is None else str(int(rank))
            return [
                f"{emoji} {html.escape(symbol)} {html.escape(direction)}  rank={html.escape(rank_text)}  qty={html.escape(qty_text)}  entry={html.escape(_fmt_price(row.get('entry_price')))}  pred={html.escape(_fmt_pred(row))}",
                "  PASSED POLICY AND EXECUTED",
            ]

        def _fmt_passed_cap_row(row: dict[str, Any]) -> list[str]:
            symbol = str(row.get("symbol") or "").upper()
            direction = str(row.get("direction") or "").upper()
            emoji = "🟢" if direction == "LONG" else "🔴"
            reason = _pre_live_block_label(row)
            return [
                f"{emoji} {html.escape(symbol)} {html.escape(direction)}  pred={html.escape(_fmt_pred(row))}",
                f"  PASSED POLICY BUT BLOCKED BY CAP/HEADROOM — {html.escape(reason.replace('BLOCKED: ', ''))}",
            ]

        def _fmt_passed_other_row(row: dict[str, Any]) -> list[str]:
            symbol = str(row.get("symbol") or "").upper()
            direction = str(row.get("direction") or "").upper()
            emoji = "🟢" if direction == "LONG" else "🔴"
            reason = _pre_live_block_label(row)
            return [
                f"{emoji} {html.escape(symbol)} {html.escape(direction)}  pred={html.escape(_fmt_pred(row))}",
                f"  PASSED POLICY BUT FAILED OTHER — {html.escape(reason.replace('BLOCKED: ', ''))}",
            ]

        def _fmt_failed_policy_row(row: dict[str, Any]) -> list[str]:
            symbol = str(row.get("symbol") or "").upper()
            direction = str(row.get("direction") or "").upper()
            emoji = "🟢" if direction == "LONG" else "🔴"
            entry = row.get("entry_price")
            detail = ""
            if row.get("_policy_eval"):
                detail = _live_policy_requirement_bits({"decision_reason_json": json.dumps((row.get("_policy_eval") or {}).get("reason_json") or {})})
            else:
                detail = _live_policy_requirement_bits(row)
            label = _live_policy_block_label({"decision_reason_json": json.dumps((row.get("_policy_eval") or {}).get("reason_json") or {})}) if row.get("_policy_eval") else _live_policy_block_label(row)
            first = f"{emoji} {html.escape(symbol)} {html.escape(direction)}  pred={html.escape(_fmt_pred(row))}"
            if entry not in (None, ""):
                first += f"  entry={html.escape(_fmt_price(entry))}"
            second = f"  {html.escape(label)}"
            if detail:
                second += f"  {html.escape(detail)}"
            return [first, second]

        def _fmt_manual_row(row: dict[str, Any]) -> list[str]:
            symbol = str(row.get("symbol") or "").upper()
            direction = str(row.get("direction") or "").upper()
            emoji = "🟢" if direction == "LONG" else "🔴"
            if str(row.get("execution_decision") or "").upper() == "EXECUTED":
                payload = {}
                try:
                    payload = json.loads(row.get("decision_reason_json") or "{}")
                except Exception:
                    payload = {}
                rank = payload.get("global_cycle_rank_abs_pred")
                if rank is None:
                    key = (
                        cycle_ts,
                        str(row.get("profile_id") or ""),
                        symbol,
                        direction,
                    )
                    rank = (v5_rank_lookup.get(key) or {}).get("global_cycle_rank_abs_pred")
                rank_text = "-" if rank is None else str(int(rank))
                return [
                    f"{emoji} {html.escape(symbol)} {html.escape(direction)}  rank={html.escape(rank_text)}  pred={html.escape(_fmt_pred(row))}",
                    "  MANUAL CANDIDATE — passed policy and executed live",
                ]
            reason = _pre_live_block_label(row).replace("BLOCKED: ", "")
            return [
                f"{emoji} {html.escape(symbol)} {html.escape(direction)}  pred={html.escape(_fmt_pred(row))}",
                f"  MANUAL CANDIDATE — passed policy, blocked by {html.escape(reason)}",
            ]

        for policy_id, policy_profile_ids in sorted(policy_to_profile_ids.items()):
            live_policy_cfg = dict(((runner_cfg.get("policies") or {}).get(policy_id) or {}))
            max_global_rank = live_policy_cfg.get("max_global_cycle_rank_abs_pred")
            min_abs_pred = live_policy_cfg.get("min_abs_predicted_return")
            policy_profile_set = {str(pid or "").strip() for pid in policy_profile_ids if str(pid or "").strip()}
            policy_live_rows = [
                row for row in live_policy_rows
                if str(row.get("profile_id") or "").strip() in policy_profile_set
                and str(row.get("execution_policy_id") or "").strip() == policy_id
            ]
            policy_executed_rows = [row for row in policy_live_rows if str(row.get("execution_decision") or "").upper() == "EXECUTED"]
            policy_pre_live_rows = [row for row in pre_live_rows if str(row.get("profile_id") or "").strip() in policy_profile_set]
            passed_policy_pre_live = [
                row for row in policy_pre_live_rows
                if _passes_live_policy(row, policy_id, live_policy_cfg)
            ]
            passed_policy_cap = [
                row for row in passed_policy_pre_live
                if str(row.get("decision_reason_code") or "").upper() in cap_reason_codes
            ]
            passed_policy_other = [
                row for row in passed_policy_pre_live
                if str(row.get("decision_reason_code") or "").upper() not in cap_reason_codes
            ]
            manual_candidates = list(policy_executed_rows) + passed_policy_cap + passed_policy_other

            audit_policy_filtered = 0
            audit_tradeability_rows = 0
            audit_tradeability_pass_rows = 0
            audit_failed_checks: Counter[str] = Counter()
            audit_selection_reasons: Counter[str] = Counter()
            for row in cycle_audit_rows:
                if str(row.get("profile_id") or "").strip() not in policy_profile_set:
                    continue
                if str(row.get("execution_policy_id") or "").strip() not in {"", policy_id}:
                    continue
                audit_policy_filtered += int(row.get("live_policy_filtered_rows") or 0)
                audit_tradeability_rows += int(row.get("tradeability_rows") or 0)
                audit_tradeability_pass_rows += int(row.get("tradeability_pass_rows") or 0)
                try:
                    audit_failed_checks.update({
                        str(k): int(v or 0)
                        for k, v in (json.loads(row.get("live_policy_failed_checks_json") or "{}") or {}).items()
                    })
                except Exception:
                    pass
                try:
                    audit_selection_reasons.update({
                        str(k): int(v or 0)
                        for k, v in (json.loads(row.get("selection_reasons_json") or "{}") or {}).items()
                    })
                except Exception:
                    pass

            total_rows = len(policy_live_rows)
            lines.append(f"━━ POLICY {html.escape(policy_id)} ━━")
            lines.append(f"profiles={', '.join(html.escape(self._profile_display_label(pid)) for pid in policy_profile_ids)}")
            if not policy_executed_rows:
                live_counts = dict((summary or {}).get("live") or {}) if len(policy_to_profile_ids) == 1 else {}
                skipped_policy = int(live_counts.get("rejected", 0) or 0) or audit_policy_filtered
                total_considered = total_rows or skipped_policy or audit_tradeability_rows
                lines.append("━━ NO LIVE PASS ━━")
                lines.append("No FX pairs passed the full routing path this cycle.")
                if total_considered:
                    lines.append(f"considered={total_considered}  skipped_policy={skipped_policy}")
                if audit_tradeability_pass_rows:
                    route_candidate_rows = len(policy_executed_rows) + len(passed_policy_cap) + len(passed_policy_other)
                    lines.append(f"tradeability_pass={audit_tradeability_pass_rows}  route_candidates={route_candidate_rows}")
                if audit_selection_reasons:
                    reason_bits = [
                        f"{key}={value}"
                        for key, value in sorted(audit_selection_reasons.items())
                        if int(value or 0) > 0
                    ]
                    if reason_bits:
                        lines.append(f"selection_reasons: {'  '.join(reason_bits)}")
                if audit_failed_checks:
                    failed_bits = [
                        f"{key}={value}"
                        for key, value in sorted(audit_failed_checks.items())
                        if int(value or 0) > 0
                    ]
                    if failed_bits:
                        lines.append(f"policy_failed_checks: {'  '.join(failed_bits)}")
                lines.append("")
            _append_rows("PASSED POLICY + EXECUTED", policy_executed_rows, _fmt_executed_row)
            _append_rows("PASSED POLICY BUT BLOCKED BY CAP/HEADROOM", passed_policy_cap, _fmt_passed_cap_row)
            _append_rows("PASSED POLICY BUT FAILED OTHER", passed_policy_other, _fmt_passed_other_row)
            _append_rows("MANUAL CANDIDATES", manual_candidates, _fmt_manual_row)
        return "\n".join(lines).rstrip()

    def _dwx_bridge_recent_lifecycle_activity(self, profile_id: str, lookback_seconds: int = 180) -> dict[str, Any] | None:
        """
        Return the most recent close/modify lifecycle action on this profile within a short lookback.

        DWX uses a single local file bridge for both closes and new entries. A brief quiet period
        after lifecycle close activity avoids contaminating fresh entry submits with stale close
        confirmations or bridge churn.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=int(lookback_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT request_id, action_type, trade_id, broker_position_id, status,
                       COALESCE(finished_at_utc, submitted_at_utc, requested_at_utc) AS activity_ts_utc
                FROM vanguard_execution_requests
                WHERE profile_id = ?
                  AND action_type IN ('CLOSE_POSITION', 'CLOSE_AT_TIMEOUT', 'SET_EXACT_SL_TP', 'MOVE_SL_TO_BREAKEVEN')
                  AND COALESCE(finished_at_utc, submitted_at_utc, requested_at_utc) >= ?
                ORDER BY COALESCE(finished_at_utc, submitted_at_utc, requested_at_utc) DESC, request_id DESC
                LIMIT 1
                """,
                (profile_id, cutoff),
            ).fetchone()
        return dict(row) if row else None

    def _checkpoint_db_wal(self, *, min_wal_bytes: int = 256 * 1024 * 1024) -> None:
        """Run a controlled WAL checkpoint outside the hot DB connection path."""
        try:
            checkpoint_wal_truncate(self.db_path, min_wal_bytes=min_wal_bytes)
        except Exception as exc:
            logger.warning("WAL checkpoint maintenance skipped for %s: %s", self.db_path, exc)

    def _log_loop_db_health(self) -> None:
        """Emit open-FD and WAL size metrics so DB-handle leaks are visible."""
        try:
            fd_count = len(os.listdir("/dev/fd"))
        except Exception:
            fd_count = -1
        wal_bytes = warn_if_large_wal(self.db_path, threshold_bytes=0)
        logger.info(
            "[DB HEALTH] open_fds=%s wal_size=%.2f MiB db_path=%s",
            fd_count if fd_count >= 0 else "?",
            wal_bytes / (1024 * 1024),
            self.db_path,
        )

    def _use_ibkr_for_equity(self) -> bool:
        """CLI source override first; auto mode prefers IBKR without profile coupling."""
        if self.equity_data_source == "ibkr":
            return True
        if self.equity_data_source == "alpaca":
            return False
        return True

    def _context_refresh_is_fresh(
        self,
        *,
        profile_id: str,
        context_source_id: str,
        freshness_within_s: float,
    ) -> bool:
        if freshness_within_s <= 0:
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=30) as con:
                row = con.execute(
                    """
                    SELECT finished_ts_utc, source_status, quotes_written, account_written
                    FROM vanguard_context_ingest_runs
                    WHERE profile_id = ?
                      AND context_source_id = ?
                    ORDER BY finished_ts_utc DESC
                    LIMIT 1
                    """,
                    (profile_id, context_source_id),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.debug(
                "[CTX] refresh freshness check failed profile=%s source=%s err=%s",
                profile_id,
                context_source_id,
                exc,
            )
            return False
        if not row:
            return False
        finished_ts_utc, source_status, quotes_written, account_written = row
        if str(source_status or "").upper() not in {"OK", "PARTIAL"}:
            return False
        if int(quotes_written or 0) <= 0 or int(account_written or 0) <= 0:
            return False
        try:
            finished_dt = datetime.fromisoformat(str(finished_ts_utc).replace("Z", "+00:00"))
        except ValueError:
            return False
        age_s = max(0.0, (datetime.now(timezone.utc) - finished_dt.astimezone(timezone.utc)).total_seconds())
        return age_s <= freshness_within_s

    def _refresh_active_context_snapshots(self) -> dict[str, Any]:
        """Refresh broker-backed context truth for active profiles before V6."""
        runtime_cfg = self._runtime_config or get_runtime_config()
        refresh_cfg = ((runtime_cfg.get("runtime") or {}).get("context_refresh_before_v6") or {})
        if not bool(refresh_cfg.get("enabled", False)):
            return {"enabled": False, "runs": []}

        context_sources = runtime_cfg.get("context_sources") or {}
        subscribe_sleep_sec = float(refresh_cfg.get("subscribe_sleep_sec") or 0.35)
        freshness_within_s = float(refresh_cfg.get("skip_if_fresh_within_s") or 45.0)
        report_dir = Path(str(refresh_cfg.get("coverage_report_dir") or "/tmp/vanguard_context_refresh"))
        _ensure_dwx_context_schema(self.db_path)
        runs: list[dict[str, Any]] = []

        for profile in self._accounts or []:
            profile_id = str(profile.get("id") or "")
            source_id = str(profile.get("context_source_id") or "")
            if not profile_id or not source_id:
                continue
            source_cfg = dict(context_sources.get(source_id) or {})
            if not source_cfg or not source_cfg.get("enabled", False):
                continue
            if self._context_refresh_is_fresh(
                profile_id=profile_id,
                context_source_id=source_id,
                freshness_within_s=freshness_within_s,
            ):
                runs.append({
                    "profile_id": profile_id,
                    "context_source_id": source_id,
                    "status": "SKIPPED_FRESH",
                    "freshness_within_s": freshness_within_s,
                })
                logger.info(
                    "[CTX] refresh skipped profile=%s source=%s reason=fresh_daemon_snapshot within=%.1fs",
                    profile_id,
                    source_id,
                    freshness_within_s,
                )
                continue
            source_type = str(source_cfg.get("source_type") or "").lower()
            if source_type not in {"dwx_mt5", "metaapi_mt5"}:
                continue
            source_cfg["_source_id"] = source_id

            symbols, asset_class_by_symbol, _ = _dwx_load_symbols_for_context_source(
                runtime_cfg,
                source_cfg,
                profile_id,
            )
            symbols = [
                symbol
                for symbol in symbols
                if self._is_polling_asset_enabled(asset_class_by_symbol.get(symbol, ""))
            ]
            if not symbols:
                continue

            try:
                default_suffix = _dwx_resolve_default_suffix(source_cfg, None)
                suffix_map = _dwx_resolve_symbol_suffix_map(
                    source_cfg,
                    symbols,
                    asset_class_by_symbol,
                    default_suffix,
                )
                if source_type == "dwx_mt5":
                    source_path = str(source_cfg.get("dwx_files_path") or "")
                    adapter = None
                    owns_adapter = False
                    if (
                        self._mt5_adapter is not None
                        and self._mt5_adapter.is_connected()
                        and str(Path(self._mt5_adapter.dwx_files_path).expanduser()) == str(Path(source_path).expanduser())
                    ):
                        adapter = self._mt5_adapter
                    else:
                        try:
                            adapter = MT5DWXAdapter(
                                dwx_files_path=source_path,
                                db_path=self.db_path,
                                symbol_suffix=str(source_cfg.get("symbol_suffix", ".x") or ""),
                            )
                            if not adapter.connect():
                                logger.warning("[CTX] refresh connect failed profile=%s source=%s", profile_id, source_id)
                                continue
                            owns_adapter = True
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[CTX] refresh init failed profile=%s source=%s err=%s", profile_id, source_id, exc)
                            continue
                    try:
                        coverage = _dwx_collect_context_once(
                            adapter=adapter,
                            db_path=self.db_path,
                            profile_id=profile_id,
                            context_source_id=source_id,
                            source_cfg=source_cfg,
                            symbols=symbols,
                            asset_class_by_symbol=asset_class_by_symbol,
                            suffix_map=suffix_map,
                            subscribe_sleep_sec=subscribe_sleep_sec,
                            coverage_report=report_dir / f"{profile_id}_{source_id}.json",
                        )
                    finally:
                        if owns_adapter and adapter is not None:
                            try:
                                adapter.disconnect()
                            except Exception:  # noqa: BLE001
                                pass
                else:
                    try:
                        metaapi_adapter = MetaApiContextAdapter(source_cfg=source_cfg, profile_id=profile_id)
                        coverage = _metaapi_collect_context_once(
                            adapter=metaapi_adapter,
                            db_path=self.db_path,
                            profile_id=profile_id,
                            context_source_id=source_id,
                            source_cfg=source_cfg,
                            symbols=symbols,
                            asset_class_by_symbol=asset_class_by_symbol,
                            suffix_map=suffix_map,
                            keep_subscription=bool(source_cfg.get("keep_subscription", True)),
                        )
                    except (MetaApiUnavailable, ValueError) as exc:
                        logger.warning("[CTX] refresh init failed profile=%s source=%s err=%s", profile_id, source_id, exc)
                        continue
                runs.append({
                    "profile_id": profile_id,
                    "context_source_id": source_id,
                    "quotes_written": coverage.get("quotes_written"),
                    "symbols_requested": coverage.get("symbols_requested"),
                    "account_written": coverage.get("account_written"),
                    "source_status": coverage.get("source_status"),
                })
                logger.info(
                    "[CTX] refreshed profile=%s source=%s quotes=%s/%s account=%s status=%s",
                    profile_id,
                    source_id,
                    coverage.get("quotes_written"),
                    coverage.get("symbols_requested"),
                    coverage.get("account_written"),
                    coverage.get("source_status"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[CTX] refresh failed profile=%s source=%s err=%s", profile_id, source_id, exc)

        return {"enabled": True, "runs": runs}

    def _finalize_early_cycle_exit(
        self,
        *,
        cycle_ts: str,
        result: dict[str, Any],
        stage_timings: dict[str, float],
        cycle_start: float,
        refresh_context: bool,
    ) -> dict[str, Any]:
        """Finalize a healthy early exit and optionally refresh context/state first."""
        if refresh_context:
            try:
                stage_start = time.time()
                context_refresh = self._refresh_active_context_snapshots()
                stage_timings["context_refresh"] = time.time() - stage_start
                result["context_refresh"] = context_refresh
            except Exception as exc:
                logger.error("Context refresh failed: %s", exc, exc_info=True)
                result["context_refresh_error"] = str(exc)
                result["status"] = "context_refresh_error"
                raise
        self._attach_mt5_coverage_details(result)
        self._log_context_state_diagnostics(cycle_ts)
        try:
            _refresh_cycle_audit(self.db_path, str(cycle_ts or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cycle_audit] refresh failed for early-exit cycle=%s: %s", cycle_ts, exc)
        result["stage_timings"] = stage_timings
        self._record_cycle_trace_result(result)
        self._check_cycle_lag(cycle_start)
        return result

    def _is_polling_asset_enabled(
        self,
        asset_class: str,
        et: Optional[datetime] = None,
    ) -> bool:
        """Return True when an asset class should be polled this cycle."""
        normalized = str(asset_class or "").lower().strip()
        if not normalized:
            return False
        if self._force_assets and normalized not in self._force_assets:
            return False
        return _is_asset_session_active(normalized, et or self._now_et())

    def _is_live_routing_asset_allowed(self, asset_class: str, account_id: str = "") -> bool:
        """Return True when an asset class may route live orders."""
        normalized = str(asset_class or "").lower().strip()
        if not normalized:
            return False
        runtime_cfg = self._runtime_config or {}
        exec_cfg = runtime_cfg.get("execution") or {}
        profile_cfg = self._get_account_profile(account_id) or {}
        profile_allowed = profile_cfg.get("live_allowed_asset_classes")
        if profile_allowed is None:
            profile_allowed = exec_cfg.get("live_allowed_asset_classes")
        if not profile_allowed:
            return True
        allowed = {str(asset).strip().lower() for asset in profile_allowed if str(asset).strip()}
        return normalized in allowed

    def _init_telegram(self) -> Optional[TelegramAlerts]:
        tg      = self._exec_cfg.get("telegram", {})
        token   = tg.get("bot_token", "")
        chat_id = tg.get("chat_id", "")
        enabled = tg.get("enabled", False)
        if not token or not chat_id:
            return None
        return TelegramAlerts(bot_token=token, chat_id=chat_id, enabled=enabled)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def run_session(self) -> None:
        """
        Top-level entry point. Blocks until session ends or shutdown is requested.
        """
        signal.signal(signal.SIGINT,  self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self._startup()
        try:
            self._main_loop()
        finally:
            self._wind_down()

    def _handle_signal(self, signum: int, frame: Any) -> None:
        logger.info("Signal %s received — requesting shutdown", signum)
        self._shutdown_requested = True

    def _startup(self) -> None:
        """Init DB tables, record session start, send Telegram alert."""
        boot_t0 = time.time()
        et = self._now_et()
        self._session_date    = et.strftime("%Y-%m-%d")
        self._session_start_ts = self._now_utc()
        self._running         = True

        if not self._is_test_execution_mode():
            self._checkpoint_db_wal()

            t = time.time()
            _ensure_execution_log(self.db_path)
            logger.info("[BOOT] ensure_execution_log took %.2fs", time.time() - t)
            t = time.time()
            _ensure_execution_attempts(self.db_path)
            logger.info("[BOOT] ensure_execution_attempts took %.2fs", time.time() - t)

            t = time.time()
            self._ensure_session_log()
            logger.info("[BOOT] ensure_session_log took %.2fs", time.time() - t)

        t = time.time()
        self._validate_profiles()
        logger.info("[BOOT] validate_profiles took %.2fs", time.time() - t)

        if not self._is_test_execution_mode():
            t = time.time()
            self._materialize_universe_boot()
            logger.info("[BOOT] materialize_universe took %.2fs", time.time() - t)

        t = time.time()
        self._init_stage1_adapters()
        logger.info("[BOOT] init_stage1_adapters took %.2fs", time.time() - t)

        t = time.time()
        self._verify_data_sources()
        logger.info("[BOOT] verify_data_sources took %.2fs", time.time() - t)

        if not self._is_test_execution_mode():
            t = time.time()
            self._upsert_session_log(status="running")
            logger.info("[BOOT] upsert_session_log took %.2fs", time.time() - t)

        msg = (
            f"🚀 <b>[{self._telegram_env_label()}] VANGUARD SESSION START</b>\n"
            f"Date:     {self._session_date}\n"
            f"Mode:     {self.execution_mode.upper()}\n"
            f"Window:   {self.session_start}–{self.session_end} ET\n"
            f"Accounts: {', '.join(str(a.get('name') or a.get('id') or '') for a in self._accounts) or 'none'}\n"
            f"Tactical bans: {self._tactical_bans_summary('forex')}\n"
            f"Routing limit: {self._throughput_mode_summary()}"
        )
        logger.info("Session start: %s mode=%s", self._session_date, self.execution_mode)
        logger.info("[BOOT] startup total took %.2fs", time.time() - boot_t0)
        self._send_telegram(msg, message_type="LIFECYCLE")
        self._send_telegram(
            self._build_live_policy_contract_telegram(asset_class="forex"),
            message_type="POLICY_DIGEST",
        )

    def _main_loop(self) -> None:
        """5-minute cycle loop. Runs until session ends, shutdown, or too many failures."""
        first_cycle = True
        while not self._shutdown_requested:
            et = self._now_et()
            result: dict[str, Any] | None = None
            shadow_summary: dict[str, Any] | None = None

            if not self.is_within_session(et):
                if self._was_in_session_before(et):
                    logger.info("Session window ended — exiting main loop")
                    break
                logger.info("Waiting for session window (%s ET)…", self.session_start)
                time.sleep(30)
                continue

            if first_cycle and self.cycle_interval_seconds == CYCLE_MINUTES * 60 and self.cycle_offset_seconds > 0:
                logger.info(
                    "Aligning first cycle to offset=%ss within the 5-minute cadence",
                    self.cycle_offset_seconds,
                )
                self._wait_for_next_bar()
            try:
                result = self.run_cycle()
                self._total_cycles += 1
                first_cycle = False

                if result.get("status") == "ok":
                    self._consecutive_failures = 0
                    if result.get("approved", 0) > 0:
                        self._execute_approved(result.get("cycle_ts"))
                    else:
                        live_shortlist_msg = self._build_live_trade_shortlist_telegram(
                            str(result.get("cycle_ts") or ""),
                            {
                                "live": {
                                    "policy_id": str(
                                        (
                                            self._load_execution_policy_runner_config("forex") or {}
                                        ).get("live_policy_id")
                                        or ""
                                    ),
                                    "approved": 0,
                                    "executed": 0,
                                    "filled": 0,
                                    "failed": 0,
                                    "forward_tracked": 0,
                                    "rejected": 0,
                                }
                            },
                        )
                        if live_shortlist_msg:
                            self._send_telegram(live_shortlist_msg, message_type="SHORTLIST")

                    # Shadow Eval V2 — observational only. Runs AFTER
                    # _execute_approved so decision_log rows for the cycle
                    # exist. ZERO effect on live routing. Any failure is
                    # swallowed so a broken evaluator never affects trading.
                    try:
                        from vanguard.analytics.policy_shadow_eval import evaluate_and_log_cycle_from_context
                        _shadow_summary = evaluate_and_log_cycle_from_context(
                            db_path=self.db_path,
                            runtime_config=self._runtime_config or {},
                            cycle_ts_utc=result.get("cycle_ts") or "",
                            profile_id="ftmo_demo_100k",
                            asset_class="forex",
                        )
                        shadow_summary = dict(_shadow_summary or {})
                        if _shadow_summary.get("enabled"):
                            logger.info(
                                "[shadow_eval] cycle=%s rows=%d cands=%d run=%s",
                                result.get("cycle_ts"),
                                _shadow_summary.get("rows_written", 0),
                                _shadow_summary.get("n_candidates", 0),
                                _shadow_summary.get("shadow_run_id", "?"),
                            )
                    except Exception as _shadow_exc:   # noqa: BLE001
                        logger.warning("[shadow_eval] skipped: %s", _shadow_exc)

                    if not self._is_test_execution_mode():
                        # BUG 19: checkpoint every cycle (not just when WAL >256MB)
                        # so WAL doesn't grow unbounded and FD pressure stays flat.
                        self._checkpoint_db_wal(min_wal_bytes=0)
                    try:
                        _refresh_cycle_audit(self.db_path, str(result.get("cycle_ts") or ""))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[cycle_audit] refresh failed cycle=%s: %s", result.get("cycle_ts"), exc)
                    self._upsert_orchestrator_service_state(
                        status="OK",
                        cycle_ts_utc=str(result.get("cycle_ts") or ""),
                    )
                else:
                    logger.warning("Cycle non-ok status: %s", result.get("status"))
                    self._upsert_orchestrator_service_state(
                        status=str(result.get("status") or "WARN").upper(),
                        cycle_ts_utc=str(result.get("cycle_ts") or ""),
                    )

            except Exception as exc:
                self._failed_cycles        += 1
                self._consecutive_failures += 1
                self._record_cycle_trace_error(exc)
                self._upsert_orchestrator_service_state(status="ERROR", error=exc)
                logger.error("Cycle error: %s", exc, exc_info=True)
                self._send_telegram(
                    f"⚠️ <b>CYCLE ERROR</b>\nError: {exc}\n"
                    f"Consecutive failures: {self._consecutive_failures}/{MAX_CONSECUTIVE_FAILS}",
                    message_type="ERROR",
                )
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILS:
                    logger.error(
                        "Aborting session — %d consecutive failures", MAX_CONSECUTIVE_FAILS
                    )
                    self._send_telegram(
                        f"🚨 <b>SESSION ABORTED</b>\n"
                        f"Reason: {MAX_CONSECUTIVE_FAILS} consecutive cycle failures",
                        message_type="ERROR",
                    )
                    self._upsert_session_log(status="aborted")
                    return
            finally:
                if result is not None:
                    self._record_cycle_trace_result(result)
                    if shadow_summary:
                        self._record_cycle_trace_shadow(result.get("cycle_ts") or "", shadow_summary)
                    self._flush_cycle_trace(status=str(result.get("status") or "unknown"))
                elif self._active_cycle_trace:
                    self._flush_cycle_trace(status="exception")

            self._log_loop_db_health()

            self._wait_for_next_bar()

    def _was_in_session_before(self, et: datetime) -> bool:
        """True if the session window start has already passed today."""
        start_h, start_m = _parse_hhmm(self.session_start)
        session_open = et.replace(
            hour=start_h, minute=start_m, second=0, microsecond=0
        )
        return et > session_open

    def _wind_down(self) -> None:
        """EOD flatten intraday accounts, write session-end log, send Telegram."""
        et = self._now_et()

        if self._alpaca_adapter:
            try:
                asyncio.run(self._alpaca_adapter.stop())
            except Exception:
                self._alpaca_adapter.running = False
        if self._alpaca_task and self._alpaca_task.is_alive():
            self._alpaca_task.join(timeout=5)
        if self._ibkr_adapter:
            self._ibkr_adapter.disconnect()

        for account in self._accounts:
            if account.get("must_close_eod"):
                self._eod_flatten(account, et)

        session_end_ts = self._now_utc()
        elapsed = ""
        if self._session_start_ts:
            secs    = int((session_end_ts - self._session_start_ts).total_seconds())
            elapsed = f"{secs // 3600}h {(secs % 3600) // 60}m"

        end_status = "shutdown" if self._shutdown_requested else "complete"
        if not self._is_test_execution_mode():
            self._upsert_session_log(
                status=end_status, session_end=iso_utc(session_end_ts)
            )

        msg = (
            f"🏁 <b>[{self._telegram_env_label()}] VANGUARD SESSION END</b>\n"
            f"Date:             {self._session_date}\n"
            f"Mode:             {self.execution_mode.upper()}\n"
            f"Total cycles:     {self._total_cycles}\n"
            f"Failed cycles:    {self._failed_cycles}\n"
            f"Trades placed:    {self._trades_placed}\n"
            f"Forward tracked:  {self._forward_tracked}\n"
            f"Elapsed:          {elapsed}"
        )
        logger.info(
            "Session end: cycles=%d, failed=%d, trades=%d",
            self._total_cycles, self._failed_cycles, self._trades_placed,
        )
        if not self._is_test_execution_mode():
            self._checkpoint_db_wal()
        self._send_telegram(msg, message_type="LIFECYCLE")

    def _release_v1_market_data_adapters(self, *, release_mt5: bool = True) -> None:
        """Release long-lived V1 adapters when a caller only needs one polling cycle."""
        if release_mt5 and self._mt5_adapter is not None:
            try:
                self._mt5_adapter.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[V1] Could not disconnect MT5 DWX adapter cleanly: %s", exc)
            finally:
                self._mt5_adapter = None

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def is_within_session(self, et: Optional[datetime] = None) -> bool:
        """
        True if at least one asset-class market session is currently active,
        or an explicit --force-assets override is present.
        """
        if et is None:
            et = self._now_et()
        return any(
            self._is_polling_asset_enabled(asset_class, et=et)
            for asset_class in ("equity", "index", "forex", "crypto")
        )

    def _wait_for_next_bar(self) -> None:
        """
        Sleep until the next scheduled cycle.

        Default behavior aligns to the next 5-minute bar boundary + buffer.
        When --interval overrides the cadence, sleep for that many seconds.
        Polls every 5 seconds to allow clean shutdown.
        """
        utc_now  = self._now_utc()
        if self.cycle_interval_seconds == CYCLE_MINUTES * 60:
            bar_open = round_down_5m(utc_now)
            next_bar = bar_open + timedelta(minutes=CYCLE_MINUTES)
            target   = next_bar + timedelta(seconds=CYCLE_BUFFER_SECONDS + self.cycle_offset_seconds)
        else:
            next_bar = utc_now + timedelta(seconds=self.cycle_interval_seconds)
            target = next_bar

        sleep_secs = (target - utc_now).total_seconds()
        if sleep_secs < 0:
            sleep_secs = CYCLE_BUFFER_SECONDS

        logger.info(
            "Next cycle in %.0fs (bar=%s UTC, offset=%ss)",
            sleep_secs, next_bar.strftime("%H:%M"),
            self.cycle_offset_seconds if self.cycle_interval_seconds == CYCLE_MINUTES * 60 else 0,
        )

        deadline = time.monotonic() + sleep_secs
        while time.monotonic() < deadline and not self._shutdown_requested:
            time.sleep(min(5.0, deadline - time.monotonic()))

    # ------------------------------------------------------------------
    # Pipeline cycle
    # ------------------------------------------------------------------

    def _check_config_reload(self) -> None:
        """
        Phase 5: Hot-reload runtime config if the file changed or the reload flag was written.
        - On success: updates self._runtime_config and self._config_mtime, logs new config_version.
        - On failure: keeps old config, logs error, sends Telegram alert.
        Never raises — orchestrator must continue even on reload failure.
        """
        try:
            flag_triggered = self._phase5_reload_flag.exists()
            if flag_triggered:
                try:
                    self._phase5_reload_flag.unlink()
                except OSError:
                    pass

            current_mtime = self._config_path.stat().st_mtime if self._config_path.exists() else 0.0
            if not flag_triggered and current_mtime == self._config_mtime:
                return  # nothing changed

            logger.info("[config_reload] config file changed or reload flag set — reloading")
            new_cfg = load_runtime_config(self._config_path, refresh=True)
            self._runtime_config = new_cfg
            self._accounts = _load_accounts(self.db_path)
            self._config_mtime = current_mtime
            new_version = (new_cfg or {}).get("config_version", "unknown")
            logger.info(
                "[config_reload] reload OK — config_version=%s active_profiles=%s",
                new_version,
                [str(account.get("id") or "") for account in (self._accounts or [])],
            )
        except Exception as exc:
            logger.error(
                "[config_reload] reload FAILED — keeping previous config. Error: %s", exc
            )
            try:
                if hasattr(self, "_telegram") and self._telegram:
                    self._telegram.send(f"CONFIG_RELOAD_FAILED: {exc}")
            except Exception:
                pass

    def run_cycle(self, dry_run: Optional[bool] = None) -> dict:
        """
        Run one pipeline cycle: V1 → V3 → V5 → V6.

        Returns dict with status and per-stage metrics.
        Raises on hard V5/V6 failures (triggers consecutive failure counter).
        """
        # Phase 5: check for config file change or reload flag at cycle start
        self._check_config_reload()

        cycle_start = time.time()

        if dry_run is None:
            dry_run = self.dry_run

        cycle_ts = self._force_cycle_ts if self._force_cycle_ts else iso_utc(self._now_utc())
        self._start_cycle_trace(cycle_ts)
        self._refresh_forward_checkpoints()
        self._refresh_signal_forward_checkpoints()
        logger.info(
            "=== Cycle start: %s%s ===",
            utc_to_et(parse_utc(cycle_ts)).strftime("%Y-%m-%d %H:%M:%S ET"),
            " [FORCED]" if self._force_cycle_ts else "",
        )
        result: dict = {"status": "ok", "cycle_ts": cycle_ts}
        stage_timings: dict[str, float] = {}

        # ── Phase 2a: Resolve active universe ─────────────────────────
        in_scope_symbols = self._resolve_runtime_universe_for_cycle(cycle_ts)

        # ── V1: Stage 1 multi-source bar freshness ────────────────────
        result["v1_cache_used"] = False
        v1_cache_cfg = self._get_v1_precompute_config()
        v1_state = self._load_v1_precompute_state() if bool(v1_cache_cfg.get("enabled")) else None
        if self._is_v1_precompute_state_fresh(v1_state):
            details = (v1_state or {}).get("details") or {}
            v1_result = dict(details.get("v1_result") or {})
            result.update(v1_result)
            result["v1_cache_used"] = True
            result["v1_cache_cycle_ts_utc"] = details.get("cycle_ts_utc")
            stage_timings["v1"] = 0.0
            logger.info(
                "V1 cache hit: cycle_ts=%s source_health=%d",
                details.get("cycle_ts_utc"),
                v1_result.get("v1_source_health", 0),
            )
        else:
            try:
                stage_start = time.time()
                v1_result = self._run_v1(in_scope_symbols=in_scope_symbols)
                stage_timings["v1"] = time.time() - stage_start
                result.update(v1_result)
                logger.info(
                    "V1 complete: alpaca_bars=%d, td_bars=%d, bars_5m=%d, bars_1h=%d, source_health=%d, elapsed=%.2fs",
                    v1_result.get("v1_alpaca_bars", 0),
                    v1_result.get("v1_td_bars", 0),
                    v1_result.get("v1_bars_5m", 0),
                    v1_result.get("v1_bars_1h", 0),
                    v1_result.get("v1_source_health", 0),
                    stage_timings["v1"],
                )
            except Exception as exc:
                logger.error("V1 data step failed: %s", exc, exc_info=True)
                result["v1_error"] = str(exc)

        # ── V2: Health / survivors ─────────────────────────────────────
        try:
            from stages.vanguard_prefilter import run as v2_run
            stage_start = time.time()
            # Build universe label from Phase 2a resolved universe (Bug 5 fix)
            _ru = getattr(self, "_current_resolved_universe", None)
            if _ru is not None:
                _scopes = {
                    str(p.get("instrument_scope") or "")
                    for p in (self._runtime_config or {}).get("profiles", [])
                    if str(p.get("is_active", "")).strip().lower() in {"1", "true", "yes", "on"}
                }
                _scope_label = next(iter(_scopes), "runtime_universe") if _scopes else "runtime_universe"
                _v2_universe_label = f"{_scope_label}({_ru.mode})"
            else:
                _v2_universe_label = "runtime_universe"
            v2_result = v2_run(dry_run=dry_run, cycle_ts=cycle_ts,
                               in_scope_symbols=in_scope_symbols,
                               universe_name=_v2_universe_label)
            stage_timings["v2"] = time.time() - stage_start
            # Index/commodity/metal/energy symbols allowed through as STALE if they have bars.
            # Core equity/forex/crypto require ACTIVE status.
            _STALE_OK_CLASSES = {"index", "commodity", "metal", "energy"}
            survivors = [
                row for row in v2_result
                if row.get("status") == "ACTIVE"
                or (
                    row.get("status") == "STALE"
                    and str(row.get("asset_class") or "").lower() in _STALE_OK_CLASSES
                    and int(row.get("bars_available") or 0) > 0
                )
            ]
            result["v2_rows"] = len(v2_result)
            result["v2_survivors"] = len(survivors)
            logger.info(
                "V2 complete: %d survivors out of %d rows, elapsed=%.2fs",
                len(survivors), len(v2_result), stage_timings["v2"],
            )
            if not survivors:
                logger.warning("V2 produced 0 survivors — skipping V3-V6")
                result["status"] = "no_survivors"
                result["approved"] = 0
                return self._finalize_early_cycle_exit(
                    cycle_ts=cycle_ts,
                    result=result,
                    stage_timings=stage_timings,
                    cycle_start=cycle_start,
                    refresh_context=True,
                )
        except Exception as exc:
            logger.error("V2 prefilter failed: %s", exc, exc_info=True)
            result["v2_error"] = str(exc)
            result["status"] = "v2_error"
            raise

        # ── V3: Factor Engine ──────────────────────────────────────────
        # Delegates to pipeline/stages/v2_feature_matrix.py per STAGE_CONTRACTS.md.
        try:
            from pipeline.stages.v2_feature_matrix import FeatureMatrixStage
            from pipeline.contracts import run_stage_with_audit
            stage_start = time.time()
            _stage = FeatureMatrixStage()
            _stage_cfg = {"feature_matrix": {"dry_run": dry_run, "survivors": survivors}}
            with sqlite3.connect(self.db_path, timeout=30) as _audit_con:
                _stage_result = run_stage_with_audit(
                    _stage, cycle_ts, _audit_con, cfg=_stage_cfg,
                )
            stage_timings["v3"] = time.time() - stage_start
            v3_symbols_count = _stage_result.rows_written
            result["v3_symbols"] = v3_symbols_count
            result["v3_stage_status"] = _stage_result.status
            logger.info(
                "V3 complete: %d symbols, elapsed=%.2fs (via pipeline.stages.v2_feature_matrix, stage_status=%s)",
                v3_symbols_count, stage_timings["v3"], _stage_result.status,
            )
            self._log_chain_integrity(survivors, [{}] * v3_symbols_count, cycle_ts)
        except Exception as exc:
            logger.error("V3 factor engine failed: %s", exc, exc_info=True)
            result["v3_error"]   = str(exc)
            result["v3_symbols"] = 0
            result["status"] = "v3_error"
            raise

        # ── V4B: Regressor scorer ─────────────────────────────────────
        # Delegates to pipeline/stages/v3_model_score.py per STAGE_CONTRACTS.md.
        try:
            from pipeline.stages.v3_model_score import ModelScoreStage
            from pipeline.contracts import run_stage_with_audit
            stage_start = time.time()
            _stage = ModelScoreStage()
            _stage_cfg = {"model_score": {"dry_run": dry_run}}
            with sqlite3.connect(self.db_path, timeout=30) as _audit_con:
                _stage_result = run_stage_with_audit(
                    _stage, cycle_ts, _audit_con, cfg=_stage_cfg,
                )
            stage_timings["v4b"] = time.time() - stage_start
            v4b_status = _stage_result.observations.get("scorer_status", "unknown")
            v4b_rows = _stage_result.rows_written
            result["v4b_status"] = v4b_status
            result["v4b_rows"] = v4b_rows
            result["v4b_stage_status"] = _stage_result.status
            result["v4b_stage_reason"] = _stage_result.reason
            logger.info(
                "V4B complete: status=%s, rows=%d, elapsed=%.2fs (via pipeline.stages.v3_model_score, stage_status=%s)",
                v4b_status, v4b_rows, stage_timings["v4b"], _stage_result.status,
            )
            if v4b_rows == 0 or v4b_status in ("no_features", "no_models"):
                logger.warning("V4B produced 0 predictions — skipping V5/V6")
                result["status"] = "no_predictions"
                result["approved"] = 0
                return self._finalize_early_cycle_exit(
                    cycle_ts=cycle_ts,
                    result=result,
                    stage_timings=stage_timings,
                    cycle_start=cycle_start,
                    refresh_context=True,
                )
        except Exception as exc:
            logger.error("V4B scorer failed: %s", exc, exc_info=True)
            result["v4b_error"] = str(exc)
            result["status"] = "v4b_error"
            raise

        try:
            stage_start = time.time()
            context_refresh = self._refresh_active_context_snapshots()
            stage_timings["context_refresh"] = time.time() - stage_start
            result["context_refresh"] = context_refresh
        except Exception as exc:
            logger.error("Context refresh failed: %s", exc, exc_info=True)
            result["context_refresh_error"] = str(exc)
            result["status"] = "context_refresh_error"
            raise

        # ── V5.2: Selection (delegated to pipeline) ───────────────────
        try:
            v5_cfg = (self._runtime_config or {}).get("v5") or {}
            active_profile_ids = [str(account.get("id") or "") for account in (self._accounts or []) if str(account.get("id") or "").strip()]
            selection_engine = str(v5_cfg.get("selection_engine") or "simple_legacy")

            from pipeline.stages.v5_selection import SelectionStage
            from pipeline.stages.v6_risk_filters import RiskFiltersStage
            from pipeline.contracts import run_stage_with_audit

            stage_start = time.time()
            _selection_stage = SelectionStage()
            _selection_cfg = {"selection": {
                "dry_run": dry_run,
                "selection_engine": selection_engine,
                "profile_ids": active_profile_ids,
                "runtime_cfg": self._runtime_config,
            }}
            with sqlite3.connect(self.db_path, timeout=30) as _audit_con:
                _selection_stage_result = run_stage_with_audit(
                    _selection_stage, cycle_ts, _audit_con, cfg=_selection_cfg,
                )
            stage_timings["v5_2"] = time.time() - stage_start
            v5_status = _selection_stage_result.observations.get("selection_status", "unknown")
            v5_rows = int(_selection_stage_result.observations.get("selection_rows") or 0)
            result["v5_status"] = v5_status
            result["v5_candidates"] = v5_rows
            result["v5_stage_status"] = _selection_stage_result.status
            logger.info(
                "V5.2 complete: sel_status=%s sel_rows=%d elapsed=%.2fs (via pipeline.stages.v5_selection, stage_status=%s)",
                v5_status, v5_rows, stage_timings["v5_2"], _selection_stage_result.status,
            )

            if _selection_stage_result.status == "SKIPPED":
                logger.warning("V5 produced 0 candidates — skipping downstream")
                result["status"] = "no_candidates"
                result["approved"] = 0
                return self._finalize_early_cycle_exit(
                    cycle_ts=cycle_ts,
                    result=result,
                    stage_timings=stage_timings,
                    cycle_start=cycle_start,
                    refresh_context=True,
                )

            # ── V6: Risk Filters (delegated to pipeline) ──────────────
            stage_start = time.time()
            _risk_stage = RiskFiltersStage()
            _risk_cfg = {"risk_filters": {
                "dry_run": dry_run,
                "runtime_cfg": self._runtime_config,
            }}
            with sqlite3.connect(self.db_path, timeout=30) as _audit_con:
                _risk_stage_result = run_stage_with_audit(
                    _risk_stage, cycle_ts, _audit_con, cfg=_risk_cfg,
                )
            stage_timings["v6"] = time.time() - stage_start
            v6_status = _risk_stage_result.observations.get("risk_filters_status", "unknown")
            v6_rows = int(_risk_stage_result.observations.get("risk_filters_rows") or 0)
            result["v6_status"] = v6_status
            result["v6_rows"] = v6_rows
            result["v6_stage_status"] = _risk_stage_result.status
            logger.info(
                "V6 complete: risk_status=%s risk_rows=%d elapsed=%.2fs (via pipeline.stages.v6_risk_filters, stage_status=%s)",
                v6_status, v6_rows, stage_timings["v6"], _risk_stage_result.status,
            )

        except Exception as exc:
            logger.error("V5.2/V6 delegated stage failed: %s", exc, exc_info=True)
            result["v5_v6_error"] = str(exc)
            result["status"] = "v5_v6_error"
            raise

        try:
            self._attach_mt5_coverage_details(result)
            self._log_context_state_diagnostics(cycle_ts)
        except Exception as exc:
            logger.error("Context refresh failed: %s", exc, exc_info=True)
            result["context_refresh_error"] = str(exc)
            result["status"] = "context_refresh_error"
            raise

        self._log_v6_diagnostics(cycle_ts)
        approved = _get_approved_rows(self.db_path, cycle_ts)
        if not approved:
            result["decision_log_emit"] = self._emit_nonapproved_cycle_decisions(cycle_ts)
        result["approved"] = len(approved)
        logger.info("Approved for execution: %d", len(approved))

        result["stage_timings"] = stage_timings
        self._record_cycle_trace_result(result)
        logger.info("[V7] Stage timings: %s", {k: round(v, 2) for k, v in stage_timings.items()})
        self._check_cycle_lag(cycle_start)
        return result

    def _log_chain_integrity(
        self,
        v2_survivors: list[dict],
        v3_features: list[dict],
        cycle_ts: str,
    ) -> None:
        """Log chain coverage and survivor composition for operator visibility."""
        survivor_count = len(v2_survivors)
        feature_count = len(v3_features)
        coverage = (feature_count / max(survivor_count, 1)) * 100.0
        by_class = Counter(row.get("asset_class") or "unknown" for row in v2_survivors)
        logger.info(
            "[chain] cycle=%s | V2 survivors=%d | V3 features=%d | coverage=%.0f%% | by_class=%s",
            cycle_ts,
            survivor_count,
            feature_count,
            coverage,
            dict(by_class),
        )

    def _materialize_universe(self, force: bool = False) -> int:
        """Refresh the canonical Stage 1 universe on intra-day refresh times."""
        et = self._now_et()
        refresh_key = et.strftime("%Y-%m-%d %H:%M")
        if not force and et.strftime("%H:%M") not in UNIVERSE_REFRESH_TIMES:
            return 0
        if not force and self._last_universe_refresh == refresh_key:
            return 0
        written = materialize_universe_members(self._vg_db, now_utc_str=iso_utc(self._now_utc()))
        self._last_universe_refresh = refresh_key
        return written

    def _materialize_universe_boot(self) -> int:
        """Boot-time universe materialization with equity session and enforce-mode guards.

        Bug 9 fix:
        - Skip Alpaca materialization entirely when equity is not in the
          resolved universe's active asset classes (e.g. weekend / after-hours).
        - In enforce mode, only materialize the 15 GFT equity symbols instead
          of the full 371-row Alpaca universe.
        """
        now_utc_str = iso_utc(self._now_utc())
        equity_in_session = self._is_polling_asset_enabled("equity") or self._is_polling_asset_enabled("index")
        rt = self._runtime_config or {}
        universe_mode = str((rt.get("runtime") or {}).get("resolved_universe_mode", "observe"))

        if not equity_in_session:
            logger.info(
                "[BOOT] Skipping Alpaca materialization — equity not in session (weekend/after-hours)"
            )
            # Still write non-equity rows (forex/crypto) from TwelveData config
            written = materialize_universe_members(
                self._vg_db,
                now_utc_str=now_utc_str,
                skip_equity=True,
            )
        elif universe_mode == "enforce":
            # In enforce mode only materialize the configured active-profile equity symbols.
            runtime_universes = (rt.get("universes") or {})
            active_scopes = {
                str(profile.get("instrument_scope") or "")
                for profile in (rt.get("profiles") or [])
                if str(profile.get("is_active", "")).strip().lower() in {"1", "true", "yes", "on"}
            }
            configured_equity = sorted(
                {
                    str(symbol).upper()
                    for scope_id in active_scopes
                    for symbol in ((((runtime_universes.get(scope_id) or {}).get("symbols") or {}).get("equity") or []))
                    if str(symbol).strip()
                }
            )
            if configured_equity:
                logger.info(
                    "[BOOT] enforce mode — materializing %d configured equity symbols (not full Alpaca)",
                    len(configured_equity),
                )
                written = materialize_universe_members(
                    self._vg_db,
                    now_utc_str=now_utc_str,
                    equity_symbols_override=configured_equity,
                )
            else:
                logger.info(
                    "[BOOT] enforce mode — no configured equity symbols for active scopes; skipping equity materialization"
                )
                written = materialize_universe_members(
                    self._vg_db,
                    now_utc_str=now_utc_str,
                    skip_equity=True,
                )
        else:
            # Observe mode: full Alpaca materialization (legacy behavior)
            written = materialize_universe_members(self._vg_db, now_utc_str=now_utc_str)

        self._last_universe_refresh = self._now_et().strftime("%Y-%m-%d %H:%M")
        return written

    def _init_stage1_adapters(self) -> None:
        """Initialize IBKR, Alpaca WS, Alpaca REST, and Twelve Data for V1."""
        equity_session_active = self._is_polling_asset_enabled("equity") or self._is_polling_asset_enabled("index")
        forex_session_active = self._is_polling_asset_enabled("forex")
        use_ibkr_equity = self._use_ibkr_for_equity()
        equity_symbols = get_active_equity_symbols(self.db_path) if equity_session_active else []
        if self._ibkr_adapter is None and (equity_session_active or forex_session_active):
            try:
                self._ibkr_adapter = IBKRStreamingAdapter(client_id=10)
                if self._ibkr_adapter.connect():
                    written = self._ibkr_adapter.register_universe(self.db_path)
                    qualified = 0
                    subscribed = 0
                    if forex_session_active:
                        forex_symbols = self._ibkr_adapter.get_forex_pairs()
                        qualified = self._ibkr_adapter.qualify_contracts(
                            forex_symbols,
                            asset_class="forex",
                        )
                        subscribed = self._ibkr_adapter.subscribe_streaming(
                            asset_class="forex",
                            db_path=_IBKR_INTRADAY_DB,
                            use_rth=False,
                        )
                    equity_qualified = 0
                    equity_subscribed = 0
                    # === CLI/AUTO EQUITY SOURCE SELECTION ===
                    if equity_session_active and use_ibkr_equity and equity_symbols:
                        logger.info("[V1] Equity source=%s → using IBKR streaming for equity bars", self.equity_data_source)
                        equity_qualified = self._ibkr_adapter.qualify_contracts(
                            equity_symbols,
                            asset_class="equity",
                        )
                        equity_subscribed = self._ibkr_adapter.subscribe_streaming(
                            equity_symbols,
                            asset_class="equity",
                            db_path=_IBKR_INTRADAY_DB,
                            use_rth=False,
                        )
                    logger.info(
                        "[V1] IBKR connected and registered %d universe rows; forex qualified=%d subscribed=%d; equity qualified=%d subscribed=%d",
                        written,
                        qualified,
                        subscribed,
                        equity_qualified,
                        equity_subscribed,
                    )
                else:
                    self._ibkr_adapter = None
            except Exception as exc:
                logger.warning("[V1] Could not initialize IBKR adapter: %s", exc)
                self._ibkr_adapter = None

        ws_dead = self._alpaca_task is not None and not self._alpaca_task.is_alive()
        if ws_dead:
            logger.warning("[V1] Alpaca WebSocket thread is not alive; restarting")
            self._alpaca_adapter = None
            self._alpaca_task = None
        has_ibkr_equity_stream = (
            equity_session_active
            and use_ibkr_equity
            and self._ibkr_adapter is not None
            and self._ibkr_adapter.is_connected()
            and any(
                key[0] == "equity"
                for key in getattr(self._ibkr_adapter, "valid_contracts", {}).keys()
            )
        )
        disable_alpaca_ws = bool(getattr(self, "_disable_alpaca_ws", False))
        if use_ibkr_equity and has_ibkr_equity_stream:
            logger.info("[V1] Equity source=%s → skipping Alpaca WebSocket because IBKR owns equities", self.equity_data_source)
        elif disable_alpaca_ws and equity_symbols and equity_session_active:
            logger.info("[V1] Alpaca WebSocket disabled for this orchestrator instance; using REST-only equity polling")
        if equity_symbols and equity_session_active and self._alpaca_adapter is None:
            if not (use_ibkr_equity and has_ibkr_equity_stream) and not disable_alpaca_ws:
                try:
                    self._alpaca_adapter = AlpacaWSAdapter(equity_symbols, db_path=self.db_path)
                    self._alpaca_task = threading.Thread(
                        target=lambda: asyncio.run(self._alpaca_adapter.start()),
                        name="vanguard-alpaca-ws",
                        daemon=True,
                    )
                    self._alpaca_task.start()
                    logger.info("[V1] Alpaca WebSocket started for %d symbols", len(equity_symbols))
                except Exception as exc:
                    logger.warning("[V1] Could not start Alpaca WebSocket: %s", exc)
        if equity_symbols and equity_session_active and self._alpaca_rest is None:
            try:
                self._alpaca_rest = AlpacaAdapter()
            except Exception as exc:
                logger.warning("[V1] Could not initialize Alpaca REST client: %s", exc)
        if self._td_adapter is None:
            try:
                self._td_adapter = load_twelvedata_adapter(db_path=self.db_path)
            except Exception as exc:
                logger.warning("[V1] Could not initialize Twelve Data adapter: %s", exc)
        if self._mt5_adapter is None:
            runtime_cfg = get_runtime_config()
            mt5_asset_classes = _expand_mt5_asset_classes({
                asset_class
                for asset_class in {"equity", "forex", "crypto", "index", "commodity", "metal", "energy", "agriculture", "futures"}
                if self._is_polling_asset_enabled(asset_class)
                and resolve_market_data_source_id(asset_class, runtime_config=runtime_cfg) == "mt5_local"
            })
            mt5_cfg = _resolve_active_mt5_local_config(runtime_cfg)
            if mt5_asset_classes and mt5_cfg.get("enabled"):
                try:
                    self._mt5_adapter = MT5DWXAdapter(
                        dwx_files_path=mt5_cfg["dwx_files_path"],
                        db_path=self.db_path,
                        symbol_suffix=str(mt5_cfg.get("symbol_suffix", ".x") or ""),
                    )
                    if not self._mt5_adapter.connect():
                        logger.warning("[V1] MT5 DWX adapter failed to connect (MT5 not running?)")
                        self._mt5_adapter = None
                    else:
                        logger.info("[V1] MT5 DWX adapter connected")
                except Exception as exc:
                    logger.warning("[V1] Could not initialize MT5 DWX adapter: %s", exc)
                    self._mt5_adapter = None

    def _poll_alpaca_rest_1m(self, symbols: list[str]) -> int:
        """Fetch a short recent 1m window for active equities via Alpaca REST."""
        if not symbols or self._alpaca_rest is None:
            return 0
        end = self._now_utc()
        start = end - timedelta(minutes=12)
        raw = self._alpaca_rest.get_1m_bars(symbols, start, end)
        now_iso = iso_utc(end)
        rows: list[dict[str, Any]] = []
        for symbol, bars in raw.items():
            for bar in bars:
                open_dt = parse_utc(bar["t"])
                end_ts = (open_dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows.append(
                    {
                        "symbol": symbol.upper(),
                        "bar_ts_utc": end_ts,
                        "open": float(bar["o"]),
                        "high": float(bar["h"]),
                        "low": float(bar["l"]),
                        "close": float(bar["c"]),
                        "volume": int(bar.get("v", 0)),
                        "tick_volume": int(bar.get("n", 0)),
                        "asset_class": "equity",
                        "data_source": "alpaca_rest",
                        "ingest_ts_utc": now_iso,
                    }
                )
        written = self._vg_db.upsert_bars_1m(rows)
        logger.info("[V1] Alpaca REST polled: %d bars", written)
        return written

    def _mirror_ibkr_bars_1m(self, bars_map: dict[str, list[dict[str, Any]]]) -> int:
        """Mirror IBKR 1m bars into the canonical vanguard_bars_1m table."""
        rows: list[dict[str, Any]] = []
        now_iso = iso_utc(self._now_utc())
        for symbol, bars in bars_map.items():
            for bar in bars:
                rows.append(
                    {
                        "symbol": symbol.upper(),
                        "bar_ts_utc": bar["bar_ts_utc"],
                        "open": float(bar.get("open") or 0.0),
                        "high": float(bar.get("high") or 0.0),
                        "low": float(bar.get("low") or 0.0),
                        "close": float(bar.get("close") or 0.0),
                        "volume": int(bar.get("volume") or 0),
                        "tick_volume": int(bar.get("tick_volume") or 0),
                        "asset_class": str(bar.get("asset_class") or "equity"),
                        "data_source": "ibkr",
                        "ingest_ts_utc": now_iso,
                    }
                )
        return self._vg_db.upsert_bars_1m(rows)

    def _poll_ibkr_1m(self) -> dict[str, int]:
        """
        Fetch short 1m windows from IBKR and persist to both IBKR and Vanguard DBs.

        Immediate hot-path policy:
        - Equities come from Alpaca WS / Alpaca REST fallback.
        - IBKR is used for forex only until the equity path is converted to
          keepUpToDate=True streaming.
        """
        if not self._ibkr_adapter or not self._ibkr_adapter.is_connected():
            return {"ibkr_equity_bars": 0, "ibkr_forex_bars": 0}

        equity_written = 0
        equity_session_active = self._is_polling_asset_enabled("equity") or self._is_polling_asset_enabled("index")
        forex_session_active = self._is_polling_asset_enabled("forex")
        use_ibkr_equity = self._use_ibkr_for_equity()
        if equity_session_active and use_ibkr_equity:
            equity_bars = self._ibkr_adapter.get_latest_bars(
                _IBKR_INTRADAY_DB,
                minutes=20,
                asset_class="equity",
            )
            equity_written = self._mirror_ibkr_bars_1m(equity_bars)

        forex_written = 0
        if forex_session_active:
            forex_bars = self._ibkr_adapter.get_latest_bars(
                _IBKR_INTRADAY_DB,
                minutes=20,
                asset_class="forex",
            )
            forex_written = self._mirror_ibkr_bars_1m(forex_bars)

        if use_ibkr_equity:
            logger.info(
                "[V1] IBKR polled: equity=%d bars forex=%d bars (equity source=%s)",
                equity_written,
                forex_written,
                self.equity_data_source,
            )
        else:
            logger.info(
                "[V1] IBKR polled: equity=%d bars forex=%d bars (equity skipped; Alpaca owns equities)",
                equity_written,
                forex_written,
            )
        return {"ibkr_equity_bars": equity_written, "ibkr_forex_bars": forex_written}

    def _resolve_v1_source_plan(
        self,
        runtime_cfg: dict,
        active_profile_ids: list[str],
    ) -> dict[str, dict]:
        """Build a per-asset-class routing plan for V1 bar ingestion.

        Returns: {asset_class: {
            "primary_source_id": str,   # from lane policy or legacy resolver
            "primary_profile_id": str | None,  # which profile's lane policy applied
            "freshness_max_s": int | None,
            "fallback_ids": list[str],
            "primary_age_s": float | None,
            "fallback_needed": bool,
            "fallback_reason": str | None,
        }}

        Staleness is computed by querying max(bar_ts_utc) from vanguard_bars_1m
        for (symbol in any active-profile universe for this asset class) limited
        to rows where data_source matches the primary family. A non-TD primary
        (metaapi, mt5_local) triggers fallback when bars exceed freshness_max_s.
        """
        from vanguard.config.runtime_config import (
            resolve_market_data_source_id as _rmsi,
            resolve_market_data_policy as _rmp,
        )
        from datetime import datetime, timezone

        plan: dict[str, dict] = {}
        # Union of asset classes across all active profiles.
        active_asset_classes: set[str] = set()
        for pid in active_profile_ids:
            for prof in runtime_cfg.get("profiles") or []:
                if str(prof.get("id") or "") != pid:
                    continue
                for ac in (prof.get("live_allowed_asset_classes") or []):
                    active_asset_classes.add(str(ac).lower())
                lanes = prof.get("asset_lanes") or {}
                active_asset_classes.update(str(k).lower() for k in lanes.keys())

        for asset_class in active_asset_classes:
            primary_source_id: str = ""
            primary_profile_id: str | None = None
            policy: dict = {}
            for pid in active_profile_ids:
                mdp = _rmp(asset_class, profile_id=pid, runtime_config=runtime_cfg)
                if mdp.get("bars_primary"):
                    primary_source_id = str(mdp["bars_primary"])
                    primary_profile_id = pid
                    policy = mdp
                    break
            if not primary_source_id:
                primary_source_id = _rmsi(asset_class, runtime_config=runtime_cfg)
            fallback_ids = [str(x) for x in (policy.get("fallback") or [])]
            freshness_max_s = policy.get("freshness_max_s")

            # Only compute freshness when primary is a non-TD source with a
            # policy-defined freshness bound. TD-primary always polls, so no
            # staleness check is needed.
            primary_age_s: float | None = None
            fallback_needed = False
            fallback_reason: str | None = None
            if (
                primary_source_id
                and primary_source_id != "twelvedata"
                and freshness_max_s is not None
            ):
                try:
                    with self._vg_db.connect() as conn:
                        row = conn.execute(
                            """
                            SELECT MAX(bar_ts_utc) AS mx
                            FROM vanguard_bars_1m
                            WHERE asset_class = ? AND data_source = ?
                            """,
                            (asset_class, primary_source_id),
                        ).fetchone()
                    last_ts = row[0] if row else None
                    if last_ts:
                        try:
                            parsed = datetime.fromisoformat(
                                str(last_ts).replace("Z", "+00:00")
                            )
                            primary_age_s = (
                                datetime.now(timezone.utc) - parsed
                            ).total_seconds()
                        except ValueError:
                            primary_age_s = None
                    if primary_age_s is None or primary_age_s > float(freshness_max_s):
                        fallback_needed = True
                        fallback_reason = (
                            f"{primary_source_id}_stale"
                            if primary_age_s is not None
                            else f"{primary_source_id}_no_bars"
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[V1] freshness probe failed asset=%s primary=%s: %s",
                        asset_class, primary_source_id, exc,
                    )

            plan[asset_class] = {
                "primary_source_id": primary_source_id,
                "primary_profile_id": primary_profile_id,
                "freshness_max_s": freshness_max_s,
                "fallback_ids": fallback_ids,
                "primary_age_s": primary_age_s,
                "fallback_needed": fallback_needed,
                "fallback_reason": fallback_reason,
            }

        return plan

    def _run_v1(self, in_scope_symbols: list[str] | None = None) -> dict[str, int]:
        """Ensure latest Stage 1 bars are present from all live data sources.

        in_scope_symbols: Phase 2a enforce-mode filter. When set, only those
        symbols are polled. None = full universe (observe mode or no resolver).
        """
        if is_replay_from_source_db():
            logger.info("[V1] QA replay mode — using read-only source DB bars, skipping live polling")
            return {
                "v1_alpaca_bars": 0,
                "v1_td_bars": 0,
                "v1_ibkr_equity_bars": 0,
                "v1_ibkr_forex_bars": 0,
                "v1_bars_5m": 0,
                "v1_bars_1h": 0,
                "v1_source_health": 0,
            }
        if self._is_test_execution_mode():
            logger.info("[V1] TEST mode — skipping bar/source-health writes")
            return {
                "v1_alpaca_bars": 0,
                "v1_td_bars": 0,
                "v1_ibkr_equity_bars": 0,
                "v1_ibkr_forex_bars": 0,
                "v1_bars_5m": 0,
                "v1_bars_1h": 0,
                "v1_source_health": 0,
            }

        timings: dict[str, float] = {}
        t = time.time()
        self._materialize_universe(force=False)
        timings["universe_refresh"] = time.time() - t

        t = time.time()
        self._init_stage1_adapters()
        timings["adapter_init"] = time.time() - t

        equity_session_active = self._is_polling_asset_enabled("equity") or self._is_polling_asset_enabled("index")
        forex_session_active = self._is_polling_asset_enabled("forex")
        crypto_session_active = self._is_polling_asset_enabled("crypto")
        equity_symbols = get_active_equity_symbols(self.db_path) if equity_session_active else []

        td_bars = 0
        mt5_bars = 0
        mt5_details: dict[str, Any] = {
            "requested_symbols": [],
            "successful_symbols": [],
            "failed_symbols": [],
            "timed_out_symbols": [],
            "written_rows": 0,
        }
        ibkr_equity_bars = 0
        ibkr_forex_bars = 0
        alpaca_rest_bars = 0

        t = time.time()
        if self._ibkr_adapter and self._ibkr_adapter.is_connected():
            ibkr_counts = self._poll_ibkr_1m()
            ibkr_equity_bars = ibkr_counts["ibkr_equity_bars"]
            ibkr_forex_bars = ibkr_counts["ibkr_forex_bars"]
        timings["ibkr_poll"] = time.time() - t

        t = time.time()
        if self._alpaca_rest and equity_session_active and equity_symbols and ibkr_equity_bars == 0:
            alpaca_rest_bars = self._poll_alpaca_rest_1m(equity_symbols)
        timings["alpaca_rest_poll"] = time.time() - t

        t = time.time()
        if self._mt5_adapter and self._mt5_adapter.is_connected():
            # MT5 DWX is the primary source for forex/crypto/index/commodity.
            # Build symbol list from in_scope_symbols or full universe.
            runtime_cfg = get_runtime_config()
            mt5_asset_classes = _expand_mt5_asset_classes({
                asset_class
                for asset_class in {"equity", "forex", "crypto", "index", "commodity", "metal", "energy", "agriculture", "futures"}
                if self._is_polling_asset_enabled(asset_class)
                and resolve_market_data_source_id(asset_class, runtime_config=runtime_cfg) == "mt5_local"
            })
            if in_scope_symbols is not None:
                _mt5_syms = self._filter_symbols_for_asset_classes(in_scope_symbols, mt5_asset_classes)
            else:
                _mt5_syms = self._load_active_symbols_for_asset_classes(mt5_asset_classes)
            if _mt5_syms:
                mt5_bars = self._mt5_adapter.poll(symbol_filter=_mt5_syms)
                mt5_details = self._mt5_adapter.get_last_poll_details()
                logger.info(
                    "[V1] MT5 DWX polled: %d bars (%d requested, %d ok, %d failed, %d timed out)",
                    mt5_bars,
                    len(mt5_details.get("requested_symbols", [])),
                    len(mt5_details.get("successful_symbols", [])),
                    len(mt5_details.get("failed_symbols", [])),
                    len(mt5_details.get("timed_out_symbols", [])),
                )
        timings["mt5_poll"] = time.time() - t

        t = time.time()
        # Market-data hub plan: per-asset routing incl. staleness-aware fallback.
        # When a lane policy points primary at a non-TD source (metaapi/mt5_local)
        # and bars are fresh, TD is skipped for that class. When primary is stale
        # beyond freshness_max_s, TD is re-added to the poll list as explicit
        # fallback — attribution flows through vanguard_market_data_ingest_runs.
        active_profile_ids = self._active_profile_ids()
        v1_source_plan = self._resolve_v1_source_plan(
            runtime_cfg=get_runtime_config(),
            active_profile_ids=active_profile_ids,
        )
        td_fallback_classes: set[str] = set()
        if self._td_adapter:
            td_symbols_backup = self._td_adapter.symbols
            try:
                runtime_cfg = get_runtime_config()
                market_sources_cfg = ((runtime_cfg.get("market_data") or {}).get("sources") or {})
                td_asset_classes = set()
                for asset_class, session_active in (
                    ("crypto", crypto_session_active),
                    ("forex", forex_session_active),
                    ("metal", self._is_polling_asset_enabled("metal")),
                ):
                    if not session_active:
                        continue
                    primary_source = resolve_market_data_source_id(asset_class, runtime_config=runtime_cfg)
                    # Gate semantics: TD polls only when primary is TD,
                    # or when the lane plan says fallback is needed.
                    plan_entry = v1_source_plan.get(asset_class) or {}
                    plan_primary = plan_entry.get("primary_source_id") or primary_source
                    if plan_primary == "twelvedata":
                        td_asset_classes.add(asset_class)
                    elif plan_entry.get("fallback_needed"):
                        fallback_ids = plan_entry.get("fallback_ids") or []
                        if "twelvedata" in fallback_ids:
                            td_asset_classes.add(asset_class)
                            td_fallback_classes.add(asset_class)
                            logger.warning(
                                "[V1] TD FALLBACK asset=%s primary=%s reason=%s age_s=%s freshness_max_s=%s",
                                asset_class,
                                plan_primary,
                                plan_entry.get("fallback_reason"),
                                plan_entry.get("primary_age_s"),
                                plan_entry.get("freshness_max_s"),
                            )
                mt5_missing = {
                    _canonical_symbol(sym)
                    for sym in mt5_details.get("requested_symbols", [])
                    if _canonical_symbol(sym) not in {
                        _canonical_symbol(ok) for ok in mt5_details.get("successful_symbols", [])
                    }
                }
                mt5_asset_classes = _expand_mt5_asset_classes({
                    asset_class
                    for asset_class in {"equity", "forex", "crypto", "index", "commodity", "metal", "energy", "agriculture", "futures"}
                    if self._is_polling_asset_enabled(asset_class)
                    and resolve_market_data_source_id(asset_class, runtime_config=runtime_cfg) == "mt5_local"
                })
                # Bug 7: intersect with Phase 2a resolved universe so we only
                # poll the 33 GFT symbols, not the full 96+ from vanguard_universe_members.
                # Canonicalize both sides (strip slashes/spaces) for comparison.
                # Deduplicate: some symbols exist in both "EUR/USD" and "EURUSD" form —
                # keep only one entry per canonical symbol (prefer slash format for TD API).
                if in_scope_symbols is not None:
                    _scope_canon = {_canonical_symbol(s) for s in in_scope_symbols}
                    _canonical_seen: set[str] = set()
                    _filtered: dict[str, str] = {}
                    for _sym, _ac in td_symbols_backup.items():
                        if str(_ac or "").lower() not in td_asset_classes:
                            continue
                        _canon = _canonical_symbol(_sym)
                        if _canon not in _scope_canon:
                            continue
                        if str(_ac or "").lower() in mt5_asset_classes and _canon not in mt5_missing:
                            continue
                        if _canon in _canonical_seen:
                            continue  # skip duplicate (already have this canonical symbol)
                        _canonical_seen.add(_canon)
                        _filtered[_sym] = _ac
                    self._td_adapter.symbols = _filtered
                else:
                    _canonical_seen: set[str] = set()
                    _filtered: dict[str, str] = {}
                    for symbol, asset_class in td_symbols_backup.items():
                        if str(asset_class or "").lower() not in td_asset_classes:
                            continue
                        _canon = _canonical_symbol(symbol)
                        if str(asset_class or "").lower() in mt5_asset_classes and _canon not in mt5_missing:
                            continue
                        if _canon in _canonical_seen:
                            continue
                        _canonical_seen.add(_canon)
                        _filtered[symbol] = asset_class
                    self._td_adapter.symbols = _filtered
                logger.info(
                    "[V1] Twelve Data asset filter=%s symbols=%d missing_from_dwx=%d",
                    sorted(td_asset_classes),
                    len(self._td_adapter.symbols),
                    len(mt5_missing),
                )
                if self._td_adapter.symbols:
                    td_bars = self._td_adapter.poll_latest_bars()
                    logger.info("[V1] Twelve Data polled: %d bars", td_bars)
                    # Attribute fallback writes to vanguard_market_data_ingest_runs
                    # so every row is traceable to (profile, provider, fallback_reason).
                    if td_fallback_classes and td_bars:
                        import uuid as _uuid
                        from vanguard.helpers.clock import iso_utc, now_utc
                        fb_reasons = ",".join(sorted({
                            (v1_source_plan.get(ac) or {}).get("fallback_reason") or "unknown"
                            for ac in td_fallback_classes
                        }))
                        try:
                            self._vg_db.insert_market_data_ingest_run(
                                run_id=str(_uuid.uuid4()),
                                profile_id=(active_profile_ids[0] if active_profile_ids else "unknown"),
                                provider_id="twelvedata",
                                source_family="twelvedata",
                                asset_classes=sorted(td_fallback_classes),
                                timeframe="1m",
                                started_ts_utc=iso_utc(now_utc()),
                                finished_ts_utc=iso_utc(now_utc()),
                                symbols_requested=len(self._td_adapter.symbols),
                                symbols_ok=len(self._td_adapter.symbols),
                                symbols_failed=0,
                                rows_written=int(td_bars or 0),
                                fallback_used=True,
                                fallback_reason=fb_reasons,
                                details={
                                    "trigger": "v1_td_fallback",
                                    "fallback_asset_classes": sorted(td_fallback_classes),
                                    "plan_snapshot": {
                                        ac: v1_source_plan.get(ac) for ac in td_fallback_classes
                                    },
                                },
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "[V1] fallback ingest_run insert failed: %s", exc,
                            )
            finally:
                self._td_adapter.symbols = td_symbols_backup
        timings["twelvedata_poll"] = time.time() - t

        t = time.time()
        intraday_counts = self._aggregate_recent_1m_to_intraday()
        timings["aggregate_1m_to_intraday"] = time.time() - t
        t = time.time()
        bars_1h = self._aggregate_recent_5m_to_1h()
        timings["aggregate_5m_to_1h"] = time.time() - t
        t = time.time()
        health_rows = self._update_source_health()
        timings["source_health"] = time.time() - t
        self._last_v1_runtime_details = {
            "source_labels": {
                "crypto": resolve_market_data_source_id("crypto", runtime_config=runtime_cfg),
                "forex": resolve_market_data_source_id("forex", runtime_config=runtime_cfg),
                "metal": resolve_market_data_source_id("metal", runtime_config=runtime_cfg),
            },
            "mt5_poll_details": dict(mt5_details),
            "in_scope_symbols": list(in_scope_symbols or []),
        }
        logger.info("[V1] timings=%s", {k: round(v, 3) for k, v in timings.items()})
        return {
            "v1_alpaca_bars": alpaca_rest_bars,
            "v1_td_bars": td_bars,
            "v1_mt5_bars": mt5_bars,
            "v1_ibkr_equity_bars": ibkr_equity_bars,
            "v1_ibkr_forex_bars": ibkr_forex_bars,
            "v1_bars_5m": intraday_counts["5m"],
            "v1_bars_10m": intraday_counts["10m"],
            "v1_bars_15m": intraday_counts["15m"],
            "v1_bars_30m": intraday_counts["30m"],
            "v1_bars_1h": bars_1h,
            "v1_source_health": health_rows,
        }

    def _load_active_symbols_for_asset_classes(self, asset_classes: set[str]) -> list[str]:
        """Load active canonical symbols from universe membership for the requested asset classes."""
        if not asset_classes:
            return []
        placeholders = ",".join("?" for _ in asset_classes)
        with self._vg_db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT symbol
                FROM vanguard_universe_members
                WHERE is_active = 1
                  AND asset_class IN ({placeholders})
                ORDER BY data_source, symbol
                """,
                tuple(sorted(asset_classes)),
            ).fetchall()
        seen: set[str] = set()
        ordered: list[str] = []
        for row in rows:
            canon = _canonical_symbol(row["symbol"])
            if canon and canon not in seen:
                seen.add(canon)
                ordered.append(canon)
        return ordered

    def _filter_symbols_for_asset_classes(
        self,
        symbols: list[str],
        asset_classes: set[str],
    ) -> list[str]:
        seen: set[str] = set()
        filtered: list[str] = []
        for symbol in symbols:
            canon = _canonical_symbol(symbol)
            if not canon or canon in seen:
                continue
            if self._mt5_adapter and self._mt5_adapter._get_asset_class(canon) in asset_classes:
                seen.add(canon)
                filtered.append(canon)
        return filtered

    def _aggregate_recent_1m_to_intraday(self) -> dict[str, int]:
        """Aggregate recent 1m bars into the intraday timeframe tables used by the engine."""
        latest_cutoffs: dict[str, datetime] = {}
        lookbacks = {"5m": 5, "10m": 10, "15m": 15, "30m": 30}
        for timeframe, minutes in lookbacks.items():
            latest_ts = self._vg_db.latest_bar_ts_utc(f"vanguard_bars_{timeframe}")
            if latest_ts:
                latest_cutoffs[timeframe] = parse_utc(latest_ts) - timedelta(minutes=minutes)
            else:
                latest_cutoffs[timeframe] = self._now_utc() - timedelta(minutes=max(70, minutes * 14))

        recent_default = self._now_utc() - timedelta(hours=8)
        cutoff = max(min(latest_cutoffs.values()), recent_default).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._vg_db.connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, bar_ts_utc, open, high, low, close, volume, asset_class, data_source
                FROM vanguard_bars_1m
                WHERE bar_ts_utc >= ?
                ORDER BY symbol, bar_ts_utc
                """,
                (cutoff,),
            ).fetchall()
        bars_1m = [dict(row) for row in rows]
        counts: dict[str, int] = {"5m": 0, "10m": 0, "15m": 0, "30m": 0}
        if not bars_1m:
            return counts

        bars_5m = aggregate_1m_to_5m(bars_1m)
        counts["5m"] = self._vg_db.upsert_bars_5m(bars_5m)

        for minutes in _MTF_INTRADAY_MINUTES:
            timeframe = f"{minutes}m"
            aggregated = aggregate_1m_to_timeframe(bars_1m, minutes)
            counts[timeframe] = self._vg_db.upsert_bars_mtf(aggregated, timeframe)

        ibkr_rows = [
            (
                bar["symbol"],
                bar["bar_ts_utc"],
                bar.get("open"),
                bar.get("high"),
                bar.get("low"),
                bar.get("close"),
                bar.get("volume"),
                0,
                5,
                bar.get("asset_class") or "equity",
                "ibkr",
            )
            for bar in bars_5m
            if str(bar.get("data_source") or "") == "ibkr"
        ]
        if ibkr_rows:
            with sqlite3.connect(_IBKR_INTRADAY_DB, timeout=30) as con:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO ibkr_bars_5m
                        (symbol, bar_ts_utc, open, high, low, close, volume,
                         tick_volume, bar_count, asset_class, data_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ibkr_rows,
                )
                con.commit()
        return counts

    def _aggregate_recent_1m_to_5m(self) -> int:
        """Aggregate a recent 1m window into 5m bars and upsert them."""
        return self._aggregate_recent_1m_to_intraday()["5m"]

    def _aggregate_recent_5m_to_1h(self) -> int:
        """Aggregate a recent 5m window into 1h bars and upsert them."""
        latest_5m = self._vg_db.latest_bar_ts_utc("vanguard_bars_5m")
        if not latest_5m:
            return 0
        latest_5m_dt = parse_utc(latest_5m)
        latest_1h = self._vg_db.latest_bar_ts_utc("vanguard_bars_1h")
        latest_1h_dt = parse_utc(latest_1h) if latest_1h else None
        if latest_5m_dt.minute != 0:
            return 0
        if latest_1h_dt and latest_1h_dt >= latest_5m_dt:
            return 0
        if latest_1h_dt:
            cutoff = (latest_1h_dt - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            cutoff = (self._now_utc() - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._vg_db.connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, bar_ts_utc, open, high, low, close, volume, asset_class, data_source
                FROM vanguard_bars_5m
                WHERE bar_ts_utc >= ?
                ORDER BY symbol, bar_ts_utc
                """,
                (cutoff,),
            ).fetchall()
        bars_5m = [dict(row) for row in rows]
        return self._vg_db.upsert_bars_1h(aggregate_5m_to_1h(bars_5m))

    def _update_source_health(self) -> int:
        """Update Stage 1 source freshness from universe membership and 1m bars."""
        now_iso = iso_utc(self._now_utc())
        cutoff_5m = (self._now_utc() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cutoff_1d = (self._now_utc() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows: list[dict[str, Any]] = []
        source_map = {
            "ibkr": ("ibkr",),
            "alpaca": ("alpaca_ws", "alpaca_rest"),
            "metaapi": ("metaapi",),
            "twelvedata": ("twelvedata",),
        }
        source_names = tuple(source_map.keys())
        with self._vg_db.connect() as conn:
            recent_rows = conn.execute(
                """
                SELECT data_source, asset_class, COUNT(*) AS bars_last_5min,
                       COUNT(DISTINCT symbol) AS symbols_with_recent_bar,
                       MAX(bar_ts_utc) AS last_bar_ts
                FROM vanguard_bars_1m INDEXED BY idx_vg_bars_1m_ts_source_asset_symbol
                WHERE bar_ts_utc >= ?
                GROUP BY data_source, asset_class
                """,
                (cutoff_5m,),
            ).fetchall()
            recent_stats = {
                (row["data_source"], row["asset_class"]): dict(row)
                for row in recent_rows
            }
            recent_last_day_rows = conn.execute(
                """
                SELECT data_source, asset_class, MAX(bar_ts_utc) AS last_bar_ts
                FROM vanguard_bars_1m INDEXED BY idx_vg_bars_1m_ts_source_asset_symbol
                WHERE bar_ts_utc >= ?
                GROUP BY data_source, asset_class
                """,
                (cutoff_1d,),
            ).fetchall()
            last_day_stats = {
                (row["data_source"], row["asset_class"]): dict(row)
                for row in recent_last_day_rows
            }
            placeholders = ",".join("?" for _ in source_names)
            active_rows = conn.execute(
                f"""
                SELECT data_source, asset_class, COUNT(DISTINCT symbol) AS active_count
                FROM vanguard_universe_members
                WHERE is_active = 1 AND data_source IN ({placeholders})
                GROUP BY data_source, asset_class
                ORDER BY data_source, asset_class
                """,
                source_names,
            ).fetchall()
            active_stats = {
                (row["data_source"], row["asset_class"]): int(row["active_count"] or 0)
                for row in active_rows
            }
            for source, bar_sources in source_map.items():
                asset_classes = sorted(
                    asset_class
                    for source_name, asset_class in active_stats
                    if source_name == source
                )
                for asset_class in asset_classes:
                    source_keys = [(bar_source, asset_class) for bar_source in bar_sources]
                    active_count = active_stats.get((source, asset_class), 0)
                    recent_symbols = sum(
                        recent_stats.get(key, {}).get("symbols_with_recent_bar", 0)
                        for key in source_keys
                    )
                    recent_bars = sum(
                        recent_stats.get(key, {}).get("bars_last_5min", 0)
                        for key in source_keys
                    )
                    candidate_last_bar_ts = [
                        recent_stats.get(key, {}).get("last_bar_ts")
                        or last_day_stats.get(key, {}).get("last_bar_ts")
                        for key in source_keys
                    ]
                    last_bar_ts = max((ts for ts in candidate_last_bar_ts if ts), default=None)
                    status = "live" if recent_symbols > 0 else "stale"
                    rows.append({
                        "data_source": source,
                        "asset_class": asset_class,
                        "last_bar_ts": last_bar_ts,
                        "last_poll_ts": now_iso,
                        "symbols_active": active_count,
                        "symbols_with_recent_bar": recent_symbols,
                        "bars_last_5min": recent_bars,
                        "status": status,
                        "last_updated": now_iso,
                    })
        return self._vg_db.upsert_source_health(rows)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    @staticmethod
    def _is_model_v2_forex_row(row: dict[str, Any], asset_class: str | None = None) -> bool:
        """True for new forex LGBM/V5.2 rows that should not legacy-forward-track."""
        normalized_asset = str(asset_class or "").lower()
        readiness = str(row.get("model_readiness") or "").lower()
        model_source = str(row.get("model_source") or "").lower()
        return (
            normalized_asset == "forex"
            and readiness == "v5_2_selected"
            and ("lgbm_vp45" in model_source or model_source)
        )

    def _emit_nonapproved_cycle_decisions(self, cycle_ts: str | None = None) -> dict[str, int]:
        """Emit decision-log rows for blocked/held cycles even when nothing reaches execution."""
        _signal_decision_log_ensure_table(self.db_path)
        _signal_forward_checkpoints_ensure_table(self.db_path)
        tradeable_rows = _get_tradeable_rows(self.db_path, cycle_ts)
        cycle_ts_utc = str(cycle_ts or (tradeable_rows[0].get("cycle_ts_utc") if tradeable_rows else "") or "")
        if not cycle_ts_utc:
            return {"meta_gate": 0, "blocked": 0, "held": 0}

        candidate_metrics_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        selection_blocked_rows: list[dict[str, Any]] = []
        meta_gate_rows: list[dict[str, Any]] = []
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                runtime_cfg = get_runtime_config()
                decision_streams = runtime_cfg.get("decision_streams") or {}
                metagate_stream_profiles: dict[str, str] = {}
                for stream_id, stream_cfg in decision_streams.items():
                    stream_id = str(stream_id or "")
                    if not stream_id:
                        continue
                    gate_cfg = (stream_cfg or {}).get("meta_gate")
                    if not isinstance(gate_cfg, dict) or gate_cfg.get("enabled") is False:
                        continue
                    profile_id = str((stream_cfg or {}).get("execution_profile_id") or "")
                    if profile_id:
                        metagate_stream_profiles[stream_id] = profile_id
                if metagate_stream_profiles:
                    placeholders = ",".join("?" for _ in metagate_stream_profiles)
                    p_cols = self._table_columns(con, "vanguard_predictions_history")
                    p_model_family = self._optional_col_expr(
                        p_cols, "p", "model_family", fallback_sql="NULL", out_alias="model_family"
                    )
                    p_model_run_id = self._optional_col_expr(
                        p_cols, "p", "model_run_id", fallback_sql="NULL", out_alias="model_run_id"
                    )
                    p_target_horizon_bars = self._optional_col_expr(
                        p_cols, "p", "target_horizon_bars", fallback_sql="NULL", out_alias="target_horizon_bars"
                    )
                    meta_rows = con.execute(
                        f"""
                        SELECT p.cycle_ts_utc,
                               p.symbol,
                               p.direction,
                               p.asset_class,
                               p.lane_id,
                               p.decision_stream_id,
                               p.predicted_return,
                               p.conviction,
                               p.model_id,
                               {p_model_run_id},
                               {p_model_family},
                               {p_target_horizon_bars},
                               p.meta_score,
                               p.meta_pass,
                               p.meta_gate_id
                        FROM vanguard_predictions_history p
                        WHERE p.cycle_ts_utc = ?
                          AND p.decision_stream_id IN ({placeholders})
                          AND COALESCE(p.meta_pass, 0) = 0
                        """,
                        [cycle_ts_utc, *metagate_stream_profiles.keys()],
                    ).fetchall()
                    for row in meta_rows:
                        row_dict = dict(row)
                        row_dict["profile_id"] = metagate_stream_profiles.get(str(row["decision_stream_id"] or ""), "")
                        if row_dict["profile_id"]:
                            meta_gate_rows.append(row_dict)

                s_cols = self._table_columns(con, "vanguard_v5_selection")
                t_cols = self._table_columns(con, "vanguard_v5_tradeability")
                s_live_policy_reason = self._optional_col_expr(
                    s_cols, "s", "live_policy_reason_json", out_alias="live_policy_reason_json"
                )
                t_predicted_move_bps = self._optional_col_expr(
                    t_cols, "t", "predicted_move_bps", out_alias="predicted_move_bps"
                )
                t_spread_bps = self._optional_col_expr(t_cols, "t", "spread_bps", out_alias="spread_bps")
                t_cost_bps = self._optional_col_expr(t_cols, "t", "cost_bps", out_alias="cost_bps")
                t_after_cost_bps = self._optional_col_expr(
                    t_cols, "t", "after_cost_bps", out_alias="after_cost_bps"
                )
                t_metrics_json = self._optional_col_expr(
                    t_cols, "t", "metrics_json", fallback_sql="'{}'", out_alias="metrics_json"
                )
                t_conviction = self._optional_col_expr(t_cols, "t", "conviction", out_alias="conviction")
                t_target_horizon_bars = self._optional_col_expr(
                    t_cols, "t", "target_horizon_bars", out_alias="target_horizon_bars"
                )
                t_model_family = self._optional_col_expr(t_cols, "t", "model_family", out_alias="model_family")
                t_model_source = self._optional_col_expr(
                    t_cols, "t", "model_id", fallback_sql="''", out_alias="model_source"
                )
                t_model_readiness = self._optional_col_expr(
                    t_cols, "t", "model_readiness", fallback_sql="''", out_alias="model_readiness"
                )
                t_gate_policy = self._optional_col_expr(
                    t_cols, "t", "gate_policy", fallback_sql="''", out_alias="gate_policy"
                )
                t_pair_state_ref_json = self._optional_col_expr(
                    t_cols, "t", "pair_state_ref_json", fallback_sql="'{}'", out_alias="pair_state_ref_json"
                )
                rows = con.execute(
                    f"""
                    SELECT s.profile_id,
                           s.symbol,
                           s.asset_class,
                           s.direction,
                           s.selected,
                           s.selection_rank,
                           s.selection_reason,
                           s.route_tier,
                           s.selection_state,
                           {s_live_policy_reason},
                           s.decision_stream_id,
                           s.top_rank_streak,
                           s.strong_prediction_streak,
                           s.same_bucket_streak,
                           s.flip_count_window,
                           s.live_followthrough_pips,
                           s.live_followthrough_bps,
                           s.live_followthrough_state,
                           s.direction_streak,
                           t.predicted_return,
                           {t_predicted_move_bps},
                           {t_spread_bps},
                           {t_cost_bps},
                           {t_after_cost_bps},
                           {t_metrics_json},
                           {t_conviction},
                           {t_target_horizon_bars},
                           {t_model_family},
                           {t_model_source},
                           {t_model_readiness},
                           {t_gate_policy},
                           {t_pair_state_ref_json}
                    FROM vanguard_v5_selection s
                    LEFT JOIN vanguard_v5_tradeability t
                      ON t.cycle_ts_utc = s.cycle_ts_utc
                     AND t.profile_id = s.profile_id
                     AND t.symbol = s.symbol
                     AND t.direction = s.direction
                    WHERE s.cycle_ts_utc = ?
                    """,
                    (cycle_ts_utc,),
                ).fetchall()
                for row in rows:
                    row_dict = dict(row)
                    candidate_metrics_map[
                        (
                            str(row["profile_id"] or ""),
                            str(row["symbol"] or "").upper(),
                            str(row["direction"] or "").upper(),
                        )
                    ] = row_dict
                    if (
                        not int(row_dict.get("selected") or 0)
                        and str(row_dict.get("selection_reason") or "") in {
                            "POCKET_FILTER_REJECT",
                            "PROFILE_SELECTION_CAP",
                            "PROFILE_DIRECTION_SELECTION_CAP",
                            "ASSET_CLASS_SELECTION_CAP",
                            "BASE_CURRENCY_SELECTION_CAP",
                            "LIVE_POLICY_FILTER_NOT_MET",
                        }
                    ):
                        selection_blocked_rows.append(row_dict)
        except Exception as exc:
            logger.warning("[decision_log] could not load cycle candidate metrics: %s", exc)

        selection_reason_to_decision = {
            "POCKET_FILTER_REJECT": "SKIPPED_POCKET_FILTER",
            "PROFILE_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "PROFILE_DIRECTION_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "ASSET_CLASS_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "BASE_CURRENCY_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "LIVE_POLICY_FILTER_NOT_MET": "SKIPPED_POLICY",
        }
        context_positions_cache: dict[str, list[dict[str, Any]]] = {}
        inserted_meta_gate = 0
        inserted_blocked = 0
        inserted_held = 0

        def _positions_for_profile(profile_id: str) -> list[dict[str, Any]]:
            if profile_id not in context_positions_cache:
                context_positions_cache[profile_id] = self._load_context_positions_snapshot(profile_id)
            return context_positions_cache.get(profile_id) or []

        for rejected in meta_gate_rows:
            profile_id = str(rejected.get("profile_id") or "")
            if not profile_id:
                continue
            profile_cfg = self._runtime_profile_by_id(profile_id) or {"id": profile_id}
            binding = resolve_profile_lane_binding(
                profile_cfg,
                str(rejected.get("asset_class") or "forex"),
                runtime_cfg=self._runtime_config or get_runtime_config(),
                strict=False,
                allow_legacy=binding_allows_legacy(
                    profile_cfg,
                    str(rejected.get("asset_class") or "forex"),
                    runtime_cfg=self._runtime_config or get_runtime_config(),
                ),
            )
            positions = _positions_for_profile(profile_id)
            max_slots = self._get_max_open_positions(profile_id)
            decision_stream_id = str(rejected.get("decision_stream_id") or "")
            symbol = str(rejected.get("symbol") or "").upper()
            direction = str(rejected.get("direction") or "").upper()
            meta_gate_id = str(rejected.get("meta_gate_id") or "")
            decision_reason_json = {
                "source": "meta_gate",
                "meta_gate_id": meta_gate_id,
                "meta_score": rejected.get("meta_score"),
                "meta_pass": 0,
                "conviction": rejected.get("conviction"),
            }
            decision_id = f"meta:{cycle_ts_utc}:{profile_id}:{decision_stream_id}:{symbol}:{direction}"
            _signal_decision_insert(
                self.db_path,
                decision_id=decision_id,
                cycle_ts_utc=cycle_ts_utc,
                profile_id=profile_id,
                symbol=symbol,
                direction=direction,
                asset_class=str(rejected.get("asset_class") or "").lower() or None,
                model_id=str(rejected.get("model_id") or "") or None,
                model_run_id=rejected.get("model_run_id"),
                model_family=rejected.get("model_family"),
                decision_stream_id=decision_stream_id,
                lane_id=rejected.get("lane_id"),
                execution_policy_id=binding.get("execution_policy_id"),
                execution_policy_mode=str(binding.get("execution_mode") or "").lower() or None,
                model_readiness=binding.get("model_readiness"),
                gate_policy=binding.get("gate_policy"),
                predicted_return=rejected.get("predicted_return"),
                open_positions_count=len(positions),
                max_slots=max_slots,
                free_slots=max(max_slots - len(positions), 0) if max_slots > 0 else 0,
                execution_decision="SKIPPED_META_GATE",
                decision_reason_code="META_GATE_FAIL",
                decision_reason_json=decision_reason_json,
                incumbent_snapshot_json=self._serialize_incumbent_snapshot(positions),
            )
            inserted_meta_gate += 1

        for blocked in selection_blocked_rows:
            profile_id = str(blocked.get("profile_id") or "")
            if not profile_id:
                continue
            positions = _positions_for_profile(profile_id)
            asset_class = str(blocked.get("asset_class") or "").lower() or None
            max_slots = self._get_max_open_positions(profile_id)
            execution_policy_id = None
            execution_policy_mode = None
            pair_state_ref = {}
            try:
                pair_state_ref = json.loads(str(blocked.get("pair_state_ref_json") or "{}"))
                if not isinstance(pair_state_ref, dict):
                    pair_state_ref = {}
            except Exception:
                pair_state_ref = {}
            session_bucket = str(pair_state_ref.get("session_bucket") or "").lower() or None
            decision_reason_json = {
                "selection_state": str(blocked.get("selection_state") or ""),
                "route_tier": str(blocked.get("route_tier") or ""),
                "source": "v5_selection",
            }
            try:
                live_policy_reason_json = json.loads(str(blocked.get("live_policy_reason_json") or "{}"))
                if isinstance(live_policy_reason_json, dict) and live_policy_reason_json:
                    decision_reason_json["live_policy"] = live_policy_reason_json
            except Exception:
                pass
            if str(blocked.get("selection_reason") or "") == "LIVE_POLICY_FILTER_NOT_MET":
                blocked_policy_runner_cfg = self._load_execution_policy_runner_config(asset_class)
                blocked_live_policy_id = str(blocked_policy_runner_cfg.get("live_policy_id") or "").strip()
                if blocked_policy_runner_cfg.get("enabled") and blocked_live_policy_id:
                    execution_policy_id = blocked_live_policy_id
                    execution_policy_mode = "live"
            _signal_decision_insert(
                self.db_path,
                cycle_ts_utc=cycle_ts_utc,
                profile_id=profile_id,
                execution_policy_id=execution_policy_id,
                execution_policy_mode=execution_policy_mode,
                symbol=str(blocked.get("symbol") or "").upper(),
                direction=str(blocked.get("direction") or "").upper(),
                asset_class=asset_class,
                session_bucket=session_bucket,
                entry_price=None,
                model_id=str(blocked.get("model_source") or "") or None,
                model_family=blocked.get("model_family"),
                decision_stream_id=blocked.get("decision_stream_id"),
                model_readiness=blocked.get("model_readiness"),
                gate_policy=blocked.get("gate_policy"),
                predicted_return=blocked.get("predicted_return"),
                selection_rank=blocked.get("selection_rank"),
                direction_streak=blocked.get("direction_streak"),
                top_rank_streak=blocked.get("top_rank_streak"),
                strong_prediction_streak=blocked.get("strong_prediction_streak"),
                same_bucket_streak=blocked.get("same_bucket_streak"),
                flip_count_window=blocked.get("flip_count_window"),
                live_followthrough_pips=blocked.get("live_followthrough_pips"),
                live_followthrough_bps=blocked.get("live_followthrough_bps"),
                live_followthrough_state=blocked.get("live_followthrough_state"),
                open_positions_count=len(positions),
                max_slots=max_slots,
                free_slots=max(max_slots - len(positions), 0) if max_slots > 0 else 0,
                execution_decision=selection_reason_to_decision.get(
                    str(blocked.get("selection_reason") or ""),
                    "SKIPPED_SELECTION_CAP",
                ),
                decision_reason_code=str(blocked.get("selection_reason") or ""),
                decision_reason_json=decision_reason_json,
                incumbent_snapshot_json=self._serialize_incumbent_snapshot(positions),
            )
            inserted_blocked += 1

        for row in tradeable_rows:
            if str(row.get("status") or "").upper() == "APPROVED":
                continue
            profile_id = str(row.get("account_id") or "")
            if not profile_id:
                continue
            symbol = str(row.get("symbol") or "").upper()
            direction = str(row.get("direction") or "").upper()
            metrics = candidate_metrics_map.get((profile_id, symbol, direction), {})
            positions = _positions_for_profile(profile_id)
            asset_class = str(
                row.get("asset_class")
                or metrics.get("asset_class")
                or "unknown"
            ).lower() or None
            max_slots = self._get_max_open_positions(profile_id)
            reason_json: list[Any]
            try:
                parsed = json.loads(str(row.get("v6_reasons_json") or "[]"))
                if isinstance(parsed, list):
                    reason_json = parsed
                elif parsed in (None, ""):
                    reason_json = []
                else:
                    reason_json = [parsed]
            except Exception:
                reason_json = []
            decision_reason_json = {
                "source": "vanguard_tradeable_portfolio",
                "status": str(row.get("status") or "").upper(),
                "route_tier": str(metrics.get("route_tier") or ""),
                "selection_state": str(metrics.get("selection_state") or ""),
                "v6_reasons": reason_json,
            }
            pair_state_ref = {}
            try:
                pair_state_ref = json.loads(str(metrics.get("pair_state_ref_json") or "{}"))
                if not isinstance(pair_state_ref, dict):
                    pair_state_ref = {}
            except Exception:
                pair_state_ref = {}
            _signal_decision_insert(
                self.db_path,
                cycle_ts_utc=cycle_ts_utc,
                profile_id=profile_id,
                symbol=symbol,
                direction=direction,
                asset_class=asset_class,
                session_bucket=str(pair_state_ref.get("session_bucket") or "").lower() or None,
                entry_price=row.get("entry_price"),
                model_id=str(row.get("model_source") or metrics.get("model_source") or "") or None,
                model_family=row.get("model_family") or metrics.get("model_family"),
                decision_stream_id=row.get("decision_stream_id") or metrics.get("decision_stream_id"),
                model_readiness=row.get("model_readiness") or metrics.get("model_readiness"),
                gate_policy=row.get("gate_policy") or metrics.get("gate_policy"),
                v6_state=row.get("v6_state"),
                predicted_return=row.get("predicted_return") if row.get("predicted_return") is not None else metrics.get("predicted_return"),
                edge_score=row.get("edge_score"),
                selection_rank=metrics.get("selection_rank", row.get("rank")),
                direction_streak=metrics.get("direction_streak"),
                top_rank_streak=metrics.get("top_rank_streak"),
                strong_prediction_streak=metrics.get("strong_prediction_streak"),
                same_bucket_streak=metrics.get("same_bucket_streak"),
                flip_count_window=metrics.get("flip_count_window"),
                live_followthrough_pips=metrics.get("live_followthrough_pips"),
                live_followthrough_bps=metrics.get("live_followthrough_bps"),
                live_followthrough_state=metrics.get("live_followthrough_state"),
                risk_usd=row.get("risk_dollars"),
                risk_pct=row.get("risk_pct"),
                lot_size=row.get("shares_or_lots"),
                open_positions_count=len(positions),
                max_slots=max_slots,
                free_slots=max(max_slots - len(positions), 0) if max_slots > 0 else 0,
                execution_decision="SKIPPED_POLICY",
                decision_reason_code=str(row.get("rejection_reason") or row.get("status") or "HOLD"),
                decision_reason_json=decision_reason_json,
                incumbent_snapshot_json=self._serialize_incumbent_snapshot(positions),
            )
            inserted_held += 1

        logger.info(
            "[decision_log] cycle=%s emitted meta_gate=%d blocked=%d held=%d for non-approved cycle",
            cycle_ts_utc,
            inserted_meta_gate,
            inserted_blocked,
            inserted_held,
        )
        return {"meta_gate": inserted_meta_gate, "blocked": inserted_blocked, "held": inserted_held}

    def _execute_approved(self, cycle_ts: str | None = None) -> dict[str, Any]:
        """
        Execute approved positions from the latest V6 cycle.

        off/manual/paper → write FORWARD_TRACKED to execution_log (no real orders)
        live             → send via MetaApi/SignalStack, write FILLED or FAILED
        """
        approved = _get_approved_rows(self.db_path, cycle_ts)
        if not approved:
            return

        logger.info(
            "Executing %d approved rows (mode=%s)", len(approved), self.execution_mode
        )

        # Build symbol→asset_class map from vanguard_shortlist for journal rows
        _symbol_asset_class: dict[str, str] = {}
        try:
            with sqlite_conn(self.db_path) as _jcon:
                _jcon.row_factory = sqlite3.Row
                _latest = _jcon.execute(
                    "SELECT MAX(cycle_ts_utc) AS ts FROM vanguard_shortlist"
                ).fetchone()
                if _latest and _latest["ts"]:
                    _jrows = _jcon.execute(
                        "SELECT symbol, asset_class FROM vanguard_shortlist WHERE cycle_ts_utc = ?",
                        (_latest["ts"],),
                    ).fetchall()
                    _symbol_asset_class = {
                        str(r["symbol"]).upper(): str(r["asset_class"] or "unknown").lower()
                        for r in _jrows
                    }
        except Exception as _exc:
            logger.warning("[trade_journal] Could not load asset_class map: %s", _exc)

        # Ensure journal table exists before first write
        try:
            _journal_ensure_table(self.db_path)
            _signal_decision_log_ensure_table(self.db_path)
            _signal_forward_checkpoints_ensure_table(self.db_path)
        except Exception as _exc:
            logger.warning("[trade_journal] ensure_table failed: %s", _exc)

        _candidate_metrics_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        _selection_blocked_rows: list[dict[str, Any]] = []
        try:
            _metrics_cycle = str(approved[0].get("cycle_ts_utc") or cycle_ts or "")
            if _metrics_cycle:
                with sqlite_conn(self.db_path) as _mcon:
                    _mcon.row_factory = sqlite3.Row
                    _mrows = _mcon.execute(
                        """
                        SELECT s.profile_id,
                               s.symbol,
                               s.direction,
                               s.selection_rank,
                               s.global_cycle_rank_abs_pred,
                               s.decision_stream_id,
                               t.predicted_return,
                               t.predicted_move_bps,
                               t.spread_bps,
                               t.cost_bps,
                               t.after_cost_bps,
                               t.metrics_json,
                               t.conviction,
                               s.strong_prediction_streak,
                               s.same_bucket_streak,
                               s.top_rank_streak,
                               s.flip_count_window,
                               s.live_followthrough_pips,
                               s.live_followthrough_bps,
                               s.live_followthrough_state,
                               t.target_horizon_bars,
                               s.direction_streak
                        FROM vanguard_v5_selection s
                        LEFT JOIN vanguard_v5_tradeability t
                          ON t.cycle_ts_utc = s.cycle_ts_utc
                         AND t.profile_id = s.profile_id
                         AND t.symbol = s.symbol
                         AND t.direction = s.direction
                        WHERE s.cycle_ts_utc = ?
                        """,
                        (_metrics_cycle,),
                    ).fetchall()
                    _candidate_metrics_map = {
                        (
                            str(_row["profile_id"] or ""),
                            str(_row["symbol"] or "").upper(),
                            str(_row["direction"] or "").upper(),
                        ): {
                            "selection_rank": _row["selection_rank"],
                            "global_cycle_rank_abs_pred": _row["global_cycle_rank_abs_pred"],
                            "decision_stream_id": _row["decision_stream_id"],
                            "predicted_return": _row["predicted_return"],
                            "predicted_move_bps": _row["predicted_move_bps"],
                            "spread_bps": _row["spread_bps"],
                            "cost_bps": _row["cost_bps"],
                            "after_cost_bps": _row["after_cost_bps"],
                            "metrics_json": _row["metrics_json"],
                            "conviction": _row["conviction"],
                            "strong_prediction_streak": _row["strong_prediction_streak"],
                            "same_bucket_streak": _row["same_bucket_streak"],
                            "top_rank_streak": _row["top_rank_streak"],
                            "flip_count_window": _row["flip_count_window"],
                            "live_followthrough_pips": _row["live_followthrough_pips"],
                            "live_followthrough_bps": _row["live_followthrough_bps"],
                            "live_followthrough_state": _row["live_followthrough_state"],
                            "target_horizon_bars": _row["target_horizon_bars"],
                            "direction_streak": _row["direction_streak"],
                        }
                        for _row in _mrows
                    }
                    _blocked_rows = _mcon.execute(
                        """
                        SELECT s.cycle_ts_utc,
                               s.profile_id,
                               s.symbol,
                               s.asset_class,
                               s.direction,
                               s.selection_rank,
                               s.selection_reason,
                               s.route_tier,
                               s.selection_state,
                               s.live_policy_reason_json,
                               t.predicted_return,
                               t.pair_state_ref_json
                        FROM vanguard_v5_selection s
                        LEFT JOIN vanguard_v5_tradeability t
                          ON t.cycle_ts_utc = s.cycle_ts_utc
                         AND t.profile_id = s.profile_id
                         AND t.symbol = s.symbol
                         AND t.direction = s.direction
                        WHERE s.cycle_ts_utc = ?
                          AND COALESCE(s.selected, 0) = 0
                          AND COALESCE(s.selection_reason, '') IN (
                              'POCKET_FILTER_REJECT',
                              'PROFILE_SELECTION_CAP',
                              'PROFILE_DIRECTION_SELECTION_CAP',
                              'ASSET_CLASS_SELECTION_CAP',
                              'BASE_CURRENCY_SELECTION_CAP',
                              'LIVE_POLICY_FILTER_NOT_MET'
                          )
                        """,
                        (_metrics_cycle,),
                    ).fetchall()
                    _selection_blocked_rows = [dict(_row) for _row in _blocked_rows]
        except Exception as _exc:
            logger.debug("Could not load v5 candidate metrics: %s", _exc)

        submitted = 0
        filled    = 0
        failed    = 0
        forward   = 0
        checkpoint_trade_ids: list[str] = []
        checkpoint_decision_ids: list[str] = []
        cycle_policy_summary: dict[str, Any] = {
            "cycle_ts": str(cycle_ts or approved[0].get("cycle_ts_utc") or ""),
            "live": {
                "policy_id": "",
                "approved": 0,
                "executed": 0,
                "filled": 0,
                "failed": 0,
                "forward_tracked": 0,
                "rejected": 0,
            },
            "shadow": {},
        }
        metaapi_executor_cls = None
        metaapi_symbol_fn = None
        metaapi_symbol_same_fn = None
        metaapi_executors: dict[str, Any] = {}
        gft_execution_state: dict[str, dict[str, Any]] = {}
        metaapi_execution_state: dict[str, dict[str, Any]] = {}
        gft_manual_rows: list[dict[str, Any]] = []
        failure_reasons: Counter[str] = Counter()
        context_positions_cache: dict[str, list[dict[str, Any]]] = {}
        _selection_reason_to_decision = {
            "POCKET_FILTER_REJECT": "SKIPPED_POCKET_FILTER",
            "PROFILE_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "PROFILE_DIRECTION_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "ASSET_CLASS_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "BASE_CURRENCY_SELECTION_CAP": "SKIPPED_SELECTION_CAP",
            "LIVE_POLICY_FILTER_NOT_MET": "SKIPPED_POLICY",
        }

        for _blocked in _selection_blocked_rows:
            _profile_id = str(_blocked.get("profile_id") or "")
            if not _profile_id:
                continue
            if _profile_id not in context_positions_cache:
                context_positions_cache[_profile_id] = self._load_context_positions_snapshot(_profile_id)
            _positions = context_positions_cache.get(_profile_id) or []
            _asset_class = str(_blocked.get("asset_class") or "").lower() or None
            if _profile_id.lower().startswith("gft_") and str((self._account_profiles.get(_profile_id) or {}).get("execution_bridge") or "").lower() == "mt5":
                _max_slots = 3
            else:
                _max_slots = self._get_max_open_positions(_profile_id)
            _pair_state_ref = {}
            try:
                _pair_state_ref = json.loads(str(_blocked.get("pair_state_ref_json") or "{}"))
                if not isinstance(_pair_state_ref, dict):
                    _pair_state_ref = {}
            except Exception:
                _pair_state_ref = {}
            _session_bucket = str(_pair_state_ref.get("session_bucket") or "").lower() or None
            _candidate_metrics = _candidate_metrics_map.get(
                (
                    _profile_id,
                    str(_blocked.get("symbol") or "").upper(),
                    str(_blocked.get("direction") or "").upper(),
                ),
                {},
            )
            _execution_policy_id: str | None = None
            _execution_policy_mode: str | None = None
            _metrics_payload = _candidate_metrics.get("metrics_json") or {}
            if not isinstance(_metrics_payload, dict):
                try:
                    _metrics_payload = json.loads(str(_metrics_payload or "{}"))
                    if not isinstance(_metrics_payload, dict):
                        _metrics_payload = {}
                except Exception:
                    _metrics_payload = {}
            _blocked_entry_price = _blocked.get("entry_price")
            if _blocked_entry_price in (None, ""):
                _blocked_entry_price = _candidate_metrics.get("entry_price")
            if _blocked_entry_price in (None, ""):
                _blocked_entry_price = _metrics_payload.get("live_mid")
            _decision_reason_json = {
                "selection_state": str(_blocked.get("selection_state") or ""),
                "route_tier": str(_blocked.get("route_tier") or ""),
                "source": "v5_selection",
            }
            try:
                _live_policy_reason_json = json.loads(str(_blocked.get("live_policy_reason_json") or "{}"))
                if isinstance(_live_policy_reason_json, dict) and _live_policy_reason_json:
                    _decision_reason_json["live_policy"] = _live_policy_reason_json
            except Exception:
                pass
            _blocked_policy_runner_cfg = self._load_execution_policy_runner_config(_asset_class)
            _blocked_live_policy_id = str(_blocked_policy_runner_cfg.get("live_policy_id") or "").strip()
            if _blocked_policy_runner_cfg.get("enabled") and _blocked_live_policy_id:
                _blocked_live_policy_spec = ((_blocked_policy_runner_cfg.get("policies") or {}).get(_blocked_live_policy_id) or {})
                _blocked_policy_result = self._evaluate_execution_policy(
                    _blocked_live_policy_id,
                    _blocked_live_policy_spec,
                    row=_blocked,
                    asset_class=_asset_class,
                    direction=str(_blocked.get("direction") or "").upper(),
                    session_bucket=_session_bucket,
                    candidate_metrics=_candidate_metrics,
                )
                if bool(_blocked_policy_result.get("approved")):
                    _execution_policy_id = _blocked_live_policy_id
                    _execution_policy_mode = "live"
                    _decision_reason_json = {
                        **(_blocked_policy_result.get("reason_json") or {}),
                        **_decision_reason_json,
                        "policy_id": _blocked_live_policy_id,
                    }
                elif str(_blocked.get("selection_reason") or "") == "LIVE_POLICY_FILTER_NOT_MET":
                    _execution_policy_id = _blocked_live_policy_id
                    _execution_policy_mode = "live"
                    _decision_reason_json = {
                        **_decision_reason_json,
                        "policy_id": _blocked_live_policy_id,
                    }
            _decision_id = _signal_decision_insert(
                self.db_path,
                cycle_ts_utc=str(_blocked.get("cycle_ts_utc") or _metrics_cycle or cycle_ts or ""),
                profile_id=_profile_id,
                execution_policy_id=_execution_policy_id,
                execution_policy_mode=_execution_policy_mode,
                symbol=str(_blocked.get("symbol") or "").upper(),
                direction=str(_blocked.get("direction") or "").upper(),
                asset_class=_asset_class,
                session_bucket=_session_bucket,
                entry_price=float(_blocked_entry_price) if _blocked_entry_price not in (None, "") else None,
                model_id=_candidate_metrics.get("model_id"),
                model_family=_candidate_metrics.get("model_family"),
                decision_stream_id=_candidate_metrics.get("decision_stream_id"),
                predicted_return=_candidate_metrics.get("predicted_return", _blocked.get("predicted_return")),
                selection_rank=_blocked.get("selection_rank"),
                direction_streak=_candidate_metrics.get("direction_streak"),
                top_rank_streak=_candidate_metrics.get("top_rank_streak"),
                strong_prediction_streak=_candidate_metrics.get("strong_prediction_streak"),
                same_bucket_streak=_candidate_metrics.get("same_bucket_streak"),
                flip_count_window=_candidate_metrics.get("flip_count_window"),
                live_followthrough_pips=_candidate_metrics.get("live_followthrough_pips"),
                live_followthrough_bps=_candidate_metrics.get("live_followthrough_bps"),
                live_followthrough_state=_candidate_metrics.get("live_followthrough_state"),
                open_positions_count=len(_positions),
                max_slots=_max_slots,
                free_slots=max(_max_slots - len(_positions), 0) if _max_slots > 0 else 0,
                execution_decision=_selection_reason_to_decision.get(
                    str(_blocked.get("selection_reason") or ""),
                    "SKIPPED_SELECTION_CAP",
                ),
                decision_reason_code=str(_blocked.get("selection_reason") or ""),
                decision_reason_json=_decision_reason_json,
                incumbent_snapshot_json=self._serialize_incumbent_snapshot(_positions),
            )
            checkpoint_decision_ids.append(_decision_id)
        gft_time_exit_rows = self._execute_gft_time_exits()
        throughput_cfg = self._load_throughput_control_config()
        cost_routing_cfg = self._load_cost_aware_routing_config()
        cycle_live_routes_taken = 0
        approved = sorted(
            approved,
            key=lambda _row: self._candidate_priority_sort_key(
                _row,
                _candidate_metrics_map.get(
                    (
                        str(_row.get("account_id") or ""),
                        str(_row.get("symbol") or "").upper(),
                        str(_row.get("direction") or "").upper(),
                    ),
                    {},
                ),
            ),
        )
        for row in gft_time_exit_rows:
            status = str(row.get("status") or "").upper()
            if status == "FILLED":
                filled += 1
            elif status == "FORWARD_TRACKED":
                forward += 1
            else:
                failed += 1

        for row in approved:
            symbol      = row["symbol"]
            direction   = row["direction"]
            account     = row.get("account_id", "")
            _acct_profile = self._get_account_profile(account) or {}
            _execution_bridge = str(_acct_profile.get("execution_bridge") or "").lower()
            raw_shares = row.get("shares_or_lots") or 0
            _fractional_lot_asset = str(row.get("asset_class") or "").lower().strip() != "equity"
            shares = (
                float(raw_shares)
                if _fractional_lot_asset
                or str(account or "").lower().startswith("gft_")
                or "/" in str(symbol or "")
                or _execution_bridge.startswith("mt5_local")
                else int(raw_shares)
            )
            tier        = row.get("tier", "vanguard")
            entry_px    = row.get("entry_price")
            stop_loss   = row.get("stop_loss")
            if stop_loss is None:
                stop_loss = row.get("stop_price")
            take_profit = row.get("take_profit")
            if take_profit is None:
                take_profit = row.get("tp_price")
            scores: dict = {}
            try:
                if row.get("feature_scores"):
                    scores = json.loads(row["feature_scores"])
            except Exception:
                pass

            _asset_class = _symbol_asset_class.get(str(symbol or "").upper(), "unknown")
            timeout_policy = self._timeout_policy_for_cycle_ts(str(row.get("cycle_ts_utc") or cycle_ts or ""), _asset_class)
            session_bucket = str(timeout_policy.get("session_bucket") or "").lower() or None
            candidate_metrics = _candidate_metrics_map.get(
                (str(account or ""), str(symbol or "").upper(), str(direction or "").upper()),
                {},
            )
            _profile_lane = self._profile_lane_for_asset(_acct_profile, _asset_class)
            candidate_stop_pips = self._price_delta_to_pips(symbol, entry_px, stop_loss)
            candidate_tp_pips = self._price_delta_to_pips(symbol, entry_px, take_profit)
            _skip_legacy_forward_track = self._is_model_v2_forex_row(row, _asset_class)
            policy_runner_cfg = self._load_execution_policy_runner_config(_asset_class)
            live_execution_policy_id = ""
            live_execution_policy_mode: str | None = None

            def _record_active_signal_decision(
                *,
                execution_decision: str,
                decision_reason_code: str,
                decision_reason_json: dict[str, Any] | None = None,
                positions: list[dict[str, Any]] | None = None,
                trade_id: str | None = None,
                broker_position_id: str | None = None,
                execution_request_id: str | None = None,
            ) -> str:
                return _record_signal_decision(
                    execution_decision=execution_decision,
                    decision_reason_code=decision_reason_code,
                    decision_reason_json=decision_reason_json,
                    execution_policy_id=live_execution_policy_id or None,
                    execution_policy_mode=live_execution_policy_mode,
                    positions=positions,
                    trade_id=trade_id,
                    broker_position_id=broker_position_id,
                    execution_request_id=execution_request_id,
                )

            def _positions_for_decision(positions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
                if positions is not None:
                    return positions
                if account not in context_positions_cache:
                    context_positions_cache[account] = self._load_context_positions_snapshot(str(account or ""))
                return context_positions_cache.get(account, [])

            def _record_signal_decision(
                *,
                execution_decision: str,
                decision_reason_code: str,
                decision_reason_json: dict[str, Any] | None = None,
                execution_policy_id: str | None = None,
                execution_policy_mode: str | None = None,
                positions: list[dict[str, Any]] | None = None,
                trade_id: str | None = None,
                broker_position_id: str | None = None,
                execution_request_id: str | None = None,
            ) -> str:
                if self._is_test_execution_mode():
                    return ""
                incumbent_positions = _positions_for_decision(positions)
                if account.lower().startswith("gft_") and str((_acct_profile or {}).get("execution_bridge") or "").lower() == "mt5":
                    max_slots = 3
                else:
                    max_slots = self._get_max_open_positions(str(account or ""))
                open_positions_count = len(incumbent_positions)
                free_slots = max(max_slots - open_positions_count, 0) if max_slots > 0 else 0
                decision_id = _signal_decision_insert(
                    self.db_path,
                    cycle_ts_utc=str(row.get("cycle_ts_utc") or cycle_ts or ""),
                    profile_id=str(account or ""),
                    execution_policy_id=execution_policy_id,
                    execution_policy_mode=execution_policy_mode,
                    trade_id=trade_id,
                    broker_position_id=broker_position_id,
                    execution_request_id=execution_request_id,
                    symbol=str(symbol or "").upper(),
                    direction=str(direction or "").upper(),
                    asset_class=_asset_class,
                    session_bucket=session_bucket,
                    entry_price=float(entry_px or 0.0) if entry_px not in (None, "") else None,
                    model_id=str(row.get("model_source") or "") or None,
                    model_family=row.get("model_family") or _infer_model_family(row.get("model_source")),
                    decision_stream_id=(
                        candidate_metrics.get("decision_stream_id")
                        or row.get("decision_stream_id")
                    ),
                    model_readiness=str(row.get("model_readiness") or "") or None,
                    gate_policy=str(row.get("gate_policy") or "") or None,
                    v6_state=str(row.get("v6_state") or "") or None,
                    predicted_return=candidate_metrics.get("predicted_return"),
                    edge_score=row.get("edge_score"),
                    selection_rank=candidate_metrics.get("selection_rank", row.get("rank")),
                    direction_streak=candidate_metrics.get("direction_streak", row.get("direction_streak")),
                    top_rank_streak=candidate_metrics.get("top_rank_streak", row.get("top_rank_streak")),
                    strong_prediction_streak=candidate_metrics.get("strong_prediction_streak", row.get("strong_prediction_streak")),
                    same_bucket_streak=candidate_metrics.get("same_bucket_streak", row.get("same_bucket_streak")),
                    flip_count_window=candidate_metrics.get("flip_count_window", row.get("flip_count_window")),
                    live_followthrough_pips=candidate_metrics.get("live_followthrough_pips", row.get("live_followthrough_pips")),
                    live_followthrough_bps=candidate_metrics.get("live_followthrough_bps", row.get("live_followthrough_bps")),
                    live_followthrough_state=candidate_metrics.get("live_followthrough_state", row.get("live_followthrough_state")),
                    risk_usd=row.get("risk_dollars"),
                    risk_pct=row.get("risk_pct"),
                    lot_size=float(shares or 0.0),
                    stop_pips=candidate_stop_pips,
                    tp_pips=candidate_tp_pips,
                    timeout_policy_minutes=timeout_policy.get("timeout_minutes"),
                    analysis_dedupe_minutes=timeout_policy.get("dedupe_minutes"),
                    open_positions_count=open_positions_count,
                    max_slots=max_slots,
                    free_slots=free_slots,
                    execution_decision=execution_decision,
                    decision_reason_code=decision_reason_code,
                    decision_reason_json=decision_reason_json or {},
                    incumbent_snapshot_json=self._serialize_incumbent_snapshot(incumbent_positions, timeout_policy=timeout_policy),
                )
                checkpoint_decision_ids.append(decision_id)
                return decision_id

            def _record_execution_attempt_fact(
                *,
                attempt_status: str,
                reason_code: str | None = None,
                response_json: dict[str, Any] | None = None,
                broker_order_id: str | None = None,
                broker_position_id: str | None = None,
                execution_bridge: str | None = None,
                trade_id: str | None = None,
            ) -> None:
                if self._is_test_execution_mode():
                    return
                _write_execution_attempt(
                    cycle_ts_utc=str(row.get("cycle_ts_utc") or cycle_ts or ""),
                    profile_id=str(account or ""),
                    decision_stream_id=str(
                        candidate_metrics.get("decision_stream_id")
                        or row.get("decision_stream_id")
                        or ""
                    ),
                    trade_id=trade_id or _trade_id,
                    symbol=str(symbol or "").upper(),
                    direction=str(direction or "").upper(),
                    execution_mode=self.execution_mode,
                    execution_bridge=str(
                        execution_bridge
                        or _execution_bridge
                        or (_acct_profile or {}).get("execution_bridge")
                        or "disabled"
                    ).lower(),
                    attempt_status=attempt_status,
                    reason_code=reason_code,
                    broker_order_id=broker_order_id,
                    broker_position_id=broker_position_id,
                    response_json=response_json or {},
                    db_path=self.db_path,
                )

            _trade_id: Optional[str] = None
            if policy_runner_cfg.get("enabled"):
                available_policies = dict(policy_runner_cfg.get("policies") or {})
                _preferred_live_policy_id = str(
                    _profile_lane.get("execution_policy_id")
                    or row.get("execution_policy_id")
                    or ""
                ).strip()
                _runner_live_policy_id = str(policy_runner_cfg.get("live_policy_id") or "").strip()
                if _preferred_live_policy_id and _preferred_live_policy_id in available_policies:
                    live_execution_policy_id = _preferred_live_policy_id
                else:
                    live_execution_policy_id = _runner_live_policy_id or live_execution_policy_id
                live_execution_policy_mode = "live"
                if live_execution_policy_id:
                    _summary_policy_id = str(cycle_policy_summary["live"].get("policy_id") or "").strip()
                    if _summary_policy_id and _summary_policy_id != live_execution_policy_id:
                        cycle_policy_summary["live"]["policy_id"] = "mixed"
                    else:
                        cycle_policy_summary["live"]["policy_id"] = live_execution_policy_id
                for shadow_policy_id in policy_runner_cfg.get("shadow_policy_ids") or []:
                    cycle_policy_summary["shadow"].setdefault(
                        str(shadow_policy_id),
                        {"approved": 0, "rejected": 0},
                    )
                policy_results: dict[str, dict[str, Any]] = {}
                policy_order = list(policy_runner_cfg.get("policy_order") or [])
                if live_execution_policy_id and live_execution_policy_id in available_policies and live_execution_policy_id not in policy_order:
                    policy_order.insert(0, live_execution_policy_id)
                for policy_id in policy_order:
                    policy_spec = available_policies.get(policy_id) or {}
                    mode = str(policy_spec.get("mode") or ("live" if policy_id == live_execution_policy_id else "shadow")).lower()
                    policy_result = self._evaluate_execution_policy(
                        policy_id,
                        policy_spec,
                        row=row,
                        asset_class=_asset_class,
                        direction=direction,
                        session_bucket=session_bucket,
                        candidate_metrics=candidate_metrics,
                    )
                    policy_result["mode"] = mode
                    policy_result["reason_json"] = {
                        **(policy_result.get("reason_json") or {}),
                        "policy_id": policy_id,
                    }
                    policy_results[policy_id] = policy_result
                    if mode == "shadow":
                        bucket = cycle_policy_summary["shadow"].setdefault(
                            str(policy_id),
                            {"approved": 0, "rejected": 0},
                        )
                        bucket["approved" if policy_result.get("approved") else "rejected"] += 1
                        _record_signal_decision(
                            execution_decision="SHADOW_APPROVED" if policy_result.get("approved") else "SHADOW_REJECTED",
                            decision_reason_code=str(policy_result.get("reason_code") or "POLICY_UNKNOWN"),
                            decision_reason_json=policy_result.get("reason_json") or {},
                            execution_policy_id=str(policy_id),
                            execution_policy_mode="shadow",
                        )
                live_policy_result = policy_results.get(live_execution_policy_id)
                if not live_policy_result:
                    live_policy_result = {
                        "approved": True,
                        "reason_code": "POLICY_RUNNER_BYPASS",
                        "reason_json": {"policy_id": live_execution_policy_id},
                    }
                if not bool(live_policy_result.get("approved")):
                    cycle_policy_summary["live"]["rejected"] += 1
                    _record_signal_decision(
                        execution_decision="SKIPPED_POLICY",
                        decision_reason_code=str(live_policy_result.get("reason_code") or "POLICY_FILTER_NOT_MET"),
                        decision_reason_json=live_policy_result.get("reason_json") or {},
                        execution_policy_id=live_execution_policy_id or None,
                        execution_policy_mode="live",
                    )
                    _record_execution_attempt_fact(
                        attempt_status="SKIPPED_POLICY",
                        reason_code=str(live_policy_result.get("reason_code") or "POLICY_FILTER_NOT_MET"),
                        response_json=live_policy_result.get("reason_json") or {},
                    )
                    continue
                cycle_policy_summary["live"]["approved"] += 1

            if (
                not self._is_manual_execution_mode()
                and self.execution_mode == "live"
                and self._scheduled_flatten_window_active(str(account or ""))
            ):
                _record_active_signal_decision(
                    execution_decision="SKIPPED_POLICY",
                    decision_reason_code="SCHEDULED_FLATTEN_WINDOW",
                    decision_reason_json={
                        "reason": "scheduled_flatten_window",
                        "execution_mode": self.execution_mode,
                        "window_active": True,
                    },
                )
                cycle_policy_summary["live"]["forward_tracked"] += 1
                forward += 1
                _notes = self._scheduled_flatten_window_note(str(account or "")) or "Intentional liquidity guard: live routing disabled during scheduled flatten window"
                if not _skip_legacy_forward_track:
                    _write_execution_row(
                        symbol=symbol,
                        direction=direction,
                        tier=tier,
                        shares=shares,
                        entry_price=entry_px,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        status="FORWARD_TRACKED",
                        response_json={
                            "mode": self.execution_mode,
                            "forward_tracked": True,
                            "execution_bridge": row.get("execution_bridge") or "",
                            "account_id": account,
                            "reason": "scheduled_flatten_window",
                        },
                        account=account,
                        source_scores=scores,
                        tags=["vanguard", "scheduled_flatten_window", str(_asset_class or "unknown")],
                        notes=_notes,
                        db_path=self.db_path,
                    )
                logger.info(
                    "[SCHEDULED FLATTEN WINDOW] %s %s skipped — intentional liquidity guard active for account=%s",
                    symbol,
                    direction,
                    account,
                )
                _record_execution_attempt_fact(
                    attempt_status="FORWARD_TRACKED",
                    reason_code="SCHEDULED_FLATTEN_WINDOW",
                    response_json={
                        "reason": "scheduled_flatten_window",
                        "execution_mode": self.execution_mode,
                        "window_active": True,
                    },
                )
                continue

            # ── Trade journal: insert approval row ─────────────────────────
            # Construct candidate and policy_decision dicts from the approved row.
            # asset_class is looked up from vanguard_shortlist; falls back to inference.
            if not self._is_manual_execution_mode() and not self._is_live_routing_asset_allowed(_asset_class, str(account or "")):
                _record_active_signal_decision(
                    execution_decision="SKIPPED_POLICY",
                    decision_reason_code="LIVE_ASSET_DISABLED",
                    decision_reason_json={
                        "reason": "live_asset_disabled",
                        "asset_class": _asset_class,
                        "execution_mode": self.execution_mode,
                    },
                )
                cycle_policy_summary["live"]["forward_tracked"] += 1
                forward += 1
                _notes = f"live routing disabled for asset_class={_asset_class}"
                if not _skip_legacy_forward_track:
                    _write_execution_row(
                        symbol=symbol,
                        direction=direction,
                        tier=tier,
                        shares=shares,
                        entry_price=entry_px,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        status="FORWARD_TRACKED",
                        response_json={
                            "mode": self.execution_mode,
                            "forward_tracked": True,
                            "execution_bridge": row.get("execution_bridge") or "",
                            "account_id": account,
                            "reason": "live_asset_disabled",
                            "asset_class": _asset_class,
                        },
                        account=account,
                        source_scores=scores,
                        tags=["vanguard", "live_asset_disabled", str(_asset_class or "unknown")],
                        notes=_notes,
                        db_path=self.db_path,
                    )
                logger.info("[LIVE GATE] %s %s skipped — asset_class=%s disabled for live routing", symbol, direction, _asset_class)
                _record_execution_attempt_fact(
                    attempt_status="FORWARD_TRACKED",
                    reason_code="LIVE_ASSET_DISABLED",
                    response_json={
                        "reason": "live_asset_disabled",
                        "asset_class": _asset_class,
                        "execution_mode": self.execution_mode,
                    },
                )
                continue
            # Global manual/test mode always wins. Profiles may only downgrade live routing,
            # not silently override a manual run into live execution.
            _profile_mode = self._profile_execution_mode_for_asset(_acct_profile, _asset_class)
            _profile_auto = _profile_mode == "auto"
            _effective_manual = (
                self._is_manual_execution_mode()
                or (_profile_mode in {"manual", "off", "paper"})
                or not _profile_auto
            )
            if not self._is_manual_execution_mode() and not _effective_manual:
                _cost_routing_state = self._candidate_cost_routing_state(
                    row,
                    candidate_metrics,
                    cost_routing_cfg,
                )
                if cost_routing_cfg.get("enabled") and not bool(_cost_routing_state.get("passes")):
                    cycle_policy_summary["live"]["rejected"] += 1
                    _record_active_signal_decision(
                        execution_decision="SKIPPED_COST_ROUTING",
                        decision_reason_code=str(_cost_routing_state.get("reason_code") or "COST_ROUTING_REJECTED"),
                        decision_reason_json=dict(_cost_routing_state.get("reason_json") or {}),
                    )
                    logger.info(
                        "[COST ROUTING] %s %s skipped — reason=%s",
                        symbol,
                        direction,
                        _cost_routing_state.get("reason_code") or "COST_ROUTING_REJECTED",
                    )
                    _record_execution_attempt_fact(
                        attempt_status="SKIPPED_COST_ROUTING",
                        reason_code=str(_cost_routing_state.get("reason_code") or "COST_ROUTING_REJECTED"),
                        response_json=dict(_cost_routing_state.get("reason_json") or {}),
                    )
                    continue
                _ban_rule = self._match_tactical_ban(
                    asset_class=_asset_class,
                    symbol=symbol,
                    direction=direction,
                )
                if _ban_rule:
                    cycle_policy_summary["live"]["rejected"] += 1
                    _record_active_signal_decision(
                        execution_decision="SKIPPED_TACTICAL_BAN",
                        decision_reason_code="TACTICAL_BAN",
                        decision_reason_json={
                            "reason": str(_ban_rule.get("reason") or "tactical_ban"),
                            "symbol": str(_ban_rule.get("symbol") or symbol),
                            "side": str(_ban_rule.get("side") or direction),
                            "asset_class": _asset_class,
                        },
                    )
                    logger.info(
                        "[TACTICAL BAN] %s %s skipped — reason=%s",
                        symbol,
                        direction,
                        _ban_rule.get("reason") or "tactical_ban",
                    )
                    _record_execution_attempt_fact(
                        attempt_status="SKIPPED_TACTICAL_BAN",
                        reason_code="TACTICAL_BAN",
                        response_json={
                            "reason": str(_ban_rule.get("reason") or "tactical_ban"),
                            "symbol": str(_ban_rule.get("symbol") or symbol),
                            "side": str(_ban_rule.get("side") or direction),
                            "asset_class": _asset_class,
                        },
                    )
                    continue
                if throughput_cfg.get("enabled"):
                    _allow = cycle_live_routes_taken < int(throughput_cfg.get("default_max_new_entries_per_cycle") or 1)
                    if not _allow and cycle_live_routes_taken == int(throughput_cfg.get("default_max_new_entries_per_cycle") or 1):
                        _allow = self._candidate_allows_second_entry(
                            row,
                            candidate_metrics,
                            throughput_cfg,
                        )
                    if not _allow:
                        cycle_policy_summary["live"]["rejected"] += 1
                        _record_active_signal_decision(
                            execution_decision="SKIPPED_THROUGHPUT_PRIORITY",
                            decision_reason_code="THROUGHPUT_LIMIT",
                            decision_reason_json={
                                "routing_limit": self._throughput_mode_summary(),
                                "routes_taken_this_cycle": cycle_live_routes_taken,
                                "directional_conviction": self._directional_conviction_strength(direction, candidate_metrics.get("conviction", row.get("conviction"))),
                                "top_rank_streak": int(candidate_metrics.get("top_rank_streak") or 0),
                            },
                        )
                        logger.info(
                            "[THROUGHPUT LIMIT] %s %s skipped — routes_taken=%d mode=%s",
                            symbol,
                            direction,
                            cycle_live_routes_taken,
                            self._throughput_mode_summary(),
                        )
                        _record_execution_attempt_fact(
                            attempt_status="SKIPPED_THROUGHPUT_PRIORITY",
                            reason_code="THROUGHPUT_LIMIT",
                            response_json={
                                "routing_limit": self._throughput_mode_summary(),
                                "routes_taken_this_cycle": cycle_live_routes_taken,
                                "directional_conviction": self._directional_conviction_strength(direction, candidate_metrics.get("conviction", row.get("conviction"))),
                                "top_rank_streak": int(candidate_metrics.get("top_rank_streak") or 0),
                            },
                        )
                        continue
                    cycle_live_routes_taken += 1
            _lifecycle_policy_id = self._lifecycle_policy_id_for_candidate(
                _asset_class,
                direction,
                session_bucket,
            )
            if not live_execution_policy_id:
                live_execution_policy_id = str(_lifecycle_policy_id or "")
            _candidate = {
                "symbol":        symbol,
                "asset_class":   _asset_class,
                "side":          direction,
                "policy_id":     live_execution_policy_id or _lifecycle_policy_id,
                "decision_stream_id": row.get("decision_stream_id"),
                "entry_price":   entry_px,
                "stop_price":    stop_loss,
                "tp_price":      take_profit,
                "shares_or_lots": shares,
                "model_id":      row.get("model_source"),
                "model_family":  row.get("model_family") or _infer_model_family(row.get("model_source")),
                "conviction":    row.get("conviction"),
                "target_horizon_bars": row.get("target_horizon_bars"),
                "spread_pips":   row.get("spread_pips"),
                "spread_bps":    row.get("spread_bps"),
                "spread_pct":    (
                    float(row.get("spread_bps")) / 100.0
                    if row.get("spread_bps") not in (None, "")
                    else None
                ),
                "original_entry_ref_price": entry_px,
                "original_stop_price": stop_loss,
                "original_tp_price": take_profit,
                "original_risk_dollars": row.get("risk_dollars"),
                "original_r_distance_native": (
                    abs(float(entry_px or 0.0) - float(stop_loss or 0.0))
                    if entry_px not in (None, "") and stop_loss not in (None, "")
                    else None
                ),
                "original_rr_multiple": (
                    abs(float(take_profit or 0.0) - float(entry_px or 0.0))
                    / abs(float(entry_px or 0.0) - float(stop_loss or 0.0))
                    if entry_px not in (None, "")
                    and stop_loss not in (None, "")
                    and take_profit not in (None, "")
                    and abs(float(entry_px or 0.0) - float(stop_loss or 0.0)) > 0
                    else None
                ),
                "original_r_multiple_target": (
                    abs(float(take_profit or 0.0) - float(entry_px or 0.0))
                    / abs(float(entry_px or 0.0) - float(stop_loss or 0.0))
                    if entry_px not in (None, "")
                    and stop_loss not in (None, "")
                    and take_profit not in (None, "")
                    and abs(float(entry_px or 0.0) - float(stop_loss or 0.0)) > 0
                    else None
                ),
            }
            _policy_decision = {
                "decision":       "APPROVED",
                "reject_reason":  None,
                "policy_id":      live_execution_policy_id or _lifecycle_policy_id,
                "lifecycle_policy_id": _lifecycle_policy_id,
                "approved_qty":   shares,
                "approved_sl":    stop_loss,
                "approved_tp":    take_profit,
                "approved_side":  direction,
                "original_qty":   shares,
                "sizing_method":  str(row.get("gate_policy") or ""),
                "notes":          [],
                "edge_score":     row.get("edge_score"),
                "model_readiness": row.get("model_readiness"),
                "model_source":   row.get("model_source"),
                "model_id":       row.get("model_source"),
                "model_family":   row.get("model_family") or _infer_model_family(row.get("model_source")),
                "decision_stream_id": row.get("decision_stream_id"),
                "conviction":     row.get("conviction"),
                "target_horizon_bars": row.get("target_horizon_bars"),
                "risk_pct":       row.get("risk_pct"),
            }
            _cycle_ts = str(row.get("cycle_ts_utc") or "")
            if shares <= 0 and not _effective_manual:
                cycle_policy_summary["live"]["rejected"] += 1
                _record_active_signal_decision(
                    execution_decision="SKIPPED_POLICY",
                    decision_reason_code="ZERO_SIZING",
                    decision_reason_json={
                        "reason": "zero_sizing",
                        "approved_qty": float(shares or 0.0),
                        "profile_execution_mode": _profile_mode,
                    },
                )
                _record_execution_attempt_fact(
                    attempt_status="SKIPPED_POLICY",
                    reason_code="ZERO_SIZING",
                    response_json={
                        "reason": "zero_sizing",
                        "approved_qty": float(shares or 0.0),
                        "profile_execution_mode": _profile_mode,
                    },
                )
                logger.warning("Skipping %s — shares=%s", symbol, shares)
                continue
            _journal_initial_status = (
                "FORWARD_TRACKED"
                if self._is_test_execution_mode() or _effective_manual
                else "PENDING_FILL"
            )
            if not self._is_test_execution_mode():
                try:
                    _trade_id = _journal_insert_approval(
                        db_path=self.db_path,
                        profile_id=str(account or ""),
                        candidate=_candidate,
                        policy_decision=_policy_decision,
                        cycle_ts_utc=_cycle_ts,
                        status=_journal_initial_status,
                    )
                except Exception as _exc:
                    logger.error("[trade_journal] insert_approval_row failed for %s: %s", symbol, _exc)
                if _trade_id:
                    checkpoint_trade_ids.append(_trade_id)
            # ── End trade journal insert ───────────────────────────────────

            # ── Shadow execution log (Phase 5) ─────────────────────────────
            # Writes every would-be-submit in all modes for paper vs real diff.
            if not self._is_test_execution_mode():
                try:
                    from vanguard.execution.shadow_log import ensure_tables as _shadow_ensure, insert_shadow_row as _shadow_insert
                    _shadow_ensure(self.db_path)
                    _shadow_payload = {
                        "symbol": symbol,
                        "direction": direction,
                        "shares": float(shares or 0),
                        "entry_price": float(entry_px or 0),
                        "stop_loss": float(stop_loss or 0),
                        "take_profit": float(take_profit or 0),
                        "account": account,
                        "execution_mode": self.execution_mode,
                        "trade_id": _trade_id,
                        "execution_policy_id": live_execution_policy_id or None,
                    }
                    _shadow_insert(
                        self.db_path,
                        cycle_ts_utc=_cycle_ts,
                        profile_id=str(account or ""),
                        symbol=symbol,
                        side=direction,
                        qty=float(shares or 0),
                        expected_entry=float(entry_px or 0),
                        expected_sl=float(stop_loss or 0),
                        expected_tp=float(take_profit or 0),
                        execution_mode=self.execution_mode,
                        metaapi_payload=_shadow_payload,
                        notes=f"trade_id={_trade_id} policy_id={live_execution_policy_id or ''}",
                    )
                except Exception as _shadow_exc:
                    logger.debug("[shadow_log] insert failed (non-blocking): %s", _shadow_exc)
            # ── End shadow execution log ────────────────────────────────────

            if self._is_test_execution_mode():
                _record_active_signal_decision(
                    execution_decision="EXECUTED",
                    decision_reason_code="TEST_NO_PERSIST",
                    decision_reason_json={
                        "execution_mode": self.execution_mode,
                        "forward_tracked": True,
                    },
                    trade_id=_trade_id,
                )
                cycle_policy_summary["live"]["executed"] += 1
                cycle_policy_summary["live"]["forward_tracked"] += 1
                forward += 1
                _record_execution_attempt_fact(
                    attempt_status="TEST_FORWARD_TRACKED",
                    reason_code="TEST_NO_PERSIST",
                    response_json={
                        "execution_mode": self.execution_mode,
                        "forward_tracked": True,
                    },
                    trade_id=_trade_id,
                )
                logger.info(
                    "[TEST NO_PERSIST] %s %s x%s account=%s trade_id=%s",
                    symbol,
                    direction,
                    shares,
                    account,
                    _trade_id,
                )
                continue

            if _effective_manual:
                _record_active_signal_decision(
                    execution_decision="EXECUTED",
                    decision_reason_code="MANUAL_FORWARD_TRACKED",
                    decision_reason_json={
                        "execution_mode": self.execution_mode,
                        "forward_tracked": True,
                        "profile_execution_mode": _profile_mode,
                    },
                    trade_id=_trade_id,
                )
                cycle_policy_summary["live"]["executed"] += 1
                log_id = None
                if not _skip_legacy_forward_track:
                    log_id = _write_execution_row(
                        symbol=symbol, direction=direction, tier=tier,
                        shares=shares, entry_price=entry_px,
                        stop_loss=stop_loss, take_profit=take_profit,
                        status="FORWARD_TRACKED",
                        response_json={
                            "mode": self.execution_mode,
                            "forward_tracked": True,
                            "manual_execution": True,
                            "account_id": account,
                        },
                        account=account, source_scores=scores,
                        tags=["vanguard", "forward_tracked", "manual_mode"],
                        db_path=self.db_path,
                    )
                forward += 1
                cycle_policy_summary["live"]["forward_tracked"] += 1
                _record_execution_attempt_fact(
                    attempt_status="FORWARD_TRACKED",
                    reason_code="MANUAL_FORWARD_TRACKED",
                    response_json={
                        "execution_mode": self.execution_mode,
                        "forward_tracked": True,
                        "manual_execution": True,
                        "account_id": account,
                    },
                    trade_id=_trade_id,
                )
                logger.info(
                    "[MANUAL FORWARD_TRACKED] %s %s x%s account=%s (legacy_execution_log_id=%s)",
                    symbol,
                    direction,
                    shares,
                    account,
                    log_id,
                )

            else:
                submitted += 1
                cycle_policy_summary["live"]["executed"] += 1
                # _acct_profile already fetched above; reuse it here.
                profile = _acct_profile
                bridge = str((profile or {}).get("execution_bridge") or "disabled").lower()
                webhook_url = str((profile or {}).get("webhook_url") or "").strip()
                _decision_id: str | None = None

                if account.lower().startswith("gft_") and bridge == "mt5":
                    # MetaApi credentials:
                    #   export METAAPI_TOKEN="..."
                    #   export GFT_ACCOUNT_ID="..."
                    # Optional per-account overrides:
                    #   export GFT_5K_ACCOUNT_ID="..."
                    #   export GFT_10K_ACCOUNT_ID="..."
                    try:
                        if metaapi_executor_cls is None or metaapi_symbol_fn is None:
                            from vanguard.executors.mt5_executor import (
                                MetaApiExecutor as metaapi_executor_cls,
                                _mt5_symbol as metaapi_symbol_fn,
                            )
                        metaapi_executor = metaapi_executors.get(account)
                        if metaapi_executor is None:
                            metaapi_executor = metaapi_executor_cls(account_key=account)
                            metaapi_executors[account] = metaapi_executor

                        if metaapi_executor.connect():
                            state = gft_execution_state.get(account)
                            if state is None:
                                open_positions = metaapi_executor.get_open_positions()
                                if not open_positions:
                                    fallback_positions = self._load_context_positions_snapshot(str(account or ""))
                                    if fallback_positions:
                                        open_positions = fallback_positions
                                        logger.warning(
                                            "[GFT METAAPI] %s positions fallback -> context snapshot count=%d bridge=%s",
                                            account,
                                            len(open_positions),
                                            bridge,
                                        )
                                if not open_positions:
                                    fallback_positions = self._load_open_journal_positions_snapshot(str(account or ""))
                                    if fallback_positions:
                                        open_positions = fallback_positions
                                        logger.warning(
                                            "[GFT METAAPI] %s positions fallback -> trade journal count=%d bridge=%s",
                                            account,
                                            len(open_positions),
                                            bridge,
                                        )
                                open_symbols = {
                                    str(position.get("symbol") or "").upper().strip()
                                    for position in open_positions
                                    if str(position.get("symbol") or "").strip()
                                }
                                state = {
                                    "open_symbols": open_symbols,
                                    "remaining_slots": max(0, 3 - len(open_positions)),
                                    "open_positions": [
                                        {
                                            "ticket": str(position.get("ticket") or position.get("id") or position.get("positionId") or ""),
                                            "broker_position_id": str(position.get("positionId") or position.get("position_id") or position.get("id") or ""),
                                            "symbol": str(position.get("symbol") or "").upper().strip(),
                                            "direction": str(position.get("type") or position.get("direction") or position.get("side") or "").upper().replace("_", ""),
                                            "qty": position.get("volume"),
                                            "unrealized_pnl": position.get("profit"),
                                            "holding_minutes": position.get("holding_minutes"),
                                        }
                                        for position in open_positions
                                    ],
                                }
                                gft_execution_state[account] = state
                                logger.info(
                                    "[GFT METAAPI] %s open_positions=%d remaining_slots=%d",
                                    account,
                                    len(open_symbols),
                                    state["remaining_slots"],
                                )

                            mapped_symbol = metaapi_symbol_fn(symbol) if metaapi_symbol_fn else symbol
                            if mapped_symbol in state["open_symbols"]:
                                _record_active_signal_decision(
                                    execution_decision="SKIPPED_DUPLICATE_SYMBOL_OPEN",
                                    decision_reason_code="METAAPI_SYMBOL_ALREADY_OPEN",
                                    decision_reason_json={
                                        "execution_bridge": bridge,
                                        "broker_symbol": mapped_symbol,
                                    },
                                    positions=list(state.get("open_positions") or []),
                                    trade_id=_trade_id,
                                )
                                cycle_policy_summary["live"]["forward_tracked"] += 1
                                forward += 1
                                if not _skip_legacy_forward_track:
                                    _write_execution_row(
                                        symbol=symbol, direction=direction, tier=tier,
                                        shares=shares, entry_price=entry_px,
                                        stop_loss=stop_loss, take_profit=take_profit,
                                        status="FORWARD_TRACKED",
                                        response_json={
                                            "mode": self.execution_mode,
                                            "forward_tracked": True,
                                            "execution_bridge": bridge,
                                            "account_id": account,
                                            "reason": "metaapi_symbol_already_open",
                                            "broker_symbol": mapped_symbol,
                                        },
                                        account=account, source_scores=scores,
                                        tags=["vanguard", "gft_metaapi", "already_open"],
                                        db_path=self.db_path,
                                    )
                                logger.info(
                                    "[GFT METAAPI] %s %s skipped — already open (%s)",
                                    symbol,
                                    direction,
                                    mapped_symbol,
                                )
                                _record_execution_attempt_fact(
                                    attempt_status="FORWARD_TRACKED",
                                    reason_code="METAAPI_SYMBOL_ALREADY_OPEN",
                                    response_json={"broker_symbol": mapped_symbol},
                                    trade_id=_trade_id,
                                )
                                if _trade_id:
                                    try:
                                        _journal_update_skipped_before_submit(
                                            self.db_path,
                                            _trade_id,
                                            f"metaapi_symbol_already_open:{mapped_symbol}",
                                        )
                                    except Exception as _jexc:
                                        logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                                continue

                            if state["remaining_slots"] <= 0:
                                _record_active_signal_decision(
                                    execution_decision="SKIPPED_NO_CAPACITY",
                                    decision_reason_code="METAAPI_MAX_POSITIONS_REACHED",
                                    decision_reason_json={
                                        "execution_bridge": bridge,
                                        "broker_open_symbols": sorted(state["open_symbols"]),
                                    },
                                    positions=list(state.get("open_positions") or []),
                                    trade_id=_trade_id,
                                )
                                cycle_policy_summary["live"]["forward_tracked"] += 1
                                forward += 1
                                if not _skip_legacy_forward_track:
                                    _write_execution_row(
                                        symbol=symbol, direction=direction, tier=tier,
                                        shares=shares, entry_price=entry_px,
                                        stop_loss=stop_loss, take_profit=take_profit,
                                        status="FORWARD_TRACKED",
                                        response_json={
                                            "mode": self.execution_mode,
                                            "forward_tracked": True,
                                            "execution_bridge": bridge,
                                            "account_id": account,
                                            "reason": "metaapi_max_positions_reached",
                                            "broker_open_symbols": sorted(state["open_symbols"]),
                                        },
                                        account=account, source_scores=scores,
                                        tags=["vanguard", "gft_metaapi", "max_positions"],
                                        db_path=self.db_path,
                                    )
                                logger.info(
                                    "[GFT METAAPI] %s %s skipped — max positions reached for %s",
                                    symbol,
                                    direction,
                                    account,
                                )
                                _record_execution_attempt_fact(
                                    attempt_status="FORWARD_TRACKED",
                                    reason_code="METAAPI_MAX_POSITIONS_REACHED",
                                    response_json={
                                        "broker_open_symbols": sorted(state["open_symbols"]),
                                    },
                                    trade_id=_trade_id,
                                )
                                if _trade_id:
                                    try:
                                        _journal_update_skipped_before_submit(
                                            self.db_path,
                                            _trade_id,
                                            "metaapi_max_positions_reached",
                                        )
                                    except Exception as _jexc:
                                        logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                                continue

                            _decision_id = _record_active_signal_decision(
                                execution_decision="EXECUTED",
                                decision_reason_code="METAAPI_SUBMIT",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                    "broker_symbol": mapped_symbol,
                                },
                                positions=list(state.get("open_positions") or []),
                                trade_id=_trade_id,
                            )
                            resp = metaapi_executor.execute_trade(
                                symbol=symbol,
                                direction=direction,
                                lot_size=float(shares),
                                stop_loss=stop_loss,
                                take_profit=take_profit,
                            )
                            status = "FILLED" if resp.get("status") == "filled" else "FAILED"
                            if status == "FILLED":
                                filled += 1
                                cycle_policy_summary["live"]["filled"] += 1
                            else:
                                failed += 1
                                cycle_policy_summary["live"]["failed"] += 1
                                failure_reasons[str(resp.get("error") or "unknown")] += 1
                            _write_execution_row(
                                symbol=symbol, direction=direction, tier=tier,
                                shares=shares, entry_price=resp.get("price") or entry_px,
                                stop_loss=stop_loss, take_profit=take_profit,
                                status=status, response_json=resp,
                                account=account, source_scores=scores,
                                db_path=self.db_path,
                            )
                            if status == "FILLED" and _decision_id:
                                _signal_decision_update(
                                    self.db_path,
                                    _decision_id,
                                    trade_id=_trade_id,
                                    broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                )
                            # ── Trade journal: update fill/rejection ───────
                            if _trade_id:
                                try:
                                    _now_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                    _broker_order_id = str(resp.get("order_id") or resp.get("id") or "")
                                    if _broker_order_id:
                                        _journal_update_submitted(
                                            self.db_path, _trade_id, _broker_order_id, _now_s
                                        )
                                    if status == "FILLED":
                                        _signal_decision_update(
                                            self.db_path,
                                            _decision_id,
                                            trade_id=_trade_id,
                                            broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                        )
                                        _journal_update_filled(
                                            db_path=self.db_path,
                                            trade_id=_trade_id,
                                            broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                            fill_price=float(resp.get("price") or entry_px or 0.0),
                                            fill_qty=float(
                                                resp.get("submitted_lots")
                                                or resp.get("filled_qty")
                                                or resp.get("qty")
                                                or (resp.get("request") or {}).get("volume")
                                                or shares
                                            ),
                                            filled_at_utc=_now_s,
                                            expected_entry=float(entry_px or 0.0),
                                            side=direction,
                                        )
                                    else:
                                        _signal_decision_update(
                                            self.db_path,
                                            _decision_id,
                                            trade_id=_trade_id,
                                            execution_decision="EXECUTION_FAILED",
                                            decision_reason_code="BROKER_REJECTED",
                                            decision_reason_json={
                                                "error": str(resp.get("error") or "broker_rejected"),
                                                "execution_bridge": bridge,
                                            },
                                        )
                                        _journal_update_rejected(
                                            self.db_path, _trade_id,
                                            str(resp.get("error") or "broker_rejected"),
                                        )
                                except Exception as _jexc:
                                    logger.warning("[trade_journal] post-fill update failed: %s", _jexc)
                            _record_execution_attempt_fact(
                                attempt_status=status,
                                reason_code=(
                                    "METAAPI_SUBMIT_FILLED"
                                    if status == "FILLED"
                                    else str(resp.get("error") or "broker_rejected")
                                ),
                                response_json=resp,
                                broker_order_id=str(resp.get("order_id") or resp.get("id") or ""),
                                broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                execution_bridge=bridge,
                                trade_id=_trade_id,
                            )
                            # ── End trade journal update ───────────────────
                            logger.info(
                                "[GFT METAAPI] %s %s x%s stop=%s tp=%s -> %s",
                                symbol,
                                direction,
                                shares,
                                stop_loss,
                                take_profit,
                                resp.get("status"),
                            )
                            if status == "FILLED":
                                state["open_symbols"].add(mapped_symbol)
                                state["remaining_slots"] = max(0, int(state["remaining_slots"]) - 1)
                                state.setdefault("open_positions", []).append(
                                    {
                                        "ticket": str(resp.get("order_id") or resp.get("id") or ""),
                                        "broker_position_id": str(resp.get("position_id") or resp.get("id") or ""),
                                        "symbol": str(mapped_symbol or symbol).upper(),
                                        "direction": str(direction or "").upper(),
                                        "qty": float(shares),
                                        "unrealized_pnl": 0.0,
                                        "holding_minutes": 0.0,
                                    }
                                )
                        else:
                            _record_active_signal_decision(
                                execution_decision="SKIPPED_POLICY",
                                decision_reason_code="METAAPI_NOT_CONNECTED",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                    "forward_tracked": True,
                                },
                                trade_id=_trade_id,
                            )
                            cycle_policy_summary["live"]["forward_tracked"] += 1
                            forward += 1
                            log_id = None
                            if not _skip_legacy_forward_track:
                                log_id = _write_execution_row(
                                    symbol=symbol, direction=direction, tier=tier,
                                    shares=shares, entry_price=entry_px,
                                    stop_loss=stop_loss, take_profit=take_profit,
                                    status="FORWARD_TRACKED",
                                    response_json={
                                        "mode": self.execution_mode,
                                        "forward_tracked": True,
                                        "execution_bridge": bridge,
                                        "account_id": account,
                                        "error": "metaapi_connect_failed",
                                    },
                                    account=account, source_scores=scores,
                                    tags=["vanguard", "gft_metaapi", "forward_tracked"],
                                    db_path=self.db_path,
                                )
                            gft_manual_rows.append(
                                {
                                    "account": account,
                                    "symbol": symbol,
                                    "direction": direction,
                                    "shares": shares,
                                    "entry_price": entry_px,
                                    "stop_loss": stop_loss,
                                    "take_profit": take_profit,
                                    "edge_score": row.get("edge_score"),
                                    "log_id": log_id,
                                }
                            )
                            logger.warning(
                                "[GFT METAAPI] %s %s forward-tracked because MetaApi connect failed",
                                symbol,
                                direction,
                            )
                            _record_execution_attempt_fact(
                                attempt_status="FORWARD_TRACKED",
                                reason_code="METAAPI_NOT_CONNECTED",
                                response_json={"error": "metaapi_connect_failed"},
                                execution_bridge=bridge,
                                trade_id=_trade_id,
                            )
                            if _trade_id:
                                try:
                                    _journal_update_skipped_before_submit(
                                        self.db_path,
                                        _trade_id,
                                        "metaapi_connect_failed",
                                    )
                                except Exception as _jexc:
                                    logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                    except Exception as exc:
                        if _decision_id:
                            _signal_decision_update(
                                self.db_path,
                                _decision_id,
                                trade_id=_trade_id,
                                execution_decision="EXECUTION_FAILED",
                                decision_reason_code="METAAPI_EXECUTION_EXCEPTION",
                                decision_reason_json={"error": str(exc), "execution_bridge": bridge},
                            )
                        failed += 1
                        cycle_policy_summary["live"]["failed"] += 1
                        failure_reasons[str(exc)] += 1
                        if _trade_id:
                            try:
                                _journal_update_skipped_before_submit(
                                    self.db_path,
                                    _trade_id,
                                    f"metaapi_execution_exception:{exc}",
                                )
                            except Exception as _jexc:
                                logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                        logger.error("MetaApi execution failed: %s", exc)
                        _write_execution_row(
                            symbol=symbol, direction=direction, tier=tier,
                            shares=shares, entry_price=None,
                            stop_loss=stop_loss, take_profit=take_profit,
                            status="FAILED",
                            response_json={"error": str(exc), "execution_bridge": bridge, "account_id": account},
                            account=account, source_scores=scores,
                            db_path=self.db_path,
                        )
                        _record_execution_attempt_fact(
                            attempt_status="FAILED",
                            reason_code="METAAPI_EXECUTION_EXCEPTION",
                            response_json={"error": str(exc), "execution_bridge": bridge},
                            execution_bridge=bridge,
                            trade_id=_trade_id,
                        )
                    continue

                if bridge.startswith("metaapi_"):
                    try:
                        if metaapi_executor_cls is None or metaapi_symbol_same_fn is None:
                            from vanguard.executors.mt5_executor import MetaApiExecutor as metaapi_executor_cls
                            from vanguard.symbols import is_same_canonical as metaapi_symbol_same_fn
                        bridges_cfg = (((self._runtime_config or {}).get("execution") or {}).get("bridges") or {})
                        bridge_cfg = dict(bridges_cfg.get(bridge) or {})
                        metaapi_executor = metaapi_executors.get(account)
                        if metaapi_executor is None:
                            account_key = bridge[len("metaapi_") :] if bridge.startswith("metaapi_") else account
                            metaapi_executor = metaapi_executor_cls(
                                account_key=account_key,
                                account_id=str(bridge_cfg.get("account_id") or ""),
                                token=str(bridge_cfg.get("api_token") or ""),
                                base_url=str(bridge_cfg.get("base_url") or ""),
                                timeout=float(bridge_cfg.get("timeout_s") or 30),
                            )
                            metaapi_executors[account] = metaapi_executor

                        if metaapi_executor.connect():
                            state = metaapi_execution_state.get(account)
                            if state is None:
                                open_positions = metaapi_executor.get_open_positions()
                                if not open_positions:
                                    fallback_positions = self._load_context_positions_snapshot(str(account or ""))
                                    if fallback_positions:
                                        open_positions = fallback_positions
                                        logger.warning(
                                            "[METAAPI] %s positions fallback -> context snapshot count=%d bridge=%s",
                                            account,
                                            len(open_positions),
                                            bridge,
                                        )
                                if not open_positions:
                                    fallback_positions = self._load_open_journal_positions_snapshot(str(account or ""))
                                    if fallback_positions:
                                        open_positions = fallback_positions
                                        logger.warning(
                                            "[METAAPI] %s positions fallback -> trade journal count=%d bridge=%s",
                                            account,
                                            len(open_positions),
                                            bridge,
                                        )
                                open_symbols = {
                                    str(position.get("symbol") or "").upper().strip()
                                    for position in open_positions
                                    if str(position.get("symbol") or "").strip()
                                }
                                max_slots = self._get_max_open_positions(str(account or ""))
                                state = {
                                    "open_symbols": open_symbols,
                                    "remaining_slots": max(max_slots - len(open_positions), 0) if max_slots > 0 else 0,
                                    "open_positions": [
                                        {
                                            "ticket": str(position.get("ticket") or position.get("id") or position.get("positionId") or ""),
                                            "broker_position_id": str(position.get("position_id") or position.get("positionId") or position.get("id") or ""),
                                            "symbol": str(position.get("symbol") or "").upper().strip(),
                                            "direction": str(position.get("side") or position.get("direction") or "").upper().replace("_", ""),
                                            "qty": position.get("qty") if position.get("qty") is not None else position.get("volume"),
                                            "unrealized_pnl": position.get("pnl") if position.get("pnl") is not None else position.get("profit"),
                                            "holding_minutes": position.get("holding_minutes"),
                                        }
                                        for position in open_positions
                                    ],
                                }
                                metaapi_execution_state[account] = state
                                logger.info(
                                    "[METAAPI] %s open_positions=%d remaining_slots=%d bridge=%s",
                                    account,
                                    len(open_positions),
                                    state["remaining_slots"],
                                    bridge,
                                )

                            if any(
                                metaapi_symbol_same_fn(position.get("symbol"), symbol)
                                for position in (state.get("open_positions") or [])
                            ):
                                _record_active_signal_decision(
                                    execution_decision="SKIPPED_DUPLICATE_SYMBOL_OPEN",
                                    decision_reason_code="METAAPI_SYMBOL_ALREADY_OPEN",
                                    decision_reason_json={
                                        "execution_bridge": bridge,
                                    },
                                    positions=list(state.get("open_positions") or []),
                                    trade_id=_trade_id,
                                )
                                cycle_policy_summary["live"]["forward_tracked"] += 1
                                forward += 1
                                if not _skip_legacy_forward_track:
                                    _write_execution_row(
                                        symbol=symbol, direction=direction, tier=tier,
                                        shares=shares, entry_price=entry_px,
                                        stop_loss=stop_loss, take_profit=take_profit,
                                        status="FORWARD_TRACKED",
                                        response_json={
                                            "mode": self.execution_mode,
                                            "forward_tracked": True,
                                            "execution_bridge": bridge,
                                            "account_id": account,
                                            "reason": "metaapi_symbol_already_open",
                                        },
                                        account=account, source_scores=scores,
                                        tags=["vanguard", "metaapi", "already_open"],
                                        db_path=self.db_path,
                                    )
                                logger.info(
                                    "[METAAPI] %s %s skipped — already open",
                                    symbol,
                                    direction,
                                )
                                if _trade_id:
                                    try:
                                        _journal_update_skipped_before_submit(
                                            self.db_path,
                                            _trade_id,
                                            "metaapi_symbol_already_open",
                                        )
                                    except Exception as _jexc:
                                        logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                                continue

                            if state["remaining_slots"] <= 0:
                                _record_active_signal_decision(
                                    execution_decision="SKIPPED_NO_CAPACITY",
                                    decision_reason_code="METAAPI_MAX_POSITIONS_REACHED",
                                    decision_reason_json={
                                        "execution_bridge": bridge,
                                        "broker_open_symbols": sorted(state["open_symbols"]),
                                    },
                                    positions=list(state.get("open_positions") or []),
                                    trade_id=_trade_id,
                                )
                                cycle_policy_summary["live"]["forward_tracked"] += 1
                                forward += 1
                                if not _skip_legacy_forward_track:
                                    _write_execution_row(
                                        symbol=symbol, direction=direction, tier=tier,
                                        shares=shares, entry_price=entry_px,
                                        stop_loss=stop_loss, take_profit=take_profit,
                                        status="FORWARD_TRACKED",
                                        response_json={
                                            "mode": self.execution_mode,
                                            "forward_tracked": True,
                                            "execution_bridge": bridge,
                                            "account_id": account,
                                            "reason": "metaapi_max_positions_reached",
                                            "broker_open_symbols": sorted(state["open_symbols"]),
                                        },
                                        account=account, source_scores=scores,
                                        tags=["vanguard", "metaapi", "max_positions"],
                                        db_path=self.db_path,
                                    )
                                logger.info(
                                    "[METAAPI] %s %s skipped — max positions reached for %s",
                                    symbol,
                                    direction,
                                    account,
                                )
                                _record_execution_attempt_fact(
                                    attempt_status="FORWARD_TRACKED",
                                    reason_code="METAAPI_MAX_POSITIONS_REACHED",
                                    response_json={
                                        "broker_open_symbols": sorted(state["open_symbols"]),
                                    },
                                    execution_bridge=bridge,
                                    trade_id=_trade_id,
                                )
                                if _trade_id:
                                    try:
                                        _journal_update_skipped_before_submit(
                                            self.db_path,
                                            _trade_id,
                                            "metaapi_max_positions_reached",
                                        )
                                    except Exception as _jexc:
                                        logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                                continue

                            _decision_id = _record_active_signal_decision(
                                execution_decision="EXECUTED",
                                decision_reason_code="METAAPI_SUBMIT",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                },
                                positions=list(state.get("open_positions") or []),
                                trade_id=_trade_id,
                            )
                            resp = metaapi_executor.execute_trade(
                                symbol=symbol,
                                direction=direction,
                                lot_size=float(shares),
                                stop_loss=stop_loss,
                                take_profit=take_profit,
                            )
                            status = "FILLED" if resp.get("status") == "filled" else "FAILED"
                            if status == "FILLED":
                                filled += 1
                                cycle_policy_summary["live"]["filled"] += 1
                            else:
                                failed += 1
                                cycle_policy_summary["live"]["failed"] += 1
                                failure_reasons[str(resp.get("error") or "unknown")] += 1
                            _write_execution_row(
                                symbol=symbol, direction=direction, tier=tier,
                                shares=shares, entry_price=resp.get("price") or entry_px,
                                stop_loss=stop_loss, take_profit=take_profit,
                                status=status, response_json=resp,
                                account=account, source_scores=scores,
                                db_path=self.db_path,
                            )
                            if status == "FILLED" and _decision_id:
                                _signal_decision_update(
                                    self.db_path,
                                    _decision_id,
                                    trade_id=_trade_id,
                                    broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                )
                            if _trade_id:
                                try:
                                    _now_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                    _broker_order_id = str(resp.get("order_id") or resp.get("id") or "")
                                    if _broker_order_id:
                                        _journal_update_submitted(
                                            self.db_path, _trade_id, _broker_order_id, _now_s
                                        )
                                    if status == "FILLED":
                                        _signal_decision_update(
                                            self.db_path,
                                            _decision_id,
                                            trade_id=_trade_id,
                                            broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                        )
                                        _journal_update_filled(
                                            db_path=self.db_path,
                                            trade_id=_trade_id,
                                            broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                            fill_price=float(resp.get("price") or entry_px or 0.0),
                                            fill_qty=float(
                                                resp.get("submitted_lots")
                                                or resp.get("filled_qty")
                                                or resp.get("qty")
                                                or (resp.get("request") or {}).get("volume")
                                                or shares
                                            ),
                                            filled_at_utc=_now_s,
                                            expected_entry=float(entry_px or 0.0),
                                            side=direction,
                                        )
                                    else:
                                        _signal_decision_update(
                                            self.db_path,
                                            _decision_id,
                                            trade_id=_trade_id,
                                            execution_decision="EXECUTION_FAILED",
                                            decision_reason_code="BROKER_REJECTED",
                                            decision_reason_json={
                                                "error": str(resp.get("error") or "broker_rejected"),
                                                "execution_bridge": bridge,
                                            },
                                        )
                                        _journal_update_rejected(
                                            self.db_path, _trade_id,
                                            str(resp.get("error") or "broker_rejected"),
                                        )
                                except Exception as _jexc:
                                    logger.warning("[trade_journal] post-fill update failed: %s", _jexc)
                            _record_execution_attempt_fact(
                                attempt_status=status,
                                reason_code=(
                                    "METAAPI_SUBMIT_FILLED"
                                    if status == "FILLED"
                                    else str(resp.get("error") or "broker_rejected")
                                ),
                                response_json=resp,
                                broker_order_id=str(resp.get("order_id") or resp.get("id") or ""),
                                broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                execution_bridge=bridge,
                                trade_id=_trade_id,
                            )
                            logger.info(
                                "[METAAPI] %s %s x%s stop=%s tp=%s -> %s",
                                symbol,
                                direction,
                                shares,
                                stop_loss,
                                take_profit,
                                resp.get("status"),
                            )
                            if status == "FILLED":
                                state["open_symbols"].add(str(resp.get("symbol") or symbol).upper())
                                state["remaining_slots"] = max(0, int(state["remaining_slots"]) - 1)
                                state.setdefault("open_positions", []).append(
                                    {
                                        "ticket": str(resp.get("order_id") or resp.get("id") or ""),
                                        "broker_position_id": str(resp.get("position_id") or resp.get("id") or ""),
                                        "symbol": str(resp.get("symbol") or symbol).upper(),
                                        "direction": str(direction or "").upper(),
                                        "qty": float(shares),
                                        "unrealized_pnl": 0.0,
                                        "holding_minutes": 0.0,
                                    }
                                )
                        else:
                            _record_active_signal_decision(
                                execution_decision="SKIPPED_POLICY",
                                decision_reason_code="METAAPI_NOT_CONNECTED",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                    "forward_tracked": True,
                                },
                                trade_id=_trade_id,
                            )
                            cycle_policy_summary["live"]["forward_tracked"] += 1
                            forward += 1
                            if not _skip_legacy_forward_track:
                                _write_execution_row(
                                    symbol=symbol, direction=direction, tier=tier,
                                    shares=shares, entry_price=entry_px,
                                    stop_loss=stop_loss, take_profit=take_profit,
                                    status="FORWARD_TRACKED",
                                    response_json={
                                        "mode": self.execution_mode,
                                        "forward_tracked": True,
                                        "execution_bridge": bridge,
                                        "account_id": account,
                                        "error": "metaapi_connect_failed",
                                    },
                                    account=account, source_scores=scores,
                                    tags=["vanguard", "metaapi", "forward_tracked"],
                                    db_path=self.db_path,
                                )
                            logger.warning(
                                "[METAAPI] %s %s forward-tracked because MetaApi connect failed",
                                symbol,
                                direction,
                            )
                            _record_execution_attempt_fact(
                                attempt_status="FORWARD_TRACKED",
                                reason_code="METAAPI_NOT_CONNECTED",
                                response_json={"error": "metaapi_connect_failed"},
                                execution_bridge=bridge,
                                trade_id=_trade_id,
                            )
                            if _trade_id:
                                try:
                                    _journal_update_skipped_before_submit(
                                        self.db_path,
                                        _trade_id,
                                        "metaapi_connect_failed",
                                    )
                                except Exception as _jexc:
                                    logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                    except Exception as exc:
                        if _decision_id:
                            _signal_decision_update(
                                self.db_path,
                                _decision_id,
                                trade_id=_trade_id,
                                execution_decision="EXECUTION_FAILED",
                                decision_reason_code="METAAPI_EXECUTION_EXCEPTION",
                                decision_reason_json={"error": str(exc), "execution_bridge": bridge},
                            )
                        failed += 1
                        cycle_policy_summary["live"]["failed"] += 1
                        failure_reasons[str(exc)] += 1
                        if _trade_id:
                            try:
                                _journal_update_skipped_before_submit(
                                    self.db_path,
                                    _trade_id,
                                    f"metaapi_execution_exception:{exc}",
                                )
                            except Exception as _jexc:
                                logger.warning("[trade_journal] skip-before-submit update failed: %s", _jexc)
                        logger.error("MetaApi execution failed: %s", exc)
                        _write_execution_row(
                            symbol=symbol, direction=direction, tier=tier,
                            shares=shares, entry_price=None,
                            stop_loss=stop_loss, take_profit=take_profit,
                            status="FAILED",
                            response_json={"error": str(exc), "execution_bridge": bridge, "account_id": account},
                            account=account, source_scores=scores,
                            db_path=self.db_path,
                        )
                        _record_execution_attempt_fact(
                            attempt_status="FAILED",
                            reason_code="METAAPI_EXECUTION_EXCEPTION",
                            response_json={"error": str(exc), "execution_bridge": bridge},
                            execution_bridge=bridge,
                            trade_id=_trade_id,
                        )
                    continue

                # ── DWX local bridge (mt5_local_*) ─────────────────────────
                if bridge.startswith("mt5_local") and self._mt5_adapter and self._mt5_adapter.is_connected():
                    try:
                        from vanguard.executors.dwx_executor import DWXExecutor  # noqa: PLC0415
                        dwx_exec = DWXExecutor(
                            mt5_adapter=self._mt5_adapter,
                            profile_id=account,
                            db_path=self.db_path,
                        )
                        if not dwx_exec.connect():
                            _record_active_signal_decision(
                                execution_decision="SKIPPED_POLICY",
                                decision_reason_code="DWX_NOT_CONNECTED",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                    "forward_tracked": True,
                                },
                                trade_id=_trade_id,
                            )
                            cycle_policy_summary["live"]["forward_tracked"] += 1
                            forward += 1
                            if not _skip_legacy_forward_track:
                                _write_execution_row(
                                    symbol=symbol, direction=direction, tier=tier,
                                    shares=shares, entry_price=entry_px,
                                    stop_loss=stop_loss, take_profit=take_profit,
                                    status="FORWARD_TRACKED",
                            response_json={
                                "mode": self.execution_mode,
                                "forward_tracked": True,
                                "execution_bridge": bridge,
                                "account_id": account,
                                "error": "dwx_not_connected",
                            },
                            account=account, source_scores=scores,
                            tags=["vanguard", "mt5_local", "dwx_not_connected"],
                            db_path=self.db_path,
                        )
                            logger.warning("[MT5 LOCAL] %s %s forward-tracked — DWX not connected", symbol, direction)
                            continue

                        # Duplicate-symbol guard
                        dwx_positions = dwx_exec.get_open_positions()
                        open_dwx_symbols = {
                            str(p.get("symbol") or "").upper()
                            for p in dwx_positions
                            if p.get("symbol")
                        }
                        if symbol.upper() in open_dwx_symbols:
                            _record_active_signal_decision(
                                execution_decision="SKIPPED_DUPLICATE_SYMBOL_OPEN",
                                decision_reason_code="DWX_SYMBOL_ALREADY_OPEN",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                },
                                positions=[
                                    {
                                        "ticket": str(p.get("ticket") or p.get("broker_position_id") or ""),
                                        "broker_position_id": str(p.get("broker_position_id") or p.get("ticket") or ""),
                                        "symbol": str(p.get("symbol") or "").upper(),
                                        "direction": str(p.get("direction") or p.get("side") or "").upper(),
                                        "qty": p.get("qty") if p.get("qty") is not None else p.get("lots"),
                                        "unrealized_pnl": p.get("pnl"),
                                        "holding_minutes": p.get("holding_minutes"),
                                    }
                                    for p in dwx_positions
                                ],
                                trade_id=_trade_id,
                            )
                            cycle_policy_summary["live"]["forward_tracked"] += 1
                            forward += 1
                            if not _skip_legacy_forward_track:
                                _write_execution_row(
                                    symbol=symbol, direction=direction, tier=tier,
                                    shares=shares, entry_price=entry_px,
                                    stop_loss=stop_loss, take_profit=take_profit,
                                    status="FORWARD_TRACKED",
                                    response_json={
                                        "mode": self.execution_mode,
                                        "forward_tracked": True,
                                        "execution_bridge": bridge,
                                        "account_id": account,
                                        "reason": "dwx_symbol_already_open",
                                    },
                                    account=account, source_scores=scores,
                                    tags=["vanguard", "mt5_local", "already_open"],
                                    db_path=self.db_path,
                                )
                            logger.info("[MT5 LOCAL] %s %s skipped — already open via DWX", symbol, direction)
                            continue

                        recent_dwx_activity = self._dwx_bridge_recent_lifecycle_activity(account, lookback_seconds=180)
                        if recent_dwx_activity:
                            _record_active_signal_decision(
                                execution_decision="SKIPPED_POLICY",
                                decision_reason_code="DWX_BRIDGE_BUSY_RECENT_LIFECYCLE",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                    "recent_action": recent_dwx_activity,
                                },
                                trade_id=_trade_id,
                            )
                            cycle_policy_summary["live"]["forward_tracked"] += 1
                            forward += 1
                            if not _skip_legacy_forward_track:
                                _write_execution_row(
                                    symbol=symbol, direction=direction, tier=tier,
                                    shares=shares, entry_price=entry_px,
                                    stop_loss=stop_loss, take_profit=take_profit,
                                    status="FORWARD_TRACKED",
                                    response_json={
                                        "mode": self.execution_mode,
                                        "forward_tracked": True,
                                        "execution_bridge": bridge,
                                        "account_id": account,
                                        "reason": "dwx_bridge_busy_recent_lifecycle",
                                        "recent_action": recent_dwx_activity,
                                    },
                                    account=account, source_scores=scores,
                                    tags=["vanguard", "mt5_local", "bridge_busy"],
                                    db_path=self.db_path,
                                )
                            logger.info(
                                "[MT5 LOCAL] %s %s forward-tracked — recent lifecycle activity on DWX bridge request_id=%s action=%s status=%s",
                                symbol,
                                direction,
                                recent_dwx_activity.get("request_id"),
                                recent_dwx_activity.get("action_type"),
                                recent_dwx_activity.get("status"),
                            )
                            continue

                        _decision_id = _record_active_signal_decision(
                            execution_decision="EXECUTED",
                            decision_reason_code="DWX_SUBMIT",
                            decision_reason_json={
                                "execution_bridge": bridge,
                            },
                            positions=[
                                {
                                    "ticket": str(p.get("ticket") or p.get("broker_position_id") or ""),
                                    "broker_position_id": str(p.get("broker_position_id") or p.get("ticket") or ""),
                                    "symbol": str(p.get("symbol") or "").upper(),
                                    "direction": str(p.get("direction") or p.get("side") or "").upper(),
                                    "qty": p.get("qty") if p.get("qty") is not None else p.get("lots"),
                                    "unrealized_pnl": p.get("pnl"),
                                    "holding_minutes": p.get("holding_minutes"),
                                }
                                for p in dwx_positions
                            ],
                            trade_id=_trade_id,
                        )
                        resp = dwx_exec.execute_trade(
                            symbol=symbol,
                            direction=direction,
                            lot_size=float(shares),
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                        )
                        status = "FILLED" if resp.get("status") == "filled" else "FAILED"
                        if status == "FILLED":
                            filled += 1
                            cycle_policy_summary["live"]["filled"] += 1
                        else:
                            failed += 1
                            cycle_policy_summary["live"]["failed"] += 1
                            failure_reasons[str(resp.get("error") or "unknown")] += 1
                        _write_execution_row(
                            symbol=symbol, direction=direction, tier=tier,
                            shares=shares, entry_price=resp.get("price") or entry_px,
                            stop_loss=stop_loss, take_profit=take_profit,
                            status=status, response_json=resp,
                            account=account, source_scores=scores,
                            db_path=self.db_path,
                        )
                        if status == "FILLED" and _decision_id:
                            _signal_decision_update(
                                self.db_path,
                                _decision_id,
                                trade_id=_trade_id,
                                broker_position_id=str(resp.get("position_id") or resp.get("orderId") or resp.get("id") or ""),
                            )
                        # Trade journal fill update
                        if _trade_id:
                            try:
                                _now_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                _ticket = str(resp.get("orderId") or resp.get("id") or "")
                                if _ticket:
                                    _journal_update_submitted(self.db_path, _trade_id, _ticket, _now_s)
                                if status == "FILLED":
                                    _journal_update_filled(
                                        db_path=self.db_path,
                                        trade_id=_trade_id,
                                        broker_position_id=str(resp.get("position_id") or _ticket or ""),
                                        fill_price=float(resp.get("price") or entry_px or 0.0),
                                        fill_qty=float(
                                            resp.get("submitted_lots")
                                            or resp.get("filled_qty")
                                            or resp.get("qty")
                                            or (resp.get("request") or {}).get("volume")
                                            or shares
                                        ),
                                        filled_at_utc=_now_s,
                                        expected_entry=float(entry_px or 0.0),
                                        side=direction,
                                    )
                                else:
                                    _signal_decision_update(
                                        self.db_path,
                                        _decision_id,
                                        trade_id=_trade_id,
                                        execution_decision="EXECUTION_FAILED",
                                        decision_reason_code="BROKER_REJECTED",
                                        decision_reason_json={
                                            "error": str(resp.get("error") or "dwx_rejected"),
                                            "execution_bridge": bridge,
                                        },
                                    )
                                    _journal_update_rejected(
                                        self.db_path, _trade_id,
                                        str(resp.get("error") or "dwx_rejected"),
                                    )
                            except Exception as _jexc:
                                logger.warning("[trade_journal] DWX post-fill update failed: %s", _jexc)
                        _record_execution_attempt_fact(
                            attempt_status=status,
                            reason_code=(
                                "DWX_SUBMIT_FILLED"
                                if status == "FILLED"
                                else str(resp.get("error") or "dwx_rejected")
                            ),
                            response_json=resp,
                            broker_order_id=str(resp.get("orderId") or resp.get("id") or ""),
                            broker_position_id=str(resp.get("position_id") or resp.get("orderId") or resp.get("id") or ""),
                            execution_bridge=bridge,
                            trade_id=_trade_id,
                        )
                        logger.info(
                            "[MT5 LOCAL] %s %s x%.5f sl=%.5f tp=%.5f -> %s ticket=%s",
                            symbol, direction, shares, stop_loss or 0, take_profit or 0,
                            resp.get("status"), resp.get("orderId", ""),
                        )
                    except Exception as exc:
                        if _decision_id:
                            _signal_decision_update(
                                self.db_path,
                                _decision_id,
                                trade_id=_trade_id,
                                execution_decision="EXECUTION_FAILED",
                                decision_reason_code="DWX_EXECUTION_EXCEPTION",
                                decision_reason_json={"error": str(exc), "execution_bridge": bridge},
                            )
                        failed += 1
                        cycle_policy_summary["live"]["failed"] += 1
                        failure_reasons[str(exc)] += 1
                        logger.error("[MT5 LOCAL] execution failed for %s: %s", symbol, exc)
                        _write_execution_row(
                            symbol=symbol, direction=direction, tier=tier,
                            shares=shares, entry_price=None,
                            stop_loss=stop_loss, take_profit=take_profit,
                            status="FAILED",
                            response_json={"error": str(exc), "execution_bridge": bridge, "account_id": account},
                            account=account, source_scores=scores,
                            db_path=self.db_path,
                        )
                        _record_execution_attempt_fact(
                            attempt_status="FAILED",
                            reason_code="DWX_EXECUTION_EXCEPTION",
                            response_json={"error": str(exc), "execution_bridge": bridge},
                            execution_bridge=bridge,
                            trade_id=_trade_id,
                        )
                    continue
                # ── End DWX bridge ──────────────────────────────────────────

                if bridge != "signalstack" or not webhook_url:
                    _record_active_signal_decision(
                        execution_decision="SKIPPED_POLICY",
                        decision_reason_code="BRIDGE_FORWARD_TRACKED",
                        decision_reason_json={
                            "execution_bridge": bridge,
                            "webhook_configured": bool(webhook_url),
                            "forward_tracked": True,
                        },
                        trade_id=_trade_id,
                    )
                    cycle_policy_summary["live"]["forward_tracked"] += 1
                    if bridge == "mt5":
                        logger.info("[V7] MT5 execution not yet implemented for %s", account)
                    elif bridge == "signalstack":
                        logger.info("[V7] No webhook_url configured for %s — forward-tracking", account)
                    else:
                        logger.info("[V7] Execution bridge %s for %s — forward-tracking", bridge, account)
                    forward += 1
                    if not _skip_legacy_forward_track:
                        _write_execution_row(
                            symbol=symbol, direction=direction, tier=tier,
                            shares=shares, entry_price=entry_px,
                            stop_loss=stop_loss, take_profit=take_profit,
                            status="FORWARD_TRACKED",
                            response_json={
                                "mode": self.execution_mode,
                                "forward_tracked": True,
                                "execution_bridge": bridge,
                                "account_id": account,
                            },
                            account=account, source_scores=scores,
                            db_path=self.db_path,
                        )
                    _record_execution_attempt_fact(
                        attempt_status="FORWARD_TRACKED",
                        reason_code="BRIDGE_FORWARD_TRACKED",
                        response_json={
                            "execution_bridge": bridge,
                            "webhook_configured": bool(webhook_url),
                            "forward_tracked": True,
                        },
                        execution_bridge=bridge,
                        trade_id=_trade_id,
                    )
                    continue

                try:
                    _decision_id = _record_active_signal_decision(
                        execution_decision="EXECUTED",
                        decision_reason_code="SIGNALSTACK_SUBMIT",
                        decision_reason_json={
                            "execution_bridge": bridge,
                        },
                        trade_id=_trade_id,
                    )
                    resp   = self._ss_adapter.send_order(
                        symbol=symbol, direction=direction,
                        quantity=shares, operation="open", broker="ttp",
                        profile=profile,
                    )
                    status = "FILLED" if resp.get("success") else "FAILED"

                    if resp.get("success"):
                        filled += 1
                        cycle_policy_summary["live"]["filled"] += 1
                    else:
                        failed += 1
                        cycle_policy_summary["live"]["failed"] += 1
                        failure_reasons[str(resp.get("error") or "unknown")] += 1

                    _write_execution_row(
                        symbol=symbol, direction=direction, tier=tier,
                        shares=shares, entry_price=entry_px,
                        stop_loss=stop_loss, take_profit=take_profit,
                        status=status, response_json=resp,
                        account=account, source_scores=scores,
                        db_path=self.db_path,
                    )
                    if status == "FILLED" and _decision_id:
                        _signal_decision_update(
                            self.db_path,
                            _decision_id,
                            trade_id=_trade_id,
                            broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                        )
                    # ── Trade journal: update fill/rejection ───────────────
                    if _trade_id:
                        try:
                            _now_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            if status == "FILLED":
                                _journal_update_filled(
                                    db_path=self.db_path,
                                    trade_id=_trade_id,
                                    broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                                    fill_price=float(entry_px or 0.0),
                                    fill_qty=float(
                                        resp.get("submitted_lots")
                                        or resp.get("filled_qty")
                                        or resp.get("qty")
                                        or (resp.get("request") or {}).get("volume")
                                        or shares
                                    ),
                                    filled_at_utc=_now_s,
                                    expected_entry=float(entry_px or 0.0),
                                    side=direction,
                                )
                            else:
                                _signal_decision_update(
                                    self.db_path,
                                    _decision_id,
                                    trade_id=_trade_id,
                                    execution_decision="EXECUTION_FAILED",
                                    decision_reason_code="BROKER_REJECTED",
                                    decision_reason_json={
                                        "error": str(resp.get("error") or "broker_rejected"),
                                        "execution_bridge": bridge,
                                    },
                                )
                                _journal_update_rejected(
                                    self.db_path, _trade_id,
                                    str(resp.get("error") or "broker_rejected"),
                                )
                        except Exception as _jexc:
                            logger.warning("[trade_journal] SignalStack post-fill update failed: %s", _jexc)
                    _record_execution_attempt_fact(
                        attempt_status=status,
                        reason_code=(
                            "SIGNALSTACK_SUBMIT_FILLED"
                            if status == "FILLED"
                            else str(resp.get("error") or "broker_rejected")
                        ),
                        response_json=resp,
                        broker_order_id=str(resp.get("orderId") or resp.get("id") or ""),
                        broker_position_id=str(resp.get("position_id") or resp.get("id") or ""),
                        execution_bridge=bridge,
                        trade_id=_trade_id,
                    )
                    # ── End trade journal update ───────────────────────────
                    logger.info(
                        "[%s] %s %s x%d → %s",
                        self.execution_mode.upper(), symbol, direction, shares, status,
                    )

                except Exception as exc:
                    if _decision_id:
                        _signal_decision_update(
                            self.db_path,
                            _decision_id,
                            trade_id=_trade_id,
                            execution_decision="EXECUTION_FAILED",
                            decision_reason_code="SIGNALSTACK_EXCEPTION",
                            decision_reason_json={"error": str(exc), "execution_bridge": bridge},
                        )
                    failed += 1
                    cycle_policy_summary["live"]["failed"] += 1
                    failure_reasons[str(exc)] += 1
                    logger.error("Execution error for %s: %s", symbol, exc)
                    _write_execution_row(
                        symbol=symbol, direction=direction, tier=tier,
                        shares=shares, entry_price=None,
                        stop_loss=None, take_profit=None,
                        status="FAILED",
                        response_json={"error": str(exc)},
                        account=account, source_scores=scores,
                        db_path=self.db_path,
                    )
                    _record_execution_attempt_fact(
                        attempt_status="FAILED",
                        reason_code="SIGNALSTACK_EXCEPTION",
                        response_json={"error": str(exc), "execution_bridge": bridge},
                        execution_bridge=bridge,
                        trade_id=_trade_id,
                    )

        if checkpoint_trade_ids:
            self._refresh_forward_checkpoints(trade_ids=checkpoint_trade_ids)
        if checkpoint_decision_ids:
            self._refresh_signal_forward_checkpoints(decision_ids=checkpoint_decision_ids)

        self._trades_placed   += filled
        self._forward_tracked += forward

        gft_time_exit_msg = self._build_gft_time_exit_telegram(gft_time_exit_rows)
        if gft_time_exit_msg:
            self._send_telegram(gft_time_exit_msg)

        self._last_execution_policy_summary = cycle_policy_summary
        live_shortlist_msg = self._build_live_trade_shortlist_telegram(
            str(cycle_ts or approved[0].get("cycle_ts_utc") or ""),
            cycle_policy_summary,
        )
        if live_shortlist_msg:
            self._send_telegram(live_shortlist_msg, message_type="SHORTLIST")

        logger.info(
            "Execution: submitted=%d, filled=%d, failed=%d, forward_tracked=%d",
            submitted, filled, failed, forward,
        )
        live_summary = dict(cycle_policy_summary.get("live") or {})
        execution_summary = {
            "v6_approved_rows": len(approved),
            "live_approved_rows": int(live_summary.get("approved", 0) or 0),
            "submitted": submitted,
            "filled": filled,
            "failed": failed,
            "forward_tracked": forward,
            "failure_reasons": dict(failure_reasons),
            "cycle_policy_summary": cycle_policy_summary,
            "v6_approved_symbols": [
                {
                    "symbol": str(row.get("symbol") or "").upper(),
                    "direction": str(row.get("direction") or "").upper(),
                    "shares_or_lots": row.get("shares_or_lots"),
                }
                for row in approved[:20]
            ],
        }
        self._record_cycle_trace_execution(str(cycle_ts or approved[0].get("cycle_ts_utc") or ""), execution_summary)
        return execution_summary

    # ------------------------------------------------------------------
    # EOD Flatten
    # ------------------------------------------------------------------

    def _execute_gft_time_exits(self) -> list[dict[str, Any]]:
        """Close gft_* positions older than 4 hours before placing new entries."""
        if self._is_manual_execution_mode():
            return []
        if not any(str(account.get("id") or "").lower().startswith("gft_") for account in self._accounts):
            return []

        try:
            from vanguard.executors.mt5_executor import MetaApiExecutor, _mt5_symbol
        except Exception as exc:
            logger.warning("Could not import MetaApiExecutor for GFT time exits: %s", exc)
            return []

        now_utc = self._now_utc()
        exit_rows: list[dict[str, Any]] = []
        for account in self._accounts:
            account_id = str(account.get("id") or "").lower()
            if not account_id.startswith("gft_"):
                continue

            bridge = str(account.get("execution_bridge") or "disabled").lower()
            if bridge != "mt5":
                continue

            executor = MetaApiExecutor(account_key=account_id)
            if not executor.connect():
                continue

            positions = executor.get_open_positions()
            for position in positions:
                position_id = str(position.get("id") or "").strip()
                if not position_id:
                    continue

                opened_at = self._parse_metaapi_position_time(position)
                if opened_at is None:
                    continue
                age_seconds = (now_utc - opened_at).total_seconds()
                if age_seconds <= 4 * 3600:
                    continue

                symbol = self._normalize_gft_display_symbol(position.get("symbol"))
                position_type = str(position.get("type") or "").upper()
                direction = "LONG" if "BUY" in position_type else "SHORT"
                shares = float(position.get("volume") or 0.0)
                entry_price = position.get("openPrice")
                stop_loss = position.get("stopLoss")
                take_profit = position.get("takeProfit")
                broker_symbol = _mt5_symbol(symbol) if symbol else str(position.get("symbol") or "")

                if self.execution_mode == "off":
                    status = "FORWARD_TRACKED"
                    response = {
                        "mode": "off",
                        "forward_tracked": True,
                        "reason": "TIME_EXIT_4H",
                        "position_id": position_id,
                        "broker_symbol": broker_symbol,
                        "age_seconds": round(age_seconds, 1),
                    }
                else:
                    response = executor.close_position(
                        position_id=position_id,
                        symbol=symbol,
                        reason="TIME_EXIT_4H",
                    )
                    status = "FILLED" if response.get("status") == "closed" else "FAILED"

                log_id = _write_execution_row(
                    symbol=symbol or broker_symbol,
                    direction=direction,
                    tier="gft_time_exit_4h",
                    shares=shares,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    status=status,
                    response_json=response,
                    account=account_id,
                    tags=["vanguard", "gft_time_exit_4h", "TIME_EXIT_4H"],
                    notes="TIME_EXIT_4H",
                    db_path=self.db_path,
                )
                logger.info(
                    "[GFT TIME_EXIT_4H] %s %s %s x%s age=%.1fh -> %s (id=%s)",
                    account_id,
                    symbol or broker_symbol,
                    direction,
                    shares,
                    age_seconds / 3600.0,
                    status,
                    log_id,
                )
                exit_rows.append(
                    {
                        "account_id": account_id,
                        "symbol": symbol or broker_symbol,
                        "direction": direction,
                        "shares": shares,
                        "status": status,
                        "age_hours": round(age_seconds / 3600.0, 2),
                        "response": response,
                    }
                )

        return exit_rows

    @staticmethod
    def _parse_metaapi_position_time(position: dict[str, Any]) -> Optional[datetime]:
        """Parse MetaApi position open timestamps as UTC."""
        for key in ("time", "openTime", "updateTime"):
            raw = position.get(key)
            if not raw:
                continue
            try:
                value = str(raw).replace("Z", "+00:00")
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=now_utc().tzinfo)
                return parsed.astimezone(now_utc().tzinfo)
            except Exception:
                continue
        return None

    def _eod_flatten(self, account: dict, et: datetime) -> None:
        """Flatten all APPROVED positions for a must_close_eod account."""
        account_id     = account["id"]
        must_close_eod = bool(account.get("must_close_eod", 0))

        if not must_close_eod:
            return

        action = check_eod_action(
            must_close_eod=must_close_eod,
            no_new_positions_after=self.no_new_positions_after,
            eod_flatten_time=self.eod_flatten_time,
            current_et=et,
        )
        if action != "FLATTEN_ALL":
            return

        positions = get_positions_to_flatten(account_id, self.db_path)
        if not positions:
            logger.info("[%s] No open positions to flatten", account_id)
            return

        logger.info("[%s] EOD flatten: %d positions", account_id, len(positions))
        flattened = 0

        for pos in positions:
            symbol = pos["symbol"]
            shares = int(pos.get("shares_or_lots") or 0)
            if shares <= 0:
                continue

            profile = self._get_account_profile(account_id)
            bridge = str((profile or {}).get("execution_bridge") or "disabled").lower()
            webhook_url = str((profile or {}).get("webhook_url") or "").strip()

            if (
                not self._is_manual_execution_mode()
                and self._ss_adapter
                and bridge == "signalstack"
                and webhook_url
            ):
                try:
                    resp   = self._ss_adapter.flatten_position(symbol=symbol, quantity=shares, profile=profile)
                    status = "FILLED" if resp.get("success") else "FAILED"
                except Exception as exc:
                    logger.error("Flatten error %s: %s", symbol, exc)
                    resp   = {"error": str(exc)}
                    status = "FAILED"
            else:
                resp   = {"mode": self.execution_mode, "flatten": True, "execution_bridge": bridge}
                status = "FORWARD_TRACKED"

            _write_execution_row(
                symbol=symbol, direction=pos.get("direction", "LONG"),
                tier="eod_flatten", shares=shares,
                entry_price=None, stop_loss=None, take_profit=None,
                status=status, response_json=resp,
                account=account_id,
                tags=["eod_flatten"],
                notes="EOD forced flatten",
                db_path=self.db_path,
            )
            flattened += 1

        if self._telegram:
            self._telegram.alert_eod_flatten(
                positions_closed=flattened, account=account_id
            )

        logger.info("[%s] Flattened %d positions", account_id, flattened)

    # ------------------------------------------------------------------
    # Phase 2a: resolved universe log
    # ------------------------------------------------------------------

    def _write_resolved_universe_log(self, ru: Any) -> None:
        """Persist one row to vanguard_resolved_universe_log."""
        import json as _json
        config_version = (self._runtime_config or {}).get("config_version", "unknown")
        try:
            with sqlite_conn(self.db_path) as con:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vanguard_resolved_universe_log (
                      cycle_ts_utc           TEXT NOT NULL,
                      config_version         TEXT NOT NULL,
                      mode                   TEXT NOT NULL,
                      active_profile_ids     TEXT NOT NULL,
                      expected_asset_classes TEXT NOT NULL,
                      in_scope_symbols       TEXT NOT NULL,
                      in_scope_count         INTEGER NOT NULL,
                      excluded_count         INTEGER NOT NULL,
                      PRIMARY KEY (cycle_ts_utc)
                    )
                    """
                )
                con.execute(
                    """
                    INSERT OR REPLACE INTO vanguard_resolved_universe_log
                      (cycle_ts_utc, config_version, mode, active_profile_ids,
                       expected_asset_classes, in_scope_symbols, in_scope_count,
                       excluded_count)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        ru.cycle_ts_utc,
                        config_version,
                        ru.mode,
                        _json.dumps(ru.active_profile_ids),
                        _json.dumps(ru.expected_asset_classes),
                        _json.dumps(ru.in_scope_symbols),
                        len(ru.in_scope_symbols),
                        len(ru.excluded),
                    ),
                )
                con.commit()
        except Exception as exc:
            logger.warning("[Phase2a] Failed to write resolved universe log: %s", exc)

    # ------------------------------------------------------------------
    # Session log helpers
    # ------------------------------------------------------------------

    def _ensure_session_log(self) -> None:
        if self._is_test_execution_mode():
            return
        with sqlite_conn(self.db_path) as con:
            con.execute(_CREATE_SESSION_LOG)
            con.commit()

    def _upsert_session_log(
        self,
        status: str = "running",
        session_end: Optional[str] = None,
    ) -> None:
        if self._is_test_execution_mode():
            return
        session_start = (
            iso_utc(self._session_start_ts)
            if self._session_start_ts
            else iso_utc(self._now_utc())
        )
        try:
            with sqlite_conn(self.db_path) as con:
                con.execute(
                    """
                    INSERT INTO vanguard_session_log
                        (date, session_start, session_end, total_cycles, failed_cycles,
                         trades_placed, forward_tracked, execution_mode, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(date) DO UPDATE SET
                        session_end     = COALESCE(excluded.session_end, session_end),
                        total_cycles    = excluded.total_cycles,
                        failed_cycles   = excluded.failed_cycles,
                        trades_placed   = excluded.trades_placed,
                        forward_tracked = excluded.forward_tracked,
                        execution_mode  = excluded.execution_mode,
                        status          = excluded.status,
                        updated_at      = datetime('now')
                    """,
                    (
                        self._session_date, session_start, session_end,
                        self._total_cycles, self._failed_cycles,
                        self._trades_placed, self._forward_tracked,
                        self.execution_mode, status,
                    ),
                )
                con.commit()
        except Exception as exc:
            logger.warning("Could not upsert session log: %s", exc)

    # ------------------------------------------------------------------
    # Telegram helper
    # ------------------------------------------------------------------

    def _build_shortlist_telegram(self, cycle_ts: str) -> Optional[str]:
        """Build a session-aware shortlist summary with fixed-width tables."""
        shortlist_rows = []
        v5_rows = []
        portfolio_rows = []
        shortlist_prices: dict[str, float] = {}
        active_profile_ids = [str(account.get("id") or "") for account in (self._accounts or [])]
        active_profile_placeholders = ",".join("?" for _ in active_profile_ids) or "''"
        try:
            with sqlite_conn(self.db_path) as con:
                s_cols = self._table_columns(con, "vanguard_v5_selection")
                t_cols = self._table_columns(con, "vanguard_v5_tradeability")
                shortlist_rows = con.execute(
                    """
                    SELECT symbol, asset_class, direction, predicted_return, edge_score, rank
                    FROM vanguard_shortlist_v2
                    WHERE cycle_ts_utc = ?
                    ORDER BY asset_class, direction, rank
                    """,
                    (cycle_ts,),
                ).fetchall()
                t_conviction = self._optional_col_expr(t_cols, "t", "conviction", out_alias="conviction")
                t_economics_state = self._optional_col_expr(
                    t_cols, "t", "economics_state", fallback_sql="''", out_alias="economics_state"
                )
                t_live_followthrough_state = self._optional_col_expr(
                    t_cols, "t", "live_followthrough_state", fallback_sql="''", out_alias="live_followthrough_state"
                )
                t_predicted_move_pips = self._optional_col_expr(
                    t_cols, "t", "predicted_move_pips", out_alias="predicted_move_pips"
                )
                t_after_cost_pips = self._optional_col_expr(
                    t_cols, "t", "after_cost_pips", out_alias="after_cost_pips"
                )
                t_predicted_move_bps = self._optional_col_expr(
                    t_cols, "t", "predicted_move_bps", out_alias="predicted_move_bps"
                )
                t_after_cost_bps = self._optional_col_expr(
                    t_cols, "t", "after_cost_bps", out_alias="after_cost_bps"
                )
                s_selection_reason = self._optional_col_expr(
                    s_cols, "s", "selection_reason", fallback_sql="''", out_alias="selection_reason"
                )
                s_route_tier = self._optional_col_expr(
                    s_cols, "s", "route_tier", fallback_sql="''", out_alias="route_tier"
                )
                s_display_label = self._optional_col_expr(
                    s_cols, "s", "display_label", fallback_sql="''", out_alias="display_label"
                )
                s_selection_state = self._optional_col_expr(
                    s_cols, "s", "selection_state", fallback_sql="''", out_alias="selection_state"
                )
                s_direction_streak = self._optional_col_expr(
                    s_cols, "s", "direction_streak", fallback_sql="0", out_alias="direction_streak"
                )
                s_top_rank_streak = self._optional_col_expr(
                    s_cols, "s", "top_rank_streak", fallback_sql="0", out_alias="top_rank_streak"
                )
                s_flip_count_window = self._optional_col_expr(
                    s_cols, "s", "flip_count_window", fallback_sql="0", out_alias="flip_count_window"
                )
                v5_rows = con.execute(
                    f"""
                    SELECT s.profile_id,
                           s.symbol,
                           s.asset_class,
                           s.direction,
                           t.predicted_return,
                           {t_conviction},
                           {t_economics_state},
                           s.selection_rank,
                           {s_selection_reason},
                           {s_route_tier},
                           {s_display_label},
                           {s_selection_state},
                           {s_direction_streak},
                           {s_top_rank_streak},
                           {s_flip_count_window},
                           {t_live_followthrough_state},
                           {t_predicted_move_pips},
                           {t_after_cost_pips},
                           {t_predicted_move_bps},
                           {t_after_cost_bps}
                    FROM vanguard_v5_selection s
                    JOIN vanguard_v5_tradeability t
                      ON t.cycle_ts_utc = s.cycle_ts_utc
                     AND t.profile_id = s.profile_id
                     AND t.symbol = s.symbol
                     AND t.direction = s.direction
                    WHERE s.cycle_ts_utc = ?
                      AND s.profile_id IN (""" + active_profile_placeholders + """)
                      AND s.asset_class IN ('forex', 'crypto', 'equity', 'metal', 'index', 'commodity', 'energy')
                    ORDER BY s.asset_class, s.direction, s.selection_rank, s.symbol
                    """,
                    [cycle_ts, *active_profile_ids],
                ).fetchall()
                portfolio_placeholders = ",".join("?" for _ in active_profile_ids) or "''"
                portfolio_rows = con.execute(
                    """
                    SELECT account_id,
                           symbol,
                           direction,
                           stop_price,
                           tp_price,
                           shares_or_lots,
                           status,
                           COALESCE(rejection_reason, ''),
                           risk_dollars,
                           risk_pct
                    FROM vanguard_tradeable_portfolio
                    WHERE cycle_ts_utc = ?
                      AND account_id IN (""" + portfolio_placeholders + """)
                    """,
                    [cycle_ts, *active_profile_ids],
                ).fetchall()
                shortlist_prices = self._load_latest_symbol_prices(
                    con,
                    symbols=(
                        [str(row[0]) for row in shortlist_rows]
                        + [str(row[1]) for row in v5_rows]
                    ),
                    entry_price_cycle_ts=cycle_ts,
                )
        except Exception as exc:
            logger.warning("Could not build Telegram shortlist summary: %s", exc)
            return (
                f"📊 <b>[{self._telegram_env_label()}] VANGUARD SHORTLIST</b> — {cycle_ts[:16]}\n\n"
                f"{self._build_session_status_block()}\n\n"
                f"⚠️ Shortlist unavailable this cycle\n"
                f"Error: <code>{html.escape(str(exc))}</code>"
            )

        lines = [
            f"📊 <b>[{self._telegram_env_label()}] VANGUARD SHORTLIST</b> — {cycle_ts[:16]}",
            self._build_session_status_block(),
            "",
        ]
        timeout_policy_header = self._build_timeout_policy_header(cycle_ts)
        if timeout_policy_header:
            lines.append(timeout_policy_header)
            lines.append("")
        selection_contract = self._build_selection_contract_summary()
        if selection_contract:
            lines.append(selection_contract.get("regime", ""))
            lines.append(selection_contract.get("eligibility", ""))
            lines.append("")

        if not shortlist_rows and not v5_rows:
            lines.append("⚪ No shortlist rows this cycle")
            return "\n".join(lines).rstrip()

        # Enforce-mode equity filter: only show active-profile equity CFDs when universe is locked
        _ru = getattr(self, "_current_resolved_universe", None)
        _enforce_mode = _ru is not None and str(getattr(_ru, "mode", "")).lower() == "enforce"
        _active_equity_set: set[str] = set()
        if _enforce_mode:
            try:
                _rt_cfg = self._runtime_config or {}
                _rt_universes = _rt_cfg.get("universes") or {}
                _active_scopes = {
                    str(profile.get("instrument_scope") or "")
                    for profile in (_rt_cfg.get("profiles") or [])
                    if str(profile.get("is_active", "")).strip().lower() in {"1", "true", "yes", "on"}
                }
                _active_equity_set = {
                    str(symbol).upper()
                    for scope_id in _active_scopes
                    for symbol in (((( _rt_universes.get(scope_id) or {}).get("symbols") or {}).get("equity") or []))
                    if str(symbol).strip()
                }
            except Exception:
                pass

        v5_keys: set[tuple[str, str, str]] = set()
        preferred_v5_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for (
            profile_id,
            symbol,
            asset_class,
            direction,
            predicted_return,
            conviction,
            economics_state,
            selection_rank,
            selection_reason,
            route_tier,
            display_label,
            selection_state,
            direction_streak,
            top_rank_streak,
            flip_count_window,
            live_followthrough_state,
            predicted_move_pips,
            after_cost_pips,
            predicted_move_bps,
            after_cost_bps,
        ) in v5_rows:
            symbol_norm = str(symbol or "").upper()
            asset_norm = str(asset_class or "unknown").lower()
            dir_norm = str(direction or "UNKNOWN").upper()
            key = (symbol_norm, asset_norm, dir_norm)
            current = {
                "profile_id": str(profile_id or ""),
                "symbol": symbol_norm,
                "asset_class": asset_norm,
                "direction": dir_norm,
                "display_mode": "v5",
                "predicted_return": predicted_return,
                "conviction": conviction,
                "economics_state": economics_state,
                "rank": selection_rank,
                "selection_reason": selection_reason,
                "route_tier": route_tier,
                "display_label": display_label,
                "selection_state": selection_state,
                "direction_streak": direction_streak,
                "top_rank_streak": top_rank_streak,
                "flip_count_window": flip_count_window,
                "live_followthrough_state": live_followthrough_state,
                "predicted_move_pips": predicted_move_pips,
                "after_cost_pips": after_cost_pips,
                "predicted_move_bps": predicted_move_bps,
                "after_cost_bps": after_cost_bps,
            }
            existing = preferred_v5_rows.get(key)
            if existing is None:
                preferred_v5_rows[key] = current
                continue
            existing_rank = active_profile_ids.index(str(existing["profile_id"])) if str(existing["profile_id"]) in active_profile_ids else 999
            current_rank = active_profile_ids.index(str(current["profile_id"])) if str(current["profile_id"]) in active_profile_ids else 999
            if current_rank < existing_rank:
                preferred_v5_rows[key] = current

        for key in preferred_v5_rows:
            v5_keys.add(key)

        active_profile_order = {
            str(account.get("id") or ""): idx
            for idx, account in enumerate(self._accounts or [])
        }
        portfolio_overlay: dict[tuple[str, str, str], dict[str, Any]] = {}
        for (
            account_id,
            symbol,
            direction,
            stop_price,
            tp_price,
            shares_or_lots,
            status,
            rejection_reason,
            risk_dollars,
            risk_pct,
        ) in portfolio_rows:
            key = (
                str(account_id or ""),
                str(symbol or "").upper(),
                str(direction or "").upper(),
            )
            payload = {
                "account_id": str(account_id or ""),
                "stop_price": stop_price,
                "tp_price": tp_price,
                "lot_size": shares_or_lots,
                "status": str(status or "").upper(),
                "rejection_reason": str(rejection_reason or "").strip(),
                "risk_dollars": risk_dollars,
                "risk_pct": risk_pct,
            }
            existing = portfolio_overlay.get(key)
            if existing is None:
                portfolio_overlay[key] = payload
                continue
            existing_rank = active_profile_order.get(str(existing.get("account_id") or ""), 999)
            candidate_rank = active_profile_order.get(str(account_id or ""), 999)
            if candidate_rank < existing_rank:
                portfolio_overlay[key] = payload

        thesis_state_map = self._load_thesis_display_state(
            cycle_ts=cycle_ts,
            candidate_keys=[
                (str(row.get("profile_id") or ""), str(row.get("symbol") or ""), str(row.get("direction") or ""))
                for row in preferred_v5_rows.values()
            ],
        )

        self._record_shortlist_streak_history(shortlist_rows, cycle_ts)

        _rt = self._runtime_config or {}
        display_n = int((_rt.get("runtime") or {}).get("shortlist_display_top_n") or 10)

        v5_display_rows = sorted(
            preferred_v5_rows.values(),
            key=lambda row: (
                str(row.get("asset_class") or ""),
                0 if str(row.get("selection_state") or "").upper() == "SELECTED" else 1,
                int(row.get("rank") or 999999),
                str(row.get("direction") or ""),
                str(row.get("symbol") or ""),
            ),
        )

        if v5_display_rows:
            actionable_sections: list[tuple[str, list[dict[str, Any]], str]] = []
            diagnostics_rows: list[dict[str, Any]] = []
            selected_by_v5: list[dict[str, Any]] = []
            blocked_by_risky: list[dict[str, Any]] = []
            held_by_v6: list[dict[str, Any]] = []
            approved_by_v6: list[dict[str, Any]] = []
            approved_manual_by_v6: list[dict[str, Any]] = []
            for row in v5_display_rows:
                key = (
                    str(row.get("profile_id") or ""),
                    str(row.get("symbol") or "").upper(),
                    str(row.get("direction") or "").upper(),
                )
                details = portfolio_overlay.get(key)
                stage_row = dict(row)
                profile_cfg = next(
                    (
                        account
                        for account in (self._accounts or [])
                        if str(account.get("id") or "") == str(row.get("profile_id") or "")
                    ),
                    {},
                )
                profile_mode = self._profile_execution_mode_for_asset(profile_cfg, stage_row.get("asset_class"))
                stage_row["profile_execution_mode"] = profile_mode
                if details:
                    stage_row["portfolio"] = details
                stage_row.update(thesis_state_map.get(key, {}))
                stage_row["session_label"] = self._session_label_for_cycle_ts(cycle_ts)
                stage_row["model_horizon_hint"] = "90–120m"
                stage_row["exit_by_window"] = self._format_exit_by_window(cycle_ts)
                stage_row["timeout_policy"] = self._timeout_policy_for_cycle_ts(cycle_ts, stage_row.get("asset_class") or "forex")
                selection_state = str(row.get("selection_state") or "").upper()
                if selection_state != "SELECTED":
                    diagnostics_rows.append(stage_row)
                    continue
                if not details:
                    selected_by_v5.append(stage_row)
                    continue
                status = str(details.get("status") or "").upper()
                if status == "APPROVED":
                    if self._is_manual_execution_mode() or profile_mode in {"manual", "off", "paper", "test"}:
                        approved_manual_by_v6.append(stage_row)
                    else:
                        approved_by_v6.append(stage_row)
                elif status == "HOLD":
                    held_by_v6.append(stage_row)
                elif status == "REJECTED":
                    blocked_by_risky.append(stage_row)
                else:
                    selected_by_v5.append(stage_row)

            actionable_sections.extend(
                [
                    ("Selected by V5", selected_by_v5, "selected"),
                    ("Blocked by Risk", blocked_by_risky, "blocked"),
                    ("Held at V6", held_by_v6, "held"),
                    ("Ready This Cycle", approved_manual_by_v6, "approved_manual"),
                    ("Approved by V6", approved_by_v6, "approved"),
                ]
            )

            any_actionable = any(section_rows for _title, section_rows, _kind in actionable_sections)
            if any_actionable:
                for title, section_rows, section_kind in actionable_sections:
                    if not section_rows:
                        continue
                    lines.append(f"━━ {title.upper()} ━━")
                    for row in section_rows[:display_n]:
                        lines.append(
                            self._format_v5_stage_telegram_row(
                                row=row,
                                price=shortlist_prices.get(str(row.get("symbol") or "").upper()),
                                stage=section_kind,
                            )
                        )
                    lines.append("")
            else:
                lines.append("━━ V5 DIAGNOSTICS ━━")
                lines.append("No ranked V5.2 rows this cycle.")
                total_candidates = len(v5_display_rows)
                economics_fail_count = sum(
                    1 for row in diagnostics_rows
                    if str(row.get("economics_state") or "").upper() == "FAIL"
                )
                economics_pass_count = max(0, total_candidates - economics_fail_count)
                pocket_reject_count = sum(
                    1 for row in diagnostics_rows
                    if str(row.get("selection_reason") or "").upper() == "POCKET_FILTER_REJECT"
                )
                pocket_eligible_count = max(0, economics_pass_count - pocket_reject_count)
                approved_count = sum(
                    1 for row in portfolio_overlay.values()
                    if str(row.get("status") or "").upper() == "APPROVED"
                )
                lines.append(
                    "Funnel: "
                    f"Universe {total_candidates} | "
                    f"Economics pass {economics_pass_count} | "
                    f"Pocket-eligible {pocket_eligible_count} | "
                    f"Approved {approved_count}"
                )
                if selection_contract.get("regime") == "Regime: STRICT POCKET":
                    lines.append("No tradable intent this cycle under the active strict-pocket rules.")
                reason_counts = Counter(
                    str(row.get("selection_reason") or row.get("economics_state") or "UNKNOWN").upper()
                    for row in diagnostics_rows
                )
                if reason_counts:
                    summary_parts = [
                        f"{reason} {count}"
                        for reason, count in reason_counts.most_common(4)
                    ]
                    lines.append("Top filters: " + " | ".join(summary_parts))
                diag_candidates = [
                    row for row in diagnostics_rows
                    if row.get("rank") not in (None, "") or str(row.get("selection_state") or "").upper() in {"WATCHLIST", "HOLD"}
                ]
                for row in diag_candidates[:display_n]:
                    lines.append(
                        self._format_v5_stage_telegram_row(
                            row=row,
                            price=shortlist_prices.get(str(row.get("symbol") or "").upper()),
                            stage="diagnostic",
                        )
                    )
                lines.append("")

            relevant = [
                row
                for row in portfolio_overlay.values()
                if str(row.get("account_id") or "") in active_profile_order
            ]
            if relevant:
                approved = sum(1 for row in relevant if str(row.get("status") or "").upper() == "APPROVED")
                held = sum(1 for row in relevant if str(row.get("status") or "").upper() == "HOLD")
                blocked = sum(1 for row in relevant if str(row.get("status") or "").upper() == "REJECTED")
                profile_label = self._profile_display_label(str(relevant[0].get("account_id") or "")) or "ACTIVE"
                summary = f"🧮 V6 {profile_label}: ✅ {approved} approved"
                if held:
                    summary += f" | ⏸️ {held} hold"
                if blocked:
                    summary += f" | 🚫 {blocked} blocked"
                lines.append(summary)
        else:
            grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
            for symbol, asset_class, direction, predicted_return, edge_score, rank in shortlist_rows:
                _ac = str(asset_class or "unknown")
                key = (str(symbol or "").upper(), _ac.lower(), str(direction or "UNKNOWN").upper())
                if key in v5_keys and _ac.lower() in {"forex", "crypto"}:
                    continue
                if _enforce_mode and _ac == "equity" and _active_equity_set:
                    if str(symbol or "").upper() not in _active_equity_set:
                        continue
                grouped[_ac.lower()][str(direction or "UNKNOWN").upper()].append(
                    {
                        "symbol": str(symbol or "").upper(),
                        "asset_class": _ac.lower(),
                        "direction": str(direction or "UNKNOWN").upper(),
                        "display_mode": "legacy",
                        "predicted_return": predicted_return,
                        "edge_score": edge_score,
                        "rank": rank,
                    }
                )

            visible_assets = set(self._telegram_visible_asset_classes())
            for asset_class in sorted(grouped.keys()):
                if asset_class not in visible_assets:
                    continue
                lines.append(f"━━ {asset_class.upper()} — {self._asset_session_label(asset_class)} ━━")
                for direction_name, label in (("LONG", "🟢 LONG"), ("SHORT", "🔴 SHORT")):
                    trade_rows = grouped[asset_class].get(direction_name, [])
                    if not trade_rows:
                        continue
                    lines.append(f"{label} ({len(trade_rows)})")
                    lines.extend(
                        self._format_shortlist_display_row(
                            row=row,
                            price=shortlist_prices.get(str(row["symbol"]).upper()),
                            streak_counter=self._shortlist_streak_counter(str(row["symbol"])),
                            gft_details=portfolio_overlay.get(("", str(row["symbol"]).upper(), direction_name))
                            or next(
                                (
                                    details for (acct_id, sym, side), details in portfolio_overlay.items()
                                    if sym == str(row["symbol"]).upper() and side == direction_name
                                ),
                                None,
                            ),
                        )
                        for row in trade_rows[:display_n]
                    )
                    lines.append("")

        return "\n".join(lines).rstrip()

    @staticmethod
    def _directional_conviction_strength(direction: str, conviction: Any) -> float | None:
        try:
            value = float(conviction)
        except (TypeError, ValueError):
            return None
        side = str(direction or "").upper()
        if side == "SHORT":
            return 1.0 - value
        return value

    def _approved_shadow_gate_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cfg = get_shadow_gate_config()
        if not bool(cfg.get("enabled", True)):
            return []
        allowed_assets = {
            str(asset or "").strip().lower()
            for asset in (cfg.get("asset_classes") or [])
            if str(asset or "").strip()
        }
        conviction_min = float(cfg.get("conviction_min", 0.60) or 0.60)
        passed: list[dict[str, Any]] = []
        for row in rows:
            asset_class = str(row.get("asset_class") or "").lower()
            if allowed_assets and asset_class not in allowed_assets:
                continue
            strength = self._directional_conviction_strength(row.get("direction") or "", row.get("conviction"))
            if strength is None or strength < conviction_min:
                continue
            candidate = dict(row)
            candidate["shadow_conviction_strength"] = strength
            candidate["shadow_conviction_min"] = conviction_min
            passed.append(candidate)
        return sorted(
            passed,
            key=lambda row: (
                -float(row.get("shadow_conviction_strength") or 0.0),
                int(row.get("rank") or 999999),
                str(row.get("symbol") or ""),
            ),
        )

    def _format_shadow_gate_diag(self, row: dict[str, Any]) -> str:
        asset_class = str(row.get("asset_class") or "").lower()
        unit = "bps" if asset_class == "crypto" else "p"
        after_value = row.get("after_cost_bps") if unit == "bps" else row.get("after_cost_pips")
        after_text = "-" if after_value in (None, "") else f"{float(after_value):.2f}{unit}"
        conviction_strength = row.get("shadow_conviction_strength")
        conviction_text = "-"
        if conviction_strength not in (None, ""):
            conviction_text = f"{float(conviction_strength):.2f}"
        flip_text = row.get("flip_count_window")
        flip_display = "-" if flip_text in (None, "") else str(int(flip_text))
        top_rank_text = row.get("top_rank_streak")
        top_rank_display = "-" if top_rank_text in (None, "") else str(int(top_rank_text))
        followthrough = str(row.get("live_followthrough_state") or "UNKNOWN").upper()
        return (
            f"Shadow pass conv*={html.escape(conviction_text)}  "
            f"after={html.escape(after_text)}  "
            f"flip={html.escape(flip_display)}  "
            f"top_rank={html.escape(top_rank_display)}  "
            f"ft={html.escape(followthrough)}"
        )

    def _build_shadow_gate_shortlist_telegram(self, cycle_ts: str) -> Optional[str]:
        active_profile_ids = [str(account.get("id") or "") for account in (self._accounts or []) if str(account.get("id") or "").strip()]
        if not active_profile_ids:
            return None
        active_profile_placeholders = ",".join("?" for _ in active_profile_ids) or "''"
        rows: list[dict[str, Any]] = []
        shortlist_prices: dict[str, float] = {}
        try:
            with sqlite_conn(self.db_path) as con:
                raw_rows = con.execute(
                    """
                    SELECT s.profile_id,
                           s.symbol,
                           s.asset_class,
                           s.direction,
                           t.predicted_return,
                           t.conviction,
                           t.economics_state,
                           s.selection_rank,
                           s.selection_reason,
                           s.route_tier,
                           s.display_label,
                           s.selection_state,
                           s.direction_streak,
                           s.top_rank_streak,
                           s.flip_count_window,
                           t.live_followthrough_state,
                           t.predicted_move_pips,
                           t.after_cost_pips,
                           t.predicted_move_bps,
                           t.after_cost_bps,
                           p.stop_price,
                           p.tp_price,
                           p.shares_or_lots,
                           p.status,
                           COALESCE(p.rejection_reason, ''),
                           p.risk_dollars,
                           p.risk_pct
                    FROM vanguard_v5_selection s
                    JOIN vanguard_v5_tradeability t
                      ON t.cycle_ts_utc = s.cycle_ts_utc
                     AND t.profile_id = s.profile_id
                     AND t.symbol = s.symbol
                     AND t.direction = s.direction
                    JOIN vanguard_tradeable_portfolio p
                      ON p.cycle_ts_utc = s.cycle_ts_utc
                     AND p.account_id = s.profile_id
                     AND p.symbol = s.symbol
                     AND p.direction = s.direction
                    WHERE s.cycle_ts_utc = ?
                      AND s.profile_id IN (""" + active_profile_placeholders + """)
                      AND s.selection_state = 'SELECTED'
                      AND p.status = 'APPROVED'
                      AND s.asset_class IN ('forex', 'crypto', 'equity', 'metal', 'index', 'commodity', 'energy')
                    ORDER BY s.selection_rank, s.symbol
                    """,
                    [cycle_ts, *active_profile_ids],
                ).fetchall()
                shortlist_prices = self._load_latest_symbol_prices(
                    con,
                    symbols=[str(row[1]) for row in raw_rows],
                    entry_price_cycle_ts=cycle_ts,
                )
        except Exception as exc:
            logger.warning("Could not build shadow-gate shortlist query: %s", exc)
            return None

        for (
            profile_id,
            symbol,
            asset_class,
            direction,
            predicted_return,
            conviction,
            economics_state,
            selection_rank,
            selection_reason,
            route_tier,
            display_label,
            selection_state,
            direction_streak,
            top_rank_streak,
            flip_count_window,
            live_followthrough_state,
            predicted_move_pips,
            after_cost_pips,
            predicted_move_bps,
            after_cost_bps,
            stop_price,
            tp_price,
            shares_or_lots,
            status,
            rejection_reason,
            risk_dollars,
            risk_pct,
        ) in raw_rows:
            rows.append(
                {
                    "profile_id": str(profile_id or ""),
                    "symbol": str(symbol or "").upper(),
                    "asset_class": str(asset_class or "").lower(),
                    "direction": str(direction or "").upper(),
                    "predicted_return": predicted_return,
                    "conviction": conviction,
                    "economics_state": economics_state,
                    "rank": selection_rank,
                    "selection_reason": selection_reason,
                    "route_tier": route_tier,
                    "display_label": display_label,
                    "selection_state": selection_state,
                    "direction_streak": direction_streak,
                    "top_rank_streak": top_rank_streak,
                    "flip_count_window": flip_count_window,
                    "live_followthrough_state": live_followthrough_state,
                    "predicted_move_pips": predicted_move_pips,
                    "after_cost_pips": after_cost_pips,
                    "predicted_move_bps": predicted_move_bps,
                    "after_cost_bps": after_cost_bps,
                    "session_label": self._session_label_for_cycle_ts(cycle_ts),
                    "model_horizon_hint": "90–120m",
                    "exit_by_window": self._format_exit_by_window(cycle_ts),
                    "timeout_policy": self._timeout_policy_for_cycle_ts(cycle_ts, asset_class or "forex"),
                    "portfolio": {
                        "account_id": str(profile_id or ""),
                        "stop_price": stop_price,
                        "tp_price": tp_price,
                        "lot_size": shares_or_lots,
                        "status": str(status or "").upper(),
                        "rejection_reason": str(rejection_reason or "").strip(),
                        "risk_dollars": risk_dollars,
                        "risk_pct": risk_pct,
                    },
                }
            )

        display_n = int(((self._runtime_config or {}).get("runtime") or {}).get("shortlist_display_top_n") or 10)
        conviction_min = float(get_shadow_gate_config().get("conviction_min", 0.60) or 0.60)
        passed_rows = self._approved_shadow_gate_rows(rows)
        if not rows:
            return None
        lines = [
            f"🎯 <b>[{self._telegram_env_label()}] SHADOW-GATE SHORTLIST</b> — {cycle_ts[:16]}",
            f"Approved rows that pass observational conviction gate (conv* ≥ {conviction_min:.2f})",
            self._build_session_status_block(),
            "",
        ]
        if not passed_rows:
            lines.append("━━ APPROVED + SHADOW PASS ━━")
            lines.append(f"No approved rows passed conv* ≥ {conviction_min:.2f} this cycle.")
            lines.append(f"Approved rows checked: {len(rows)}")
            lines.append("")
            lines.append("Observational temp filter only. Not an active routing policy.")
            return "\n".join(lines).rstrip()

        lines.append("━━ APPROVED + SHADOW PASS ━━")
        for row in passed_rows[:display_n]:
            lines.append(
                self._format_v5_stage_telegram_row(
                    row=row,
                    price=shortlist_prices.get(str(row.get("symbol") or "").upper()),
                    stage="approved_manual",
                )
            )
            lines.append(self._format_shadow_gate_diag(row))
        lines.append("")
        lines.append("Observational temp filter only. Not an active routing policy.")
        return "\n".join(lines).rstrip()

    def _build_session_status_block(
        self,
        *,
        asset_class: str | None = None,
        profile_id: str | None = None,
        profile_ids: list[str] | None = None,
    ) -> str:
        """Return a compact session-state header for operator context."""
        visible_assets = self._telegram_visible_asset_classes()
        lines = [f"{asset.upper()} {self._asset_session_label(asset)}" for asset in visible_assets]
        primary_asset = str((asset_class or (visible_assets[0] if visible_assets else "forex")) or "forex").lower()
        requested_ids: list[str] = []
        if profile_ids:
            requested_ids.extend(str(pid or "").strip() for pid in profile_ids if str(pid or "").strip())
        elif profile_id:
            requested_ids.append(str(profile_id or "").strip())

        if requested_ids:
            seen_profile_ids: set[str] = set()
            for requested_profile_id in requested_ids:
                if requested_profile_id in seen_profile_ids:
                    continue
                seen_profile_ids.add(requested_profile_id)
                account_line = self._build_account_snapshot_line(
                    profile_id=requested_profile_id,
                    asset_class=primary_asset,
                )
                if account_line:
                    lines.append(account_line)
        else:
            account_line = self._build_account_snapshot_line(profile_id=profile_id, asset_class=primary_asset)
            if account_line:
                lines.append(account_line)
        routing_pause = self._active_routing_pause_note()
        if routing_pause:
            lines.append(f"Entry Pause: {routing_pause}")
        return "\n".join(lines)

    def _telegram_visible_asset_classes(self) -> list[str]:
        """Active asset lanes worth showing in operator Telegram surfaces."""
        standard_order = ("crypto", "forex", "equity", "metal", "index", "commodity", "energy")
        if self._force_assets:
            return [asset for asset in standard_order if asset in self._force_assets]
        visible: list[str] = []
        for asset in standard_order:
            if any(self._get_runtime_universe_symbols(str(account.get("id") or ""), asset) for account in (self._accounts or [])):
                visible.append(asset)
        return visible or ["forex"]

    def _telegram_env_label(self) -> str:
        envs = []
        for account in self._accounts or []:
            env = str(account.get("environment") or "").strip().upper()
            if env and env not in envs:
                envs.append(env)
        if envs:
            return "/".join(envs)
        db_lower = str(self.db_path or "").lower()
        if "qaenv" in db_lower:
            return "QA"
        return "PROD"

    def _profile_display_label(self, profile_id: str) -> str:
        for account in self._accounts or []:
            if str(account.get("id") or "") == str(profile_id or ""):
                return str(account.get("name") or account.get("id") or profile_id)
        return str(profile_id or "").replace("_demo_", " ").replace("_", " ").upper()

    def _runtime_profile_by_id(self, profile_id: str) -> dict[str, Any]:
        wanted = str(profile_id or "").strip()
        if not wanted:
            return {}
        cfg = self._runtime_config or get_runtime_config()
        for profile in cfg.get("profiles") or []:
            if str((profile or {}).get("id") or "").strip() == wanted:
                return dict(profile or {})
        return {}

    def _resolve_active_telegram_binding(
        self,
        asset_class: str,
        *,
        preferred_profile_id: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        cfg = self._runtime_config or get_runtime_config()
        asset = str(asset_class or "forex").strip().lower() or "forex"

        preferred_ids: list[str] = []
        if preferred_profile_id:
            preferred_ids.append(str(preferred_profile_id))
        preferred_ids.extend(
            str(account.get("id") or "")
            for account in (self._accounts or [])
            if str(account.get("id") or "").strip()
        )

        seen: set[str] = set()
        ordered_ids: list[str] = []
        for profile_id in preferred_ids:
            pid = str(profile_id or "").strip()
            if pid and pid not in seen:
                ordered_ids.append(pid)
                seen.add(pid)

        active: list[tuple[str, dict[str, Any]]] = []
        for profile_id in ordered_ids:
            profile_cfg = self._runtime_profile_by_id(profile_id)
            if not profile_cfg:
                continue
            try:
                binding = resolve_profile_lane_binding(
                    profile_cfg,
                    asset,
                    runtime_cfg=cfg,
                    strict=False,
                    allow_legacy=binding_allows_legacy(profile_cfg, asset, runtime_cfg=cfg),
                )
            except Exception:
                continue
            if not bool(binding.get("enabled", False)):
                continue
            if str(binding.get("execution_mode") or "").strip().lower() != "auto":
                continue
            if not str(binding.get("execution_bridge") or "").strip():
                continue
            if not str(binding.get("context_source_id") or "").strip():
                continue
            active.append((profile_id, dict(binding or {})))

        if active:
            return active[0]
        if ordered_ids:
            return ordered_ids[0], {}
        return None, {}

    def _active_telegram_profile_ids(self, asset_class: str) -> list[str]:
        cfg = self._runtime_config or get_runtime_config()
        asset = str(asset_class or "forex").strip().lower() or "forex"
        active_ids: list[str] = []
        for account in self._accounts or []:
            profile_id = str(account.get("id") or "").strip()
            if not profile_id:
                continue
            profile_cfg = self._runtime_profile_by_id(profile_id)
            if not profile_cfg:
                active_ids.append(profile_id)
                continue
            try:
                binding = resolve_profile_lane_binding(
                    profile_cfg,
                    asset,
                    runtime_cfg=cfg,
                    strict=False,
                    allow_legacy=binding_allows_legacy(profile_cfg, asset, runtime_cfg=cfg),
                )
            except Exception:
                continue
            if not bool(binding.get("enabled", False)):
                continue
            if str(binding.get("execution_mode") or "").strip().lower() == "auto":
                active_ids.append(profile_id)
        return active_ids

    def _active_telegram_bindings(self, asset_class: str) -> list[tuple[str, dict[str, Any]]]:
        cfg = self._runtime_config or get_runtime_config()
        asset = str(asset_class or "forex").strip().lower() or "forex"
        active_bindings: list[tuple[str, dict[str, Any]]] = []
        for profile_id in self._active_telegram_profile_ids(asset):
            profile_cfg = self._runtime_profile_by_id(profile_id)
            if not profile_cfg:
                continue
            try:
                binding = resolve_profile_lane_binding(
                    profile_cfg,
                    asset,
                    runtime_cfg=cfg,
                    strict=False,
                    allow_legacy=binding_allows_legacy(profile_cfg, asset, runtime_cfg=cfg),
                )
            except Exception:
                continue
            if not bool(binding.get("enabled", False)):
                continue
            if str(binding.get("execution_mode") or "").strip().lower() != "auto":
                continue
            active_bindings.append((profile_id, dict(binding or {})))
        return active_bindings

    def _context_snapshot_max_age_seconds(self, asset_class: str) -> int:
        cfg = self._runtime_config or get_runtime_config()
        asset = str(asset_class or "forex").strip().lower() or "forex"
        v2_cfg = (cfg.get("v2_prefilter") or {})
        defaults = dict(v2_cfg.get("defaults") or {})
        asset_thresholds = ((v2_cfg.get("asset_thresholds") or {}).get(asset) or {})
        max_stale_minutes = asset_thresholds.get("max_stale_minutes", defaults.get("max_stale_minutes", 5.0))
        try:
            return max(int(float(max_stale_minutes) * 60), 60)
        except Exception:
            return 300

    def _snapshot_is_fresh(
        self,
        ts_raw: Any,
        *,
        asset_class: str,
    ) -> bool:
        if not ts_raw:
            return False
        try:
            age_seconds = (self._now_utc() - parse_utc(str(ts_raw))).total_seconds()
        except Exception:
            return False
        return age_seconds <= float(self._context_snapshot_max_age_seconds(asset_class))

    @staticmethod
    def _table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
        try:
            return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table_name})").fetchall()}
        except Exception:
            return set()

    @staticmethod
    def _optional_col_expr(
        available_cols: set[str],
        alias: str,
        column: str,
        *,
        fallback_sql: str = "NULL",
        out_alias: str | None = None,
    ) -> str:
        expr = f"{alias}.{column}" if column in available_cols else fallback_sql
        if out_alias:
            return f"{expr} AS {out_alias}"
        return expr

    def _build_account_snapshot_line(
        self,
        *,
        profile_id: str | None = None,
        asset_class: str | None = None,
    ) -> str:
        asset = str(asset_class or "forex").strip().lower() or "forex"
        resolved_profile_id, _binding = self._resolve_active_telegram_binding(
            asset,
            preferred_profile_id=profile_id,
        )
        if not resolved_profile_id:
            return ""
        profile_id = resolved_profile_id
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                account_cols = self._table_columns(con, "vanguard_context_account_latest")
                pos_cols = self._table_columns(con, "vanguard_context_positions_latest")
                floating_pnl_expr = "floating_pnl" if "floating_pnl" in pos_cols else "0"
                ticket_expr = "ticket" if "ticket" in pos_cols else "rowid"
                ts_expr = "received_ts_utc" if "received_ts_utc" in pos_cols else "NULL"
                account = fetch_latest_context_account_row(
                    con,
                    profile_id=profile_id,
                    runtime_cfg=get_runtime_config(),
                    require_ok="source_status" in account_cols,
                )
                if not account:
                    account = con.execute(
                        """
                        SELECT profile_id, balance, equity, free_margin, received_ts_utc
                        FROM vanguard_context_account_latest
                        WHERE profile_id = ?
                        ORDER BY COALESCE(received_ts_utc, '') DESC
                        LIMIT 1
                        """,
                        (profile_id,),
                    ).fetchone()
                pos = con.execute(
                    f"""
                    SELECT COUNT(*) AS open_positions,
                           COALESCE(SUM(COALESCE(floating_pnl, 0)), 0) AS unrealized_pnl,
                           MAX(received_ts_utc) AS positions_ts_utc
                    FROM (
                        SELECT {ticket_expr} AS ticket,
                               {floating_pnl_expr} AS floating_pnl,
                               {ts_expr} AS received_ts_utc,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ticket
                                   ORDER BY received_ts_utc DESC
                               ) AS rn
                        FROM vanguard_context_positions_latest
                        WHERE profile_id = ?
                          AND source_status = 'OK'
                    )
                    WHERE rn = 1
                    """,
                    (profile_id,),
                ).fetchone()
                state = None
                if "vanguard_account_state" in {
                    str(row[0])
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }:
                    state = con.execute(
                        """
                        SELECT equity, updated_at_utc, open_positions_json
                        FROM vanguard_account_state
                        WHERE profile_id = ?
                        LIMIT 1
                        """,
                        (profile_id,),
                    ).fetchone()
        except Exception as exc:
            logger.warning("Could not build account snapshot line: %s", exc)
            return ""
        profile_label = self._profile_display_label(profile_id)
        account_fresh = bool(account) and self._snapshot_is_fresh(account["received_ts_utc"], asset_class=asset)
        positions_fresh = bool(pos) and self._snapshot_is_fresh(
            pos["positions_ts_utc"] if pos else None,
            asset_class=asset,
        )

        def _fmt_ts(ts_raw: Any) -> str:
            if not ts_raw:
                return "-"
            try:
                return utc_to_et(parse_utc(str(ts_raw))).strftime("%H:%M:%S ET")
            except Exception:
                return str(ts_raw)

        if account_fresh and positions_fresh:
            balance = float(account["balance"] or 0.0)
            equity = float(account["equity"] or 0.0)
            free_margin = float(account["free_margin"] or 0.0)
            open_positions = int((pos["open_positions"] if pos else 0) or 0)
            unrealized_pnl = float((pos["unrealized_pnl"] if pos else 0.0) or 0.0)
            ts_raw = (pos["positions_ts_utc"] if pos and pos["positions_ts_utc"] else account["received_ts_utc"])
            cached_fallback = False
            try:
                raw_payload = json.loads(account["raw_payload_json"] or "{}")
                cached_fallback = bool(raw_payload.get("cached_fallback"))
            except Exception:
                cached_fallback = False
            if cached_fallback:
                equity_est = balance + unrealized_pnl
                return (
                    f"ACCOUNT {profile_label} {_fmt_ts(ts_raw)}  "
                    f"Bal {balance:.2f}  Eq~ {equity_est:.2f}  "
                    f"Pos {open_positions}  UPL {unrealized_pnl:+.2f}  acct=cached"
                )
            return (
                f"ACCOUNT {profile_label} {_fmt_ts(ts_raw)}  "
                f"Bal {balance:.2f}  Eq {equity:.2f}  FM {free_margin:.2f}  "
                f"Pos {open_positions}  UPL {unrealized_pnl:+.2f}"
            )

        if state and self._snapshot_is_fresh(state["updated_at_utc"], asset_class=asset):
            try:
                open_positions_json = json.loads(state["open_positions_json"] or "[]")
            except Exception:
                open_positions_json = []
            open_positions = len(open_positions_json) if isinstance(open_positions_json, list) else 0
            equity = float(state["equity"] or 0.0)
            return (
                f"ACCOUNT {profile_label} {_fmt_ts(state['updated_at_utc'])}  "
                f"Eq {equity:.2f}  Pos {open_positions}  UPL n/a"
            )

        return f"ACCOUNT {profile_label} context stale"

    def _build_operator_diagnostics_telegram(self, cycle_ts: str) -> Optional[str]:
        tg_cfg = ((self._runtime_config or {}).get("telegram") or {}).get("diagnostics") or {}
        if not bool(tg_cfg.get("enabled", False)):
            return None
        max_rows = max(1, int(tg_cfg.get("max_stage_rows") or 6))
        include_context_state = bool(tg_cfg.get("include_context_state", True))
        include_v2_health = bool(tg_cfg.get("include_v2_health", True))

        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                v5_rows = [dict(row) for row in con.execute(
                    """
                    SELECT s.symbol, s.asset_class, s.direction, s.selection_state, s.selection_rank, s.route_tier,
                           s.direction_streak, t.economics_state, t.predicted_move_pips, t.after_cost_pips,
                           t.predicted_move_bps, t.after_cost_bps
                    FROM vanguard_v5_selection s
                    JOIN vanguard_v5_tradeability t
                      ON s.cycle_ts_utc = t.cycle_ts_utc
                     AND s.profile_id = t.profile_id
                     AND s.symbol = t.symbol
                     AND s.direction = t.direction
                    WHERE s.cycle_ts_utc = ?
                    ORDER BY s.selection_rank IS NULL, s.selection_rank, s.symbol
                    """,
                    (cycle_ts,),
                )]
                portfolio_rows = [dict(row) for row in con.execute(
                    """
                    SELECT account_id, symbol, direction, status, rejection_reason
                    FROM vanguard_tradeable_portfolio
                    WHERE cycle_ts_utc = ?
                    ORDER BY account_id, symbol, direction
                    """,
                    (cycle_ts,),
                )]
                health_rows = [dict(row) for row in con.execute(
                    """
                    SELECT asset_class, status, COUNT(*) AS row_count
                    FROM vanguard_health
                    WHERE cycle_ts_utc = ?
                    GROUP BY asset_class, status
                    ORDER BY asset_class, status
                    """,
                    (cycle_ts,),
                )]
        except Exception as exc:
            logger.warning("Could not build operator diagnostics Telegram: %s", exc)
            return None

        by_key = {(str(r["symbol"]).upper(), str(r["direction"]).upper()): r for r in portfolio_rows}
        v5_fail = []
        v5_weak = []
        risky_blocked = []
        v6_held = []
        approved = []
        for row in v5_rows:
            key = (str(row.get("symbol") or "").upper(), str(row.get("direction") or "").upper())
            portfolio = by_key.get(key)
            economics = str(row.get("economics_state") or "").upper()
            selection_state = str(row.get("selection_state") or "").upper()
            if economics == "FAIL":
                v5_fail.append(row)
            elif economics == "WEAK":
                v5_weak.append(row)
            elif portfolio:
                pstatus = str(portfolio.get("status") or "").upper()
                reason = str(portfolio.get("rejection_reason") or "")
                if pstatus == "APPROVED":
                    approved.append((row, portfolio))
                elif pstatus == "REJECTED":
                    risky_blocked.append((row, portfolio))
                elif pstatus == "HOLD":
                    v6_held.append((row, portfolio))
            elif selection_state == "SELECTED":
                # Selected but never materialized into V6 rows yet.
                v6_held.append((row, {"status": "HOLD", "rejection_reason": "NO_V6_ROW"}))

        lines = [f"🧭 <b>[{self._telegram_env_label()}] VANGUARD DIAGNOSTICS</b> — {html.escape(cycle_ts[:16])}"]
        account_line = self._build_account_snapshot_line()
        if account_line:
            lines.append(account_line)
        lines.append("")

        def _append_stage(title: str, rows: list[str]) -> None:
            if not rows:
                return
            lines.append(f"━━ {title.upper()} ━━")
            lines.extend(rows[:max_rows])
            lines.append("")

        _append_stage(
            "V5.1 FAIL",
            [self._format_rejection_stage_row(row, "V5.1 FAIL") for row in v5_fail],
        )
        _append_stage(
            "V5.1 WEAK",
            [self._format_rejection_stage_row(row, "V5.1 WEAK") for row in v5_weak],
        )
        _append_stage(
            "Risky Blocked",
            [self._format_blocked_stage_row(row, portfolio, "RISKY") for row, portfolio in risky_blocked],
        )
        _append_stage(
            "V6 Hold",
            [self._format_blocked_stage_row(row, portfolio, "V6 HOLD") for row, portfolio in v6_held],
        )

        if include_v2_health and health_rows:
            lines.append("━━ V2 HEALTH ━━")
            grouped: dict[str, list[str]] = defaultdict(list)
            visible_assets = set(self._telegram_visible_asset_classes())
            for row in health_rows:
                asset_class = str(row.get("asset_class") or "unknown").lower()
                if asset_class not in visible_assets:
                    continue
                grouped[str(row.get("asset_class") or "unknown").lower()].append(
                    f"{str(row.get('status') or 'UNKNOWN').upper()} {int(row.get('row_count') or 0)}"
                )
            for asset_class in sorted(grouped.keys()):
                lines.append(f"{asset_class.upper()}: " + " | ".join(grouped[asset_class]))
            lines.append("")

        if include_context_state:
            context_lines = self._build_context_state_diag_lines()
            if context_lines:
                lines.append("━━ CONTEXT / STATE ━━")
                lines.extend(context_lines)
                lines.append("")

        approved_count = len(approved)
        blocked_count = len(risky_blocked)
        hold_count = len(v6_held)
        fail_count = len(v5_fail)
        weak_count = len(v5_weak)
        lines.append(
            f"Summary: V5 FAIL {fail_count} | V5 WEAK {weak_count} | Risky Blocked {blocked_count} | V6 Hold {hold_count} | Approved {approved_count}"
        )
        return "\n".join(lines).rstrip()

    def _build_context_state_diag_lines(self) -> list[str]:
        diag_cfg = (((self._runtime_config or {}).get("runtime") or {}).get("context_state_diagnostics") or {})
        forex_fresh = int(diag_cfg.get("forex_fresh_seconds") or 180)
        crypto_fresh = int(diag_cfg.get("crypto_fresh_seconds") or 120)
        equity_fresh = int(diag_cfg.get("equity_fresh_seconds") or 180)
        lines: list[str] = []
        active_profile_ids = [str(account.get("id") or "") for account in (self._accounts or []) if str(account.get("id") or "").strip()]
        if not active_profile_ids:
            return lines
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                for profile_id in active_profile_ids:
                    visible_assets = set(self._telegram_visible_asset_classes())
                    for asset_class in ("forex", "crypto", "equity", "metal", "index", "commodity", "energy"):
                        if asset_class not in visible_assets:
                            continue
                        symbols = self._get_runtime_universe_symbols(profile_id, asset_class)
                        if not symbols:
                            # Skip asset classes this profile doesn't carry — avoids
                            # printing empty METAL/INDEX rows for equity-only profiles.
                            continue
                        fresh_seconds = (
                            forex_fresh if asset_class == "forex"
                            else crypto_fresh if asset_class == "crypto"
                            else equity_fresh
                        )
                        quote_table = "vanguard_context_quote_latest"
                        state_table = (
                            "vanguard_forex_pair_state"
                            if asset_class == "forex"
                            else "vanguard_crypto_symbol_state"
                            if asset_class == "crypto"
                            else "vanguard_metal_symbol_state"
                            if asset_class == "metal"
                            else "vanguard_equity_symbol_state"
                        )
                        expected = len(symbols)
                        fresh = stale = missing = 0
                        latest_quote_ts = None
                        if symbols:
                            placeholders = ",".join("?" for _ in symbols)
                            rows = con.execute(
                                f"""
                                SELECT symbol,
                                       COALESCE(received_ts_utc, quote_ts_utc) AS effective_quote_ts_utc
                                FROM {quote_table}
                                WHERE profile_id = ?
                                  AND symbol IN ({placeholders})
                                """,
                                [profile_id, *symbols],
                            ).fetchall()
                            seen = set()
                            now_dt = now_utc()
                            for row in rows:
                                symbol = str(row["symbol"]).upper()
                                seen.add(symbol)
                                ts = (
                                    parse_utc(str(row["effective_quote_ts_utc"]))
                                    if row["effective_quote_ts_utc"]
                                    else None
                                )
                                if ts and (latest_quote_ts is None or ts > latest_quote_ts):
                                    latest_quote_ts = ts
                                age = None if ts is None else max(0, int((now_dt - ts).total_seconds()))
                                if age is not None and age <= fresh_seconds:
                                    fresh += 1
                                else:
                                    stale += 1
                            missing = max(0, expected - len(seen))
                        state_row = con.execute(
                            f"""
                            SELECT COUNT(*) AS state_rows,
                                   COALESCE(SUM(CASE WHEN source_status = 'OK' THEN 1 ELSE 0 END), 0) AS state_ok,
                                   MAX(cycle_ts_utc) AS latest_cycle_ts
                            FROM {state_table}
                            WHERE profile_id = ?
                            """,
                            (profile_id,),
                        ).fetchone()
                        age_text = "-"
                        if latest_quote_ts is not None:
                            age_text = str(max(0, int((now_utc() - latest_quote_ts).total_seconds())))
                        lines.append(
                            f"{profile_id} {asset_class.upper()}: quotes fresh {fresh}/{expected} stale {stale} missing {missing} age_s {age_text} | state {int(state_row['state_ok'] or 0)}/{int(state_row['state_rows'] or 0)}"
                        )
        except Exception as exc:
            logger.warning("Could not build context/state diagnostics lines: %s", exc)
        return lines

    def _format_rejection_stage_row(self, row: dict[str, Any], stage: str) -> str:
        symbol = str(row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").upper()
        asset_class = str(row.get("asset_class") or "").lower()
        move_text, after_text = self._format_move_after_texts(row, asset_class)
        return f"{symbol} {direction} — {stage} | Move {move_text} | After {after_text}"

    def _format_blocked_stage_row(self, row: dict[str, Any], portfolio: dict[str, Any], stage: str) -> str:
        symbol = str(row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").upper()
        reason = self._format_telegram_reason(str(portfolio.get("rejection_reason") or portfolio.get("status") or ""))
        return f"{symbol} {direction} — {stage} {reason}"

    def _format_move_after_texts(self, row: dict[str, Any], asset_class: str) -> tuple[str, str]:
        move_text = "-"
        after_text = "-"
        if asset_class == "forex":
            if row.get("predicted_move_pips") is not None:
                move_text = f"{float(row.get('predicted_move_pips') or 0.0):.2f}p"
            if row.get("after_cost_pips") is not None:
                after_text = f"{float(row.get('after_cost_pips') or 0.0):.2f}p"
        else:
            if row.get("predicted_move_bps") is not None:
                move_text = f"{float(row.get('predicted_move_bps') or 0.0):.2f}bps"
            if row.get("after_cost_bps") is not None:
                after_text = f"{float(row.get('after_cost_bps') or 0.0):.2f}bps"
        return move_text, after_text

    def _asset_session_label(self, asset_class: str) -> str:
        """Human-readable LIVE/CLOSED state for shortlist headers."""
        asset = str(asset_class or "").lower().strip()
        if self._is_polling_asset_enabled(asset):
            return "LIVE 24/7" if asset == "crypto" else "LIVE"
        if asset == "forex":
            return "CLOSED → opens Sun 17:00 ET"
        if asset in {"equity", "index"}:
            return "CLOSED → opens Mon 09:30 ET"
        return "CLOSED"

    def _build_v6_summary_telegram(self, cycle_ts: str) -> Optional[str]:
        """Build one GFT-only crypto V6 summary — deduped by symbol (not per account).

        gft_10k is preferred when both accounts have a row for the same symbol.
        """
        try:
            with sqlite_conn(self.db_path) as con:
                rows = con.execute(
                    """
                    SELECT account_id,
                           symbol,
                           direction,
                           entry_price,
                           stop_price,
                           tp_price,
                           shares_or_lots,
                           edge_score,
                           status,
                           COALESCE(rejection_reason, '')
                    FROM vanguard_tradeable_portfolio
                    WHERE cycle_ts_utc = ?
                      AND account_id IN ('gft_10k', 'gft_5k')
                    ORDER BY account_id, status, direction, edge_score DESC, symbol
                    """,
                    (cycle_ts,),
                ).fetchall()
        except Exception as exc:
            logger.warning("Could not build GFT V6 Telegram summary: %s", exc)
            return (
                f"🧮 <b>GFT V6 CRYPTO</b> — {cycle_ts[:16]}\n"
                f"⚠️ unavailable: <code>{html.escape(str(exc))}</code>"
            )

        if not rows:
            return (
                f"🧮 <b>GFT V6 CRYPTO</b> — {cycle_ts[:16]}\n"
                "⚪ No GFT V6 rows this cycle"
            )

        gft_crypto_universe = self._load_active_crypto_universe_symbols()

        # Deduplicate by symbol — prefer gft_10k row; rows are sorted account_id ASC
        # so gft_10k appears before gft_5k; overwrite with gft_10k if seen later.
        seen_symbols: dict[str, dict[str, Any]] = {}
        for (
            account_id,
            symbol,
            direction,
            entry_price,
            stop_price,
            tp_price,
            shares_or_lots,
            edge_score,
            status,
            rejection_reason,
        ) in rows:
            normalized_symbol = self._normalize_gft_display_symbol(symbol)
            if normalized_symbol not in gft_crypto_universe:
                continue
            sym_key = str(symbol or "").upper()
            if sym_key not in seen_symbols or str(account_id) == "gft_10k":
                seen_symbols[sym_key] = {
                    "symbol": sym_key,
                    "direction": str(direction or "UNKNOWN").upper(),
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    "tp_price": tp_price,
                    "shares_or_lots": shares_or_lots,
                    "edge_score": edge_score,
                    "status": str(status or "UNKNOWN").upper(),
                    "rejection_reason": str(rejection_reason or "").strip(),
                }

        if not seen_symbols:
            return (
                f"🧮 <b>GFT V6 CRYPTO</b> — {cycle_ts[:16]}\n"
                "⚪ No GFT crypto rows in current universe"
            )

        # Group by status → direction
        by_status: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in seen_symbols.values():
            by_status[row["status"]][row["direction"]].append(row)

        ok_count = sum(len(v) for v in by_status.get("APPROVED", {}).values())
        reject_count = sum(
            len(v)
            for status_name, status_rows in by_status.items()
            if status_name != "APPROVED"
            for v in status_rows.values()
        )

        lines = [
            f"🧮 <b>GFT V6 CRYPTO</b> — {cycle_ts[:16]}",
            f"OK {ok_count} / RJ {reject_count}",
            "",
        ]
        for status_name, status_label in (
            ("APPROVED", "✅ APPROVED"),
            ("REJECTED", "🚫 REJECTED"),
        ):
            status_rows = by_status.get(status_name)
            if not status_rows:
                continue
            lines.append(status_label)
            for direction_name, side_emoji in (("LONG", "🟢"), ("SHORT", "🔴")):
                trade_rows = status_rows.get(direction_name, [])
                if not trade_rows:
                    continue
                lines.append(f"{side_emoji} {direction_name} ({len(trade_rows)})")
                for row in trade_rows[:6]:
                    status_note = (
                        "Status OK"
                        if status_name == "APPROVED"
                        else f"Reject {row['rejection_reason'] or 'REJECTED'}"
                    )
                    lines.append(
                        self._format_shortlist_telegram_row(
                            symbol=row["symbol"],
                            asset_class="crypto",
                            direction=direction_name,
                            edge_score=row["edge_score"],
                            price=row["entry_price"],
                            streak_counter=self._shortlist_streak_counter(
                                row["symbol"]
                            ),
                            gft_details={
                                "stop_price": row["stop_price"],
                                "tp_price": row["tp_price"],
                                "lot_size": row["shares_or_lots"],
                            },
                            status_note=status_note,
                        )
                    )
                lines.append("")

        return "\n".join(lines).rstrip()

    def _build_execution_summary_telegram(
        self,
        source: str,
        account: str,
        submitted: int,
        filled: int,
        failed: int,
        forward_tracked: int,
        failure_reasons: Counter[str] | None = None,
    ) -> str:
        """Build one compact execution summary with a manual GFT note."""
        emoji = "✅" if failed == 0 else "⚠️"
        note = "Manual mode: forward-tracked only" if self._is_manual_execution_mode() else "Live routing"
        rows = [
            "FIELD            VALUE",
            f"Source           {str(source or '').upper()}",
            f"Account          {str(account or '-')[:32]}",
            f"Mode             {self.execution_mode.upper()}",
            f"Submitted        {submitted}",
            f"Filled           {filled}",
            f"Failed           {failed}",
            f"Forward tracked  {forward_tracked}",
            f"Note             {note}",
            f"Time             {self._now_et().strftime('%H:%M:%S ET')}",
        ]
        if failure_reasons:
            top_error, top_count = failure_reasons.most_common(1)[0]
            rows.append(f"Top error        {str(top_error)[:40]} x{top_count}")
        return "\n".join(
            [
                f"{emoji} <b>EXECUTION RUN</b>",
                f"<pre>{html.escape(chr(10).join(rows))}</pre>",
            ]
        )

    def _record_shortlist_streak_history(
        self,
        shortlist_rows: list[tuple[Any, ...]],
        cycle_ts: str,
    ) -> None:
        # Side-only streak counter (last 5 cycles) for model direction stability.
        current_states: dict[str, str] = {}
        for symbol, _asset_class, direction, _predicted_return, _edge_score, _rank in shortlist_rows:
            normalized = str(symbol or "").upper()
            current_states[normalized] = str(direction or "FLAT").upper()

        for normalized in sorted(set(self._shortlist_direction_history.keys()) | set(current_states.keys())):
            state = current_states.get(normalized, "FLAT")
            history = self._shortlist_direction_history[normalized]
            if history and history[-1][0] == cycle_ts:
                history[-1] = (cycle_ts, state)
            else:
                history.append((cycle_ts, state))

    def _shortlist_streak_counter(self, symbol: str) -> str:
        history = [
            state
            for _cycle_ts, state in self._shortlist_direction_history.get(
                str(symbol or "").upper(),
                [],
            )
        ][-5:]
        current_side = history[-1] if history else "FLAT"
        side_letter = {"LONG": "L", "SHORT": "S", "FLAT": "F"}.get(
            current_side,
            "F",
        )
        emojis = "".join(
            {"LONG": "🟢", "SHORT": "🔴", "FLAT": "⚪"}.get(str(state or "FLAT").upper(), "⚪")
            for state in history
        )
        streak_count = 0
        if current_side in {"LONG", "SHORT"}:
            for state in reversed(history):
                if state != current_side:
                    break
                streak_count += 1
        if len(history) < 5:
            # Missing pre-session history should stay neutral instead of backfilling the current color.
            emojis = ("⚪" * (5 - len(history))) + emojis
        return f"{streak_count}{side_letter} {emojis}"

    def _build_gft_manual_execution_telegram(
        self,
        gft_manual_rows: list[dict[str, Any]],
    ) -> Optional[str]:
        """Build one GFT manual-execution summary per cycle to avoid Telegram spam."""
        if not gft_manual_rows:
            return None

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in gft_manual_rows:
            account = str(row.get("account") or "gft")
            direction = str(row.get("direction") or "UNKNOWN").upper()
            grouped[account][direction].append(row)

        lines = [
            f"✅ <b>EXECUTION RUN</b> — MANUAL ({self.execution_mode.upper()})",
            "",
        ]
        for account in sorted(grouped.keys()):
            lines.append(f"━━ {account} ━━")

            longs = grouped[account].get("LONG", [])
            if longs:
                long_text = ", ".join(
                    self._format_symbol_with_price(
                        symbol=str(row["symbol"]),
                        edge_score=row.get("edge_score"),
                        price=row.get("entry_price"),
                        suffix=f", x{float(row.get('shares') or 0):.2f}",
                    )
                    for row in longs
                )
                lines.append(f"🟢 LONG ({len(longs)}): {long_text}")

            shorts = grouped[account].get("SHORT", [])
            if shorts:
                short_text = ", ".join(
                    self._format_symbol_with_price(
                        symbol=str(row["symbol"]),
                        edge_score=row.get("edge_score"),
                        price=row.get("entry_price"),
                        suffix=f", x{float(row.get('shares') or 0):.2f}",
                    )
                    for row in shorts
                )
                lines.append(f"🔴 SHORT ({len(shorts)}): {short_text}")

            lines.append(f"✅ APPROVED: {len(longs) + len(shorts)} trades")
            lines.append("")

        return "\n".join(lines).rstrip()

    @staticmethod
    def _build_gft_time_exit_telegram(exit_rows: list[dict[str, Any]]) -> Optional[str]:
        """Build one Telegram note for GFT 4-hour time exits."""
        if not exit_rows:
            return None

        lines = ["⏱️ <b>GFT TIME EXIT 4H</b>", ""]
        for row in exit_rows:
            lines.append(
                f"{row.get('account_id', 'gft')} "
                f"{row.get('symbol', '?')} "
                f"{row.get('direction', '?')} "
                f"x{float(row.get('shares') or 0.0):.2f} "
                f"age={float(row.get('age_hours') or 0.0):.2f}h "
                f"→ {row.get('status', 'UNKNOWN')} "
                f"(TIME_EXIT_4H)"
            )
        return "\n".join(lines)

    def _load_latest_symbol_prices(
        self,
        con: sqlite3.Connection,
        symbols: list[str],
        entry_price_cycle_ts: str = "",
    ) -> dict[str, float]:
        """Resolve display prices from V6 entry_price first, then latest 5m bar close."""
        clean_symbols = sorted({str(symbol or "").upper() for symbol in symbols if str(symbol or "").strip()})
        if not clean_symbols:
            return {}

        prices: dict[str, float] = {}
        placeholders = ",".join("?" for _ in clean_symbols)

        if entry_price_cycle_ts:
            rows = con.execute(
                f"""
                SELECT symbol, MAX(entry_price)
                FROM vanguard_tradeable_portfolio
                WHERE cycle_ts_utc = ?
                  AND symbol IN ({placeholders})
                  AND entry_price IS NOT NULL
                GROUP BY symbol
                """,
                [entry_price_cycle_ts, *clean_symbols],
            ).fetchall()
            for symbol, entry_price in rows:
                if entry_price is not None:
                    prices[str(symbol).upper()] = float(entry_price)

        missing_symbols = [symbol for symbol in clean_symbols if symbol not in prices]
        if missing_symbols:
            missing_placeholders = ",".join("?" for _ in missing_symbols)
            rows = con.execute(
                f"""
                SELECT b.symbol, b.close
                FROM vanguard_bars_5m b
                JOIN (
                    SELECT symbol, MAX(bar_ts_utc) AS latest_ts
                    FROM vanguard_bars_5m
                    WHERE symbol IN ({missing_placeholders})
                    GROUP BY symbol
                ) latest
                  ON latest.symbol = b.symbol AND latest.latest_ts = b.bar_ts_utc
                """,
                missing_symbols,
            ).fetchall()
            for symbol, close_px in rows:
                if close_px is not None:
                    prices[str(symbol).upper()] = float(close_px)

        return prices

    def _format_symbol_with_price(
        self,
        symbol: str,
        edge_score: float | None,
        price: float | None,
        suffix: str = "",
    ) -> str:
        """Format SYMBOL current_price (score) while preserving old score display."""
        normalized = str(symbol or "").upper()
        if price is None:
            return f"{normalized} ({float(edge_score or 0.0):.2f}{suffix})"
        decimals = 5 if len(normalized.replace("/", "").replace(".CASH", "")) == 6 and abs(float(price)) < 10 else 2
        return f"{normalized} {float(price):.{decimals}f} ({float(edge_score or 0.0):.2f}{suffix})"

    def _format_shortlist_display_row(
        self,
        row: dict[str, Any],
        price: float | None,
        streak_counter: str = "",
        gft_details: dict[str, Any] | None = None,
    ) -> str:
        if str(row.get("display_mode") or "") == "v5" and str(row.get("asset_class") or "").lower() in {"forex", "crypto"}:
            return self._format_v5_shortlist_telegram_row(
                symbol=str(row.get("symbol") or ""),
                asset_class=str(row.get("asset_class") or ""),
                direction=str(row.get("direction") or ""),
                price=price,
                selection_rank=row.get("rank"),
                route_tier=str(row.get("route_tier") or ""),
                selection_state=str(row.get("selection_state") or ""),
                direction_streak=row.get("direction_streak"),
                predicted_move_pips=row.get("predicted_move_pips"),
                after_cost_pips=row.get("after_cost_pips"),
                predicted_move_bps=row.get("predicted_move_bps"),
                after_cost_bps=row.get("after_cost_bps"),
                economics_state=str(row.get("economics_state") or ""),
                gft_details=gft_details,
            )
        return self._format_shortlist_telegram_row(
            symbol=str(row.get("symbol") or ""),
            asset_class=str(row.get("asset_class") or ""),
            direction=str(row.get("direction") or ""),
            edge_score=row.get("edge_score"),
            price=price,
            streak_counter=streak_counter,
            gft_details=gft_details,
        )

    def _format_v5_stage_telegram_row(
        self,
        row: dict[str, Any],
        price: float | None,
        stage: str,
    ) -> str:
        normalized = str(row.get("symbol") or "").upper()
        asset_class = str(row.get("asset_class") or "").lower()
        direction = str(row.get("direction") or "UNKNOWN").upper()
        side_emoji = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"
        display_price = "-"
        if price is not None:
            decimals = 4 if asset_class == "forex" else 2
            if asset_class == "crypto" and abs(float(price)) < 100:
                decimals = 4
            display_price = f"{float(price):.{decimals}f}"

        move_text = "-"
        after_text = "-"
        if asset_class == "forex":
            if row.get("predicted_move_pips") is not None:
                move_text = f"{float(row.get('predicted_move_pips') or 0.0):.2f}p"
            if row.get("after_cost_pips") is not None:
                after_text = f"{float(row.get('after_cost_pips') or 0.0):.2f}p"
        else:
            if row.get("predicted_move_bps") is not None:
                move_text = f"{float(row.get('predicted_move_bps') or 0.0):.2f}bps"
            if row.get("after_cost_bps") is not None:
                after_text = f"{float(row.get('after_cost_bps') or 0.0):.2f}bps"

        rank = row.get("rank")
        rank_text = f"Selection Rank {int(rank)}" if rank not in (None, "") else "Selection Rank -"
        economics_text = str(row.get("economics_state") or "UNKNOWN").upper()
        selection_reason = str(row.get("selection_reason") or economics_text).upper()
        details = row.get("portfolio") or {}
        status = str(details.get("status") or "").upper()
        reason = self._format_telegram_reason(str(details.get("rejection_reason") or ""))
        risk_dollars = details.get("risk_dollars")
        risk_pct = details.get("risk_pct")
        sl = details.get("stop_price")
        tp = details.get("tp_price")
        qty = details.get("lot_size")
        thesis_state = str(row.get("thesis_state") or "NEW").upper()
        thesis_display = thesis_state
        if thesis_state.startswith("SEEN_RECENTLY"):
            thesis_display = thesis_state.replace("SEEN_RECENTLY", "REPEAT")
        session_label = str(row.get("session_label") or "Unknown")
        model_horizon_hint = str(row.get("model_horizon_hint") or "90–120m")
        exit_by_window = str(row.get("exit_by_window") or "-")
        timeout_policy = dict(row.get("timeout_policy") or {})
        timeout_guide = "-"
        if timeout_policy:
            timeout_guide = (
                f"{int(timeout_policy.get('timeout_minutes') or 0)}m "
                f"(dedupe {int(timeout_policy.get('dedupe_minutes') or 0)}) "
                f"→ {str(timeout_policy.get('timeout_label') or '-')}"
            )

        header = f"{side_emoji} {html.escape(normalized)}  {html.escape(display_price)}  {html.escape(direction)}"
        move_line = f"Move {html.escape(move_text)}  After {html.escape(after_text)}"

        if stage == "diagnostic":
            detail_text = rank_text if rank not in (None, "") else f"Reason {selection_reason}"
            return f"{header}\nV5 {html.escape(economics_text)}  {html.escape(detail_text)}\n{move_line}"

        if stage == "selected":
            return (
                f"{header}\n"
                f"V5 SELECTED  {html.escape(rank_text)}\n"
                f"{move_line}\n"
                f"Session {html.escape(session_label)}  Thesis {html.escape(thesis_display)}  Model Horizon {html.escape(model_horizon_hint)}\n"
                f"Exit by {html.escape(exit_by_window)}\n"
                f"Time Exit {html.escape(timeout_guide)}"
            )

        if stage == "blocked":
            blocked_note = "  Reentry Blocked" if str(details.get("rejection_reason") or "").upper() == "BLOCKED_ACTIVE_THESIS_REENTRY" else ""
            return (
                f"{header}\n"
                f"V5 SELECTED  {html.escape(rank_text)}\n"
                f"{move_line}\n"
                f"Session {html.escape(session_label)}  Thesis {html.escape(thesis_display)}  Model Horizon {html.escape(model_horizon_hint)}\n"
                f"Exit by {html.escape(exit_by_window)}\n"
                f"Time Exit {html.escape(timeout_guide)}\n"
                f"Decision {html.escape(reason or status or 'BLOCKED')}{html.escape(blocked_note)}"
            )

        risk_text = "-"
        if risk_dollars is not None:
            risk_text = f"${float(risk_dollars):.0f}"
            if risk_pct is not None:
                risk_text += f" {float(risk_pct) * 100:.02f}%"
        sl_text = "-" if sl in (None, "") else f"{float(sl):.5f}".rstrip("0").rstrip(".")
        tp_text = "-" if tp in (None, "") else f"{float(tp):.5f}".rstrip("0").rstrip(".")
        qty_text = "-" if qty in (None, "") else f"{float(qty):.6f}".rstrip("0").rstrip(".")
        if stage == "held":
            stage_label = "V6 HOLD"
        elif stage == "approved_manual":
            stage_label = "FORWARD TRACK"
        else:
            stage_label = "APPROVED CANDIDATE"
        stage_reason = f" {reason}" if reason else ""
        return (
            f"{header}\n"
            f"V5 SELECTED  {html.escape(rank_text)}\n"
            f"{move_line}\n"
            f"Session {html.escape(session_label)}  Thesis {html.escape(thesis_display)}  Model Horizon {html.escape(model_horizon_hint)}\n"
            f"Exit by {html.escape(exit_by_window)}\n"
            f"Time Exit {html.escape(timeout_guide)}\n"
            f"{html.escape(stage_label + stage_reason)}  Risk {html.escape(risk_text)}\n"
            f"SL {html.escape(sl_text)}  TP {html.escape(tp_text)}  Qty {html.escape(qty_text)}"
        )

    def _format_v5_shortlist_telegram_row(
        self,
        symbol: str,
        asset_class: str,
        direction: str,
        price: float | None,
        selection_rank: int | None,
        route_tier: str,
        selection_state: str,
        direction_streak: int | None,
        predicted_move_pips: float | None,
        after_cost_pips: float | None,
        predicted_move_bps: float | None,
        after_cost_bps: float | None,
        economics_state: str = "",
        gft_details: dict[str, Any] | None = None,
    ) -> str:
        normalized = str(symbol or "").upper()
        display_price = "-" if price is None else self._format_telegram_price(normalized, float(price))
        details = gft_details or {}
        stop_px = details.get("stop_price")
        tp_px = details.get("tp_price")
        lot_size = details.get("lot_size")
        timeout_minutes = details.get("timeout_minutes")
        timeout_label = details.get("timeout_label")
        dedupe_minutes = details.get("dedupe_minutes")
        sl_text = "-" if stop_px is None else self._format_telegram_price(normalized, float(stop_px))
        tp_text = "-" if tp_px is None else self._format_telegram_price(normalized, float(tp_px))
        if lot_size is None:
            lot_text = "-"
        elif str(asset_class or "").lower() == "crypto":
            lot_text = f"{float(lot_size):.6f}".rstrip("0").rstrip(".")
        else:
            lot_text = f"{float(lot_size):.2f}"
        side_emoji = "🟢" if str(direction or "").upper() == "LONG" else "🔴"
        unit = "bps" if str(asset_class or "").lower() == "crypto" else "p"
        predicted_move = predicted_move_bps if unit == "bps" else predicted_move_pips
        after_cost = after_cost_bps if unit == "bps" else after_cost_pips
        rank_text = f"R{int(selection_rank)}" if selection_rank is not None else "R-"
        streak_value = int(direction_streak or 0)
        streak_label = "LONG" if str(direction or "").upper() == "LONG" else "SHORT"
        tier_text = (route_tier or "NONE").upper()
        state_text = (selection_state or "UNKNOWN").upper()
        economics_text = (economics_state or "UNKNOWN").upper()
        move_text = "-" if predicted_move is None else f"{float(predicted_move):.2f}{unit}"
        after_text = "-" if after_cost is None else f"{float(after_cost):.2f}{unit}"
        portfolio_status = str(details.get("status") or "").upper()
        portfolio_reason = self._format_telegram_reason(str(details.get("rejection_reason") or ""))
        risk_dollars = details.get("risk_dollars")
        risk_pct = details.get("risk_pct")
        streak_text = f"Streak {streak_value} {streak_label}" if streak_value > 0 else "Streak -"
        rank_label = f"Rank {rank_text}"

        if state_text != "SELECTED":
            return (
                f"{side_emoji} {html.escape(normalized)}  {html.escape(display_price)}  "
                f"{html.escape(rank_label)}  {html.escape(streak_text)}\n"
                f"V5 {html.escape(economics_text)}  Move {html.escape(move_text)}  "
                f"After {html.escape(after_text)}"
            )

        v6_text = "-"
        if portfolio_status:
            v6_text = portfolio_status
            if portfolio_reason:
                v6_text = f"{v6_text} {portfolio_reason}"
        risk_text = "-"
        if risk_dollars is not None:
            risk_text = f"${float(risk_dollars):.0f}"
            if risk_pct is not None:
                risk_text += f" {float(risk_pct) * 100:.02f}%"
        return (
            f"{side_emoji} {html.escape(normalized)}  {html.escape(display_price)}  "
            f"{html.escape(rank_label)}  {html.escape(streak_text)}\n"
            f"Move {html.escape(move_text)}  After {html.escape(after_text)}  "
            f"{html.escape(tier_text)}  {html.escape(state_text)}\n"
            f"V6 {html.escape(v6_text)}  Risk {html.escape(risk_text)}\n"
            f"SL {html.escape(sl_text)}  TP {html.escape(tp_text)}  "
            f"Lot {html.escape(lot_text)}"
        )

    def _format_shortlist_telegram_row(
        self,
        symbol: str,
        asset_class: str,
        direction: str,
        edge_score: float | None,
        price: float | None,
        streak_counter: str = "",
        gft_details: dict[str, Any] | None = None,
        status_note: str = "",
    ) -> str:
        """Render one mobile-safe 2-line shortlist card."""
        normalized = str(symbol or "").upper()
        display_price = "-" if price is None else self._format_telegram_price(normalized, float(price))
        details = gft_details or {}
        stop_px = details.get("stop_price")
        tp_px = details.get("tp_price")
        lot_size = details.get("lot_size")
        sl_text = "-" if stop_px is None else self._format_telegram_price(normalized, float(stop_px))
        tp_text = "-" if tp_px is None else self._format_telegram_price(normalized, float(tp_px))
        if lot_size is None:
            lot_text = "-"
        elif str(asset_class or "").lower() == "crypto":
            lot_text = f"{float(lot_size):.6f}".rstrip("0").rstrip(".")
        else:
            lot_text = f"{float(lot_size):.2f}"
        streak_text = streak_counter or "F ⚪⚪⚪⚪⚪"
        side_emoji = "🟢" if str(direction or "").upper() == "LONG" else "🔴"
        timeout_minutes = details.get("timeout_minutes")
        timeout_label = details.get("timeout_label")
        dedupe_minutes = details.get("dedupe_minutes")
        timeout_note = ""
        if timeout_minutes not in (None, "") and dedupe_minutes not in (None, ""):
            timeout_note = (
                f"\nTimeout {int(timeout_minutes)}m (dedupe {int(dedupe_minutes)})"
                + (f" → {html.escape(str(timeout_label))}" if timeout_label else "")
            )
        return (
            f"{side_emoji} {html.escape(normalized)}  {html.escape(display_price)}  "
            f"Edge {float(edge_score or 0.0):.2f}  {html.escape(streak_text)}\n"
            f"SL {html.escape(sl_text)}  TP {html.escape(tp_text)}  "
            f"Lot {html.escape(lot_text)}"
            f"{timeout_note}"
            + (f"  {html.escape(status_note)}" if status_note else "")
        )

    def _load_active_crypto_universe_symbols(self) -> set[str]:
        """Load crypto symbols from active runtime profile scopes."""
        runtime_cfg = self._runtime_config or get_runtime_config()
        universes = runtime_cfg.get("universes") or {}
        scope_ids = {
            str(profile.get("instrument_scope") or "")
            for profile in (runtime_cfg.get("profiles") or [])
            if str(profile.get("is_active", "")).strip().lower() in {"1", "true", "yes", "on"}
        }
        symbols: set[str] = set()
        for scope_id in scope_ids:
            scope_cfg = universes.get(scope_id) or {}
            symbols_by_class = scope_cfg.get("symbols") if isinstance(scope_cfg.get("symbols"), dict) else {}
            for symbol in (symbols_by_class or {}).get("crypto", []) or []:
                symbols.add(self._normalize_gft_display_symbol(symbol))
        if symbols:
            return symbols
        return {"BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "BCHUSD", "SOLUSD", "BNBUSD", "ADAUSD", "DOTUSD", "AVAXUSD"}

    def _format_shortlist_telegram_item(
        self,
        symbol: str,
        asset_class: str,
        direction: str,
        edge_score: float | None,
        price: float | None,
        streak_counter: str = "",
        gft_details: dict[str, Any] | None = None,
    ) -> str:
        base = self._format_symbol_with_price(
            symbol=symbol,
            edge_score=edge_score,
            price=price,
        )
        if streak_counter:
            base = f"{base} {streak_counter}"
        if not gft_details:
            return base

        stop_price = gft_details.get("stop_price")
        tp_price = gft_details.get("tp_price")
        lot_size = gft_details.get("lot_size")
        if stop_price is None or tp_price is None or lot_size is None:
            return base

        normalized = str(symbol or "").upper().replace("/", "").replace(".CASH", "")
        decimals = 3 if normalized.endswith("JPY") else 5
        line = (
            f"{base} "
            f"SL:{float(stop_price):.{decimals}f} "
            f"TP:{float(tp_price):.{decimals}f} "
            f"Lot:{float(lot_size):.2f}"
        )
        return line

    @staticmethod
    def _format_telegram_price(symbol: str, price: float) -> str:
        normalized = str(symbol or "").upper().replace("/", "").replace(".CASH", "")
        if normalized.endswith("JPY"):
            return f"{price:.3f}"
        if len(normalized) == 6 and abs(price) < 10:
            return f"{price:.5f}"
        if abs(price) >= 100:
            return f"{price:.2f}"
        return f"{price:.4f}"

    @staticmethod
    def _format_telegram_reason(reason_code: str) -> str:
        normalized = str(reason_code or "").strip().upper()
        if not normalized:
            return ""
        labels = {
            "CRYPTO_VALIDATION_MODE_EXECUTION_DISABLED": "VALIDATION BLOCK",
            "CRYPTO_QUOTE_STALE": "QUOTE STALE",
            "BLOCKED_ACCOUNT_TRUTH_MISSING": "ACCOUNT TRUTH MISSING",
        }
        return labels.get(normalized, normalized.replace("_", " ")[:40])

    def _load_gft_open_symbols(self) -> set[str]:
        """Fetch currently open broker symbols from MetaApi and normalize to shortlist symbols."""
        if not self._has_gft_accounts():
            return set()

        try:
            from vanguard.executors.mt5_executor import MetaApiExecutor
        except Exception as exc:
            logger.warning("Could not import MetaApiExecutor for open-position tags: %s", exc)
            return set()

        open_symbols: set[str] = set()
        for account_key in ("gft_10k", "gft_5k"):
            executor = MetaApiExecutor(account_key=account_key)
            if not executor.token or not executor.account_id:
                continue
            try:
                positions = executor.get_open_positions()
            except Exception as exc:
                logger.warning("Could not load GFT open positions for %s: %s", account_key, exc)
                continue
            for position in positions:
                open_symbols.add(self._normalize_gft_display_symbol(position.get("symbol")))
        return {symbol for symbol in open_symbols if symbol}

    @staticmethod
    def _normalize_gft_display_symbol(symbol: Any) -> str:
        """Normalize MetaApi symbols and shortlist symbols to one comparison key."""
        return (
            str(symbol or "")
            .upper()
            .replace("/", "")
            .replace(".CASH", "")
            .replace(".X", "")
            .strip()
        )

    @staticmethod
    def _build_fallback_gft_forex_details(
        symbol: str,
        asset_class: str,
        direction: str,
        price: float | None,
    ) -> dict[str, float] | None:
        return VanguardOrchestrator._build_fallback_gft_display_details(
            symbol=symbol,
            asset_class=asset_class,
            direction=direction,
            price=price,
        )

    @staticmethod
    def _build_fallback_gft_display_details(
        symbol: str,
        asset_class: str,
        direction: str,
        price: float | None,
    ) -> dict[str, float] | None:
        """Display-only fallback SL/TP/Lot for non-approved rows."""
        asset = str(asset_class or "").lower()
        if price is None:
            return None

        normalized = str(symbol or "").upper().replace("/", "").replace(".CASH", "")
        entry_price = float(price)

        if asset == "crypto":
            sl_offset = max(entry_price * 0.08, 0.01)
            tp_offset = max(entry_price * 0.16, 0.02)
            decimals = 2 if entry_price >= 100 else 5 if entry_price < 10 else 4
            risk_dollars = 50.0
        elif asset == "forex":
            is_jpy = normalized.endswith("JPY")
            sl_offset = 0.20 if is_jpy else 0.0020
            tp_offset = 0.40 if is_jpy else 0.0040
            decimals = 3 if is_jpy else 5
            risk_dollars = 50.0
        elif asset in {"equity", "index"}:
            # ATR estimate: ~1.5% of price for SL, ~3% for TP
            sl_offset = round(entry_price * 0.015, 2)
            tp_offset = round(entry_price * 0.030, 2)
            decimals = 2
            risk_dollars = 50.0
        elif asset in {"commodity", "energy", "metal", "agriculture"}:
            # Commodities / energy / metals: ~2% SL, ~4% TP
            sl_offset = round(entry_price * 0.02, 2)
            tp_offset = round(entry_price * 0.04, 2)
            decimals = 2
            risk_dollars = 50.0
        else:
            return None

        if str(direction or "").upper() == "LONG":
            stop_price = round(entry_price - sl_offset, decimals)
            tp_price = round(entry_price + tp_offset, decimals)
        else:
            stop_price = round(entry_price + sl_offset, decimals)
            tp_price = round(entry_price - tp_offset, decimals)

        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return None

        if asset == "crypto":
            contract_size = VanguardOrchestrator._crypto_display_contract_size(symbol, entry_price)
            lot_size = math.floor((risk_dollars / (stop_distance * contract_size)) * 1_000_000.0) / 1_000_000.0
            lot_size = max(0.000001, lot_size)
        elif asset == "forex":
            # Lot size: risk_dollars / (sl_pips * pip_value_per_lot)
            # pip_value table lives in policy_templates (any gft template).
            try:
                _cfg = get_runtime_config()
                _tmpl = (
                    _cfg.get("policy_templates", {}).get("gft_10k_v1")
                    or _cfg.get("policy_templates", {}).get("gft_standard_v1")
                    or {}
                )
                _pip_tbl = (
                    _tmpl.get("sizing_by_asset_class", {})
                    .get("forex", {})
                    .get("pip_value_usd_per_standard_lot", {})
                )
            except Exception:
                _pip_tbl = {}
            pip_value = float(_pip_tbl.get(normalized, _pip_tbl.get("default", 10.0)))
            is_jpy = normalized.endswith("JPY")
            sl_pips = 20.0
            lot_size = round(risk_dollars / (sl_pips * pip_value), 2)
        else:
            # equity/index/commodity: whole shares/contracts
            lot_size = max(1.0, math.floor(risk_dollars / stop_distance))

        return {
            "stop_price": stop_price,
            "tp_price": tp_price,
            "lot_size": lot_size,
        }

    @staticmethod
    def _crypto_display_contract_size(symbol: str, entry_price: float) -> float:
        """Display-side approximation of broker contract unit for crypto symbols."""
        normalized = (
            str(symbol or "")
            .upper()
            .replace("/", "")
            .replace(".CASH", "")
            .replace(".X", "")
            .strip()
        )
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

    def _send_telegram(self, message: str, *, message_type: str | None = None) -> None:
        if not self._telegram or not message:
            logger.debug("[TELEGRAM] Skipped: telegram=%s message_len=%d",
                         bool(self._telegram), len(message or ""))
            return
        chunks = self._chunk_telegram_message(message)
        self._record_cycle_trace_telegram(message, message_type, len(chunks))
        logger.info("[TELEGRAM] Sending %d chunk(s), total %d chars", len(chunks), len(message))
        for i, chunk in enumerate(chunks):
            try:
                self._telegram.send(chunk, message_type=message_type)
                logger.info("[TELEGRAM] Chunk %d/%d sent OK", i + 1, len(chunks))
            except Exception as exc:
                logger.warning("[TELEGRAM] Chunk %d/%d FAILED: %s", i + 1, len(chunks), exc)

    @staticmethod
    def _chunk_telegram_message(message: str, max_chars: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
        """Split large Telegram payloads on line boundaries without cutting rows."""
        text = str(message or "")
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        current = ""
        for line in text.splitlines():
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = line
        if current:
            chunks.append(current)
        return chunks

    # ------------------------------------------------------------------
    # Status display
    # ------------------------------------------------------------------

    def display_status(self) -> None:
        """Print current session status and approved portfolio to stdout."""
        et = self._now_et()
        print(f"\n{'─'*60}")
        print("  VANGUARD ORCHESTRATOR STATUS")
        print(f"  Time:       {et.strftime('%Y-%m-%d %H:%M:%S ET')}")
        print(f"  In session: {self.is_within_session(et)}")
        print(f"  Mode:       {self.execution_mode}")
        print(f"  Accounts:   {[a['id'] for a in self._accounts]}")

        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                today = et.strftime("%Y-%m-%d")
                row = con.execute(
                    "SELECT * FROM vanguard_session_log WHERE date = ?", (today,)
                ).fetchone()
            if row:
                r = dict(row)
                print(f"\n  Today's session ({today}):")
                print(f"    Status:          {r.get('status')}")
                print(f"    Cycles:          {r.get('total_cycles')} (failed: {r.get('failed_cycles')})")
                print(f"    Trades placed:   {r.get('trades_placed')}")
                print(f"    Forward tracked: {r.get('forward_tracked')}")
            else:
                print(f"\n  No session log for {today}")
        except Exception as exc:
            print(f"\n  (Could not read session log: {exc})")

        approved = _get_approved_rows(self.db_path)
        print(f"\n  Current approved positions: {len(approved)}")
        for r in approved[:10]:
            print(
                f"    {r.get('account_id','?'):22s}  "
                f"{r.get('symbol','?'):6s}  "
                f"{r.get('direction','?'):5s}  "
                f"x{r.get('shares_or_lots', 0)}"
            )
        print(f"{'─'*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    runtime_cfg = get_runtime_config().get("runtime") or {}
    default_mode = _normalize_execution_mode(runtime_cfg.get("execution_mode", "manual"))
    default_interval = int(runtime_cfg.get("cycle_interval_seconds", CYCLE_MINUTES * 60))
    default_offset = int(runtime_cfg.get("cycle_offset_seconds", 0))
    default_force_assets = ",".join(runtime_cfg.get("force_assets") or [])
    p = argparse.ArgumentParser(
        description="Vanguard V7 Orchestrator — session lifecycle loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 stages/vanguard_orchestrator.py --single-cycle --dry-run\n"
            "  python3 stages/vanguard_orchestrator.py --loop --interval 300 --dry-run\n"
            "  python3 stages/vanguard_orchestrator.py --execution-mode manual\n"
            "  python3 stages/vanguard_orchestrator.py --execution-mode test --single-cycle\n"
            "  python3 stages/vanguard_orchestrator.py --status\n"
        ),
    )
    p.add_argument(
        "--execution-mode", dest="execution_mode",
        choices=["manual", "live", "test", "off", "paper"],
        default=default_mode,
        metavar="{manual,live,test}",
        help=f"manual=forward-track only, live=real orders, test=no-persist dry run (default: {default_mode}). Deprecated aliases off/paper map to manual.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="No-persist validation mode. Overrides execution mode to internal test mode for both single-cycle and loop runs.",
    )
    p.add_argument(
        "--single-cycle", action="store_true",
        help="Run exactly one pipeline cycle then exit",
    )
    p.add_argument(
        "--loop", action="store_true",
        help="Run the continuous session lifecycle loop (default behavior when --single-cycle is not set)",
    )
    p.add_argument(
        "--interval", type=int, default=default_interval,
        help=f"Cycle interval in seconds for loop mode (default: {default_interval})",
    )
    p.add_argument(
        "--cycle-offset-seconds", type=int, default=default_offset,
        help=f"Deterministic offset from the 5-minute boundary in seconds (default: {default_offset})",
    )
    p.add_argument(
        "--status", action="store_true",
        help="Print current status and exit",
    )
    p.add_argument(
        "--flatten-now", action="store_true",
        help="Trigger immediate EOD flatten for all must_close_eod accounts",
    )
    p.add_argument(
        "--session-start", default=DEFAULT_SESSION_START,
        help=f"Session start HH:MM ET (default: {DEFAULT_SESSION_START})",
    )
    p.add_argument(
        "--session-end", default=DEFAULT_SESSION_END,
        help=f"Session end HH:MM ET (default: {DEFAULT_SESSION_END})",
    )
    p.add_argument(
        "--db-path", default=_DB_PATH,
        help="Path to vanguard_universe.db",
    )
    p.add_argument(
        "--force-regime", dest="force_regime", default=None,
        choices=["ACTIVE", "CAUTION", "DEAD", "CLOSED"],
        help="Override V5 regime detection (passed through to vanguard_selection.py)",
    )
    p.add_argument(
        "--force-assets",
        dest="force_assets",
        default=default_force_assets,
        help="Comma-separated asset classes to bypass market-session polling gates, e.g. forex,crypto",
    )
    p.add_argument(
        "--force-cycle-ts", dest="force_cycle_ts", default=None,
        metavar="ISO_UTC",
        help="Override cycle timestamp to a specific ISO-8601 UTC string (e.g. 2026-04-04T14:30:00Z). "
             "Implies --single-cycle.",
    )
    p.add_argument(
        "--no-telegram", dest="no_telegram", action="store_true",
        help="Disable Telegram notifications for this run (useful for parity harness replay).",
    )
    source_group = p.add_mutually_exclusive_group()
    source_group.add_argument(
        "--IBKR",
        dest="equity_data_source",
        action="store_const",
        const="ibkr",
        default="auto",
        help="Force IBKR streaming for equity bars regardless of account profile",
    )
    source_group.add_argument(
        "--Alpaca",
        dest="equity_data_source",
        action="store_const",
        const="alpaca",
        help="Force Alpaca WebSocket/REST for equity bars regardless of account profile",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    args.execution_mode = _normalize_execution_mode(args.execution_mode)

    if not args.status and not args.flatten_now and not args.single_cycle:
        if not _acquire_loop_lock(args.db_path):
            return

    # --force-cycle-ts implies single-cycle
    if args.force_cycle_ts and not args.single_cycle:
        args.single_cycle = True
        logger.info("--force-cycle-ts set: forcing --single-cycle mode")

    orch = VanguardOrchestrator(
        execution_mode=args.execution_mode,
        dry_run=args.dry_run,
        db_path=args.db_path,
        session_start=args.session_start,
        session_end=args.session_end,
        force_regime=args.force_regime,
        equity_data_source=args.equity_data_source,
        force_assets=args.force_assets,
        cycle_interval_seconds=args.interval,
        cycle_offset_seconds=args.cycle_offset_seconds,
        force_cycle_ts=args.force_cycle_ts,
        no_telegram=args.no_telegram,
    )

    if args.status:
        orch.display_status()
        return

    if args.flatten_now:
        et = orch._now_et()
        for account in orch._accounts:
            if account.get("must_close_eod"):
                # Force flatten regardless of current time
                saved = orch.eod_flatten_time
                orch.eod_flatten_time = "00:00"
                orch._eod_flatten(account, et)
                orch.eod_flatten_time = saved
        return

    if args.single_cycle:
        logger.info("Single-cycle mode — running one cycle and exiting")
        if not orch._is_test_execution_mode():
            _ensure_execution_log(args.db_path)
            t0 = time.time()
            orch._materialize_universe_boot()
            logger.info("[BOOT] materialize_universe took %.2fs", time.time() - t0)
        orch._session_date     = orch._now_et().strftime("%Y-%m-%d")
        orch._session_start_ts = orch._now_utc()
        if not orch._is_test_execution_mode():
            orch._ensure_session_log()
        result = orch.run_cycle()
        logger.info("Cycle result: %s", result)
        if not orch.dry_run and result.get("approved", 0) > 0:
            orch._execute_approved()
        if not orch._is_test_execution_mode():
            orch._upsert_session_log(status="single_cycle_complete")
        return

    # Full session lifecycle
    orch.run_session()


if __name__ == "__main__":
    main()
