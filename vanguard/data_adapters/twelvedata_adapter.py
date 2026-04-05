"""
twelvedata_adapter.py — Twelve Data REST adapter for non-equity instruments.

Polls 1-minute bars for forex, indices, metals, crypto, commodities.
Grow plan: 377 API calls/min. Up to 120 symbols per batch request.

Key facts:
  - REST polling only (no WebSocket on Grow plan)
  - Batch: up to 120 symbols per time_series request
  - Twelve Data timestamp is bar OPEN time — +60s → bar END time (Vanguard convention)
  - Rate limit: 377 calls/min on Grow (we make at most 1 call/batch/cycle)
  - Env var: TWELVEDATA_API_KEY  (or TWELVE_DATA_API_KEY)
  - Base URL: https://api.twelvedata.com

Writes to: vanguard_bars_1m (same table as AlpacaWSAdapter)

Location: ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL    = "https://api.twelvedata.com"
_BATCH_SIZE  = int(os.environ.get("TWELVEDATA_BATCH_SIZE", "20"))
_REQUEST_TIMEOUT = 30       # seconds
_INTER_BATCH_SLEEP = 0.2    # seconds between batches (rate-limit pacing)
_CREDITS_PER_MINUTE = int(os.environ.get("TWELVEDATA_CREDITS_PER_MINUTE", "55"))

_INVALID_API_KEYS = {
    "",
    "your_actual_key_here",
    "YOUR_ACTUAL_KEY_HERE",
    "your_api_key_here",
    "YOUR_API_KEY_HERE",
    "changeme",
    "CHANGE_ME",
}


def _normalize_api_key(value: str | None) -> str:
    key = str(value or "").strip().strip("'\"")
    return "" if key in _INVALID_API_KEYS else key


def _load_twelve_data_key() -> str:
    """Load Twelve Data API key from env first, then known .env locations."""
    env_paths = [
        Path.home() / "SS" / ".env.shared",
        Path(__file__).resolve().parents[2] / ".env",
        Path.home() / "SS" / "Vanguard" / ".env",
        Path.home() / "SS" / ".env",
    ]
    for env_path in env_paths:
        if env_path.exists():
            load_dotenv(env_path, override=True)

    key = _normalize_api_key(os.environ.get("TWELVE_DATA_API_KEY")) or _normalize_api_key(
        os.environ.get("TWELVEDATA_API_KEY")
    )
    if key:
        return key

    for env_path in env_paths:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            env_key, value = text.split("=", 1)
            env_key = env_key.strip()
            if env_key not in {"TWELVE_DATA_API_KEY", "TWELVEDATA_API_KEY"}:
                continue
            key = _normalize_api_key(value)
            if not key:
                continue
            os.environ[env_key] = key
            return key
    return ""


_API_KEY = _load_twelve_data_key()


def get_active_symbols(
    db_path: str,
    data_source: str = "twelvedata",
) -> dict[str, str]:
    """Load active symbols for a source from vanguard_universe_members."""
    with sqlite_conn(db_path) as con:
        rows = con.execute(
            """
            SELECT symbol, asset_class
            FROM vanguard_universe_members
            WHERE data_source = ? AND is_active = 1
            ORDER BY symbol
            """,
            (data_source,),
        ).fetchall()
    return {row[0]: row[1] for row in rows}

# ---------------------------------------------------------------------------
# TwelveDataAdapter
# ---------------------------------------------------------------------------


class TwelveDataAdapter:
    """
    REST polling adapter for forex, indices, metals, crypto, commodities.

    `symbols` is a dict mapping symbol string → asset_class string, e.g.:
        {
            "EUR/USD": "forex",
            "XAU/USD": "metal",
            "BTC/USD": "crypto",
            "SPX":     "index",
        }

    Call poll_latest_bars() every 5 minutes from the V1 orchestrator.
    Call fetch_historical() once for initial backfill.
    """

    def __init__(
        self,
        symbols: dict[str, str],
        db_path: str,
        api_key: str | None = None,
    ):
        self.symbols  = symbols   # {symbol: asset_class}
        self.db_path  = db_path
        self.api_key  = api_key or _API_KEY or ""

        # Stats
        self.bars_received: int         = 0
        self.last_poll_ts:  str | None  = None
        self._error_count:  int         = 0
        self._last_errors:  list[str]   = []

        self._ensure_1m_table()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_1m_table(self) -> None:
        """Create vanguard_bars_1m if it doesn't exist (idempotent)."""
        with sqlite_conn(self.db_path) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_bars_1m (
                    symbol        TEXT    NOT NULL,
                    bar_ts_utc    TEXT    NOT NULL,
                    open          REAL,
                    high          REAL,
                    low           REAL,
                    close         REAL,
                    volume        INTEGER,
                    tick_volume   INTEGER DEFAULT 0,
                    asset_class   TEXT    DEFAULT 'unknown',
                    data_source   TEXT    DEFAULT 'twelvedata',
                    ingest_ts_utc TEXT,
                    PRIMARY KEY (symbol, bar_ts_utc)
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_vg_bars_1m_ts
                    ON vanguard_bars_1m(bar_ts_utc)
            """)
            con.commit()

    # ------------------------------------------------------------------
    # Public polling API
    # ------------------------------------------------------------------

    def poll_latest_bars(self) -> int:
        """
        Fetch the latest 5 completed 1m bars for all symbols.
        Call this every 5 minutes from the V7 cycle.
        Returns total bars written.
        """
        global _API_KEY
        if not self.api_key:
            _API_KEY = _API_KEY or _load_twelve_data_key()
            self.api_key = _API_KEY
        if not self.api_key:
            logger.error(
                "[twelvedata] No API key found in env or .env files. Skipping poll."
            )
            return 0

        symbol_list = list(self.symbols.keys())
        total_written = 0
        credits_used = 0
        window_started = time.time()

        for i in range(0, len(symbol_list), _BATCH_SIZE):
            batch = symbol_list[i : i + _BATCH_SIZE]
            elapsed = time.time() - window_started
            batch_credits = len(batch)
            if credits_used + batch_credits > _CREDITS_PER_MINUTE and elapsed < 60:
                sleep_for = 60 - elapsed + 1
                logger.info(
                    "[twelvedata] Credit budget reached (%d/%d). Sleeping %.1fs before next batch",
                    credits_used,
                    _CREDITS_PER_MINUTE,
                    sleep_for,
                )
                time.sleep(max(sleep_for, 0))
                window_started = time.time()
                credits_used = 0
            # Fetch 5 completed 1m bars so the downstream 1m->5m aggregator
            # can build a real 5m bucket instead of a single-minute stub.
            written = self._fetch_and_write_batch(batch, outputsize=5)
            total_written += written
            credits_used += batch_credits

            if i + _BATCH_SIZE < len(symbol_list):
                time.sleep(_INTER_BATCH_SLEEP)

        self.last_poll_ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.bars_received += total_written
        logger.info(
            "[twelvedata] Poll complete: %d symbols → %d bars written",
            len(symbol_list), total_written,
        )
        return total_written

    def fetch_historical(
        self,
        symbol: str,
        start_date: str,
        end_date: str | None = None,
        outputsize: int = 5000,
    ) -> int:
        """
        Fetch historical 1m bars for a single symbol (backfill).

        start_date / end_date: "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS" (UTC)
        outputsize: max bars per request (Twelve Data cap: 5000 on Grow)
        Returns bars written.
        """
        global _API_KEY
        if not self.api_key:
            _API_KEY = _API_KEY or _load_twelve_data_key()
            self.api_key = _API_KEY
        if not self.api_key:
            logger.error("[twelvedata] No API key found in env or .env files")
            return 0

        params: dict[str, Any] = {
            "symbol":     symbol,
            "interval":   "1min",
            "start_date": start_date,
            "apikey":     self.api_key,
            "timezone":   "UTC",
            "outputsize": outputsize,
        }
        if end_date:
            params["end_date"] = end_date

        try:
            resp = requests.get(
                f"{_BASE_URL}/time_series", params=params, timeout=_REQUEST_TIMEOUT
            )
            if resp.status_code != 200:
                logger.error(
                    "[twelvedata] Historical %s HTTP %d: %s",
                    symbol, resp.status_code, resp.text[:200],
                )
                return 0

            data = resp.json()
            if data.get("status") == "error":
                logger.error(
                    "[twelvedata] Historical %s API error: %s",
                    symbol, data.get("message", "unknown"),
                )
                return 0

            written = self._write_single_symbol(symbol, data)
            self.bars_received += written
            logger.info("[twelvedata] Backfill %s: %d bars", symbol, written)
            return written

        except Exception as exc:
            logger.error("[twelvedata] Historical %s error: %s", symbol, exc)
            self._last_errors.append(str(exc))
            return 0

    # ------------------------------------------------------------------
    # BaseDataAdapter stubs (for interface compatibility)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """REST adapter — no persistent connection. No-op."""

    async def stop(self) -> None:
        """REST adapter — no persistent connection. No-op."""

    def get_status(self) -> dict:
        return {
            "adapter":       "twelvedata",
            "symbols":       len(self.symbols),
            "bars_received": self.bars_received,
            "last_poll":     self.last_poll_ts,
            "running":       True,
            "errors":        self._error_count,
            "last_errors":   self._last_errors[-5:],
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_and_write_batch(self, batch: list[str], outputsize: int = 1) -> int:
        """Fetch a batch of symbols from Twelve Data and write to DB."""
        sym_str = ",".join(batch)
        try:
            resp = requests.get(
                f"{_BASE_URL}/time_series",
                params={
                    "symbol":     sym_str,
                    "interval":   "1min",
                    "outputsize": outputsize,
                    "apikey":     self.api_key,
                    "timezone":   "UTC",
                },
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            logger.error("[twelvedata] Request error (batch %s...): %s", batch[0], exc)
            self._error_count += 1
            self._last_errors.append(str(exc))
            return 0

        if resp.status_code == 429:
            logger.warning("[twelvedata] Rate limit hit — sleeping 60s")
            time.sleep(60)
            return 0
        if resp.status_code != 200:
            logger.error("[twelvedata] Batch HTTP %d: %s", resp.status_code, resp.text[:200])
            self._error_count += 1
            return 0

        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "error":
            message = data.get("message", "unknown")
            logger.error("[twelvedata] Batch API error: %s", message)
            self._error_count += 1
            self._last_errors.append(str(message))
            if data.get("code") == 429:
                time.sleep(61)
            return 0

        # Single symbol → Twelve Data returns dict directly (not nested)
        if len(batch) == 1:
            data = {batch[0]: data}

        written = 0
        for sym, sym_data in data.items():
            if not isinstance(sym_data, dict):
                continue
            if sym_data.get("status") == "error":
                logger.debug(
                    "[twelvedata] %s: %s", sym, sym_data.get("message", "error")
                )
                continue
            written += self._write_single_symbol(sym, sym_data)

        return written

    def _write_single_symbol(self, symbol: str, data: dict) -> int:
        """
        Write bars from a single-symbol Twelve Data response.

        Twelve Data `values` list has most-recent bar first.
        Each bar: datetime (open time UTC), open, high, low, close, volume.
        Vanguard convention: store bar END = open + 60s.
        """
        values = data.get("values")
        if not values:
            return 0

        asset_class = self.symbols.get(symbol, "unknown")
        now_iso     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows        = []

        for bar in values:
            try:
                # Parse bar open time (UTC)
                dt_open = datetime.strptime(bar["datetime"], "%Y-%m-%d %H:%M:%S")
                dt_open = dt_open.replace(tzinfo=timezone.utc)
                # Vanguard convention: bar_ts_utc = bar END time
                bar_end_ts = (dt_open + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

                rows.append((
                    symbol, bar_end_ts,
                    float(bar["open"]),
                    float(bar["high"]),
                    float(bar["low"]),
                    float(bar["close"]),
                    int(bar["volume"]) if bar.get("volume") not in (None, "0", 0, "") else 0,
                    asset_class, now_iso,
                ))
            except Exception as exc:
                logger.warning("[twelvedata] Skipping bar for %s: %s", symbol, exc)

        if not rows:
            return 0

        try:
            with sqlite_conn(self.db_path) as con:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO vanguard_bars_1m
                        (symbol, bar_ts_utc, open, high, low, close, volume,
                         asset_class, data_source, ingest_ts_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'twelvedata', ?)
                    """,
                    rows,
                )
                con.commit()
            return len(rows)
        except Exception as exc:
            logger.error("[twelvedata] DB write failed for %s: %s", symbol, exc)
            self._error_count += 1
            return 0


