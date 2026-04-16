#!/usr/bin/env python3
"""
ibkr_adapter.py — Universal IBKR data adapter for SignalStack.

Serves both:
- Vanguard intraday (1m/5m bars for equities + forex)
- Meridian / S1 daily (daily OHLCV for shared equity universe)

Location: ~/SS/Vanguard/vanguard/data_adapters/ibkr_adapter.py
"""
from __future__ import annotations

import asyncio

# Python 3.14 compatibility: ib_insync expects an event loop to exist.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from dotenv import load_dotenv
from ib_insync import IB, Contract, Crypto, Forex, Stock

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SS_ROOT = _REPO_ROOT.parent
_INTRADAY_DB = _REPO_ROOT / "data" / "ibkr_intraday.db"
_DAILY_DB = _SS_ROOT / "Meridian" / "data" / "ibkr_daily.db"
_VANGUARD_DB = _REPO_ROOT / "data" / "vanguard_universe.db"
_MERIDIAN_DB = _SS_ROOT / "Meridian" / "data" / "v2_universe.db"
_ADVANCE_FUC_DB = _SS_ROOT / "Advance" / "data_cache" / "universe_ohlcv.db"
_S8_FUC_DB = _SS_ROOT / "SignalStack8" / "data_cache" / "universe_ohlcv.db"
_ETF_LIST_PATH = _SS_ROOT / "Meridian" / "config" / "etf_tickers.json"

for env_path in (
    _SS_ROOT / ".env",
    _SS_ROOT / ".env.shared",
    _REPO_ROOT / ".env",
):
    if env_path.exists():
        load_dotenv(env_path, override=False)

IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "4001"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "10"))
IB_HISTORY_SLEEP = float(os.environ.get("IB_HISTORY_SLEEP", "0.35"))
IB_HISTORY_BATCH_SLEEP = float(os.environ.get("IB_HISTORY_BATCH_SLEEP", "2.0"))
IB_HISTORY_BATCH_SIZE = int(os.environ.get("IB_HISTORY_BATCH_SIZE", "8"))
# Historical reqHistoricalData is still one request per symbol; 200 is the
# practical default until this path is converted to a true streaming/scanner flow.
IB_INTRADAY_SYMBOL_LIMIT = int(os.environ.get("IB_INTRADAY_SYMBOL_LIMIT", "200"))

_FOREX_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "AUDJPY", "CHFJPY", "EURCAD",
]
_CRYPTO_SYMBOLS = ["BTC", "ETH", "LTC", "BCH"]
_VALID_EQUITY_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "SMART", "BATS", "IEX"}
_EXCLUDED_SUFFIXES = (".WS", ".WT", ".U", ".R", "WS", "WT", "U", "R")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_etf_tickers() -> set[str]:
    if not _ETF_LIST_PATH.exists():
        return set()
    try:
        data = json.loads(_ETF_LIST_PATH.read_text())
        return {str(item).upper() for item in data}
    except Exception as exc:
        logger.warning("Could not load ETF exclusion list from %s: %s", _ETF_LIST_PATH, exc)
        return set()


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace("/", "").strip()


def _read_table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _ib_datetime_to_utc(dt_value: Any) -> datetime:
    if isinstance(dt_value, datetime):
        if dt_value.tzinfo is None:
            return dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)
    text = str(dt_value).strip()
    if not text:
        raise ValueError("Empty IB date value")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(text, "%Y%m%d %H:%M:%S")
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _infer_asset_class(symbol: str) -> str:
    sym = _normalize_symbol(symbol)
    if sym in _FOREX_PAIRS:
        return "forex"
    if sym in _CRYPTO_SYMBOLS:
        return "crypto"
    return "equity"


def _parse_stream_bar(symbol: str, bar: Any, timeframe: str = "1m") -> dict[str, Any]:
    opened = _ib_datetime_to_utc(getattr(bar, "date", ""))
    ended = opened + timedelta(minutes=1)
    return {
        "timeframe": timeframe,
        "bar_ts_utc": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": _safe_float(getattr(bar, "open", 0.0)),
        "high": _safe_float(getattr(bar, "high", 0.0)),
        "low": _safe_float(getattr(bar, "low", 0.0)),
        "close": _safe_float(getattr(bar, "close", 0.0)),
        "volume": _safe_int(getattr(bar, "volume", 0)),
        "tick_volume": _safe_int(getattr(bar, "barCount", 0)),
        "bar_count": _safe_int(getattr(bar, "barCount", 0)),
        "asset_class": _infer_asset_class(symbol),
        "data_source": "ibkr",
    }


