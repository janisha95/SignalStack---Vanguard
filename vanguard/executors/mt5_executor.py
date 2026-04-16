#!/usr/bin/env python3
"""MetaApi executor for GFT cloud trading.

Environment:
  export METAAPI_TOKEN="your-metaapi-token"
  export GFT_ACCOUNT_ID="your-default-metaapi-account-id"

Optional per-account overrides:
  export GFT_5K_ACCOUNT_ID="metaapi-account-id-for-gft-5k"
  export GFT_10K_ACCOUNT_ID="metaapi-account-id-for-gft-10k"

The module keeps ``connect()`` / ``execute_trade()`` wrappers for legacy callers
and exposes ``MetaApiExecutor`` for account-aware execution.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv(Path.home() / "SS" / ".env")

try:
    from vanguard.helpers.symbol_identity import to_broker as _symbol_to_broker
    _HAS_SYMBOL_IDENTITY = True
except ImportError:
    _HAS_SYMBOL_IDENTITY = False

logger = logging.getLogger("mt5_executor")

METAAPI_BASE_URL = os.environ.get(
    "METAAPI_BASE_URL",
    "https://mt-client-api-v1.new-york.agiliumtrade.ai",
).rstrip("/")
METAAPI_TIMEOUT = float(os.environ.get("METAAPI_TIMEOUT", "20"))

VANGUARD_TO_GFT = {
    "AAPL": "AAPL.x", "AMZN": "AMZN.x", "BA": "BA.x", "COST": "COST.x",
    "JPM": "JPM.x", "KO": "KO.x", "META": "META.x", "MSFT": "MSFT.x",
    "NFLX": "NFLX.x", "NVDA": "NVDA.x", "ORCL": "ORCL.x", "PLTR": "PLTR.x",
    "TSLA": "TSLA.x", "UNH": "UNH.x", "V": "V.x",
    "SPY": "SPX500.x", "QQQ": "NAS100.x", "DIA": "US30.x",
    "EURUSD": "EURUSD.x", "GBPUSD": "GBPUSD.x", "USDJPY": "USDJPY.x",
    "AUDUSD": "AUDUSD.x", "NZDUSD": "NZDUSD.x", "USDCAD": "USDCAD.x",
    "USDCHF": "USDCHF.x", "EURGBP": "EURGBP.x", "EURJPY": "EURJPY.x",
    "GBPJPY": "GBPJPY.x", "AUDJPY": "AUDJPY.x", "EURCHF": "EURCHF.x",
    "EURAUD": "EURAUD.x", "EURCAD": "EURCAD.x",
    # GFT crypto mappings use slashless symbols + .x suffix; verify broker codes
    # against the live MetaApi symbol catalog before enabling automated orders.
    "BTCUSD": "BTCUSD.x", "ETHUSD": "ETHUSD.x", "LTCUSD": "LTCUSD.x",
    "XRPUSD": "XRPUSD.x", "SOLUSD": "SOLUSD.x", "ADAUSD": "ADAUSD.x",
    "DOTUSD": "DOTUSD.x", "LINKUSD": "LINKUSD.x", "AVAXUSD": "AVAXUSD.x",
    "MATICUSD": "MATICUSD.x", "DOGEUSD": "DOGEUSD.x", "ATOMUSD": "ATOMUSD.x",
    "UNIUSD": "UNIUSD.x", "AAVEUSD": "AAVEUSD.x", "BNBUSD": "BNBUSD.x",
}

_EXECUTOR_CACHE: dict[str, "MetaApiExecutor"] = {}


def _mt5_symbol(symbol: str) -> str:
    """Translate canonical internal symbol to GFT broker form.

    Priority:
      1. Explicit VANGUARD_TO_GFT map (handles special cases like SPY→SPX500.x)
      2. to_broker('metaapi_gft', canonical) via symbol_identity (adds .x suffix)
      3. Raw normalized fallback
    """
    normalized = str(symbol or "").upper().replace("/", "").replace(".CASH", "").strip()
    if normalized in VANGUARD_TO_GFT:
        return VANGUARD_TO_GFT[normalized]
    if _HAS_SYMBOL_IDENTITY:
        try:
            return _symbol_to_broker("metaapi_gft", normalized)
        except Exception:
            pass
    return normalized


def _resolve_account_env(account_key: str = "") -> str:
    normalized = str(account_key or "").upper().strip()
    if normalized:
        for env_name in (
            f"{normalized}_ACCOUNT_ID",
            f"{normalized}_METAAPI_ACCOUNT_ID",
        ):
            account_id = os.environ.get(env_name, "").strip()
            if account_id:
                return account_id
    return os.environ.get("GFT_ACCOUNT_ID", "").strip()


class MetaApiExecutor:
    """Submit market orders to a MetaApi-connected MetaTrader account."""

    def __init__(
        self,
        account_key: str = "",
        token: str | None = None,
        account_id: str | None = None,
        base_url: str = METAAPI_BASE_URL,
        timeout: float = METAAPI_TIMEOUT,
    ) -> None:
        self.account_key = str(account_key or "").lower().strip()
        self.token = (token or os.environ.get("METAAPI_TOKEN", "")).strip()
        self.account_id = (account_id or _resolve_account_env(self.account_key)).strip()
        self.base_url = str(base_url or METAAPI_BASE_URL).rstrip("/")
        self.timeout = float(timeout or METAAPI_TIMEOUT)
        self._connect_attempted = False
        self._connected = False

    def connect(self) -> bool:
        """Validate MetaApi credentials and mark the executor ready."""
        if self._connected:
            return True
        if self._connect_attempted:
            return False
        self._connect_attempted = True

        if not self.token:
            logger.error("METAAPI_TOKEN is not set")
            return False
        if not self.account_id:
            logger.error(
                "GFT_ACCOUNT_ID is not set for account=%s "
                "(optional overrides: GFT_5K_ACCOUNT_ID / GFT_10K_ACCOUNT_ID)",
                self.account_key or "default",
            )
            return False
        self._connected = True
        logger.info(
            "[MetaApi] Ready account=%s metaapi_account_id=%s",
            self.account_key or "default",
            self.account_id,
        )
        return True

    def execute_trade(
        self,
        symbol: str,
        direction: str,
        lot_size: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Place a market order with optional SL/TP on MetaApi."""
        if not self._connected and not self.connect():
            return {
                "status": "failed",
                "error": "metaapi_connect_failed",
                "symbol": _mt5_symbol(symbol),
                "account_id": self.account_key or "default",
            }

        order_direction = str(direction or "").upper().strip()
        if order_direction not in ("LONG", "SHORT"):
            return {
                "status": "failed",
                "error": f"unsupported direction={direction!r}",
                "symbol": _mt5_symbol(symbol),
            }

        gft_symbol = _mt5_symbol(symbol)
        if float(lot_size or 0.0) > 1.0:
            logger.error(
                "[MetaApi] SAFETY: lot_size=%s too large for %s. Capping to 0.50",
                lot_size,
                gft_symbol,
            )
            lot_size = 0.50
        if float(lot_size or 0.0) > 0.50:
            logger.warning(
                "[MetaApi] lot_size=%s > 0.50, capping for GFT safety",
                lot_size,
            )
            lot_size = 0.50
        payload: dict[str, Any] = {
            "actionType": "ORDER_TYPE_BUY" if order_direction == "LONG" else "ORDER_TYPE_SELL",
            "symbol": gft_symbol,
            "volume": float(lot_size),
            "comment": f"SS_{symbol}_{order_direction}",
        }
        if stop_loss is not None:
            payload["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            payload["takeProfit"] = float(take_profit)

        trade_url = f"{self.base_url}/users/current/accounts/{self.account_id}/trade"
        req = Request(
            trade_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "auth-token": self.token,
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "[MetaApi] %s %s x%s HTTP %s: %s",
                order_direction,
                gft_symbol,
                lot_size,
                exc.code,
                body,
            )
            return {
                "status": "failed",
                "error": f"HTTP {exc.code}: {body}",
                "symbol": gft_symbol,
                "request": payload,
            }
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            logger.error("[MetaApi] %s %s x%s failed: %s", order_direction, gft_symbol, lot_size, exc)
            return {
                "status": "failed",
                "error": str(exc),
                "symbol": gft_symbol,
                "request": payload,
            }

        string_code = str(parsed.get("stringCode") or "").upper()
        numeric_code = int(parsed.get("numericCode") or 0)
        if string_code == "TRADE_RETCODE_DONE" or numeric_code == 10009:
            logger.info(
                "[MetaApi] %s %s x%s -> ticket=%s",
                order_direction,
                gft_symbol,
                lot_size,
                parsed.get("orderId"),
            )
            return {
                "status": "filled",
                "ticket": parsed.get("orderId"),
                "symbol": gft_symbol,
                "request": payload,
                "response": parsed,
            }

        return {
            "status": "failed",
            "error": parsed.get("message") or parsed.get("stringCode") or str(parsed),
            "symbol": gft_symbol,
            "request": payload,
            "response": parsed,
        }

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Return current MetaApi open positions for this account."""
        if not self._connected and not self.connect():
            return []

        positions_url = f"{self.base_url}/users/current/accounts/{self.account_id}/positions"
        req = Request(
            positions_url,
            headers={
                "Accept": "application/json",
                "auth-token": self.token,
            },
            method="GET",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else []
            return payload if isinstance(payload, list) else []
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("[MetaApi] positions HTTP %s: %s", exc.code, body)
            return []
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            logger.error("[MetaApi] positions fetch failed: %s", exc)
            return []

    def close_position(
        self,
        position_id: str,
        symbol: str = "",
        reason: str = "TIME_EXIT_4H",
    ) -> dict[str, Any]:
        """Close one MetaApi position by id."""
        if not self._connected and not self.connect():
            return {
                "status": "failed",
                "error": "metaapi_connect_failed",
                "symbol": _mt5_symbol(symbol),
                "position_id": position_id,
            }

        broker_symbol = _mt5_symbol(symbol) if symbol else ""
        payload: dict[str, Any] = {
            "actionType": "POSITION_CLOSE_ID",
            "positionId": str(position_id),
            "comment": str(reason or "TIME_EXIT_4H"),
        }
        trade_url = f"{self.base_url}/users/current/accounts/{self.account_id}/trade"
        req = Request(
            trade_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "auth-token": self.token,
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "[MetaApi] close_position %s %s HTTP %s: %s",
                broker_symbol or position_id,
                position_id,
                exc.code,
                body,
            )
            return {
                "status": "failed",
                "error": f"HTTP {exc.code}: {body}",
                "symbol": broker_symbol,
                "position_id": position_id,
                "request": payload,
            }
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            logger.error("[MetaApi] close_position %s %s failed: %s", broker_symbol or position_id, position_id, exc)
            return {
                "status": "failed",
                "error": str(exc),
                "symbol": broker_symbol,
                "position_id": position_id,
                "request": payload,
            }

        string_code = str(parsed.get("stringCode") or "").upper()
        numeric_code = int(parsed.get("numericCode") or 0)
        if string_code == "TRADE_RETCODE_DONE" or numeric_code == 10009:
            logger.info(
                "[MetaApi] TIME_EXIT_4H closed %s position_id=%s",
                broker_symbol or position_id,
                position_id,
            )
            return {
                "status": "closed",
                "symbol": broker_symbol,
                "position_id": position_id,
                "response": parsed,
            }

        return {
            "status": "failed",
            "error": parsed.get("message") or parsed.get("stringCode") or str(parsed),
            "symbol": broker_symbol,
            "position_id": position_id,
            "request": payload,
            "response": parsed,
        }


def _get_executor(account_key: str = "") -> MetaApiExecutor:
    cache_key = str(account_key or "default").lower().strip() or "default"
    executor = _EXECUTOR_CACHE.get(cache_key)
    if executor is None:
        executor = MetaApiExecutor(account_key=cache_key)
        _EXECUTOR_CACHE[cache_key] = executor
    return executor


def connect(account_key: str = "") -> bool:
    """Legacy wrapper for existing orchestrator imports."""
    return _get_executor(account_key=account_key).connect()


def execute_trade(
    symbol: str,
    direction: str,
    lot_size: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    account_key: str = "",
) -> dict[str, Any]:
    """Legacy wrapper for existing orchestrator imports."""
    return _get_executor(account_key=account_key).execute_trade(
        symbol=symbol,
        direction=direction,
        lot_size=lot_size,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