# ---------------------------------------------------------------------------
# Factory: load from config file
# ---------------------------------------------------------------------------


def load_from_config(
    config_path: str | None = None,
    db_path: str | None = None,
    api_key: str | None = None,
) -> TwelveDataAdapter:
    """
    Build a TwelveDataAdapter from config/twelvedata_symbols.json.

    config_path: path to twelvedata_symbols.json (default: repo/config/)
    db_path:     path to vanguard_universe.db
    """
    _repo_root = Path(__file__).resolve().parent.parent.parent
    if config_path is None:
        config_path = str(_repo_root / "config" / "twelvedata_symbols.json")
    if db_path is None:
        db_path = str(_repo_root / "data" / "vanguard_universe.db")

    symbol_map = get_active_symbols(db_path)
    if symbol_map:
        logger.info(
            "[twelvedata] Loaded %d active symbols from universe_members",
            len(symbol_map),
        )
    else:
        with open(config_path) as fh:
            cfg = json.load(fh)

        symbol_map = {}
        for asset_class, symbols in cfg.items():
            if asset_class.startswith("_"):
                continue
            if isinstance(symbols, list):
                for sym in symbols:
                    symbol_map[sym] = asset_class

        logger.info(
            "[twelvedata] Loaded %d symbols from %s", len(symbol_map), config_path
        )
    return TwelveDataAdapter(symbols=symbol_map, db_path=db_path, api_key=api_key)
