"""
alpaca_adapter.py — Alpaca REST API client for Vanguard.

Fetches 5m bars and daily bars for US equities (IEX free tier).
Batch-fetches up to 500 symbols per request (Alpaca's limit).

Endpoints used:
  Assets:    https://paper-api.alpaca.markets/v2/assets
  Bars/Data: https://data.alpaca.markets/v2/stocks/bars
  Snapshots: https://data.alpaca.markets/v2/stocks/snapshots

Env vars required:
  ALPACA_KEY    — API key ID
  ALPACA_SECRET — API secret key

Location: ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROKER_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL = "https://data.alpaca.markets/v2"

BATCH_SIZE = 500          # Alpaca max symbols per multi-symbol request
DEFAULT_FEED = "iex"      # Free tier; swap to "sip" with paid subscription

# Dotted suffix patterns (e.g. XYZ.WS, XYZ.WT, XYZ.U)
_EXCLUDED_DOTTED_SUFFIXES = (".WS", ".WT", ".U", ".R", ".A", ".B", ".W")
# Bare suffix patterns for symbols ≥5 chars (e.g. RCKTW, XYZR, XYZU)
# Only applied when the base portion is ≥3 chars to avoid false positives
_EXCLUDED_BARE_SUFFIXES = ("WS", "WT", "WW", "W", "R", "U")

# Known leveraged/inverse ETF prefixes (extend as needed)
_LEVERAGED_ETF_SYMBOLS = frozenset({
    "TQQQ", "SQQQ", "UVXY", "SVXY", "SPXU", "SPXS", "SPXL", "UPRO",
    "SOXS", "SOXL", "LABD", "LABU", "FAZ", "FAS", "TNA", "TZA",
    "HIBL", "HIBS", "NAIL", "DRIP", "GUSH", "OILU", "OILD",
})

# Exchanges accepted as "non-OTC"
_VALID_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "BATS", "IEX", "CBOE"})

_INVALID_ALPACA_VALUES = {
    "",
    "your_key",
    "your_secret",
    "your_api_key_here",
    "your_api_secret_here",
    "your_actual_key_here",
    "your_actual_secret_here",
    "changeme",
    "CHANGE_ME",
}


def _normalize_cred(value: str | None) -> str:
    text = str(value or "").strip().strip("'\"")
    return "" if text in _INVALID_ALPACA_VALUES else text


def _load_alpaca_creds() -> tuple[str, str]:
    env_paths = [
        Path.home() / "SS" / ".env",
        Path.home() / "SS" / ".env.shared",
        Path(__file__).resolve().parents[2] / ".env",
        Path.home() / "SS" / "Vanguard" / ".env",
    ]
    for env_path in env_paths:
        if env_path.exists():
            load_dotenv(env_path, override=False)

    key = (
        _normalize_cred(os.environ.get("ALPACA_KEY"))
        or _normalize_cred(os.environ.get("ALPACA_API_KEY"))
        or _normalize_cred(os.environ.get("APCA_API_KEY_ID"))
    )
    secret = (
        _normalize_cred(os.environ.get("ALPACA_SECRET"))
        or _normalize_cred(os.environ.get("ALPACA_API_SECRET"))
        or _normalize_cred(os.environ.get("APCA_API_SECRET_KEY"))
    )

    if key and secret:
        return key, secret

    wanted = {
        "ALPACA_KEY",
        "ALPACA_API_KEY",
        "APCA_API_KEY_ID",
        "ALPACA_SECRET",
        "ALPACA_API_SECRET",
        "APCA_API_SECRET_KEY",
    }
    for env_path in env_paths:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            env_key, raw_value = text.split("=", 1)
            env_key = env_key.strip()
            if env_key not in wanted:
                continue
            value = _normalize_cred(raw_value)
            if not value:
                continue
            os.environ.setdefault(env_key, value)
        key = (
            _normalize_cred(os.environ.get("ALPACA_KEY"))
            or _normalize_cred(os.environ.get("ALPACA_API_KEY"))
            or _normalize_cred(os.environ.get("APCA_API_KEY_ID"))
        )
        secret = (
            _normalize_cred(os.environ.get("ALPACA_SECRET"))
            or _normalize_cred(os.environ.get("ALPACA_API_SECRET"))
            or _normalize_cred(os.environ.get("APCA_API_SECRET_KEY"))
        )
        if key and secret:
            return key, secret

    return key, secret


_ALPACA_KEY, _ALPACA_SECRET = _load_alpaca_creds()


def get_active_equity_symbols(db_path: str) -> list[str]:
    """Load active Alpaca equity symbols from vanguard_universe_members."""
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            """
            SELECT symbol
            FROM vanguard_universe_members
            WHERE data_source = 'alpaca' AND is_active = 1
            ORDER BY symbol
            """
        ).fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# AlpacaAdapter
# ---------------------------------------------------------------------------

class AlpacaAdapter:
    """
    REST-only Alpaca market data adapter.
    Fetches assets, daily bars (for prefilter), and 5m bars for cache.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        feed: str = DEFAULT_FEED,
        request_timeout: int = 30,
    ):
        self.api_key = _normalize_cred(api_key) or _ALPACA_KEY
        self.api_secret = _normalize_cred(api_secret) or _ALPACA_SECRET
        if not self.api_key or not self.api_secret:
            raise EnvironmentError(
                "Alpaca credentials missing. Set ALPACA_KEY and ALPACA_SECRET."
            )
        self.feed = feed
        self.timeout = request_timeout
        self._headers = {
            "APCA-API-KEY-ID":     self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> dict | list:
        """GET with error handling. Returns parsed JSON."""
        resp = requests.get(url, headers=self._headers, params=params, timeout=self.timeout)
        if resp.status_code == 429:
            logger.warning("Alpaca rate limit hit — sleeping 60s")
            time.sleep(60)
            resp = requests.get(url, headers=self._headers, params=params, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Alpaca API error {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def _paginate_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: str,
        end: str,
    ) -> dict[str, list[dict]]:
        """
        Fetch multi-symbol bars with pagination.
        Handles next_page_token until exhausted.
        Returns {symbol: [bar_dict, ...]}
        """
        result: dict[str, list[dict]] = {}
        params: dict = {
            "symbols":   ",".join(symbols),
            "timeframe": timeframe,
            "start":     start,
            "end":       end,
            "feed":      self.feed,
            "limit":     10000,
            "adjustment": "raw",
        }
        while True:
            data = self._get(f"{DATA_URL}/stocks/bars", params)
            bars_map: dict = data.get("bars") or {}
            for sym, bars in bars_map.items():
                result.setdefault(sym, []).extend(bars)
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return result

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def get_assets(self) -> list[dict]:
        """
        Fetch all active US equity assets from the broker API.
        Returns raw list of asset dicts.
        """
        logger.info("Fetching Alpaca asset list…")
        assets = self._get(
            f"{BROKER_URL}/assets",
            params={"status": "active", "asset_class": "us_equity"},
        )
        logger.info("Alpaca returned %d total assets", len(assets))
        return assets

    def filter_assets(self, assets: list[dict]) -> list[str]:
        """
        Apply hard filters to reduce the raw asset list.
        Returns list of tradeable symbols (non-OTC, no warrants/units, not leveraged ETF).
        """
        survivors = []
        for a in assets:
            sym = a.get("symbol", "")
            if not a.get("tradable", False):
                continue
            if a.get("exchange", "") not in _VALID_EXCHANGES:
                continue
            upper = sym.upper()
            if any(upper.endswith(sfx) for sfx in _EXCLUDED_DOTTED_SUFFIXES):
                continue
            # Bare warrant/unit/rights suffixes (e.g. RCKTW, XYZR, XYZU)
            # Only filter if base is at least 3 chars (avoids filtering "W" or "U")
            if any(
                upper.endswith(sfx) and len(upper) >= len(sfx) + 3
                for sfx in _EXCLUDED_BARE_SUFFIXES
            ):
                continue
            if sym.upper() in _LEVERAGED_ETF_SYMBOLS:
                continue
            survivors.append(sym.upper())
        logger.info(
            "Asset filter: %d total → %d after hard filter",
            len(assets), len(survivors),
        )
        return survivors

    # ------------------------------------------------------------------
    # Daily bars (for prefilter avg volume)
    # ------------------------------------------------------------------

    def get_daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
    ) -> dict[str, list[dict]]:
        """
        Fetch daily bars for a list of symbols in batches of BATCH_SIZE.

        start / end: ISO-8601 date or datetime strings (UTC).
        Returns {symbol: [bar_dict, ...]} where each bar has keys:
          t, o, h, l, c, v, n, vw
        """
        result: dict[str, list[dict]] = {}
        total = len(symbols)
        for i in range(0, total, BATCH_SIZE):
            batch = symbols[i : i + BATCH_SIZE]
            logger.debug(
                "Daily bars batch %d/%d (%d symbols)",
                i // BATCH_SIZE + 1,
                (total + BATCH_SIZE - 1) // BATCH_SIZE,
                len(batch),
            )
            batch_result = self._paginate_bars(batch, "1Day", start, end)
            result.update(batch_result)
        return result

    # ------------------------------------------------------------------
    # 5-minute bars (main cache fetch)
    # ------------------------------------------------------------------

    def get_5m_bars(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        """
        Fetch 5-minute bars for a list of symbols in batches of BATCH_SIZE.

        start / end: tz-aware datetime objects (UTC).
        Returns {symbol: [bar_dict, ...]} where bar dict has keys:
          t (bar open time UTC ISO), o, h, l, c, v, n, vw
        """
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso   = end.strftime("%Y-%m-%dT%H:%M:%SZ")

        result: dict[str, list[dict]] = {}
        total = len(symbols)
        batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        for i in range(0, total, BATCH_SIZE):
            batch = symbols[i : i + BATCH_SIZE]
            logger.debug(
                "5m bars batch %d/%d (%d symbols) %s → %s",
                i // BATCH_SIZE + 1, batches, len(batch),
                start_iso, end_iso,
            )
            batch_result = self._paginate_bars(batch, "5Min", start_iso, end_iso)
            result.update(batch_result)

        n_with_data = sum(1 for v in result.values() if v)
        logger.info(
            "5m bars fetched: %d/%d symbols have data",
            n_with_data, total,
        )
        return result

    def get_1m_bars(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        """Fetch 1-minute bars for a list of symbols."""
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        result: dict[str, list[dict]] = {}
        total = len(symbols)
        batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, total, BATCH_SIZE):
            batch = symbols[i : i + BATCH_SIZE]
            logger.debug(
                "1m bars batch %d/%d (%d symbols) %s → %s",
                i // BATCH_SIZE + 1, batches, len(batch), start_iso, end_iso,
            )
            batch_result = self._paginate_bars(batch, "1Min", start_iso, end_iso)
            result.update(batch_result)
        n_with_data = sum(1 for v in result.values() if v)
        logger.info("1m bars fetched: %d/%d symbols have data", n_with_data, total)
        return result

    def get_bars(
        self,
        symbols: list[str],
        timeframe: str = "5Min",
        limit: int = 5,
    ):
        """Convenience fetch used for diagnostics; returns a DataFrame."""
        import pandas as pd

        tf = timeframe.lower()
        minutes = {"1min": 1, "5min": 5}.get(tf, 5)
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=max(limit * minutes * 3, 15))
        if tf == "1min":
            raw = self.get_1m_bars(symbols, start, end)
        else:
            raw = self.get_5m_bars(symbols, start, end)

        rows: list[dict] = []
        for symbol, bars in raw.items():
            for bar in bars[-limit:]:
                rows.append(
                    {
                        "symbol": symbol,
                        "bar_ts_utc": bar["t"],
                        "open": float(bar["o"]),
                        "high": float(bar["h"]),
                        "low": float(bar["l"]),
                        "close": float(bar["c"]),
                        "volume": int(bar.get("v", 0)),
                    }
                )
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # Snapshots (fast prefilter check)
    # ------------------------------------------------------------------

    def get_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """
        Fetch latest snapshots for a list of symbols.
        Returns {symbol: {dailyBar, prevDailyBar, latestTrade, ...}}
        """
        result: dict[str, dict] = {}
        for i in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[i : i + BATCH_SIZE]
            data = self._get(
                f"{DATA_URL}/stocks/snapshots",
                params={"symbols": ",".join(batch), "feed": self.feed},
            )
            result.update(data)
        return result

    # ------------------------------------------------------------------
    # Prefilter: assets → survivors
    # ------------------------------------------------------------------

    def build_equity_universe(
        self,
        min_price: float = 2.0,
        min_avg_volume: int = 500_000,
        lookback_days: int = 10,
    ) -> list[str]:
        """
        Full prefilter pipeline:
          1. Fetch all active US equity assets
          2. Hard filter (non-OTC, no warrants, tradable)
          3. Use Alpaca snapshots for volume+price (returns SIP-level daily volume,
             not IEX-sampled). Falls back to IEX daily bars for any symbols not
             found in snapshots.
          4. Apply: close > min_price AND avg_volume > min_avg_volume
        Returns list of survivor symbols (~1,500-2,000).

        NOTE: IEX daily bars only capture ~2% of real market volume. The snapshot
        API (prevDailyBar) returns actual SIP volume and is used preferentially.
        """
        # Step 1–2: assets
        raw_assets = self.get_assets()
        candidates = self.filter_assets(raw_assets)

        # Step 3: Use snapshots for SIP-level daily volume (fast, one call per 500)
        logger.info("Fetching snapshots for %d candidates (SIP volume check)…", len(candidates))
        snapshots = self.get_snapshots(candidates)

        # Step 4: apply volume + price filter using snapshot data
        survivors = []
        no_snapshot = []
        for sym in candidates:
            snap = snapshots.get(sym)
            if snap:
                # Use prevDailyBar for stable (completed) daily OHLCV
                daily_bar = snap.get("prevDailyBar") or snap.get("dailyBar") or {}
                last_close = float(daily_bar.get("c", 0))
                daily_vol  = int(daily_bar.get("v", 0))
                if last_close >= min_price and daily_vol >= min_avg_volume:
                    survivors.append(sym)
            else:
                no_snapshot.append(sym)

        if no_snapshot:
            logger.debug("%d symbols had no snapshot — skipping (no recent data)", len(no_snapshot))

        logger.info(
            "Prefilter complete: %d candidates → %d survivors "
            "(price≥$%.2f, avg_vol≥%d)",
            len(candidates), len(survivors), min_price, min_avg_volume,
        )
        return sorted(survivors)


# ---------------------------------------------------------------------------
# AlpacaWSAdapter — WebSocket streaming for 1m bars
# ---------------------------------------------------------------------------


WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
_WS_BATCH = 500          # symbols per subscribe message
_RECONNECT_BASE  = 5.0   # seconds before first reconnect
_RECONNECT_MAX   = 120.0 # cap


class AlpacaWSAdapter:
    """
    Alpaca IEX WebSocket adapter for real-time 1-minute bars.

    Streams completed 1m bars for up to 2,500 US equity symbols.
    Writes to vanguard_bars_1m (bar_ts_utc = bar END time, Vanguard convention).

    Usage (in async context):
        adapter = AlpacaWSAdapter(symbols=["AAPL", "MSFT", ...], db_path=DB_PATH)
        task = asyncio.create_task(adapter.start())
        # … run session …
        await adapter.stop()
    """

    def __init__(
        self,
        symbols: list[str],
        db_path: str,
        api_key: str | None = None,
        api_secret: str | None = None,
    ):
        self.symbols    = [s.upper() for s in symbols]
        self.db_path    = db_path
        self.api_key    = _normalize_cred(api_key) or _ALPACA_KEY
        self.api_secret = _normalize_cred(api_secret) or _ALPACA_SECRET
        self.running    = False

        # Stats
        self.bars_received: int        = 0
        self.last_bar_ts:   str | None = None
        self.connect_attempts: int     = 0
        self.last_error:    str | None = None

        # Single persistent DB connection reused for ALL bar writes.
        # sqlite3.connect() used as a context manager only manages transactions,
        # not connection lifetime — opening one per write leaks file descriptors.
        self._db_con: sqlite3.Connection | None = None
        self._bars_since_checkpoint: int = 0
        _CHECKPOINT_EVERY = 500  # WAL checkpoint after this many bars written

        self._checkpoint_interval = _CHECKPOINT_EVERY

        # Ensure vanguard_bars_1m table exists on init (sync, one-time)
        self._ensure_1m_table()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _get_db_con(self) -> sqlite3.Connection:
        """
        Return the persistent DB connection, opening it if not yet open.

        Reusing one connection for all writes eliminates the FD leak caused by
        opening sqlite3.connect() on every _write_bars() call.
        """
        if self._db_con is None:
            self._db_con = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            self._db_con.execute("PRAGMA journal_mode=WAL;")
            self._db_con.execute("PRAGMA synchronous=NORMAL;")
            self._db_con.execute("PRAGMA busy_timeout=30000;")
            logger.debug("[alpaca_ws] Opened persistent DB connection")
        return self._db_con

    def _checkpoint_wal(self) -> None:
        """Run WAL checkpoint to prevent unbounded WAL growth."""
        try:
            if self._db_con is not None:
                self._db_con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._db_con.commit()
                logger.debug("[alpaca_ws] WAL checkpoint done")
        except Exception as exc:
            logger.warning("[alpaca_ws] WAL checkpoint failed: %s", exc)

    def _ensure_1m_table(self) -> None:
        """Create vanguard_bars_1m if it doesn't exist."""
        con = self._get_db_con()
        con.execute("""
            CREATE TABLE IF NOT EXISTS vanguard_bars_1m (
                symbol       TEXT    NOT NULL,
                bar_ts_utc   TEXT    NOT NULL,
                open         REAL,
                high         REAL,
                low          REAL,
                close        REAL,
                volume       INTEGER,
                tick_volume  INTEGER DEFAULT 0,
                asset_class  TEXT    DEFAULT 'equity',
                data_source  TEXT    DEFAULT 'alpaca_ws',
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
    # Async start/stop (implements BaseDataAdapter interface)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Connect to Alpaca IEX WebSocket, subscribe to all symbols,
        and stream bars. Reconnects with exponential backoff on failure.
        Runs until stop() is called.
        """
        import asyncio
        import websockets

        self.running = True
        delay = _RECONNECT_BASE

        while self.running:
            self.connect_attempts += 1
            try:
                async with websockets.connect(WS_URL, ping_interval=30) as ws:
                    logger.info("[alpaca_ws] Connected (attempt %d)", self.connect_attempts)
                    delay = _RECONNECT_BASE  # reset on success

                    # Alpaca sends an initial "connected" success event before auth.
                    try:
                        initial = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if isinstance(initial, list) and initial:
                            logger.debug("[alpaca_ws] Initial handshake: %s", initial)
                    except Exception:
                        pass

                    # ── Auth ──────────────────────────────────────────────
                    await ws.send(json.dumps({
                        "action": "auth",
                        "key":    self.api_key,
                        "secret": self.api_secret,
                    }))
                    resp = None
                    authenticated = False
                    for _ in range(3):
                        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if any(msg.get("msg") == "authenticated" for msg in resp if isinstance(msg, dict)):
                            authenticated = True
                            break
                        if any(msg.get("msg") == "connected" for msg in resp if isinstance(msg, dict)):
                            continue
                    if not authenticated:
                        logger.error("[alpaca_ws] Auth failed: %s", resp)
                        self.last_error = f"auth_failed: {resp}"
                        return  # credentials won't fix themselves — stop

                    logger.info("[alpaca_ws] Authenticated")

                    # ── Subscribe in batches ───────────────────────────────
                    for i in range(0, len(self.symbols), _WS_BATCH):
                        batch = self.symbols[i : i + _WS_BATCH]
                        await ws.send(json.dumps({"action": "subscribe", "bars": batch}))
                        await asyncio.wait_for(ws.recv(), timeout=15)  # consume confirm
                        logger.info(
                            "[alpaca_ws] Subscribed batch %d/%d (%d symbols)",
                            i // _WS_BATCH + 1,
                            (len(self.symbols) + _WS_BATCH - 1) // _WS_BATCH,
                            len(batch),
                        )
                        if len(self.symbols) > _WS_BATCH:
                            await asyncio.sleep(0.2)  # gentle pacing

                    logger.info("[alpaca_ws] All %d symbols subscribed — listening", len(self.symbols))

                    # ── Receive loop ───────────────────────────────────────
                    while self.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        except asyncio.TimeoutError:
                            # Send ping to keep connection alive
                            await ws.send(json.dumps({"action": "ping"}))
                            continue

                        messages = json.loads(raw)
                        bars = [m for m in messages if m.get("T") == "b"]
                        if bars:
                            self._write_bars(bars)

            except Exception as exc:
                self.last_error = str(exc)
                if not self.running:
                    break
                logger.warning(
                    "[alpaca_ws] Disconnected (%s) — reconnecting in %.0fs",
                    exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX)

        logger.info("[alpaca_ws] Stopped")

    async def stop(self) -> None:
        """Signal the receive loop to exit cleanly, then flush and close DB."""
        self.running = False
        logger.info("[alpaca_ws] Stop requested")
        self._checkpoint_wal()
        if self._db_con is not None:
            try:
                self._db_con.close()
                logger.debug("[alpaca_ws] DB connection closed")
            except Exception as exc:
                logger.warning("[alpaca_ws] DB close error: %s", exc)
            finally:
                self._db_con = None

    # ------------------------------------------------------------------
    # DB write
    # ------------------------------------------------------------------

    def _write_bars(self, bars: list[dict]) -> None:
        """
        Write Alpaca 1m bars to vanguard_bars_1m using the persistent connection.

        Fix (BUG 19): reuse self._db_con for all writes instead of opening a new
        sqlite3.connect() per call. The context-manager form of sqlite3.connect()
        only manages transactions — it does NOT close the connection, so every call
        leaked one file descriptor, driving open_fds from 60 → 194+ in minutes.

        Alpaca WS bar keys:
            S — symbol
            t — bar OPEN time (UTC ISO-8601)
            o, h, l, c — OHLC
            v — volume
            n — trade count (tick_volume proxy)
        Vanguard convention: store bar END time = t + 1 minute.
        """
        from datetime import timedelta as _td

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = []
        for b in bars:
            try:
                # t is bar open time — add 1 min for bar END (Vanguard convention)
                open_dt  = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                end_ts   = (open_dt + _td(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows.append((
                    b["S"].upper(), end_ts,
                    float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                    int(b.get("v", 0)), int(b.get("n", 0)),
                    now_iso,
                ))
                self.last_bar_ts = end_ts
            except Exception as exc:
                logger.warning("[alpaca_ws] Skipping bad bar %s: %s", b.get("S"), exc)

        if not rows:
            return

        try:
            con = self._get_db_con()
            con.executemany(
                """
                INSERT OR REPLACE INTO vanguard_bars_1m
                    (symbol, bar_ts_utc, open, high, low, close, volume,
                     tick_volume, asset_class, data_source, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'equity', 'alpaca_ws', ?)
                """,
                rows,
            )
            con.commit()
            self.bars_received += len(rows)
            self._bars_since_checkpoint += len(rows)
            logger.debug("[alpaca_ws] Wrote %d bars", len(rows))

            # Periodic WAL checkpoint to prevent unbounded WAL growth
            if self._bars_since_checkpoint >= self._checkpoint_interval:
                self._checkpoint_wal()
                self._bars_since_checkpoint = 0

        except Exception as exc:
            logger.error("[alpaca_ws] DB write failed: %s", exc)
            # Attempt to reset the connection on error so next write gets a fresh one
            if self._db_con is not None:
                try:
                    self._db_con.close()
                except Exception:
                    pass
                self._db_con = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "adapter":          "alpaca_ws",
            "symbols":          len(self.symbols),
            "bars_received":    self.bars_received,
            "last_bar":         self.last_bar_ts,
            "running":          self.running,
            "connect_attempts": self.connect_attempts,
            "last_error":       self.last_error,
        }


AlpacaRESTClient = AlpacaAdapter


if __name__ == "__main__":
    import asyncio

    db_path = str(Path(__file__).resolve().parents[2] / "data" / "vanguard_universe.db")
    symbols = get_active_equity_symbols(db_path)
    ws = AlpacaWSAdapter(symbols=symbols, db_path=db_path)
    print(f"Starting Alpaca WebSocket for {len(ws.symbols)} symbols...")
    asyncio.run(ws.start())