def _ensure_intraday_db(db_path: str | Path = _INTRADAY_DB) -> None:
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS ibkr_bars_1m (
                symbol TEXT NOT NULL,
                bar_ts_utc TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume INTEGER DEFAULT 0,
                tick_volume INTEGER DEFAULT 0,
                asset_class TEXT DEFAULT 'equity',
                data_source TEXT DEFAULT 'ibkr',
                PRIMARY KEY (symbol, bar_ts_utc)
            );

            CREATE TABLE IF NOT EXISTS ibkr_bars_5m (
                symbol TEXT NOT NULL,
                bar_ts_utc TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume INTEGER DEFAULT 0,
                tick_volume INTEGER DEFAULT 0,
                bar_count INTEGER DEFAULT 5,
                asset_class TEXT DEFAULT 'equity',
                data_source TEXT DEFAULT 'ibkr',
                PRIMARY KEY (symbol, bar_ts_utc)
            );

            CREATE TABLE IF NOT EXISTS ibkr_universe (
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                exchange TEXT,
                currency TEXT DEFAULT 'USD',
                con_id INTEGER,
                min_tick REAL,
                lot_size REAL,
                is_active INTEGER DEFAULT 1,
                last_seen TEXT,
                PRIMARY KEY (symbol, asset_class)
            );

            CREATE INDEX IF NOT EXISTS idx_ib1m_ts ON ibkr_bars_1m(bar_ts_utc);
            CREATE INDEX IF NOT EXISTS idx_ib5m_ts ON ibkr_bars_5m(bar_ts_utc);
            CREATE INDEX IF NOT EXISTS idx_ib1m_sym ON ibkr_bars_1m(symbol);
            """
        )
        con.commit()
    finally:
        con.close()


def _ensure_daily_db(db_path: str | Path = _DAILY_DB) -> None:
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS ibkr_daily_bars (
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume INTEGER DEFAULT 0,
                avg_volume_20d REAL,
                dollar_volume REAL,
                asset_class TEXT DEFAULT 'equity',
                data_source TEXT DEFAULT 'ibkr',
                PRIMARY KEY (symbol, date)
            );

            CREATE TABLE IF NOT EXISTS ibkr_daily_universe (
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                exchange TEXT,
                currency TEXT DEFAULT 'USD',
                con_id INTEGER,
                market_cap REAL,
                sector TEXT,
                industry TEXT,
                is_active INTEGER DEFAULT 1,
                last_seen TEXT,
                PRIMARY KEY (symbol)
            );

            CREATE INDEX IF NOT EXISTS idx_ibd_date ON ibkr_daily_bars(date);
            CREATE INDEX IF NOT EXISTS idx_ibd_sym ON ibkr_daily_bars(symbol);
            """
        )
        if _read_table_exists(con, "daily_bars"):
            try:
                con.execute("DROP VIEW daily_bars")
            except sqlite3.OperationalError:
                pass
        con.execute(
            """
            CREATE VIEW IF NOT EXISTS daily_bars AS
            SELECT
                symbol AS ticker,
                date,
                open,
                high,
                low,
                close,
                volume,
                data_source AS source
            FROM ibkr_daily_bars
            """
        )
        con.commit()
    finally:
        con.close()


