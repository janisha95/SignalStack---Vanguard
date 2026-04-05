"""
signalstack_adapter.py — HTTP POST client for SignalStack webhook.

Sends JSON payloads to execute trades on TTP via Trader Evolution.
Location: ~/SS/Vanguard/vanguard/execution/signalstack_adapter.py
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class SignalStackAdapter:
    """
    HTTP POST client for SignalStack webhook.
    Sends JSON payloads to execute trades on TTP via Trader Evolution.
    """

    def __init__(
        self,
        webhook_url: str = "",
        timeout: int = 5,
        max_retries: int = 2,
        retry_delay: float = 1.0,
        log_payloads: bool = True,
    ):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.log_payloads = log_payloads

    def _direction_to_action(self, direction: str, operation: str, broker: str = "ttp") -> str:
        """
        Map direction + operation to SignalStack action.

        Broker-specific actions:
          TTP:  buy, sell, cancel, close
          Lime: buy, sell, sell_short, buy_to_cover, close
        """
        direction = direction.upper()
        broker = broker.lower()

        if operation == "flatten":
            return "close"

        if broker == "ttp":
            if operation == "open":
                return "buy" if direction == "LONG" else "sell"
            if operation == "close":
                return "close"
        elif broker == "lime":
            if operation == "open":
                return "buy" if direction == "LONG" else "sell_short"
            if operation == "close":
                return "sell" if direction == "LONG" else "buy_to_cover"

        if operation == "open":
            return "buy" if direction == "LONG" else "sell"
        if operation == "close":
            return "close"
        raise ValueError(f"Unknown operation: {operation}")

    def build_payload(
        self,
        symbol: str,
        action: str,
        quantity: int,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> dict:
        """Build SignalStack JSON payload."""
        payload: dict = {
            "action": action,
            "ticker": symbol,
            "quantity": quantity,
        }
        if limit_price is not None:
            payload["limit_price"] = limit_price
        if stop_price is not None:
            payload["stop_price"] = stop_price
        return payload

    def _resolve_webhook(self, profile: Optional[dict] = None) -> tuple[str, dict[str, str], Optional[str]]:
        profile = profile or {}
        webhook_url = str(profile.get("webhook_url") or "").strip()
        api_key = str(profile.get("webhook_api_key") or "").strip()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        profile_id = str(profile.get("id") or "") or None
        return webhook_url, headers, profile_id

    def send_order(
        self,
        symbol: Optional[str] = None,
        direction: Optional[str] = None,
        quantity: Optional[int] = None,
        operation: str = "open",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        broker: str = "ttp",
        order: Optional[dict] = None,
        profile: Optional[dict] = None,
    ) -> dict:
        """
        Send order to SignalStack webhook.

        Returns:
            {
                "success": bool,
                "status_code": int,
                "response_body": str,
                "latency_ms": int,
                "payload": dict,
                "error": str or None
            }
        """
        payload_order = dict(order or {})
        if payload_order:
            symbol = str(payload_order.get("symbol") or symbol or "").upper()
            direction = str(payload_order.get("direction") or direction or "").upper()
            quantity = int(
                payload_order.get("shares_or_lots")
                or payload_order.get("shares")
                or quantity
                or 0
            )
            operation = str(payload_order.get("action") or operation or "open").lower()
            limit_price = payload_order.get("limit_price", limit_price)
            stop_price = payload_order.get("stop_price", stop_price)
        else:
            symbol = str(symbol or "").upper()
            direction = str(direction or "").upper()
            quantity = int(quantity or 0)

        webhook_url, headers, profile_id = self._resolve_webhook(profile)
        if not webhook_url:
            return {
                "success": False,
                "status_code": 0,
                "response_body": "",
                "latency_ms": 0,
                "payload": {},
                "error": f"No webhook_url for profile {profile_id or 'unknown'}",
                "profile_id": profile_id,
                "webhook_used": None,
            }

        action = self._direction_to_action(direction, operation, broker)
        payload = self.build_payload(symbol, action, quantity, limit_price, stop_price)

        if self.log_payloads:
            logger.info(f"SignalStack payload: {json.dumps(payload)}")

        result: dict = {
            "success": False,
            "status_code": 0,
            "response_body": "",
            "latency_ms": 0,
            "payload": payload,
            "error": None,
            "profile_id": profile_id,
            "webhook_used": webhook_url[:30] + "...",
        }

        for attempt in range(self.max_retries + 1):
            try:
                start = time.time()
                response = requests.post(
                    webhook_url,
                    json=payload,
                    timeout=self.timeout,
                    headers=headers,
                )
                latency_ms = int((time.time() - start) * 1000)

                result = {
                    "success": response.status_code in (200, 201, 202),
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "latency_ms": latency_ms,
                    "payload": payload,
                    "error": (
                        None
                        if response.status_code in (200, 201, 202)
                        else f"HTTP {response.status_code}: {response.text[:200]}"
                    ),
                    "profile_id": profile_id,
                    "webhook_used": webhook_url[:30] + "...",
                }

                if result["success"]:
                    logger.info(
                        f"SignalStack OK: {symbol} {action} {quantity} ({latency_ms}ms)"
                    )
                    return result
                else:
                    logger.warning(
                        f"SignalStack FAIL attempt {attempt + 1}: {result['error']}"
                    )

            except requests.exceptions.Timeout:
                logger.warning(f"SignalStack TIMEOUT attempt {attempt + 1}: {symbol}")
                result = {
                    "success": False,
                    "status_code": 0,
                    "response_body": "",
                    "latency_ms": self.timeout * 1000,
                    "payload": payload,
                    "error": f"Timeout after {self.timeout}s",
                    "profile_id": profile_id,
                    "webhook_used": webhook_url[:30] + "...",
                }
            except requests.exceptions.RequestException as exc:
                logger.error(f"SignalStack ERROR attempt {attempt + 1}: {exc}")
                result = {
                    "success": False,
                    "status_code": 0,
                    "response_body": "",
                    "latency_ms": 0,
                    "payload": payload,
                    "error": str(exc),
                    "profile_id": profile_id,
                    "webhook_used": webhook_url[:30] + "...",
                }

            if attempt < self.max_retries:
                time.sleep(self.retry_delay)

        return result

    def flatten_position(self, symbol: str, quantity: int, profile: Optional[dict] = None) -> dict:
        """Close any position — direction-agnostic (sends 'close' action)."""
        webhook_url, headers, profile_id = self._resolve_webhook(profile)
        if not webhook_url:
            return {
                "success": False,
                "status_code": 0,
                "response_body": "",
                "latency_ms": 0,
                "payload": {},
                "error": f"No webhook_url for profile {profile_id or 'unknown'}",
                "profile_id": profile_id,
                "webhook_used": None,
            }
        action = self._direction_to_action("LONG", "flatten")  # always "close"
        payload = self.build_payload(symbol, action, quantity)

        if self.log_payloads:
            logger.info(f"SignalStack flatten payload: {json.dumps(payload)}")

        result: dict = {
            "success": False,
            "status_code": 0,
            "response_body": "",
            "latency_ms": 0,
            "payload": payload,
            "error": None,
            "profile_id": profile_id,
            "webhook_used": webhook_url[:30] + "...",
        }

        for attempt in range(self.max_retries + 1):
            try:
                start = time.time()
                response = requests.post(
                    webhook_url,
                    json=payload,
                    timeout=self.timeout,
                    headers=headers,
                )
                latency_ms = int((time.time() - start) * 1000)

                result = {
                    "success": response.status_code in (200, 201, 202),
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "latency_ms": latency_ms,
                    "payload": payload,
                    "error": (
                        None
                        if response.status_code in (200, 201, 202)
                        else f"HTTP {response.status_code}: {response.text[:200]}"
                    ),
                    "profile_id": profile_id,
                    "webhook_used": webhook_url[:30] + "...",
                }

                if result["success"]:
                    logger.info(f"SignalStack flatten OK: {symbol} close {quantity} ({latency_ms}ms)")
                    return result
                else:
                    logger.warning(
                        f"SignalStack flatten FAIL attempt {attempt + 1}: {result['error']}"
                    )

            except requests.exceptions.Timeout:
                logger.warning(f"SignalStack flatten TIMEOUT attempt {attempt + 1}: {symbol}")
                result = {
                    "success": False,
                    "status_code": 0,
                    "response_body": "",
                    "latency_ms": self.timeout * 1000,
                    "payload": payload,
                    "error": f"Timeout after {self.timeout}s",
                    "profile_id": profile_id,
                    "webhook_used": webhook_url[:30] + "...",
                }
            except requests.exceptions.RequestException as exc:
                logger.error(f"SignalStack flatten ERROR attempt {attempt + 1}: {exc}")
                result = {
                    "success": False,
                    "status_code": 0,
                    "response_body": "",
                    "latency_ms": 0,
                    "payload": payload,
                    "error": str(exc),
                    "profile_id": profile_id,
                    "webhook_used": webhook_url[:30] + "...",
                }

            if attempt < self.max_retries:
                time.sleep(self.retry_delay)

        return result
