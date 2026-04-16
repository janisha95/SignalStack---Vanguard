#!/usr/bin/env python3
"""
MT5 DWX Adapter — Local bridge to MetaTrader 5 via DWX Connect.

Replaces TwelveData as the primary data source for GFT forex/crypto.
Also exposes order execution methods for V7 and lifecycle daemon.

Data flow:
  DWX EA (MT5) → JSON files → dwx_client → this adapter → vanguard_bars_1m DB

Config in vanguard_runtime.json:
  "data_sources": {
    "mt5_local": {
      "enabled": true,
      "dwx_files_path": "<path to MQL5/Files>",
      "symbol_suffix": ".x",
      "primary_for": ["forex", "crypto", "index", "commodity", "equity"]
    }
  }
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from os.path import exists
from pathlib import Path
from time import sleep
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.helpers.clock import now_utc, iso_utc

logger = logging.getLogger("mt5_dwx_adapter")

# Batch size for historic data requests (to avoid overloading DWX file bridge)
_BATCH_SIZE = 5          # Smaller batches so per-symbol timeout fires sooner
_BATCH_SLEEP_S = 0.5

# Timeout waiting for DWX async responses
_CONNECT_TIMEOUT_S = 5.0
_HIST_PER_SYMBOL_S = 15.0   # Raised from 8s → 15s per symbol
_POLL_TOTAL_TIMEOUT_S = 60.0  # Hard abort if entire poll exceeds this
_ORDER_TIMEOUT_S = 5.0

# Bars to request per symbol per poll
_BARS_PER_POLL = 20
_SERVER_TZ_MAX_OFFSET_HOURS = 12


class MT5DWXAdapter:
    """
    Wraps dwx_client to provide a Vanguard-compatible data + execution interface.

    The dwx_client uses background threads that poll JSON files written by
    DWX_Server_MT5 EA every 25ms.  All reads from dwx_client attributes
    (open_orders, account_info, bar_data, historic_data, market_data) are
    thread-safe because DWX assigns them atomically.
    """

    def __init__(
        self,
        dwx_files_path: str,
        db_path: str,
        symbol_suffix: str = ".x",
        symbol_suffix_map: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            dwx_files_path: Path to MQL5/Files directory.  The dwx_client
                appends /DWX/ internally, so pass the parent.
            db_path: Absolute path to vanguard_universe.db.
            symbol_suffix: Broker suffix appended to every symbol.  GFT uses ".x".
        """
        self.dwx_files_path = str(dwx_files_path)
        self.db_path = str(db_path)
        self.symbol_suffix = symbol_suffix
        self.symbol_suffix_map = {
            self._normalize_symbol(symbol): str(suffix or "")
            for symbol, suffix in (symbol_suffix_map or {}).items()
        }
        self._client: Any = None
        self._connected = False
        self._last_poll_details: dict[str, Any] = {
            "requested_symbols": [],
            "successful_symbols": [],
            "failed_symbols": [],
            "timed_out_symbols": [],
            "written_rows": 0,
        }
        self._server_tz_offset_hours: int | None = None

        # Cache: symbol → asset_class (populated lazily from DB)
        self._asset_class_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Initialise dwx_client and wait up to 5 s for account_info to arrive.

        Returns True if account_info contains a 'balance' key.
        Returns False gracefully if MT5 / DWX files are not present.
        """
        if self._client is not None and self._connected and getattr(self._client, "ACTIVE", False):
            return True

        if not exists(self.dwx_files_path):
            logger.warning("DWX files path does not exist: %s", self.dwx_files_path)
            return False

        dwx_dir = Path(self.dwx_files_path) / "DWX"
        if not dwx_dir.exists():
            logger.warning("DWX sub-directory missing: %s", dwx_dir)
            return False

        try:
            from vanguard.data_adapters.dwx.dwx_client import dwx_client  # noqa: PLC0415

            self._client = dwx_client(
                metatrader_dir_path=self.dwx_files_path,
                sleep_delay=0.01,
                max_retry_command_seconds=5,
                load_orders_from_file=True,
                verbose=False,
            )
        except SystemExit:
            logger.error("dwx_client called exit() — MT5 directory invalid")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to initialise dwx_client: %s", exc)
            return False

        # Wait for account_info to be populated by the background thread.
        deadline = now_utc() + timedelta(seconds=_CONNECT_TIMEOUT_S)
        while now_utc() < deadline:
            if self._client.account_info.get("balance") is not None:
                self._connected = True
                bal = self._client.account_info.get("balance", "?")
                eq = self._client.account_info.get("equity", "?")
                logger.info("DWX connected — balance=%.2f equity=%.2f", bal, eq)
                return True
            sleep(0.25)

        # May still work even without account_info (EA not running or no orders file)
        logger.warning(
            "DWX connected but account_info not populated within %.0fs "
            "(MT5 EA may not be running). Proceeding anyway.",
            _CONNECT_TIMEOUT_S,
        )
        self._connected = True
        return True

    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                if hasattr(self._client, "close"):
                    self._client.close()
                else:
                    self._client.ACTIVE = False
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._connected = False

    # ------------------------------------------------------------------
    # Data: bars
    # ------------------------------------------------------------------

    def poll(self, symbol_filter: list[str] | None = None) -> int:
        """
        Request the last N M1 bars for each symbol and write them to DB.

        Processes symbols in batches.  A hard 60-second total timeout prevents
        the poll from blocking the orchestrator cycle when the EA is slow or down.

        Returns total bar rows written.
        """
        if not self.is_connected():
            logger.warning("poll() called but adapter not connected")
            return 0

        symbols = [str(s) for s in (symbol_filter or [])]
        if not symbols:
            logger.warning("poll() called with empty symbol_filter — skipping")
            return 0

        total_written = 0
        now_ts = int(now_utc().timestamp())
        start_ts = now_ts - 60 * _BARS_PER_POLL * 2  # 2× buffer for gaps
        poll_deadline = now_utc() + timedelta(seconds=_POLL_TOTAL_TIMEOUT_S)

        logger.info("[DWX] Starting poll: %d symbols (total timeout %.0fs)", len(symbols), _POLL_TOTAL_TIMEOUT_S)
        details = {
            "requested_symbols": list(symbols),
            "successful_symbols": [],
            "failed_symbols": [],
            "timed_out_symbols": [],
            "written_rows": 0,
        }

        for batch_start in range(0, len(symbols), _BATCH_SIZE):
            if now_utc() >= poll_deadline:
                remaining = symbols[batch_start:]
                logger.warning(
                    "[DWX] Poll total timeout (%.0fs) reached — skipping %d symbols: %s",
                    _POLL_TOTAL_TIMEOUT_S, len(remaining), remaining,
                )
                details["timed_out_symbols"].extend(remaining)
                break

            batch = symbols[batch_start : batch_start + _BATCH_SIZE]
            batch_written, batch_details = self._poll_batch(batch, start_ts, now_ts, poll_deadline)
            total_written += batch_written
            details["successful_symbols"].extend(batch_details["successful_symbols"])
            details["failed_symbols"].extend(batch_details["failed_symbols"])
            details["timed_out_symbols"].extend(batch_details["timed_out_symbols"])
            if batch_start + _BATCH_SIZE < len(symbols):
                sleep(_BATCH_SLEEP_S)

        details["written_rows"] = total_written
        self._last_poll_details = details
        logger.info("[DWX] Poll complete: %d bars written from %d symbols", total_written, len(symbols))
        return total_written

    def _poll_batch(
        self,
        symbols: list[str],
        start_ts: int,
        end_ts: int,
        poll_deadline: "datetime | None" = None,
    ) -> tuple[int, dict[str, list[str]]]:
        """
        Request historic data for a batch of symbols.

        Per-symbol timeout: _HIST_PER_SYMBOL_S (15s).
        Respects the overall poll_deadline to abort early if the total budget is spent.
        Timed-out symbols are logged at WARNING and skipped — partial results returned.
        """
        import time as _time

        written = 0
        details = {
            "successful_symbols": [],
            "failed_symbols": [],
            "timed_out_symbols": [],
        }

        for sym in symbols:
            if poll_deadline is not None and now_utc() >= poll_deadline:
                logger.warning("[DWX] Skipping %s — overall poll deadline reached", sym)
                details["timed_out_symbols"].append(sym)
                continue

            canonical_sym = self._normalize_symbol(sym)
            dwx_sym = self._to_dwx_symbol(sym)
            key = f"{dwx_sym}_M1"

            # Clear stale entry so we can detect the fresh response.
            self._client.historic_data.pop(key, None)

            logger.info("[DWX] Requesting %s M1 bars...", sym)
            t0 = _time.monotonic()
            try:
                self._client.get_historic_data(
                    symbol=dwx_sym,
                    time_frame="M1",
                    start=start_ts,
                    end=end_ts,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DWX] Request failed for %s (%s) — skipping", sym, exc)
                details["failed_symbols"].append(canonical_sym)
                continue

            # Poll for this symbol's response individually.
            sym_deadline = now_utc() + timedelta(seconds=_HIST_PER_SYMBOL_S)
            if poll_deadline is not None:
                # Don't wait past the overall deadline either.
                sym_deadline = min(sym_deadline, poll_deadline)

            got_data = False
            while now_utc() < sym_deadline:
                sleep(0.25)
                if key in self._client.historic_data:
                    bars_raw = self._client.historic_data[key]
                    elapsed = _time.monotonic() - t0
                    rows = self._parse_and_write_bars(canonical_sym, bars_raw)
                    logger.info(
                        "[DWX] Got %d bars for %s in %.1fs (%d rows written)",
                        len(bars_raw) if isinstance(bars_raw, dict) else 0,
                        sym, elapsed, rows,
                    )
                    written += rows
                    if rows > 0:
                        details["successful_symbols"].append(canonical_sym)
                    else:
                        details["failed_symbols"].append(canonical_sym)
                    got_data = True
                    break

            if not got_data:
                elapsed = _time.monotonic() - t0
                logger.warning(
                    "[DWX] Timeout waiting for %s after %.1fs — skipping",
                    sym, elapsed,
                )
                details["timed_out_symbols"].append(canonical_sym)

        return written, details

    def subscribe_bars(self, symbols: list[str], timeframe: str = "M1") -> None:
        """
        Subscribe to streaming bar-close events.

        After subscribing, self._client.bar_data is updated automatically.
        Key format: "EURUSD.x_M1".
        """
        if not self.is_connected():
            return
        pairs = [[self._to_dwx_symbol(s), timeframe] for s in symbols]
        self._client.subscribe_symbols_bar_data(pairs)
        logger.info("Subscribed bar data for %d symbols (%s)", len(pairs), timeframe)

    def get_latest_bar_data(self) -> dict[str, dict]:
        """
        Read the latest closed bar for each subscribed symbol from bar_data.

        Returns: {'EURUSD': {'time': ..., 'open': ..., ...}, ...}
        """
        if not self.is_connected():
            return {}
        result: dict[str, dict] = {}
        for key, bar in self._client.bar_data.items():
            # key format: "EURUSD.x_M1"
            parts = key.split("_", 1)
            sym = self._from_dwx_symbol(parts[0])
            result[sym] = dict(bar)
        return result

    # ------------------------------------------------------------------
    # Data: live quotes
    # ------------------------------------------------------------------

    def subscribe_ticks(self, symbols: list[str]) -> None:
        """Subscribe to tick data (bid/ask).  Updates self._client.market_data."""
        if not self.is_connected():
            return
        dwx_syms = [self._to_dwx_symbol(s) for s in symbols]
        self._client.subscribe_symbols(dwx_syms)

    def get_live_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """
        Get current bid/ask for each symbol.

        Returns: {'EURUSD': {'bid': 1.1550, 'ask': 1.1551}, ...}
        """
        if not self.is_connected():
            return {}
        result: dict[str, dict] = {}
        for sym in symbols:
            dwx_sym = self._to_dwx_symbol(sym)
            data = self._client.market_data.get(dwx_sym) or self._client.market_data.get(sym)
            if data:
                result[sym] = {"bid": float(data.get("bid", 0)), "ask": float(data.get("ask", 0))}
        return result

    # ------------------------------------------------------------------
    # Account / positions
    # ------------------------------------------------------------------

    def get_account_info(self) -> dict:
        """
        Returns: {'balance': 9918.30, 'equity': 9926.85, 'free_margin': ..., 'leverage': 100}
        """
        if not self.is_connected():
            return {}
        info = dict(self._client.account_info)
        # Normalise key names (DWX uses lowercase)
        return {
            "balance": float(info.get("balance") or 0),
            "equity": float(info.get("equity") or 0),
            "free_margin": float(info.get("free_margin") or info.get("freeMargin") or 0),
            "leverage": int(info.get("leverage") or 0),
        }

    def get_open_positions(self) -> dict[str, dict]:
        """
        Returns current open orders with normalised field names.

        Returns: {
            '90248521': {
                'symbol': 'USDCHF',
                'lots': 0.22,
                'type': 'buy',
                'open_price': 0.7974,
                'sl': 0.79546,
                'tp': 0.80000,
                'pnl': 8.55,
                'swap': 0.0,
                'ticket': '90248521',
                'open_time': datetime | None,
            }
        }
        """
        if not self.is_connected():
            return {}
        result: dict[str, dict] = {}
        for ticket_str, order in self._client.open_orders.items():
            if not order:
                continue
            # DWX field names vary — handle both camelCase and snake_case
            raw_sym = str(order.get("symbol") or "")
            clean_sym = self._from_dwx_symbol(raw_sym)

            # Parse open time if present
            open_time = None
            raw_time = order.get("open_time") or order.get("openTime")
            if raw_time:
                try:
                    open_time = datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
                except (ValueError, TypeError, OSError):
                    pass

            result[str(ticket_str)] = {
                "ticket": str(ticket_str),
                "symbol": clean_sym,
                "lots": float(order.get("lots") or order.get("volume") or 0),
                "type": str(order.get("type", "buy")).lower(),
                "open_price": float(order.get("open_price") or order.get("openPrice") or 0),
                "sl": float(order.get("SL") or order.get("sl") or order.get("stop_loss") or 0),
                "tp": float(order.get("TP") or order.get("tp") or order.get("take_profit") or 0),
                "pnl": float(order.get("profit") or order.get("pnl") or 0),
                "swap": float(order.get("swap") or 0),
                "open_time": open_time,
                "comment": str(order.get("comment") or ""),
                "magic": int(order.get("magic") or 0),
            }
        return result

    def get_positions_snapshot_meta(self, max_age_seconds: float = 90.0) -> dict[str, Any]:
        """
        Describe freshness of the DWX orders snapshot used for open positions.

        DWX can bootstrap from DWX_Orders_Stored.txt when the live orders file is
        missing. That is useful for startup, but dangerous for operator actions if
        the stored snapshot is old. Position-manager callers can use this metadata
        to fail closed instead of trusting ghost positions.
        """
        if not self.is_connected():
            return {
                "source": None,
                "path": None,
                "mtime_utc": None,
                "age_seconds": None,
                "is_fresh": False,
                "reason": "adapter_not_connected",
            }

        orders_path = Path(getattr(self._client, "path_orders", ""))
        stored_path = Path(getattr(self._client, "path_orders_stored", ""))
        candidates: list[tuple[str, Path]] = []
        if orders_path.exists():
            candidates.append(("orders", orders_path))
        if stored_path.exists():
            candidates.append(("stored", stored_path))
        if not candidates:
            return {
                "source": None,
                "path": None,
                "mtime_utc": None,
                "age_seconds": None,
                "is_fresh": False,
                "reason": "orders_snapshot_missing",
            }

        source, path = max(candidates, key=lambda item: item[1].stat().st_mtime)
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age_seconds = max(0.0, (datetime.now(timezone.utc) - mtime).total_seconds())
        return {
            "source": source,
            "path": str(path),
            "mtime_utc": mtime.isoformat().replace("+00:00", "Z"),
            "age_seconds": age_seconds,
            "is_fresh": age_seconds <= float(max_age_seconds),
            "reason": "" if age_seconds <= float(max_age_seconds) else f"orders_snapshot_stale>{float(max_age_seconds):.0f}s",
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def open_order(
        self,
        symbol: str,
        side: str,
        lots: float,
        price: float = 0,
        sl: float = 0,
        tp: float = 0,
        comment: str = "vanguard",
        magic: int = 20260101,
    ) -> str:
        """
        Place a market or limit order.

        Args:
            symbol: 'EURUSD' (adapter adds suffix)
            side: 'buy' or 'sell'
            lots: position size in lots
            price: 0 for market; >0 for limit/stop
            sl: stop-loss price (0 = no SL)
            tp: take-profit price (0 = no TP)

        Returns: ticket number as string, or '' on failure.
        """
        if not self.is_connected():
            logger.error("open_order called but adapter not connected")
            return ""

        dwx_sym = self._to_dwx_symbol(symbol)
        order_type = str(side).lower()  # 'buy' or 'sell'

        # Snapshot existing tickets to detect the newly opened order.
        tickets_before = set(self._client.open_orders.keys())

        self._client.open_order(
            symbol=dwx_sym,
            order_type=order_type,
            lots=round(float(lots), 5),
            price=float(price),
            stop_loss=float(sl),
            take_profit=float(tp),
            magic=int(magic),
            comment=str(comment),
            expiration=0,
        )

        # Poll for the new ticket (async response).
        deadline = now_utc() + timedelta(seconds=_ORDER_TIMEOUT_S)
        while now_utc() < deadline:
            sleep(0.2)
            new_tickets = set(self._client.open_orders.keys()) - tickets_before
            if new_tickets:
                ticket = str(sorted(new_tickets, reverse=True)[0])
                logger.info(
                    "Opened %s %s lots=%.5f sl=%.5f tp=%.5f → ticket=%s",
                    side, symbol, lots, sl, tp, ticket,
                )
                return ticket

        logger.warning(
            "open_order timed out after %.0fs — no new ticket detected for %s",
            _ORDER_TIMEOUT_S, symbol,
        )
        return ""

    def close_order(self, ticket: str, lots: float = 0) -> bool:
        """
        Close an order by ticket number.

        Args:
            ticket: MT5 ticket (string or int)
            lots: 0 = full close; >0 = partial

        Returns True if order disappears from open_orders within timeout.
        """
        if not self.is_connected():
            return False

        ticket_str = str(ticket)
        if ticket_str not in self._client.open_orders:
            logger.warning("close_order: ticket %s not found in open_orders", ticket_str)
            return False

        self._client.close_order(ticket=int(ticket), lots=float(lots))

        deadline = now_utc() + timedelta(seconds=_ORDER_TIMEOUT_S)
        while now_utc() < deadline:
            sleep(0.2)
            if ticket_str not in self._client.open_orders:
                logger.info("Closed ticket=%s", ticket_str)
                return True

        logger.warning("close_order timed out — ticket %s still present", ticket_str)
        return False

    def modify_order(self, ticket: str, sl: float = 0, tp: float = 0) -> bool:
        """
        Modify SL/TP on an existing order.

        Returns True if the change is reflected in open_orders within timeout.
        """
        if not self.is_connected():
            return False

        ticket_str = str(ticket)
        self._client.modify_order(
            ticket=int(ticket),
            stop_loss=float(sl),
            take_profit=float(tp),
        )

        deadline = now_utc() + timedelta(seconds=_ORDER_TIMEOUT_S)
        while now_utc() < deadline:
            sleep(0.2)
            order = self._client.open_orders.get(ticket_str, {})
            order_sl = float(order.get("SL") or order.get("sl") or 0)
            order_tp = float(order.get("TP") or order.get("tp") or 0)
            # Accept if at least one changed value matches (within 1e-5)
            sl_ok = sl == 0 or abs(order_sl - sl) < 1e-5
            tp_ok = tp == 0 or abs(order_tp - tp) < 1e-5
            if sl_ok and tp_ok:
                logger.info("Modified ticket=%s sl=%.5f tp=%.5f", ticket_str, sl, tp)
                return True

        logger.warning(
            "modify_order timed out — ticket=%s sl=%.5f tp=%.5f not confirmed",
            ticket_str, sl, tp,
        )
        return False

    def get_last_bridge_message(self) -> str:
        """Return the latest non-empty DWX stored message line for operator diagnostics."""
        client = getattr(self, "_client", None)
        path = getattr(client, "path_messages_stored", "")
        if not path:
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read().strip()
        except OSError:
            return ""
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return lines[-1] if lines else ""
        if isinstance(payload, dict) and payload:
            latest_key = sorted(payload.keys())[-1]
            latest = payload.get(latest_key) or {}
            if isinstance(latest, dict):
                err_type = str(latest.get("error_type") or latest.get("type") or "").strip()
                desc = str(latest.get("description") or latest.get("message") or "").strip()
                if err_type and desc:
                    return f"{err_type}: {desc}"
                if desc:
                    return desc
                return json.dumps(latest, separators=(",", ":"))
        return text

    # ------------------------------------------------------------------
    # DB write
    # ------------------------------------------------------------------

    def write_bars_to_db(self, symbol: str, bars: list[dict]) -> int:
        """
        Write a list of bar dicts to vanguard_bars_1m.

        DWX bar dict keys: time (epoch int), open, high, low, close, tick_volume
        Schema: symbol, bar_ts_utc, open, high, low, close, volume, tick_volume,
                asset_class, data_source, ingest_ts_utc
        """
        if not bars:
            return 0

        symbol = self._normalize_symbol(symbol)
        asset_class = self._get_asset_class(symbol)
        ingest_ts = iso_utc(now_utc())
        rows = []

        for b in bars:
            try:
                bar_ts = self._bar_end_iso_utc(b, interval_minutes=1)
                if not bar_ts:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "bar_ts_utc": bar_ts,
                        "open": float(b.get("open", 0)),
                        "high": float(b.get("high", 0)),
                        "low": float(b.get("low", 0)),
                        "close": float(b.get("close", 0)),
                        "volume": int(float(b.get("tick_volume", 0))),
                        "tick_volume": int(float(b.get("tick_volume", 0))),
                        "asset_class": asset_class,
                        "data_source": "mt5_dwx",
                        "ingest_ts_utc": ingest_ts,
                    }
                )
            except (TypeError, ValueError) as exc:
                logger.debug("Skipping malformed bar for %s: %s", symbol, exc)
                continue

        if not rows:
            return 0

        from vanguard.helpers.db import VanguardDB  # noqa: PLC0415

        db = VanguardDB(self.db_path)
        return db.upsert_bars_1m(rows)

    # ------------------------------------------------------------------
    # Symbol helpers
    # ------------------------------------------------------------------

    def _to_dwx_symbol(self, symbol: str) -> str:
        """EURUSD → EURUSD.x"""
        s = self._normalize_symbol(symbol)
        suffix = self._suffix_for_symbol(s)
        if suffix and not s.upper().endswith(suffix.upper()):
            return s + suffix
        return s

    def _from_dwx_symbol(self, symbol: str) -> str:
        """EURUSD.x → EURUSD"""
        s = self._normalize_symbol(symbol)
        for suffix in sorted(set(self.symbol_suffix_map.values()) | {self.symbol_suffix}, key=len, reverse=True):
            if suffix and s.upper().endswith(suffix.upper()):
                return s[: -len(suffix)]
        return s

    def _suffix_for_symbol(self, symbol: str) -> str:
        s = self._normalize_symbol(symbol)
        return self.symbol_suffix_map.get(s, self.symbol_suffix)

    def _get_asset_class(self, symbol: str) -> str:
        """Look up asset_class from universe_members, with cache."""
        symbol = self._normalize_symbol(symbol)
        if symbol in self._asset_class_cache:
            return self._asset_class_cache[symbol]
        try:
            with connect_wal(self.db_path) as con:
                row = con.execute(
                    "SELECT asset_class FROM vanguard_universe_members WHERE symbol = ? LIMIT 1",
                    (symbol,),
                ).fetchone()
            if row and row[0]:
                self._asset_class_cache[symbol] = str(row[0])
                return self._asset_class_cache[symbol]
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    def get_last_poll_details(self) -> dict[str, Any]:
        return dict(self._last_poll_details)

    def _normalize_symbol(self, symbol: str) -> str:
        return str(symbol or "").upper().replace("/", "").replace(" ", "")

    def _bar_end_iso_utc(self, bar: dict[str, Any], interval_minutes: int) -> str | None:
        raw_time = bar.get("time")
        if raw_time is not None:
            ts_epoch = int(raw_time)
            if ts_epoch <= 0:
                return None
            open_dt_utc = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
        elif bar.get("time_str"):
            open_dt_utc = self._parse_dwx_time_str(str(bar["time_str"]))
            if open_dt_utc is None:
                return None
        else:
            return None
        end_dt_utc = open_dt_utc + timedelta(minutes=interval_minutes)
        return end_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _parse_dwx_time_str(self, value: str) -> datetime | None:
        try:
            open_dt_local = datetime.strptime(value, "%Y.%m.%d %H:%M")
        except ValueError:
            logger.debug("Unexpected DWX time string: %s", value)
            return None

        if self._server_tz_offset_hours is None:
            end_dt_local = open_dt_local + timedelta(minutes=1)
            now_naive = now_utc().replace(tzinfo=None, second=0, microsecond=0)
            inferred = int(round((end_dt_local - now_naive).total_seconds() / 3600))
            if abs(inferred) > _SERVER_TZ_MAX_OFFSET_HOURS:
                inferred = 0
            self._server_tz_offset_hours = inferred
            logger.info("[DWX] Inferred server timezone offset: %+dh", inferred)

        open_dt_utc = open_dt_local - timedelta(hours=self._server_tz_offset_hours or 0)
        return open_dt_utc.replace(tzinfo=timezone.utc)

    def _prime_server_tz_offset(self, bars: list[dict]) -> None:
        """Infer server offset from the latest bar in a batch, not the oldest."""
        if self._server_tz_offset_hours is not None:
            return
        latest_time_str = ""
        for bar in bars:
            value = str(bar.get("time_str") or "").strip()
            if value and value > latest_time_str:
                latest_time_str = value
        if not latest_time_str:
            return
        try:
            latest_open_local = datetime.strptime(latest_time_str, "%Y.%m.%d %H:%M")
        except ValueError:
            logger.debug("Unexpected DWX time string in batch: %s", latest_time_str)
            return
        latest_end_local = latest_open_local + timedelta(minutes=1)
        now_naive = now_utc().replace(tzinfo=None, second=0, microsecond=0)
        inferred = int(round((latest_end_local - now_naive).total_seconds() / 3600))
        if abs(inferred) > _SERVER_TZ_MAX_OFFSET_HOURS:
            inferred = 0
        self._server_tz_offset_hours = inferred
        logger.info("[DWX] Inferred server timezone offset from latest bar: %+dh", inferred)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _parse_and_write_bars(self, symbol: str, bars_raw: Any) -> int:
        """
        Normalise DWX historic_data payload and write to DB.

        DWX can return either:
          - A list of bar dicts: [{'time': ..., 'open': ..., ...}, ...]
          - A dict keyed by bar index: {'0': {'time': ...}, '1': ...}
        """
        if not bars_raw:
            return 0

        if isinstance(bars_raw, dict):
            bars_list = []
            for key, value in bars_raw.items():
                if isinstance(value, dict):
                    bar = dict(value)
                    if "time" not in bar:
                        bar["time_str"] = key
                    bars_list.append(bar)
        elif isinstance(bars_raw, list):
            bars_list = bars_raw
        else:
            logger.debug("Unexpected bars_raw type for %s: %s", symbol, type(bars_raw))
            return 0

        self._prime_server_tz_offset(bars_list)
        return self.write_bars_to_db(symbol, bars_list)


# ---------------------------------------------------------------------------
# Standalone test (run directly to verify connectivity)
# ---------------------------------------------------------------------------

def _self_test(dwx_files_path: str, db_path: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    adapter = MT5DWXAdapter(dwx_files_path=dwx_files_path, db_path=db_path)
    print("Connecting…")
    connected = adapter.connect()
    print(f"Connected: {connected}")
    print(f"Account:   {adapter.get_account_info()}")
    print(f"Positions: {adapter.get_open_positions()}")
    print("Polling EURUSD, GBPUSD, USDJPY…")
    bars = adapter.poll(symbol_filter=["EURUSD", "GBPUSD", "USDJPY"])
    print(f"Bars written: {bars}")


if __name__ == "__main__":
    _DWX_PATH = (
        "/Users/sjani008/Library/Application Support/"
        "net.metaquotes.wine.metatrader5/drive_c/"
        "Program Files/MetaTrader 5/MQL5/Files"
    )
    _DB_PATH = os.environ.get("VANGUARD_SOURCE_DB") or str(Path(__file__).resolve().parents[2] / "data" / "vanguard_universe.db")
    _self_test(_DWX_PATH, _DB_PATH)
