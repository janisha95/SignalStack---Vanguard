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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.clock import now_et, now_utc, iso_utc, round_down_5m, utc_to_et
from vanguard.helpers.bars import aggregate_1m_to_5m, aggregate_5m_to_1h, parse_utc
from vanguard.helpers.db import VanguardDB, checkpoint_wal_truncate, sqlite_conn, warn_if_large_wal
from vanguard.helpers.eod_flatten import check_eod_action, get_positions_to_flatten
from vanguard.helpers.universe_builder import materialize_universe_members
from vanguard.data_adapters.alpaca_adapter import AlpacaAdapter, AlpacaWSAdapter, get_active_equity_symbols
from vanguard.data_adapters.ibkr_adapter import IBKRAdapter, IBKRStreamingAdapter
from vanguard.data_adapters.twelvedata_adapter import TwelveDataAdapter, load_from_config as load_twelvedata_adapter
from vanguard.execution.signalstack_adapter import SignalStackAdapter
from vanguard.execution.telegram_alerts import TelegramAlerts

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

_DB_PATH       = str(_REPO_ROOT / "data" / "vanguard_universe.db")
_IBKR_INTRADAY_DB = str(_REPO_ROOT / "data" / "ibkr_intraday.db")
_EXEC_CFG_PATH = _REPO_ROOT / "config" / "vanguard_execution_config.json"
_GFT_UNIVERSE_PATH = _REPO_ROOT / "config" / "gft_universe.json"
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
TELEGRAM_CHUNK_LIMIT = 3500

_ASSET_SESSION_WINDOWS = {
    "equity": {"days": {0, 1, 2, 3, 4}, "start": "09:30", "end": "16:00"},
    "index": {"days": {0, 1, 2, 3, 4}, "start": "09:30", "end": "16:00"},
    "forex": {"days": {6, 0, 1, 2, 3, 4}, "start": "17:00", "end": "17:00"},
    "crypto": {"days": {0, 1, 2, 3, 4, 5, 6}, "start": None, "end": None},
}

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
    """Load vanguard_execution_config.json, substituting ENV: values."""
    with open(_EXEC_CFG_PATH) as fh:
        cfg = json.load(fh)

    def _resolve(obj: Any) -> Any:
        if isinstance(obj, str) and obj.startswith("ENV:"):
            return os.environ.get(obj[4:], "")
        if isinstance(obj, dict):
            return {k: _resolve(v) for k, v in obj.items()}
        return obj

    return _resolve(cfg)


def _load_accounts(db_path: str = _DB_PATH) -> list[dict]:
    """Load all active account profiles from vanguard_universe.db."""
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


