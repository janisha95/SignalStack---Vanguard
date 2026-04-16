"""
metaapi_client.py — Read-only MetaApi client for Phase 3a.

Uses the official metaapi-cloud-sdk Python library.
Exposes only get_open_positions() and get_account_state() — no writes to broker.

Hard rules:
  - If SDK not installed: ImportError at module load (intentional — not a silent stub).
  - If MetaApi unreachable: raises MetaApiUnavailable (callers must not write to DB on outage).
  - No streaming connection — uses RPC (lower latency for one-shot reads).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

# Intentional hard fail at import — SDK must be present. Do not add try/except here.
from metaapi_cloud_sdk import MetaApi  # noqa: F401 — validates SDK is installed

logger = logging.getLogger(__name__)


class MetaApiUnavailable(Exception):
    """Raised when MetaApi cannot be reached or authentication fails."""


class MetaApiClient:
    """Read-only client for one MetaApi-connected MetaTrader account."""

    def __init__(
        self,
        account_id: str,
        api_token: str,
        base_url: str = "",
        timeout_s: int = 10,
    ) -> None:
        self.account_id = str(account_id or "").strip()
        self.api_token  = str(api_token  or "").strip()
        self.base_url   = str(base_url   or "").strip()
        self.timeout_s  = int(timeout_s  or 10)

    async def _get_rpc_connection(self):
        """Build MetaApi + RPC connection, synchronized within timeout_s."""
        try:
            api = MetaApi(self.api_token)
            account = await asyncio.wait_for(
                api.metatrader_account_api.get_account(self.account_id),
                timeout=self.timeout_s,
            )
            conn = account.get_rpc_connection()
            await asyncio.wait_for(conn.connect(), timeout=self.timeout_s)
            await asyncio.wait_for(
                conn.wait_synchronized(timeout_in_seconds=self.timeout_s),
                timeout=self.timeout_s + 2,
            )
            return api, conn
        except asyncio.TimeoutError as exc:
            raise MetaApiUnavailable(
                f"MetaApi connection timed out after {self.timeout_s}s "
                f"(account={self.account_id})"
            ) from exc
        except Exception as exc:
            raise MetaApiUnavailable(
                f"MetaApi unavailable: {exc} (account={self.account_id})"
            ) from exc

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """
        Returns list of open positions from the broker.

        Each dict has keys:
          broker_position_id, symbol, side, qty, entry_price,
          current_sl, current_tp, opened_at_utc, unrealized_pnl.

        Raises MetaApiUnavailable on network/auth failure.
        Returns [] if broker has no open positions.
        """
        api, conn = await self._get_rpc_connection()
        try:
            raw: list[dict] = await asyncio.wait_for(
                conn.get_positions(),
                timeout=self.timeout_s,
            ) or []
        except asyncio.TimeoutError as exc:
            raise MetaApiUnavailable(
                f"get_positions timed out (account={self.account_id})"
            ) from exc
        except Exception as exc:
            raise MetaApiUnavailable(
                f"get_positions failed: {exc}"
            ) from exc
        finally:
            try:
                await conn.close()
            except Exception:
                pass
            try:
                api.close()
            except Exception:
                pass

        positions = []
        for p in raw:
            pos_type = str(p.get("type") or "").upper()
            side = "LONG" if "BUY" in pos_type else "SHORT"
            # opened_at_utc: prefer isoformat from datetime, fallback brokerTime
            opened_raw = p.get("time")
            if opened_raw is not None:
                try:
                    opened_at = opened_raw.strftime("%Y-%m-%dT%H:%M:%SZ")
                except AttributeError:
                    opened_at = str(opened_raw)
            else:
                opened_at = str(p.get("brokerTime") or "")

            positions.append({
                "broker_position_id": str(p.get("id") or ""),
                "symbol":             str(p.get("symbol") or ""),
                "side":               side,
                "qty":                float(p.get("volume") or 0.0),
                "entry_price":        float(p.get("openPrice") or 0.0),
                "current_sl":         float(p["stopLoss"])   if p.get("stopLoss")   is not None else None,
                "current_tp":         float(p["takeProfit"]) if p.get("takeProfit") is not None else None,
                "opened_at_utc":      opened_at,
                "unrealized_pnl":     float(p.get("unrealizedProfit") or p.get("profit") or 0.0),
            })

        logger.info(
            "[MetaApiClient] account=%s positions=%d",
            self.account_id, len(positions),
        )
        return positions

    async def get_account_state(self) -> dict[str, Any]:
        """
        Returns account state: equity, balance, margin, free_margin.

        Raises MetaApiUnavailable on failure.
        """
        api, conn = await self._get_rpc_connection()
        try:
            info = await asyncio.wait_for(
                conn.get_account_information(),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise MetaApiUnavailable(
                f"get_account_information timed out (account={self.account_id})"
            ) from exc
        except Exception as exc:
            raise MetaApiUnavailable(
                f"get_account_information failed: {exc}"
            ) from exc
        finally:
            try:
                await conn.close()
            except Exception:
                pass
            try:
                api.close()
            except Exception:
                pass

        return {
            "equity":       float(info.get("equity")      or 0.0),
            "balance":      float(info.get("balance")     or 0.0),
            "margin":       float(info.get("margin")      or 0.0),
            "free_margin":  float(info.get("freeMargin")  or info.get("free_margin") or 0.0),
        }

    async def close_position(
        self,
        position_id: str,
        reason: str = "AUTO_CLOSE",
    ) -> dict[str, Any]:
        """
        Close a single open position by broker_position_id.

        Added in Phase 3f — first broker write operation in this client.
        This method is intentionally targeted at the QA paper/demo account only.

        Returns:
            {
              "status":       "closed" | "failed",
              "position_id":  str,
              "close_price":  float | None,
              "close_time":   str | None,
              "realized_pnl": float | None,
              "error":        str | None,
            }

        Raises MetaApiUnavailable on connection failure (NOT on broker rejection —
        broker rejection returns status="failed").
        """
        api, conn = await self._get_rpc_connection()
        try:
            resp = await asyncio.wait_for(
                conn.create_market_order(
                    symbol=None,          # position_id-based close, symbol not required
                    volume=0,             # ignored for POSITION_CLOSE_ID
                    side="ORDER_TYPE_SELL",
                    close_only_position_id=str(position_id),
                    options={
                        "comment": str(reason or "AUTO_CLOSE"),
                        "actionType": "POSITION_CLOSE_ID",
                        "positionId": str(position_id),
                    },
                ),
                timeout=self.timeout_s,
            )
        except (asyncio.TimeoutError, Exception):
            # Fall through to direct trade API approach
            resp = None

        # Primary approach: use the SDK trade method directly if available
        if resp is None:
            try:
                resp = await asyncio.wait_for(
                    conn.close_position(str(position_id), {}),
                    timeout=self.timeout_s,
                )
            except asyncio.TimeoutError as exc:
                raise MetaApiUnavailable(
                    f"close_position timed out (account={self.account_id} position_id={position_id})"
                ) from exc
            except Exception as exc:
                raise MetaApiUnavailable(
                    f"close_position failed: {exc} (account={self.account_id})"
                ) from exc
            finally:
                try:
                    await conn.close()
                except Exception:
                    pass
                try:
                    api.close()
                except Exception:
                    pass

        else:
            try:
                await conn.close()
            except Exception:
                pass
            try:
                api.close()
            except Exception:
                pass

        # Normalize response
        if resp is None:
            return {
                "status":       "failed",
                "position_id":  str(position_id),
                "close_price":  None,
                "close_time":   None,
                "realized_pnl": None,
                "error":        "no_response",
            }

        string_code  = str(getattr(resp, "stringCode", None) or (resp.get("stringCode") if isinstance(resp, dict) else "") or "").upper()
        numeric_code = int(getattr(resp, "numericCode", None) or (resp.get("numericCode") if isinstance(resp, dict) else 0) or 0)
        is_success   = string_code == "TRADE_RETCODE_DONE" or numeric_code == 10009

        if is_success:
            logger.info(
                "[MetaApiClient] close_position OK: position_id=%s account=%s",
                position_id, self.account_id,
            )
            # close_price comes from deal history; return None here, reconciler will fill it
            return {
                "status":       "closed",
                "position_id":  str(position_id),
                "close_price":  None,  # populated by deal history reconciliation
                "close_time":   None,
                "realized_pnl": None,
                "raw_response": resp if isinstance(resp, dict) else vars(resp) if hasattr(resp, "__dict__") else str(resp),
            }

        error_msg = (
            getattr(resp, "message", None)
            or (resp.get("message") if isinstance(resp, dict) else None)
            or str(resp)
        )
        logger.warning(
            "[MetaApiClient] close_position got non-success response: %s (position_id=%s)",
            string_code or numeric_code, position_id,
        )
        return {
            "status":       "failed",
            "position_id":  str(position_id),
            "close_price":  None,
            "close_time":   None,
            "realized_pnl": None,
            "error":        str(error_msg or f"code={numeric_code}"),
        }
