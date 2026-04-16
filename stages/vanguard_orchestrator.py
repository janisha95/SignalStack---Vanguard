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
from vanguard.config.runtime_config import (
    _DEFAULT_CONFIG_PATH,
    get_data_sources_config,
    get_execution_config,
    get_market_data_config,
    get_runtime_config,
    get_shadow_db_path,
    is_replay_from_source_db,
    load_runtime_config,
    resolve_market_data_source_id,
)
from vanguard.helpers.bars import aggregate_1m_to_5m, aggregate_1m_to_timeframe, aggregate_5m_to_1h, parse_utc
from vanguard.helpers.db import VanguardDB, checkpoint_wal_truncate, sqlite_conn, warn_if_large_wal
from vanguard.helpers.eod_flatten import check_eod_action, get_positions_to_flatten
from vanguard.helpers.universe_builder import materialize_universe_members
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
                "name":             str(p.get("id") or ""),  # fallback: use id as name
                "is_active":        1,
                "policy_id":        str(p.get("policy_id") or ""),
                "risk_profile_id":  str(p.get("risk_profile_id") or ""),
                "account_size":     p.get("account_size", 0),
                "instrument_scope": str(p.get("instrument_scope") or ""),
                "context_source_id": str(p.get("context_source_id") or ""),
                "context_health_mode": str(p.get("context_health_mode") or ""),
                "execution_mode":   str(p.get("execution_mode") or ""),
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
    legacy = dict((cfg.get("data_sources") or {}).get("mt5_local") or {})

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

    for source_id, source_cfg in (context_sources or {}).items():
        source_cfg = dict(source_cfg or {})
        if not source_cfg or not bool(source_cfg.get("enabled", False)):
            continue
        if str(source_cfg.get("source_type") or "").lower() != "dwx_mt5":
            continue
        source_cfg["_source_id"] = str(source_id)
        return source_cfg

    return legacy


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
        force_cycle_ts: str | None = None,
        no_telegram: bool = False,
        _now_et_fn: Optional[Callable[[], datetime]] = None,
        _now_utc_fn: Optional[Callable[[], datetime]] = None,
    ):
        boot_t0 = time.time()
        execution_mode = _normalize_execution_mode(execution_mode)
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
            self._send_telegram(f"🔴 {msg}")
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
            self._send_telegram(f"⚠️ {msg}")
        else:
            logger.info("[V7] Cycle completed in %.1fs", elapsed)

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
        parts = [
            f"{label} {int(policy['timeout_minutes'])}m (dedupe {int(policy['dedupe_minutes'])})"
            for label, policy in policies
            if policy
        ]
        if not parts:
            return ""
        return "Session Policy: " + " | ".join(parts)

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
        if not bridge.startswith("mt5_local"):
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
        start_h, start_m = self._parse_hhmm(str(cfg.get("start_et")))
        end_h, end_m = self._parse_hhmm(str(cfg.get("end_et")))
        current_minutes = now_local.hour * 60 + now_local.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        return start_minutes <= current_minutes <= end_minutes

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
        try:
            _forward_checkpoints_ensure_table(self.db_path)
            _forward_checkpoints_refresh(self.db_path, trade_ids=trade_ids)
            _refresh_timeout_policy_data(self.db_path, trade_ids=trade_ids, refresh_shell_replays_flag=True)
        except Exception as exc:
            logger.debug("forward checkpoint refresh skipped: %s", exc)

    def _refresh_signal_forward_checkpoints(self, decision_ids: list[str] | None = None) -> None:
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
        }
        now_dt = now_utc()

        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            for profile in self._accounts or []:
                profile_id = str(profile.get("id") or "")
                if not profile_id:
                    continue
                for asset_class, state_table in (
                    ("forex", "vanguard_forex_pair_state"),
                    ("crypto", "vanguard_crypto_symbol_state"),
                ):
                    expected_symbols = self._get_runtime_universe_symbols(profile_id, asset_class)
                    if not expected_symbols:
                        continue
                    placeholders = ",".join("?" for _ in expected_symbols)
                    try:
                        quote_rows = con.execute(
                            f"""
                            SELECT symbol, quote_ts_utc, source_status
                            FROM vanguard_context_quote_latest
                            WHERE profile_id = ?
                              AND symbol IN ({placeholders})
                            """,
                            [profile_id, *expected_symbols],
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
                        quote_dt = parse_utc(str(row["quote_ts_utc"])) if row["quote_ts_utc"] else None
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

    def _refresh_active_context_snapshots(self) -> dict[str, Any]:
        """Refresh MT5-backed context truth for active profiles before V6."""
        runtime_cfg = self._runtime_config or get_runtime_config()
        refresh_cfg = ((runtime_cfg.get("runtime") or {}).get("context_refresh_before_v6") or {})
        if not bool(refresh_cfg.get("enabled", False)):
            return {"enabled": False, "runs": []}

        context_sources = runtime_cfg.get("context_sources") or {}
        subscribe_sleep_sec = float(refresh_cfg.get("subscribe_sleep_sec") or 0.35)
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
            if str(source_cfg.get("source_type") or "").lower() != "dwx_mt5":
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
                default_suffix = _dwx_resolve_default_suffix(source_cfg, None)
                suffix_map = _dwx_resolve_symbol_suffix_map(
                    source_cfg,
                    symbols,
                    asset_class_by_symbol,
                    default_suffix,
                )
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
            finally:
                if owns_adapter and adapter is not None:
                    try:
                        adapter.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

        return {"enabled": True, "runs": runs}

    def _is_polling_asset_enabled(
        self,
        asset_class: str,
        et: Optional[datetime] = None,
    ) -> bool:
        """Return True when an asset class should be polled this cycle."""
        normalized = str(asset_class or "").lower().strip()
        if not normalized:
            return False
        if normalized in self._force_assets:
            return True
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
            f"🚀 <b>VANGUARD SESSION START</b>\n"
            f"Date:     {self._session_date}\n"
            f"Mode:     {self.execution_mode.upper()}\n"
            f"Window:   {self.session_start}–{self.session_end} ET\n"
            f"Accounts: {', '.join(a['id'] for a in self._accounts) or 'none'}"
        )
        logger.info("Session start: %s mode=%s", self._session_date, self.execution_mode)
        logger.info("[BOOT] startup total took %.2fs", time.time() - boot_t0)
        self._send_telegram(msg)

    def _main_loop(self) -> None:
        """5-minute cycle loop. Runs until session ends, shutdown, or too many failures."""
        while not self._shutdown_requested:
            et = self._now_et()

            if not self.is_within_session(et):
                if self._was_in_session_before(et):
                    logger.info("Session window ended — exiting main loop")
                    break
                logger.info("Waiting for session window (%s ET)…", self.session_start)
                time.sleep(30)
                continue

            try:
                result = self.run_cycle()
                self._total_cycles += 1

                if result.get("status") == "ok":
                    self._consecutive_failures = 0
                    if result.get("approved", 0) > 0:
                        self._execute_approved(result.get("cycle_ts"))
                    if not self._is_test_execution_mode():
                        # BUG 19: checkpoint every cycle (not just when WAL >256MB)
                        # so WAL doesn't grow unbounded and FD pressure stays flat.
                        self._checkpoint_db_wal(min_wal_bytes=0)
                else:
                    logger.warning("Cycle non-ok status: %s", result.get("status"))

            except Exception as exc:
                self._failed_cycles        += 1
                self._consecutive_failures += 1
                logger.error("Cycle error: %s", exc, exc_info=True)
                self._send_telegram(
                    f"⚠️ <b>CYCLE ERROR</b>\nError: {exc}\n"
                    f"Consecutive failures: {self._consecutive_failures}/{MAX_CONSECUTIVE_FAILS}"
                )
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILS:
                    logger.error(
                        "Aborting session — %d consecutive failures", MAX_CONSECUTIVE_FAILS
                    )
                    self._send_telegram(
                        f"🚨 <b>SESSION ABORTED</b>\n"
                        f"Reason: {MAX_CONSECUTIVE_FAILS} consecutive cycle failures"
                    )
                    self._upsert_session_log(status="aborted")
                    return

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
            f"🏁 <b>VANGUARD SESSION END</b>\n"
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
        self._send_telegram(msg)

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
            target   = next_bar + timedelta(seconds=CYCLE_BUFFER_SECONDS)
        else:
            next_bar = utc_now + timedelta(seconds=self.cycle_interval_seconds)
            target = next_bar

        sleep_secs = (target - utc_now).total_seconds()
        if sleep_secs < 0:
            sleep_secs = CYCLE_BUFFER_SECONDS

        logger.info(
            "Next cycle in %.0fs (bar=%s UTC)",
            sleep_secs, next_bar.strftime("%H:%M"),
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
            self._config_mtime = current_mtime
            new_version = (new_cfg or {}).get("config_version", "unknown")
            logger.info("[config_reload] reload OK — config_version=%s", new_version)
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
        in_scope_symbols: list[str] | None = None
        if self._runtime_config is not None:
            try:
                from vanguard.accounts.runtime_universe import resolve_universe_for_cycle
                from datetime import timezone as _tz
                cycle_dt = parse_utc(cycle_ts).replace(tzinfo=_tz.utc)
                self._current_resolved_universe = resolve_universe_for_cycle(
                    self._runtime_config, cycle_dt
                )
                ru = self._current_resolved_universe
                logger.info(
                    "[Phase2a] Resolved universe: mode=%s profiles=%s classes=%s symbols=%d",
                    ru.mode, ru.active_profile_ids, ru.expected_asset_classes, len(ru.in_scope_symbols),
                )
                # Write log row
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

        # ── V1: Stage 1 multi-source bar freshness ────────────────────
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
                self._log_context_state_diagnostics(cycle_ts)
                result["status"] = "no_survivors"
                result["approved"] = 0
                result["stage_timings"] = stage_timings
                self._check_cycle_lag(cycle_start)
                return result
        except Exception as exc:
            logger.error("V2 prefilter failed: %s", exc, exc_info=True)
            result["v2_error"] = str(exc)
            result["status"] = "v2_error"
            raise

        # ── V3: Factor Engine ──────────────────────────────────────────
        try:
            from stages.vanguard_factor_engine import run as v3_run
            stage_start = time.time()
            v3_result = v3_run(survivors=survivors, cycle_ts=cycle_ts, dry_run=dry_run)
            stage_timings["v3"] = time.time() - stage_start
            result["v3_symbols"] = len(v3_result) if isinstance(v3_result, list) else 0
            logger.info("V3 complete: %d symbols, elapsed=%.2fs", result["v3_symbols"], stage_timings["v3"])
            self._log_chain_integrity(survivors, v3_result, cycle_ts)
        except Exception as exc:
            logger.error("V3 factor engine failed: %s", exc, exc_info=True)
            result["v3_error"]   = str(exc)
            result["v3_symbols"] = 0
            result["status"] = "v3_error"
            raise

        # ── V4B: Regressor scorer ─────────────────────────────────────
        try:
            from stages.vanguard_scorer import run as v4b_run
            stage_start = time.time()
            v4b_result = v4b_run(dry_run=dry_run)
            stage_timings["v4b"] = time.time() - stage_start
            v4b_status = v4b_result.get("status", "unknown") if isinstance(v4b_result, dict) else "unknown"
            v4b_rows = v4b_result.get("rows", 0) if isinstance(v4b_result, dict) else 0
            result["v4b_status"] = v4b_status
            result["v4b_rows"] = v4b_rows
            logger.info(
                "V4B complete: status=%s, rows=%d, elapsed=%.2fs",
                v4b_status, v4b_rows, stage_timings["v4b"],
            )
            if v4b_rows == 0 or v4b_status in ("no_features", "no_models"):
                logger.warning("V4B produced 0 predictions — skipping V5/V6")
                result["status"] = "no_predictions"
                result["approved"] = 0
                result["stage_timings"] = stage_timings
                self._check_cycle_lag(cycle_start)
                return result
        except Exception as exc:
            logger.error("V4B scorer failed: %s", exc, exc_info=True)
            result["v4b_error"] = str(exc)
            result["status"] = "v4b_error"
            raise

        # ── V5: Selection ──────────────────────────────────────────────
        try:
            v5_cfg = (self._runtime_config or {}).get("v5") or {}
            active_profile_ids = [str(account.get("id") or "") for account in (self._accounts or []) if str(account.get("id") or "").strip()]
            if str(v5_cfg.get("selection_engine") or "simple_legacy") == "tradeability_v1":
                from stages.vanguard_selection_tradeability import run as v5_run
                v5_kwargs = {"dry_run": dry_run, "cycle_ts_utc": cycle_ts, "profile_ids": active_profile_ids}
            else:
                from stages.vanguard_selection_simple import run as v5_run
                v5_kwargs = {"dry_run": dry_run, "profile_ids": active_profile_ids}
            stage_start = time.time()
            v5_result = v5_run(**v5_kwargs)
            stage_timings["v5"] = time.time() - stage_start
            v5_status = v5_result.get("status", "unknown") if isinstance(v5_result, dict) else "unknown"
            v5_rows   = v5_result.get("rows", 0)   if isinstance(v5_result, dict) else 0
            result["v5_status"]     = v5_status
            result["v5_candidates"] = v5_rows
            logger.info(
                "V5 complete: status=%s, candidates=%d, elapsed=%.2fs",
                v5_status, v5_rows, stage_timings["v5"],
            )

            if v5_rows == 0 or v5_status in ("no_predictions", "no_candidates", "no_features", "skipped"):
                logger.warning("V5 produced 0 candidates — skipping V6")
                self._log_context_state_diagnostics(cycle_ts)
                result["status"]   = "no_candidates"
                result["approved"] = 0
                result["stage_timings"] = stage_timings
                self._check_cycle_lag(cycle_start)
                return result

        except Exception as exc:
            logger.error("V5 selection failed: %s", exc, exc_info=True)
            result["v5_error"] = str(exc)
            result["status"]   = "v5_error"
            raise   # triggers consecutive failure counter

        try:
            stage_start = time.time()
            context_refresh = self._refresh_active_context_snapshots()
            stage_timings["context_refresh"] = time.time() - stage_start
            result["context_refresh"] = context_refresh
            self._log_context_state_diagnostics(cycle_ts)
        except Exception as exc:
            logger.error("Context refresh failed: %s", exc, exc_info=True)
            result["context_refresh_error"] = str(exc)
            result["status"] = "context_refresh_error"
            raise

        # ── V6: Risk Filters ───────────────────────────────────────────
        try:
            from stages.vanguard_risk_filters import run as v6_run
            stage_start = time.time()
            v6_result = v6_run(dry_run=dry_run, cycle_ts=cycle_ts)
            stage_timings["v6"] = time.time() - stage_start
            v6_status = v6_result.get("status", "unknown") if isinstance(v6_result, dict) else "unknown"
            v6_rows   = v6_result.get("rows", 0)   if isinstance(v6_result, dict) else 0
            result["v6_status"] = v6_status
            result["v6_rows"]   = v6_rows
            logger.info(
                "V6 complete: status=%s, rows=%d, elapsed=%.2fs",
                v6_status, v6_rows, stage_timings["v6"],
            )
            self._log_v6_diagnostics(cycle_ts)

            approved          = _get_approved_rows(self.db_path, cycle_ts)
            result["approved"] = len(approved)
            logger.info("Approved for execution: %d", len(approved))

            shortlist_msg = self._build_shortlist_telegram(cycle_ts)
            logger.info("[TELEGRAM] Shortlist msg built: %s", "yes" if shortlist_msg else "empty/None")
            if shortlist_msg:
                self._send_telegram(shortlist_msg)
            diagnostics_msg = self._build_operator_diagnostics_telegram(cycle_ts)
            logger.info("[TELEGRAM] Diagnostics msg built: %s", "yes" if diagnostics_msg else "empty/None")
            if diagnostics_msg:
                self._send_telegram(diagnostics_msg)

        except Exception as exc:
            logger.error("V6 risk filters failed: %s", exc, exc_info=True)
            result["v6_error"] = str(exc)
            result["status"]   = "v6_error"
            raise   # triggers consecutive failure counter

        result["stage_timings"] = stage_timings
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
                written = materialize_universe_members(self._vg_db, now_utc_str=now_utc_str)
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
        if use_ibkr_equity and has_ibkr_equity_stream:
            logger.info("[V1] Equity source=%s → skipping Alpaca WebSocket because IBKR owns equities", self.equity_data_source)
        if equity_symbols and equity_session_active and self._alpaca_adapter is None:
            if not (use_ibkr_equity and has_ibkr_equity_stream):
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
            mt5_cfg = _resolve_active_mt5_local_config(get_runtime_config())
            if mt5_cfg.get("enabled"):
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
        if self._td_adapter:
            td_symbols_backup = self._td_adapter.symbols
            try:
                runtime_cfg = get_runtime_config()
                td_asset_classes = set()
                if crypto_session_active and resolve_market_data_source_id("crypto", runtime_config=runtime_cfg) == "twelvedata":
                    td_asset_classes.add("crypto")
                if forex_session_active and resolve_market_data_source_id("forex", runtime_config=runtime_cfg) == "twelvedata":
                    td_asset_classes.add("forex")
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
            "twelvedata": ("twelvedata",),
        }
        with self._vg_db.connect() as conn:
            recent_rows = conn.execute(
                """
                SELECT data_source, asset_class, COUNT(*) AS bars_last_5min,
                       COUNT(DISTINCT symbol) AS symbols_with_recent_bar,
                       MAX(bar_ts_utc) AS last_bar_ts
                FROM vanguard_bars_1m
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
                FROM vanguard_bars_1m
                WHERE bar_ts_utc >= ?
                GROUP BY data_source, asset_class
                """,
                (cutoff_1d,),
            ).fetchall()
            last_day_stats = {
                (row["data_source"], row["asset_class"]): dict(row)
                for row in recent_last_day_rows
            }
            for source, bar_sources in source_map.items():
                asset_rows = conn.execute(
                    """
                    SELECT DISTINCT asset_class
                    FROM vanguard_universe_members
                    WHERE data_source = ? AND is_active = 1
                    ORDER BY asset_class
                    """,
                    (source,),
                ).fetchall()
                for asset_row in asset_rows:
                    asset_class = asset_row[0]
                    source_keys = [(bar_source, asset_class) for bar_source in bar_sources]
                    active_count = conn.execute(
                        """
                        SELECT COUNT(DISTINCT symbol)
                        FROM vanguard_universe_members
                        WHERE data_source = ? AND asset_class = ? AND is_active = 1
                        """,
                        (source, asset_class),
                    ).fetchone()[0]
                    recent_symbols = sum(
                        recent_stats.get(key, {}).get("symbols_with_recent_bar", 0)
                        for key in source_keys
                    )
                    recent_bars = sum(
                        recent_stats.get(key, {}).get("bars_last_5min", 0)
                        for key in source_keys
                    )
                    last_bar_ts = next(
                        (
                            recent_stats.get(key, {}).get("last_bar_ts")
                            or last_day_stats.get(key, {}).get("last_bar_ts")
                        )
                        for key in source_keys
                        if (
                            recent_stats.get(key, {}).get("last_bar_ts")
                            or last_day_stats.get(key, {}).get("last_bar_ts")
                        )
                    ) if any(
                        recent_stats.get(key, {}).get("last_bar_ts")
                        or last_day_stats.get(key, {}).get("last_bar_ts")
                        for key in source_keys
                    ) else None
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

    def _execute_approved(self, cycle_ts: str | None = None) -> None:
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
                               t.predicted_return
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
                            "predicted_return": _row["predicted_return"],
                        }
                        for _row in _mrows
                    }
        except Exception as _exc:
            logger.debug("Could not load v5 candidate metrics: %s", _exc)

        submitted = 0
        filled    = 0
        failed    = 0
        forward   = 0
        checkpoint_trade_ids: list[str] = []
        checkpoint_decision_ids: list[str] = []
        metaapi_executor_cls = None
        metaapi_symbol_fn = None
        metaapi_executors: dict[str, Any] = {}
        gft_execution_state: dict[str, dict[str, Any]] = {}
        gft_manual_rows: list[dict[str, Any]] = []
        failure_reasons: Counter[str] = Counter()
        context_positions_cache: dict[str, list[dict[str, Any]]] = {}
        gft_time_exit_rows = self._execute_gft_time_exits()
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
            shares = (
                float(raw_shares)
                if str(account or "").lower().startswith("gft_")
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
            candidate_stop_pips = self._price_delta_to_pips(symbol, entry_px, stop_loss)
            candidate_tp_pips = self._price_delta_to_pips(symbol, entry_px, take_profit)
            _skip_legacy_forward_track = self._is_model_v2_forex_row(row, _asset_class)

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
                positions: list[dict[str, Any]] | None = None,
                trade_id: str | None = None,
                broker_position_id: str | None = None,
                execution_request_id: str | None = None,
            ) -> str:
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
                    trade_id=trade_id,
                    broker_position_id=broker_position_id,
                    execution_request_id=execution_request_id,
                    symbol=str(symbol or "").upper(),
                    direction=str(direction or "").upper(),
                    asset_class=_asset_class,
                    session_bucket=session_bucket,
                    entry_price=float(entry_px or 0.0) if entry_px not in (None, "") else None,
                    model_id=str(row.get("model_source") or "") or None,
                    model_family=_infer_model_family(row.get("model_source")),
                    model_readiness=str(row.get("model_readiness") or "") or None,
                    gate_policy=str(row.get("gate_policy") or "") or None,
                    v6_state=str(row.get("v6_state") or "") or None,
                    predicted_return=candidate_metrics.get("predicted_return"),
                    edge_score=row.get("edge_score"),
                    selection_rank=candidate_metrics.get("selection_rank", row.get("rank")),
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

            if shares <= 0:
                logger.warning("Skipping %s — shares=%s", symbol, shares)
                continue

            if (
                not self._is_manual_execution_mode()
                and self.execution_mode == "live"
                and self._scheduled_flatten_window_active(str(account or ""))
            ):
                _record_signal_decision(
                    execution_decision="SKIPPED_POLICY",
                    decision_reason_code="SCHEDULED_FLATTEN_WINDOW",
                    decision_reason_json={
                        "reason": "scheduled_flatten_window",
                        "execution_mode": self.execution_mode,
                        "window_active": True,
                    },
                )
                forward += 1
                _notes = "live routing disabled during scheduled flatten window"
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
                    "[SCHEDULED FLATTEN WINDOW] %s %s skipped — live routing paused for account=%s",
                    symbol,
                    direction,
                    account,
                )
                continue

            # ── Trade journal: insert approval row ─────────────────────────
            # Construct candidate and policy_decision dicts from the approved row.
            # asset_class is looked up from vanguard_shortlist; falls back to inference.
            if not self._is_manual_execution_mode() and not self._is_live_routing_asset_allowed(_asset_class, str(account or "")):
                _record_signal_decision(
                    execution_decision="SKIPPED_POLICY",
                    decision_reason_code="LIVE_ASSET_DISABLED",
                    decision_reason_json={
                        "reason": "live_asset_disabled",
                        "asset_class": _asset_class,
                        "execution_mode": self.execution_mode,
                    },
                )
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
                continue
            _candidate = {
                "symbol":        symbol,
                "asset_class":   _asset_class,
                "side":          direction,
                "policy_id":     str(row.get("policy_id") or (_acct_profile or {}).get("policy_id") or "") or None,
                "entry_price":   entry_px,
                "stop_price":    stop_loss,
                "tp_price":      take_profit,
                "shares_or_lots": shares,
                "model_id":      row.get("model_source"),
                "model_family":  _infer_model_family(row.get("model_source")),
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
                "policy_id":      str(row.get("policy_id") or (_acct_profile or {}).get("policy_id") or "") or None,
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
                "model_family":   _infer_model_family(row.get("model_source")),
                "risk_pct":       row.get("risk_pct"),
            }
            _cycle_ts = str(row.get("cycle_ts_utc") or "")
            # Global manual/test mode always wins. Profiles may only downgrade live routing,
            # not silently override a manual run into live execution.
            _profile_mode = str((_acct_profile or {}).get("execution_mode") or "").lower()
            _profile_auto = _profile_mode == "auto"
            _effective_manual = (
                self._is_manual_execution_mode()
                or (_profile_mode in {"manual", "off", "paper"})
                or not _profile_auto
            )
            _journal_initial_status = (
                "FORWARD_TRACKED"
                if self._is_test_execution_mode() or _effective_manual
                else "PENDING_FILL"
            )
            _trade_id: Optional[str] = None
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
                    notes=f"trade_id={_trade_id}",
                )
            except Exception as _shadow_exc:
                logger.debug("[shadow_log] insert failed (non-blocking): %s", _shadow_exc)
            # ── End shadow execution log ────────────────────────────────────

            if self._is_test_execution_mode():
                _record_signal_decision(
                    execution_decision="EXECUTED",
                    decision_reason_code="TEST_NO_PERSIST",
                    decision_reason_json={
                        "execution_mode": self.execution_mode,
                        "forward_tracked": True,
                    },
                    trade_id=_trade_id,
                )
                forward += 1
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
                _record_signal_decision(
                    execution_decision="EXECUTED",
                    decision_reason_code="MANUAL_FORWARD_TRACKED",
                    decision_reason_json={
                        "execution_mode": self.execution_mode,
                        "forward_tracked": True,
                        "profile_execution_mode": _profile_mode,
                    },
                    trade_id=_trade_id,
                )
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
                                _record_signal_decision(
                                    execution_decision="SKIPPED_DUPLICATE_SYMBOL_OPEN",
                                    decision_reason_code="METAAPI_SYMBOL_ALREADY_OPEN",
                                    decision_reason_json={
                                        "execution_bridge": bridge,
                                        "broker_symbol": mapped_symbol,
                                    },
                                    positions=list(state.get("open_positions") or []),
                                    trade_id=_trade_id,
                                )
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
                                continue

                            if state["remaining_slots"] <= 0:
                                _record_signal_decision(
                                    execution_decision="SKIPPED_NO_CAPACITY",
                                    decision_reason_code="METAAPI_MAX_POSITIONS_REACHED",
                                    decision_reason_json={
                                        "execution_bridge": bridge,
                                        "broker_open_symbols": sorted(state["open_symbols"]),
                                    },
                                    positions=list(state.get("open_positions") or []),
                                    trade_id=_trade_id,
                                )
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
                                continue

                            _decision_id = _record_signal_decision(
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
                            else:
                                failed += 1
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
                                            fill_qty=float(shares),
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
                            _record_signal_decision(
                                execution_decision="SKIPPED_POLICY",
                                decision_reason_code="METAAPI_NOT_CONNECTED",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                    "forward_tracked": True,
                                },
                                trade_id=_trade_id,
                            )
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
                        failure_reasons[str(exc)] += 1
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
                            _record_signal_decision(
                                execution_decision="SKIPPED_POLICY",
                                decision_reason_code="DWX_NOT_CONNECTED",
                                decision_reason_json={
                                    "execution_bridge": bridge,
                                    "forward_tracked": True,
                                },
                                trade_id=_trade_id,
                            )
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
                            _record_signal_decision(
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

                        _decision_id = _record_signal_decision(
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
                        else:
                            failed += 1
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
                                        fill_qty=float(shares),
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
                    continue
                # ── End DWX bridge ──────────────────────────────────────────

                if bridge != "signalstack" or not webhook_url:
                    _record_signal_decision(
                        execution_decision="SKIPPED_POLICY",
                        decision_reason_code="BRIDGE_FORWARD_TRACKED",
                        decision_reason_json={
                            "execution_bridge": bridge,
                            "webhook_configured": bool(webhook_url),
                            "forward_tracked": True,
                        },
                        trade_id=_trade_id,
                    )
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
                    continue

                try:
                    _decision_id = _record_signal_decision(
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
                    else:
                        failed += 1
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
                                    fill_qty=float(shares),
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

        if checkpoint_trade_ids:
            self._refresh_forward_checkpoints(trade_ids=checkpoint_trade_ids)
        if checkpoint_decision_ids:
            self._refresh_signal_forward_checkpoints(decision_ids=checkpoint_decision_ids)

        self._trades_placed   += filled
        self._forward_tracked += forward

        gft_time_exit_msg = self._build_gft_time_exit_telegram(gft_time_exit_rows)
        if gft_time_exit_msg:
            self._send_telegram(gft_time_exit_msg)

        logger.info(
            "Execution: submitted=%d, filled=%d, failed=%d, forward_tracked=%d",
            submitted, filled, failed, forward,
        )

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
                shortlist_rows = con.execute(
                    """
                    SELECT symbol, asset_class, direction, predicted_return, edge_score, rank
                    FROM vanguard_shortlist_v2
                    WHERE cycle_ts_utc = ?
                    ORDER BY asset_class, direction, rank
                    """,
                    (cycle_ts,),
                ).fetchall()
                v5_rows = con.execute(
                    """
                    SELECT s.profile_id,
                           s.symbol,
                           s.asset_class,
                           s.direction,
                           t.predicted_return,
                           t.economics_state,
                           s.selection_rank,
                           s.route_tier,
                           s.display_label,
                           s.selection_state,
                           s.direction_streak,
                           t.predicted_move_pips,
                           t.after_cost_pips,
                           t.predicted_move_bps,
                           t.after_cost_bps
                    FROM vanguard_v5_selection s
                    JOIN vanguard_v5_tradeability t
                      ON t.cycle_ts_utc = s.cycle_ts_utc
                     AND t.profile_id = s.profile_id
                     AND t.symbol = s.symbol
                     AND t.direction = s.direction
                    WHERE s.cycle_ts_utc = ?
                      AND s.profile_id IN (""" + active_profile_placeholders + """)
                      AND s.asset_class IN ('forex', 'crypto')
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
                f"📊 <b>VANGUARD SHORTLIST</b> — {cycle_ts[:16]}\n\n"
                f"{self._build_session_status_block()}\n\n"
                f"⚠️ Shortlist unavailable this cycle\n"
                f"Error: <code>{html.escape(str(exc))}</code>"
            )

        lines = [
            f"📊 <b>VANGUARD SHORTLIST</b> — {cycle_ts[:16]}",
            self._build_session_status_block(),
            "",
        ]
        timeout_policy_header = self._build_timeout_policy_header(cycle_ts)
        if timeout_policy_header:
            lines.append(timeout_policy_header)
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
            economics_state,
            selection_rank,
            route_tier,
            display_label,
            selection_state,
            direction_streak,
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
                "economics_state": economics_state,
                "rank": selection_rank,
                "route_tier": route_tier,
                "display_label": display_label,
                "selection_state": selection_state,
                "direction_streak": direction_streak,
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
            repeated_thesis: list[dict[str, Any]] = []

            for row in v5_display_rows:
                key = (
                    str(row.get("profile_id") or ""),
                    str(row.get("symbol") or "").upper(),
                    str(row.get("direction") or "").upper(),
                )
                details = portfolio_overlay.get(key)
                stage_row = dict(row)
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
                is_repeated_display_thesis = (
                    str(stage_row.get("thesis_state") or "").startswith("SEEN_RECENTLY")
                    and not bool(stage_row.get("reentry_blocked"))
                )
                if not details:
                    if is_repeated_display_thesis:
                        repeated_thesis.append(stage_row)
                        continue
                    selected_by_v5.append(stage_row)
                    continue
                status = str(details.get("status") or "").upper()
                if is_repeated_display_thesis and status != "REJECTED":
                    repeated_thesis.append(stage_row)
                    continue
                if status == "APPROVED":
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
                    ("Blocked by Risky", blocked_by_risky, "blocked"),
                    ("Held by V6", held_by_v6, "held"),
                    (
                        "Approved / Forward-Tracked" if self._is_manual_execution_mode() else "Approved / Routed",
                        approved_by_v6,
                        "approved",
                    ),
                    ("Repeated Thesis", repeated_thesis, "approved"),
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
                fail_weak_rows = diagnostics_rows[:display_n]
                for row in fail_weak_rows:
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
                profile_label = (
                    str(relevant[0].get("account_id") or "").replace("_demo_", " ").replace("_", " ").upper()
                    or "ACTIVE"
                )
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

            for asset_class in sorted(grouped.keys()):
                if not self._is_polling_asset_enabled(asset_class):
                    lines.append(f"━━ {asset_class.upper()} — {self._asset_session_label(asset_class)} ━━")
                    lines.append("")
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

    def _build_session_status_block(self) -> str:
        """Return a compact session-state header for operator context."""
        lines = [
            f"CRYPTO {self._asset_session_label('crypto')}",
            f"FOREX {self._asset_session_label('forex')}",
            f"EQUITY {self._asset_session_label('equity')}",
        ]
        account_line = self._build_account_snapshot_line()
        if account_line:
            lines.append(account_line)
        return "\n".join(lines)

    def _build_account_snapshot_line(self) -> str:
        active_profile_ids = [str(account.get("id") or "") for account in (self._accounts or []) if str(account.get("id") or "").strip()]
        if not active_profile_ids:
            return ""
        profile_id = active_profile_ids[0]
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                account = con.execute(
                    """
                    SELECT profile_id, balance, equity, free_margin, received_ts_utc
                    FROM vanguard_context_account_latest
                    WHERE profile_id = ?
                    ORDER BY received_ts_utc DESC
                    LIMIT 1
                    """,
                    (profile_id,),
                ).fetchone()
                pos = con.execute(
                    """
                    SELECT COUNT(*) AS open_positions,
                           COALESCE(SUM(COALESCE(floating_pnl, 0)), 0) AS unrealized_pnl,
                           MAX(received_ts_utc) AS positions_ts_utc
                    FROM (
                        SELECT ticket,
                               floating_pnl,
                               received_ts_utc,
                               ROW_NUMBER() OVER (
                                   PARTITION BY ticket
                                   ORDER BY received_ts_utc DESC
                               ) AS rn
                        FROM vanguard_context_positions_latest
                        WHERE profile_id = ?
                    )
                    WHERE rn = 1
                    """,
                    (profile_id,),
                ).fetchone()
        except Exception as exc:
            logger.warning("Could not build account snapshot line: %s", exc)
            return ""
        if not account:
            return ""
        balance = float(account["balance"] or 0.0)
        equity = float(account["equity"] or 0.0)
        free_margin = float(account["free_margin"] or 0.0)
        open_positions = int((pos["open_positions"] if pos else 0) or 0)
        unrealized_pnl = float((pos["unrealized_pnl"] if pos else 0.0) or 0.0)
        ts_raw = (pos["positions_ts_utc"] if pos and pos["positions_ts_utc"] else account["received_ts_utc"])
        ts_text = "-"
        if ts_raw:
            try:
                ts_text = utc_to_et(parse_utc(str(ts_raw))).strftime("%H:%M:%S ET")
            except Exception:
                ts_text = str(ts_raw)
        profile_label = profile_id.replace("_demo_", " ").replace("_", " ").upper()
        return (
            f"ACCOUNT {profile_label} {ts_text}  "
            f"Bal {balance:.2f}  Eq {equity:.2f}  FM {free_margin:.2f}  "
            f"Pos {open_positions}  UPL {unrealized_pnl:+.2f}"
        )

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

        lines = [f"🧭 <b>VANGUARD DIAGNOSTICS</b> — {html.escape(cycle_ts[:16])}"]
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
            for row in health_rows:
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
        lines: list[str] = []
        active_profile_ids = [str(account.get("id") or "") for account in (self._accounts or []) if str(account.get("id") or "").strip()]
        if not active_profile_ids:
            return lines
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                for profile_id in active_profile_ids:
                    for asset_class in ("forex", "crypto"):
                        symbols = self._get_runtime_universe_symbols(profile_id, asset_class)
                        fresh_seconds = forex_fresh if asset_class == "forex" else crypto_fresh
                        quote_table = "vanguard_context_quote_latest"
                        state_table = "vanguard_forex_pair_state" if asset_class == "forex" else "vanguard_crypto_symbol_state"
                        expected = len(symbols)
                        fresh = stale = missing = 0
                        latest_quote_ts = None
                        if symbols:
                            placeholders = ",".join("?" for _ in symbols)
                            rows = con.execute(
                                f"""
                                SELECT symbol, quote_ts_utc
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
                                ts = parse_utc(str(row["quote_ts_utc"])) if row["quote_ts_utc"] else None
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
        details = row.get("portfolio") or {}
        status = str(details.get("status") or "").upper()
        reason = self._format_telegram_reason(str(details.get("rejection_reason") or ""))
        risk_dollars = details.get("risk_dollars")
        risk_pct = details.get("risk_pct")
        sl = details.get("stop_price")
        tp = details.get("tp_price")
        qty = details.get("lot_size")
        thesis_state = str(row.get("thesis_state") or "NEW").upper()
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
            return f"{header}\nV5 {html.escape(economics_text)}  {html.escape(rank_text)}\n{move_line}"

        if stage == "selected":
            return (
                f"{header}\n"
                f"V5 SELECTED  {html.escape(rank_text)}\n"
                f"{move_line}\n"
                f"Session {html.escape(session_label)}  Thesis {html.escape(thesis_state)}  Model Horizon {html.escape(model_horizon_hint)}\n"
                f"Exit by {html.escape(exit_by_window)}\n"
                f"Timeout Guide {html.escape(timeout_guide)}"
            )

        if stage == "blocked":
            blocked_note = "  Reentry Blocked" if str(details.get("rejection_reason") or "").upper() == "BLOCKED_ACTIVE_THESIS_REENTRY" else ""
            return (
                f"{header}\n"
                f"V5 SELECTED  {html.escape(rank_text)}\n"
                f"{move_line}\n"
                f"Session {html.escape(session_label)}  Thesis {html.escape(thesis_state)}  Model Horizon {html.escape(model_horizon_hint)}\n"
                f"Exit by {html.escape(exit_by_window)}\n"
                f"Timeout Guide {html.escape(timeout_guide)}\n"
                f"Risk {html.escape(reason or status or 'BLOCKED')}{html.escape(blocked_note)}"
            )

        risk_text = "-"
        if risk_dollars is not None:
            risk_text = f"${float(risk_dollars):.0f}"
            if risk_pct is not None:
                risk_text += f" {float(risk_pct) * 100:.02f}%"
        sl_text = "-" if sl in (None, "") else f"{float(sl):.5f}".rstrip("0").rstrip(".")
        tp_text = "-" if tp in (None, "") else f"{float(tp):.5f}".rstrip("0").rstrip(".")
        qty_text = "-" if qty in (None, "") else f"{float(qty):.6f}".rstrip("0").rstrip(".")
        stage_label = "V6 HOLD" if stage == "held" else "APPROVED"
        stage_reason = f" {reason}" if reason else ""
        return (
            f"{header}\n"
            f"V5 SELECTED  {html.escape(rank_text)}\n"
            f"{move_line}\n"
            f"Session {html.escape(session_label)}  Thesis {html.escape(thesis_state)}  Model Horizon {html.escape(model_horizon_hint)}\n"
            f"Exit by {html.escape(exit_by_window)}\n"
            f"Timeout Guide {html.escape(timeout_guide)}\n"
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
        return {"BNBUSD", "BTCUSD", "ETHUSD", "LTCUSD", "SOLUSD", "BCHUSD"}

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

    def _send_telegram(self, message: str) -> None:
        if not self._telegram or not message:
            logger.debug("[TELEGRAM] Skipped: telegram=%s message_len=%d",
                         bool(self._telegram), len(message or ""))
            return
        chunks = self._chunk_telegram_message(message)
        logger.info("[TELEGRAM] Sending %d chunk(s), total %d chars", len(chunks), len(message))
        for i, chunk in enumerate(chunks):
            try:
                self._telegram.send(chunk)
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
    default_force_assets = ",".join(runtime_cfg.get("force_assets") or [])
    p = argparse.ArgumentParser(
        description="Vanguard V7 Orchestrator — session lifecycle loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 stages/vanguard_orchestrator.py --single-cycle --dry-run\n"
            "  python3 stages/vanguard_orchestrator.py --loop --interval 300\n"
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
        help="Run pipeline in dry-run mode (no DB writes)",
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