def _get_approved_rows(db_path: str = _DB_PATH) -> list[dict]:
    """Load APPROVED rows from the latest cycle in vanguard_tradeable_portfolio."""
    try:
        with sqlite_conn(db_path) as con:
            con.row_factory = sqlite3.Row
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
        logger.info("[BOOT] init_telegram took %.2fs", time.time() - t)

        t = time.time()
        self._accounts   = _load_accounts(db_path)
        logger.info("[BOOT] load_accounts took %.2fs", time.time() - t)

        # === ASSET-CLASS AWARE SESSION WINDOW (additive override) ===
        # If any active account trades forex or GFT, run 24/5.
        has_forex_or_gft = any(
            account.get("instrument_scope") in ("forex_cfd", "gft_universe")
            or str(account.get("id") or "").lower().startswith("gft_")
            for account in self._accounts
        )
        if has_forex_or_gft:
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
        """Startup check: verify active account profiles exist. Raises if none."""
        try:
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                profiles = con.execute(
                    "SELECT id, name, is_active, environment "
                    "FROM account_profiles WHERE is_active = 1"
                ).fetchall()
            profiles = [dict(p) for p in profiles]
        except Exception as exc:
            logger.error("[V7] Could not query account_profiles: %s", exc)
            profiles = []

        if not profiles:
            msg = "[V7] FATAL: No active account profiles found. Cannot execute trades."
            logger.error(msg)
            self._send_telegram(f"🔴 {msg}")
            raise RuntimeError(msg)

        logger.info("[V7] %d active profile(s):", len(profiles))
        for p in profiles:
            logger.info("  %s: %s (env=%s)", p["id"], p["name"], p.get("environment", "?"))
        return profiles

    def _verify_data_sources(self) -> None:
        """Startup check: verify data adapters have keys and DB has bars."""
        checks: list[tuple[str, str]] = []

        alpaca_key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_KEY")
        checks.append(("Alpaca", "OK" if alpaca_key else "NO API KEY"))

        td_key = (
            os.environ.get("TWELVE_DATA_API_KEY")
            or os.environ.get("TWELVEDATA_API_KEY")
        )
        checks.append(("TwelveData", "OK" if td_key else "NO API KEY"))

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
            self._materialize_universe(force=True)
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
                        self._execute_approved()
                    if not self._is_test_execution_mode():
                        self._checkpoint_db_wal()
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

    def run_cycle(self, dry_run: Optional[bool] = None) -> dict:
        """
        Run one pipeline cycle: V1 → V3 → V5 → V6.

        Returns dict with status and per-stage metrics.
        Raises on hard V5/V6 failures (triggers consecutive failure counter).
        """
        cycle_start = time.time()

        if dry_run is None:
            dry_run = self.dry_run

        cycle_ts = iso_utc(self._now_utc())
        logger.info(
            "=== Cycle start: %s ===",
            utc_to_et(parse_utc(cycle_ts)).strftime("%Y-%m-%d %H:%M:%S ET"),
        )
        result: dict = {"status": "ok", "cycle_ts": cycle_ts}
        stage_timings: dict[str, float] = {}

        # ── V1: Stage 1 multi-source bar freshness ────────────────────
        try:
            stage_start = time.time()
            v1_result = self._run_v1()
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
            v2_result = v2_run(dry_run=dry_run, cycle_ts=cycle_ts)
            stage_timings["v2"] = time.time() - stage_start
            survivors = [row for row in v2_result if row.get("status") == "ACTIVE"]
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
            from stages.vanguard_selection_simple import run as v5_run
            stage_start = time.time()
            v5_result = v5_run(dry_run=dry_run)
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

            approved          = _get_approved_rows(self.db_path)
            result["approved"] = len(approved)
            logger.info("Approved for execution: %d", len(approved))

            shortlist_msg = self._build_shortlist_telegram(cycle_ts)
            if shortlist_msg:
                self._send_telegram(shortlist_msg)
            v6_summary_msg = self._build_v6_summary_telegram(cycle_ts)
            if v6_summary_msg:
                self._send_telegram(v6_summary_msg)

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
        """Refresh the canonical Stage 1 universe on startup and refresh times."""
        et = self._now_et()
        refresh_key = et.strftime("%Y-%m-%d %H:%M")
        if not force and et.strftime("%H:%M") not in UNIVERSE_REFRESH_TIMES:
            return 0
        if not force and self._last_universe_refresh == refresh_key:
            return 0
        written = materialize_universe_members(self._vg_db, now_utc_str=iso_utc(self._now_utc()))
        self._last_universe_refresh = refresh_key
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

    def _run_v1(self) -> dict[str, int]:
        """Ensure latest Stage 1 bars are present from all live data sources."""
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
        if self._td_adapter:
            td_symbols_backup = self._td_adapter.symbols
            try:
                td_asset_classes = set()
                if crypto_session_active:
                    td_asset_classes.add("crypto")
                if forex_session_active and ibkr_forex_bars == 0:
                    td_asset_classes.add("forex")
                self._td_adapter.symbols = {
                    symbol: asset_class
                    for symbol, asset_class in td_symbols_backup.items()
                    if str(asset_class or "").lower() in td_asset_classes
                }
                logger.info(
                    "[V1] Twelve Data asset filter=%s symbols=%d",
                    sorted(td_asset_classes),
                    len(self._td_adapter.symbols),
                )
                if self._td_adapter.symbols:
                    td_bars = self._td_adapter.poll_latest_bars()
                    logger.info("[V1] Twelve Data polled: %d bars", td_bars)
            finally:
                self._td_adapter.symbols = td_symbols_backup
        timings["twelvedata_poll"] = time.time() - t

        t = time.time()
        bars_5m = self._aggregate_recent_1m_to_5m()
        timings["aggregate_1m_to_5m"] = time.time() - t
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
            "v1_ibkr_equity_bars": ibkr_equity_bars,
            "v1_ibkr_forex_bars": ibkr_forex_bars,
            "v1_bars_5m": bars_5m,
            "v1_bars_1h": bars_1h,
            "v1_source_health": health_rows,
        }

    def _aggregate_recent_1m_to_5m(self) -> int:
        """Aggregate a recent 1m window into 5m bars and upsert them."""
        latest_5m = self._vg_db.latest_bar_ts_utc("vanguard_bars_5m")
        if latest_5m:
            cutoff = (parse_utc(latest_5m) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            cutoff = (self._now_utc() - timedelta(minutes=70)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        bars_5m = aggregate_1m_to_5m(bars_1m)
        written = self._vg_db.upsert_bars_5m(bars_5m)
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
        return written

    def _aggregate_recent_5m_to_1h(self) -> int:
        """Aggregate a recent 5m window into 1h bars and upsert them."""
        latest_1h = self._vg_db.latest_bar_ts_utc("vanguard_bars_1h")
        if latest_1h:
            cutoff = (parse_utc(latest_1h) - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    def _execute_approved(self) -> None:
        """
        Execute approved positions from the latest V6 cycle.

        off/manual/paper → write FORWARD_TRACKED to execution_log (no real orders)
        live             → send via MetaApi/SignalStack, write FILLED or FAILED
        """
        approved = _get_approved_rows(self.db_path)
        if not approved:
            return

        logger.info(
            "Executing %d approved rows (mode=%s)", len(approved), self.execution_mode
        )

        submitted = 0
        filled    = 0
        failed    = 0
        forward   = 0
        metaapi_executor_cls = None
        metaapi_symbol_fn = None
        metaapi_executors: dict[str, Any] = {}
        gft_execution_state: dict[str, dict[str, Any]] = {}
        gft_manual_rows: list[dict[str, Any]] = []
        failure_reasons: Counter[str] = Counter()
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
            raw_shares = row.get("shares_or_lots") or 0
            shares = (
                float(raw_shares)
                if str(account or "").lower().startswith("gft_") or "/" in str(symbol or "")
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

            if shares <= 0:
                logger.warning("Skipping %s — shares=%s", symbol, shares)
                continue

            if self._is_test_execution_mode():
                forward += 1
                logger.info(
                    "[TEST NO_PERSIST] %s %s x%s account=%s",
                    symbol,
                    direction,
                    shares,
                    account,
                )
                continue

            if self._is_manual_execution_mode():
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
                    "[MANUAL FORWARD_TRACKED] %s %s x%s account=%s (id=%s)",
                    symbol,
                    direction,
                    shares,
                    account,
                    log_id,
                )

            else:
                submitted += 1
                profile = self._get_account_profile(account)
                bridge = str((profile or {}).get("execution_bridge") or "disabled").lower()
                webhook_url = str((profile or {}).get("webhook_url") or "").strip()

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
                                forward += 1
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
                                forward += 1
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
                        else:
                            forward += 1
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

                if bridge != "signalstack" or not webhook_url:
                    if bridge == "mt5":
                        logger.info("[V7] MT5 execution not yet implemented for %s", account)
                    elif bridge == "signalstack":
                        logger.info("[V7] No webhook_url configured for %s — forward-tracking", account)
                    else:
                        logger.info("[V7] Execution bridge %s for %s — forward-tracking", bridge, account)
                    forward += 1
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
                    logger.info(
                        "[%s] %s %s x%d → %s",
                        self.execution_mode.upper(), symbol, direction, shares, status,
                    )

                except Exception as exc:
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

        self._trades_placed   += filled
        self._forward_tracked += forward

        gft_time_exit_msg = self._build_gft_time_exit_telegram(gft_time_exit_rows)
        if gft_time_exit_msg:
            self._send_telegram(gft_time_exit_msg)

        if self._telegram and (submitted > 0 or forward > 0):
            unique_accounts = ",".join(
                sorted({r.get("account_id", "") for r in approved})
            )
            exec_msg = self._build_execution_summary_telegram(
                source="vanguard",
                account=unique_accounts,
                submitted=submitted,
                filled=filled,
                failed=failed,
                forward_tracked=forward,
                failure_reasons=failure_reasons,
            )
            self._send_telegram(exec_msg)

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
        gft_rows = []
        shortlist_prices: dict[str, float] = {}
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
                gft_rows = con.execute(
                    """
                    SELECT symbol, direction, stop_price, tp_price, shares_or_lots
                    FROM vanguard_tradeable_portfolio
                    WHERE cycle_ts_utc = ?
                      AND account_id = 'gft_10k'
                      AND status = 'APPROVED'
                    """,
                    (cycle_ts,),
                ).fetchall()
                shortlist_prices = self._load_latest_symbol_prices(
                    con,
                    symbols=[str(row[0]) for row in shortlist_rows],
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
            "Edge note: LONG higher=stronger | SHORT lower=stronger",
            "",
        ]

        if not shortlist_rows:
            lines.append("⚪ No shortlist rows this cycle")
            return "\n".join(lines).rstrip()

        grouped: dict[str, dict[str, list[tuple[str, float | None, float | None, int | None]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for symbol, asset_class, direction, predicted_return, edge_score, rank in shortlist_rows:
            grouped[str(asset_class or "unknown")][str(direction or "UNKNOWN")].append(
                (str(symbol), predicted_return, edge_score, rank)
            )

        gft_overlay = {
            (str(symbol or "").upper(), str(direction or "").upper()): {
                "stop_price": stop_price,
                "tp_price": tp_price,
                "lot_size": shares_or_lots,
            }
            for symbol, direction, stop_price, tp_price, shares_or_lots in gft_rows
        }

        self._record_shortlist_streak_history(shortlist_rows, cycle_ts)

        for asset_class in sorted(grouped.keys()):
            if not self._is_polling_asset_enabled(asset_class):
                lines.append(f"━━ {asset_class.upper()} — {self._asset_session_label(asset_class)} ━━")
                lines.append("")
                continue

            lines.append(f"━━ {asset_class.upper()} — {self._asset_session_label(asset_class)} ━━")

            longs = grouped[asset_class].get("LONG", [])
            if longs:
                lines.append(f"🟢 LONG ({len(longs)})")
                lines.extend(
                    self._format_shortlist_telegram_row(
                        symbol=symbol,
                        asset_class=asset_class,
                        direction="LONG",
                        edge_score=edge_score,
                        price=shortlist_prices.get(symbol),
                        streak_counter=self._shortlist_streak_counter(symbol),
                        gft_details=gft_overlay.get((symbol.upper(), "LONG")),
                    )
                    for symbol, _, edge_score, _ in longs[:5]
                )
                lines.append("")

            shorts = grouped[asset_class].get("SHORT", [])
            if shorts:
                lines.append(f"🔴 SHORT ({len(shorts)})")
                lines.extend(
                    self._format_shortlist_telegram_row(
                        symbol=symbol,
                        asset_class=asset_class,
                        direction="SHORT",
                        edge_score=edge_score,
                        price=shortlist_prices.get(symbol),
                        streak_counter=self._shortlist_streak_counter(symbol),
                        gft_details=gft_overlay.get((symbol.upper(), "SHORT")),
                    )
                    for symbol, _, edge_score, _ in shorts[:5]
                )
                lines.append("")

            lines.append("")

        return "\n".join(lines).rstrip()

    def _build_session_status_block(self) -> str:
        """Return a compact session-state header for operator context."""
        return "\n".join(
            [
                f"CRYPTO {self._asset_session_label('crypto')}",
                f"FOREX {self._asset_session_label('forex')}",
                f"EQUITY {self._asset_session_label('equity')}",
            ]
        )

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
        """Build one GFT-only crypto V6 summary from current-cycle portfolio rows."""
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

        gft_crypto_universe = self._load_gft_crypto_universe_symbols()
        grouped: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )
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
            grouped[str(account_id or "gft")][str(status or "UNKNOWN").upper()][
                str(direction or "UNKNOWN").upper()
            ].append(
                {
                    "symbol": str(symbol or "").upper(),
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    "tp_price": tp_price,
                    "shares_or_lots": shares_or_lots,
                    "edge_score": edge_score,
                    "rejection_reason": str(rejection_reason or "").strip(),
                }
            )

        if not grouped:
            return (
                f"🧮 <b>GFT V6 CRYPTO</b> — {cycle_ts[:16]}\n"
                "⚪ No GFT crypto rows in current universe"
            )

        lines = [
            f"🧮 <b>GFT V6 CRYPTO</b> — {cycle_ts[:16]}",
            "Edge note: LONG higher=stronger | SHORT lower=stronger",
            "",
        ]
        for account_id in ("gft_10k", "gft_5k"):
            account_rows = grouped.get(account_id)
            if not account_rows:
                continue

            ok_count = sum(
                len(side_rows) for side_rows in account_rows.get("APPROVED", {}).values()
            )
            reject_count = sum(
                len(side_rows)
                for status_name, status_rows in account_rows.items()
                if status_name != "APPROVED"
                for side_rows in status_rows.values()
            )
            lines.append(f"━━ {account_id} — OK {ok_count} / RJ {reject_count} ━━")

            for status_name, status_label in (
                ("APPROVED", "✅ APPROVED"),
                ("REJECTED", "🚫 REJECTED"),
            ):
                status_rows = account_rows.get(status_name)
                if not status_rows:
                    continue
                lines.append(status_label)
                for direction_name, side_emoji in (("LONG", "🟢"), ("SHORT", "🔴")):
                    trade_rows = status_rows.get(direction_name, [])
                    if not trade_rows:
                        continue
                    lines.append(f"{side_emoji} {direction_name} ({len(trade_rows)})")
                    for row in trade_rows[:6]:
                        status_note = "Status OK"
                        if status_name != "APPROVED":
                            status_note = f"Reject {row['rejection_reason'] or 'REJECTED'}"
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
            f"✅ <b>EXECUTION RUN</b> — GFT MANUAL ({self.execution_mode.upper()})",
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
        details = gft_details or self._build_fallback_gft_display_details(
            symbol=symbol,
            asset_class=asset_class,
            direction=direction,
            price=price,
        ) or {}
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
        return (
            f"{side_emoji} {html.escape(normalized)}  {html.escape(display_price)}  "
            f"Edge {float(edge_score or 0.0):.2f}  {html.escape(streak_text)}\n"
            f"SL {html.escape(sl_text)}  TP {html.escape(tp_text)}  "
            f"Lot {html.escape(lot_text)}"
            + (f"  {html.escape(status_note)}" if status_note else "")
        )

    @staticmethod
    def _load_gft_crypto_universe_symbols() -> set[str]:
        """Load current GFT crypto symbols from config/gft_universe.json."""
        try:
            with open(_GFT_UNIVERSE_PATH) as fh:
                payload = json.load(fh)
        except Exception as exc:
            logger.warning("Could not load GFT crypto universe config: %s", exc)
            return {"BNBUSD", "BTCUSD", "ETHUSD", "LTCUSD", "SOLUSD", "BCHUSD"}

        symbols = payload.get("gft_universe") if isinstance(payload, dict) else payload
        if not isinstance(symbols, list):
            return set()
        return {
            VanguardOrchestrator._normalize_gft_display_symbol(symbol)
            for symbol in symbols
            if "/" in str(symbol or "")
        }

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
            gft_details = self._build_fallback_gft_forex_details(
                symbol=symbol,
                asset_class=asset_class,
                direction=direction,
                price=price,
            )
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
        """Display-only fallback SL/TP/Lot for non-approved forex/crypto rows."""
        asset = str(asset_class or "").lower()
        if asset not in {"forex", "crypto"} or price is None:
            return None

        normalized = str(symbol or "").upper().replace("/", "").replace(".CASH", "")
        entry_price = float(price)
        if asset == "crypto":
            sl_offset = max(entry_price * 0.08, 0.01)
            tp_offset = max(entry_price * 0.16, 0.02)
            decimals = 2 if entry_price >= 100 else 5 if entry_price < 10 else 4
        else:
            is_jpy = normalized.endswith("JPY")
            sl_offset = 0.20 if is_jpy else 0.0020
            tp_offset = 0.40 if is_jpy else 0.0040
            decimals = 3 if is_jpy else 5
        if str(direction or "").upper() == "LONG":
            stop_price = round(entry_price - sl_offset, decimals)
            tp_price = round(entry_price + tp_offset, decimals)
        else:
            stop_price = round(entry_price + sl_offset, decimals)
            tp_price = round(entry_price - tp_offset, decimals)
        stop_distance = abs(entry_price - stop_price)
        if asset == "crypto" and stop_distance > 0:
            contract_size = VanguardOrchestrator._crypto_display_contract_size(symbol, entry_price)
            lot_size = math.floor((50.0 / (stop_distance * contract_size)) * 1_000_000.0) / 1_000_000.0
            lot_size = max(0.000001, lot_size)
        else:
            lot_size = 0.02
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
            return
        for chunk in self._chunk_telegram_message(message):
            try:
                self._telegram.send(chunk)
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)

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
        default="manual",
        metavar="{manual,live,test}",
        help="manual=forward-track only, live=real orders, test=no-persist dry run (default: manual). Deprecated aliases off/paper map to manual.",
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
        "--interval", type=int, default=CYCLE_MINUTES * 60,
        help=f"Cycle interval in seconds for loop mode (default: {CYCLE_MINUTES * 60})",
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
        default="",
        help="Comma-separated asset classes to bypass market-session polling gates, e.g. forex,crypto",
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