class IBKRAdapter:
    def __init__(self, host: str = IB_HOST, port: int = IB_PORT, client_id: int = IB_CLIENT_ID):
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.ib = IB()
        self._stream_stop = threading.Event()
        self._etf_tickers = _load_etf_tickers()
        _ensure_intraday_db()
        _ensure_daily_db()

    def connect(self) -> bool:
        if self.ib.isConnected():
            return True

        # Check runtime config enabled flag before attempting any connection.
        # Prevents 5 successive connection attempts (30+ error lines) when
        # IBKR/TWS is not running.
        try:
            from vanguard.config.runtime_config import get_runtime_config
            ibkr_cfg = get_runtime_config().get("data_sources", {}).get("ibkr", {})
            if str(ibkr_cfg.get("enabled", "true")).strip().lower() in {"0", "false", "no", "off"}:
                logger.info("[IBKR] data_sources.ibkr.enabled=false — skipping connection")
                return False
        except Exception:
            pass  # if config unavailable, attempt connection normally

        candidate_ids = [self.client_id]
        candidate_ids.extend(
            cid
            for cid in range(self.client_id + 1, self.client_id + 6)
            if cid != 11
        )
        connected = False
        for cid in candidate_ids:
            try:
                self.ib.connect(self.host, self.port, clientId=cid, timeout=10)
                if self.ib.isConnected():
                    if cid != self.client_id:
                        logger.warning(
                            "IBKR connected with fallback client_id=%s (requested=%s)",
                            cid,
                            self.client_id,
                        )
                        self.client_id = cid
                    connected = True
                    break
            except Exception as exc:
                logger.warning(
                    "IBKR connect failed host=%s port=%s client_id=%s: %s",
                    self.host,
                    self.port,
                    cid,
                    exc,
                )
            try:
                if self.ib.isConnected():
                    self.ib.disconnect()
            except Exception:
                pass
        if not connected:
            logger.warning(
                "[IBKR] Not connected — all %d client ID(s) failed. "
                "Set data_sources.ibkr.enabled=false in runtime config to suppress retries.",
                len(candidate_ids),
            )
        return connected

    def disconnect(self):
        try:
            self._stream_stop.set()
            if self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            logger.exception("IBKR disconnect failed")

    def is_connected(self) -> bool:
        return bool(self.ib.isConnected())

    def _stock_contract(self, symbol: str) -> Contract:
        return Stock(_normalize_symbol(symbol), "SMART", "USD")

    def _forex_contract(self, symbol: str) -> Contract:
        return Forex(_normalize_symbol(symbol))

    def _crypto_contract(self, symbol: str) -> Contract:
        return Crypto(_normalize_symbol(symbol), "PAXOS", "USD")

    def _contract_for(self, symbol: str, asset_class: str) -> Contract:
        asset_class = (asset_class or "equity").lower()
        if asset_class == "forex":
            return self._forex_contract(symbol)
        if asset_class == "crypto":
            return self._crypto_contract(symbol)
        return self._stock_contract(symbol)

    def _what_to_show(self, asset_class: str) -> str:
        asset_class = (asset_class or "equity").lower()
        if asset_class == "forex":
            return "MIDPOINT"
        return "TRADES"

    def _timeframe_spec(self, timeframe: str, limit: int) -> tuple[str, str, int]:
        timeframe = str(timeframe or "5m").lower()
        if timeframe in {"1m", "1min", "1 minute"}:
            bar_size = "1 min"
            minutes = max(limit, 10)
            duration = "1 D" if minutes <= 390 else f"{max(2, minutes // 390 + 1)} D"
            return bar_size, duration, 1
        if timeframe in {"5m", "5min", "5 minute", "5 mins"}:
            bar_size = "5 mins"
            minutes = max(limit * 5, 25)
            duration = "2 D" if minutes <= 780 else f"{max(3, minutes // 390 + 1)} D"
            return bar_size, duration, 5
        if timeframe in {"1d", "1day", "day", "daily"}:
            return "1 day", f"{max(limit + 5, 10)} D", 1440
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    def _request_contract_bars(
        self,
        contract: Contract,
        *,
        timeframe: str,
        limit: int,
        asset_class: str,
        use_rth: bool | None = None,
    ) -> list[dict[str, Any]]:
        if not self.is_connected() and not self.connect():
            return []
        bar_size, duration, bar_minutes = self._timeframe_spec(timeframe, limit)
        if use_rth is None:
            use_rth = asset_class.lower() == "equity"
        # === EXTENDED HOURS FOR EQUITY (additive override) ===
        # False = pre-market + after-hours + overnight data.
        # Old logic is retained above as fallback/reference.
        if asset_class.lower() == "equity":
            use_rth = False
        try:
            qualified = self.ib.qualifyContracts(contract)
            if qualified:
                contract = qualified[0]
        except Exception:
            logger.debug("IBKR qualifyContracts failed for %s", contract, exc_info=True)

        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=self._what_to_show(asset_class),
            useRTH=bool(use_rth),
            formatDate=2,
            keepUpToDate=False,
        )
        parsed: list[dict[str, Any]] = []
        for bar in list(bars)[-limit:]:
            opened = _ib_datetime_to_utc(bar.date)
            ended = opened + timedelta(minutes=bar_minutes)
            parsed.append(
                {
                    "timeframe": timeframe,
                    "bar_ts_utc": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": _safe_float(getattr(bar, "open", 0.0)),
                    "high": _safe_float(getattr(bar, "high", 0.0)),
                    "low": _safe_float(getattr(bar, "low", 0.0)),
                    "close": _safe_float(getattr(bar, "close", 0.0)),
                    "volume": _safe_int(getattr(bar, "volume", 0)),
                    "tick_volume": _safe_int(getattr(bar, "barCount", 0)),
                    "bar_count": _safe_int(getattr(bar, "barCount", 0)),
                    "asset_class": asset_class,
                    "data_source": "ibkr",
                }
            )
        return parsed

    def _seed_universe_from_local_dbs(self) -> list[str]:
        symbols: set[str] = set()
        for db_path, query in (
            (_MERIDIAN_DB, "SELECT DISTINCT ticker FROM daily_bars"),
            (_ADVANCE_FUC_DB, "SELECT DISTINCT ticker FROM daily_bars"),
            (_S8_FUC_DB, "SELECT DISTINCT ticker FROM daily_bars"),
        ):
            if not db_path.exists():
                continue
            try:
                con = sqlite3.connect(str(db_path), timeout=10)
                rows = con.execute(query).fetchall()
                con.close()
                symbols |= {_normalize_symbol(row[0]) for row in rows if row and row[0]}
            except Exception:
                logger.debug("Could not seed universe from %s", db_path, exc_info=True)
        return sorted(s for s in symbols if s and s.isalpha())

    def get_full_equity_universe(self) -> list[dict[str, Any]]:
        cached_rows: list[dict[str, Any]] = []
        if _DAILY_DB.exists():
            con = sqlite3.connect(str(_DAILY_DB), timeout=10)
            con.row_factory = sqlite3.Row
            try:
                if _read_table_exists(con, "ibkr_daily_universe"):
                    cached_rows = [dict(r) for r in con.execute(
                        "SELECT * FROM ibkr_daily_universe WHERE is_active = 1 ORDER BY symbol"
                    ).fetchall()]
            finally:
                con.close()
        if cached_rows:
            return cached_rows

        now = _utc_now_iso()
        rows = [
            {
                "symbol": symbol,
                "asset_class": "equity",
                "exchange": "SMART",
                "currency": "USD",
                "con_id": None,
                "market_cap": None,
                "sector": None,
                "industry": None,
                "is_active": 1,
                "last_seen": now,
            }
            for symbol in self._seed_universe_from_local_dbs()
        ]
        if rows:
            con = sqlite3.connect(str(_DAILY_DB), timeout=30)
            try:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO ibkr_daily_universe
                        (symbol, asset_class, exchange, currency, con_id, market_cap,
                         sector, industry, is_active, last_seen)
                    VALUES
                        (:symbol, :asset_class, :exchange, :currency, :con_id, :market_cap,
                         :sector, :industry, :is_active, :last_seen)
                    """,
                    rows,
                )
                con.commit()
            finally:
                con.close()
        return rows

    def get_intraday_equity_universe(self) -> list[str]:
        universe = self.get_full_equity_universe()
        symbols = [row["symbol"] for row in universe]
        if not symbols:
            return []
        daily_stats: dict[str, tuple[float, float]] = {}
        if _DAILY_DB.exists():
            con = sqlite3.connect(str(_DAILY_DB), timeout=10)
            try:
                rows = con.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            symbol,
                            date,
                            close,
                            volume,
                            ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                        FROM ibkr_daily_bars
                    )
                    SELECT
                        symbol,
                        MAX(CASE WHEN rn = 1 THEN close END) AS last_close,
                        AVG(CASE WHEN rn <= 20 THEN close * COALESCE(volume, 0) END) AS avg_dollar_vol_20d
                    FROM ranked
                    GROUP BY symbol
                    """
                ).fetchall()
            finally:
                con.close()
            daily_stats = {
                _normalize_symbol(row[0]): (_safe_float(row[1]), _safe_float(row[2]))
                for row in rows
            }
        ranked: list[tuple[float, str]] = []
        for symbol in symbols:
            sym = _normalize_symbol(symbol)
            if sym in self._etf_tickers:
                continue
            if any(sym.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES):
                continue
            last_close, avg_dollar_vol = daily_stats.get(sym, (0.0, 0.0))
            if last_close and last_close < 5.0:
                continue
            if avg_dollar_vol and avg_dollar_vol < 5_000_000:
                continue
            ranked.append((avg_dollar_vol, sym))
        filtered = [sym for _score, sym in sorted(ranked, key=lambda item: (-item[0], item[1]))]
        if IB_INTRADAY_SYMBOL_LIMIT > 0:
            return filtered[:IB_INTRADAY_SYMBOL_LIMIT]
        return filtered

    def get_forex_pairs(self) -> list[str]:
        return list(_FOREX_PAIRS)

    def get_crypto_symbols(self) -> list[str]:
        return list(_CRYPTO_SYMBOLS)

    def get_bars(
        self,
        symbols: list[str],
        timeframe: str = "5m",
        limit: int = 10,
        asset_class: str = "equity",
    ) -> dict[str, list[dict[str, Any]]]:
        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        symbols = [_normalize_symbol(symbol) for symbol in symbols if symbol]
        if not symbols:
            return bars_by_symbol
        for idx, symbol in enumerate(symbols):
            contract = self._contract_for(symbol, asset_class)
            try:
                bars_by_symbol[symbol] = self._request_contract_bars(
                    contract,
                    timeframe=timeframe,
                    limit=limit,
                    asset_class=asset_class,
                )
            except Exception as exc:
                logger.warning("IBKR bars failed for %s (%s): %s", symbol, asset_class, exc)
                bars_by_symbol[symbol] = []
            time.sleep(IB_HISTORY_SLEEP)
            if (idx + 1) % IB_HISTORY_BATCH_SIZE == 0 and idx + 1 < len(symbols):
                time.sleep(IB_HISTORY_BATCH_SLEEP)
        return bars_by_symbol

    def stream_bars(self, symbols: list[str], callback: Callable[[str, dict[str, Any]], None], asset_class: str = "equity"):
        self._stream_stop.clear()

        def _loop() -> None:
            while not self._stream_stop.is_set():
                bars_map = self.get_bars(symbols, timeframe="1m", limit=1, asset_class=asset_class)
                for symbol, bars in bars_map.items():
                    if not bars:
                        continue
                    callback(symbol, bars[-1])
                self._stream_stop.wait(timeout=60)

        thread = threading.Thread(target=_loop, name=f"ibkr-stream-{asset_class}", daemon=True)
        thread.start()
        return thread

    def get_daily_bars(self, symbols: list[str], days: int = 5) -> dict[str, list[dict[str, Any]]]:
        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        symbols = [_normalize_symbol(symbol) for symbol in symbols if symbol]
        if not symbols:
            return bars_by_symbol
        for idx, symbol in enumerate(symbols):
            contract = self._stock_contract(symbol)
            try:
                raw_bars = self._request_contract_bars(
                    contract,
                    timeframe="1d",
                    limit=max(int(days), 1),
                    asset_class="equity",
                    use_rth=True,
                )
                bars_by_symbol[symbol] = [
                    {
                        "date": bar["bar_ts_utc"][:10],
                        "open": bar["open"],
                        "high": bar["high"],
                        "low": bar["low"],
                        "close": bar["close"],
                        "volume": bar["volume"],
                        "asset_class": "equity",
                        "data_source": "ibkr",
                    }
                    for bar in raw_bars
                ]
            except Exception as exc:
                logger.warning("IBKR daily bars failed for %s: %s", symbol, exc)
                bars_by_symbol[symbol] = []
            time.sleep(IB_HISTORY_SLEEP)
            if (idx + 1) % IB_HISTORY_BATCH_SIZE == 0 and idx + 1 < len(symbols):
                time.sleep(IB_HISTORY_BATCH_SLEEP)
        return bars_by_symbol

    def write_intraday_bars(self, bars: dict[str, list[dict[str, Any]]], db_path: str | Path = _INTRADAY_DB) -> int:
        _ensure_intraday_db(db_path)
        rows_1m: list[tuple[Any, ...]] = []
        rows_5m: list[tuple[Any, ...]] = []
        for symbol, entries in bars.items():
            for bar in entries:
                row = (
                    _normalize_symbol(symbol),
                    bar["bar_ts_utc"],
                    _safe_float(bar.get("open")),
                    _safe_float(bar.get("high")),
                    _safe_float(bar.get("low")),
                    _safe_float(bar.get("close")),
                    _safe_int(bar.get("volume")),
                    _safe_int(bar.get("tick_volume")),
                    str(bar.get("asset_class") or "equity"),
                    str(bar.get("data_source") or "ibkr"),
                )
                if "1m" in str(bar.get("timeframe", "")):
                    rows_1m.append(row)
                else:
                    rows_5m.append(row + (_safe_int(bar.get("bar_count"), 5),))
        con = sqlite3.connect(str(db_path), timeout=30)
        try:
            if rows_1m:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO ibkr_bars_1m
                        (symbol, bar_ts_utc, open, high, low, close, volume,
                         tick_volume, asset_class, data_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_1m,
                )
            if rows_5m:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO ibkr_bars_5m
                        (symbol, bar_ts_utc, open, high, low, close, volume,
                         tick_volume, bar_count, asset_class, data_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_5m,
                )
            con.commit()
        finally:
            con.close()
        return len(rows_1m) + len(rows_5m)

    def write_daily_bars(self, bars: dict[str, list[dict[str, Any]]], db_path: str | Path = _DAILY_DB) -> int:
        _ensure_daily_db(db_path)
        prepared: list[tuple[Any, ...]] = []
        for symbol, entries in bars.items():
            entries = sorted(entries, key=lambda item: item["date"])
            recent_volumes: list[float] = []
            for bar in entries:
                volume = _safe_float(bar.get("volume"))
                recent_volumes.append(volume)
                avg_vol_20d = sum(recent_volumes[-20:]) / max(len(recent_volumes[-20:]), 1)
                close = _safe_float(bar.get("close"))
                prepared.append(
                    (
                        _normalize_symbol(symbol),
                        str(bar["date"]),
                        _safe_float(bar.get("open")),
                        _safe_float(bar.get("high")),
                        _safe_float(bar.get("low")),
                        close,
                        _safe_int(volume),
                        avg_vol_20d,
                        close * volume,
                        str(bar.get("asset_class") or "equity"),
                        str(bar.get("data_source") or "ibkr"),
                    )
                )
        con = sqlite3.connect(str(db_path), timeout=30)
        try:
            con.executemany(
                """
                INSERT OR REPLACE INTO ibkr_daily_bars
                    (symbol, date, open, high, low, close, volume,
                     avg_volume_20d, dollar_volume, asset_class, data_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
            con.commit()
        finally:
            con.close()
        return len(prepared)

    def register_universe(self, db_path: str | Path = _VANGUARD_DB) -> int:
        rows: list[dict[str, Any]] = []
        now = _utc_now_iso()
        for symbol in self.get_intraday_equity_universe():
            rows.append(
                {
                    "symbol": symbol,
                    "asset_class": "equity",
                    "data_source": "ibkr",
                    "universe": "ibkr_equities",
                    "exchange": "SMART",
                    "session_start": "09:30",
                    "session_end": "16:00",
                    "is_active": 1,
                    "added_at": now,
                    "last_seen_at": now,
                }
            )
        for symbol in self.get_forex_pairs():
            rows.append(
                {
                    "symbol": symbol,
                    "asset_class": "forex",
                    "data_source": "ibkr",
                    "universe": "ibkr_forex",
                    "exchange": "IDEALPRO",
                    "session_start": "00:00",
                    "session_end": "23:59",
                    "is_active": 1,
                    "added_at": now,
                    "last_seen_at": now,
                }
            )
        if not rows:
            return 0
        con = sqlite3.connect(str(db_path), timeout=30)
        try:
            con.executemany(
                """
                INSERT OR REPLACE INTO vanguard_universe_members
                    (symbol, asset_class, data_source, universe, exchange,
                     tick_size, tick_value, point_value, margin,
                     session_start, session_end, session_tz, is_active,
                     added_at, last_seen_at)
                VALUES
                    (:symbol, :asset_class, :data_source, :universe, :exchange,
                     NULL, NULL, NULL, NULL,
                     :session_start, :session_end, 'America/New_York', :is_active,
                     :added_at, :last_seen_at)
                """,
                rows,
            )
            con.commit()
        finally:
            con.close()
        return len(rows)


class IBKRStreamingAdapter(IBKRAdapter):
    """
    Streaming IBKR adapter for intraday bars.

    Contracts are qualified and subscribed once at startup with
    keepUpToDate=True. Per-cycle V1 reads recent rows from SQLite instead
    of issuing one historical request per symbol.
    """

    def __init__(
        self,
        host: str = IB_HOST,
        port: int = IB_PORT,
        client_id: int = IB_CLIENT_ID,
    ):
        super().__init__(host=host, port=port, client_id=client_id)
        self.subscriptions: dict[str, Any] = {}
        self.valid_contracts: dict[tuple[str, str], Contract] = {}
        self.bad_symbols: set[tuple[str, str]] = set()
        self._contract_asset_class: dict[tuple[str, str], str] = {}
        self._pump_thread: threading.Thread | None = None
        self._stream_db_path = str(_INTRADAY_DB)

    def _ensure_pump_thread(self) -> None:
        # ib_insync's wait helpers are safest from the owning thread in this
        # process model, so get_latest_bars() does a short waitOnUpdate() call
        # before reading SQLite instead of running a background asyncio pump.
        return

    def disconnect(self):
        self._stream_stop.set()
        try:
            for subscription in list(self.subscriptions.values()):
                try:
                    self.ib.cancelHistoricalData(subscription)
                except Exception:
                    logger.debug("Could not cancel IBKR subscription", exc_info=True)
            self.subscriptions.clear()
        finally:
            super().disconnect()
        if self._pump_thread and self._pump_thread.is_alive():
            self._pump_thread.join(timeout=3)

    def qualify_contracts(self, symbols: list[str], asset_class: str = "equity") -> int:
        asset_class = str(asset_class or "equity").lower()
        symbols = [_normalize_symbol(symbol) for symbol in symbols if symbol]
        if not symbols:
            return 0
        if not self.is_connected() and not self.connect():
            return 0

        contracts: list[Contract] = []
        contract_keys: list[tuple[str, str]] = []
        for symbol in symbols:
            key = (asset_class, symbol)
            if key in self.valid_contracts or key in self.bad_symbols:
                continue
            contracts.append(self._contract_for(symbol, asset_class))
            contract_keys.append(key)
            self._contract_asset_class[key] = asset_class

        if not contracts:
            return 0

        try:
            qualified = self.ib.qualifyContracts(*contracts)
        except Exception as exc:
            logger.warning("IBKR bulk qualify failed for %s symbols (%s): %s", len(contracts), asset_class, exc)
            qualified = []

        qualified_map: dict[str, Contract] = {}
        for item in qualified:
            if asset_class == "forex":
                q_symbol = _normalize_symbol(
                    f"{getattr(item, 'symbol', '')}{getattr(item, 'currency', '')}"
                )
            else:
                q_symbol = _normalize_symbol(getattr(item, "symbol", ""))
            if q_symbol and _safe_int(getattr(item, "conId", 0)) > 0:
                qualified_map[q_symbol] = item

        valid_count = 0
        for key in contract_keys:
            match = qualified_map.get(key[1])
            if match is not None:
                self.valid_contracts[key] = match
                valid_count += 1
            else:
                self.bad_symbols.add(key)

        logger.info(
            "[ibkr_stream] qualified %d/%d %s contracts (valid=%d bad=%d)",
            valid_count,
            len(contract_keys),
            asset_class,
            len(self.valid_contracts),
            len(self.bad_symbols),
        )
        return valid_count

    def _seed_subscription_bars(
        self,
        symbol: str,
        bars: Any,
        db_path: str | Path,
    ) -> None:
        if not bars:
            return
        payload = {
            symbol: [
                _parse_stream_bar(symbol, bar, timeframe="1m")
                for bar in list(bars)[-300:]
            ]
        }
        self.write_intraday_bars(payload, db_path)

    def _on_stream_bar(
        self,
        symbol: str,
        bar_list: Any,
        has_new_bar: bool,
        db_path: str | Path,
    ) -> None:
        if not has_new_bar or not bar_list:
            return
        bar = _parse_stream_bar(symbol, bar_list[-1], timeframe="1m")
        self.write_intraday_bars({symbol: [bar]}, db_path)

    def subscribe_streaming(
        self,
        symbols: list[str] | None = None,
        *,
        db_path: str | Path = _INTRADAY_DB,
        asset_class: str = "forex",
        use_rth: bool = False,
    ) -> int:
        if not self.is_connected() and not self.connect():
            return 0
        self._stream_db_path = str(db_path)
        _ensure_intraday_db(db_path)

        if symbols is not None:
            self.qualify_contracts(symbols, asset_class=asset_class)

        # === EXTENDED HOURS FOR EQUITY (additive override) ===
        # False = pre-market + after-hours + overnight data.
        if str(asset_class or "").lower() == "equity":
            use_rth = False

        subscribed = 0
        for (contract_asset_class, symbol), contract in list(self.valid_contracts.items()):
            if contract_asset_class != asset_class:
                continue
            if symbol in self.subscriptions:
                continue
            try:
                bars = self.ib.reqHistoricalData(
                    contract,
                    endDateTime="",
                    # Seed at least 50 synthetic 5m buckets so V2 clears warmup immediately.
                    durationStr="18000 S",
                    barSizeSetting="1 min",
                    whatToShow=self._what_to_show(contract_asset_class),
                    useRTH=bool(use_rth),
                    keepUpToDate=True,
                    formatDate=2,
                )
                self._seed_subscription_bars(symbol, bars, db_path)
                bars.updateEvent += (
                    lambda bar_list, has_new_bar, sym=symbol, path=str(db_path):
                    self._on_stream_bar(sym, bar_list, has_new_bar, path)
                )
                self.subscriptions[symbol] = bars
                subscribed += 1
            except Exception as exc:
                self.bad_symbols.add((contract_asset_class, symbol))
                logger.warning("IBKR stream subscribe failed for %s (%s): %s", symbol, contract_asset_class, exc)

        if subscribed:
            self._ensure_pump_thread()
        logger.info("[ibkr_stream] streaming %d %s symbols", subscribed, asset_class)
        return subscribed

    def get_latest_bars(
        self,
        db_path: str | Path = _INTRADAY_DB,
        *,
        minutes: int = 10,
        asset_class: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if self.is_connected() and self.subscriptions:
            try:
                self.ib.waitOnUpdate(timeout=0.2)
            except Exception:
                logger.debug("IBKR waitOnUpdate before DB read failed", exc_info=True)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(int(minutes), 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = """
            SELECT symbol, bar_ts_utc, open, high, low, close, volume,
                   tick_volume, asset_class, data_source
            FROM ibkr_bars_1m
            WHERE bar_ts_utc >= ?
        """
        params: list[Any] = [cutoff]
        if asset_class:
            query += " AND asset_class = ?"
            params.append(str(asset_class).lower())
        query += " ORDER BY symbol, bar_ts_utc"

        con = sqlite3.connect(str(db_path), timeout=10)
        try:
            frame = pd.read_sql_query(query, con, params=params)
        finally:
            con.close()

        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        if frame.empty:
            return bars_by_symbol
        for symbol, grp in frame.groupby("symbol"):
            bars_by_symbol[_normalize_symbol(symbol)] = [
                {
                    "timeframe": "1m",
                    "bar_ts_utc": str(row.bar_ts_utc),
                    "open": _safe_float(row.open),
                    "high": _safe_float(row.high),
                    "low": _safe_float(row.low),
                    "close": _safe_float(row.close),
                    "volume": _safe_int(row.volume),
                    "tick_volume": _safe_int(row.tick_volume),
                    "asset_class": str(row.asset_class or "equity"),
                    "data_source": str(row.data_source or "ibkr"),
                }
                for row in grp.itertuples(index=False)
            ]
        return bars_by_symbol


if __name__ == "__main__":
    adapter = IBKRStreamingAdapter()
    ok = adapter.connect()
    print(f"Connected: {ok}")
    if ok:
        adapter.qualify_contracts(["EURUSD", "GBPUSD"], asset_class="forex")
        adapter.subscribe_streaming(asset_class="forex")
        time.sleep(2)
        fx = adapter.get_latest_bars(minutes=120, asset_class="forex")
        print(f"Forex symbols: {list(fx)}")
    adapter.disconnect()
