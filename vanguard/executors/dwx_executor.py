"""
DWX Executor — Routes V7 approved trades to MT5 via the local DWX bridge.

Replaces MetaApiExecutor for GFT accounts when execution_bridge starts with
'mt5_local'. Interface mirrors MetaApiExecutor so the orchestrator can swap
between them transparently.

Safety:
  - MAX_LOTS_SAFETY = 0.05 hard-cap enforced on every submit_order call.
  - Requires a live, connected MT5DWXAdapter (connection validated in connect()).
  - All methods return structured dicts (never raise) so callers can check status.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from time import sleep
from typing import Any

logger = logging.getLogger("dwx_executor")

# Hard cap enforced on Day 1 of auto-execution. Raise only after manual review.
MAX_LOTS_SAFETY = 0.05


class DWXExecutor:
    """Submit, close, and modify MT5 orders via the local DWX file bridge."""

    def __init__(
        self,
        mt5_adapter: Any,   # MT5DWXAdapter — typed as Any to avoid circular import
        profile_id: str,
        db_path: str,
    ) -> None:
        """
        Args:
            mt5_adapter: Connected MT5DWXAdapter instance (shared from orchestrator).
            profile_id:  e.g. 'gft_10k'
            db_path:     Path to vanguard_universe.db (reserved for future journal writes).
        """
        self._mt5 = mt5_adapter
        self._profile_id = profile_id
        self._db_path = db_path
        self._max_lots_safety = self._load_max_lots_safety()

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Return True if the underlying DWX adapter is connected."""
        return bool(self._mt5 and self._mt5.is_connected())

    # ── Position reads ────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict[str, Any]]:
        """
        Return current open positions as a list of dicts.

        Normalised to MetaApi format so the orchestrator's open_symbols logic works
        without modification:
          [{'symbol': 'EURUSD', 'side': 'LONG', 'qty': 0.02, ...}, ...]
        """
        if not self.connect():
            return []
        dwx_positions = self._mt5.get_open_positions()  # dict[ticket, dict]
        result = []
        for ticket, pos in dwx_positions.items():
            side = "LONG" if str(pos.get("type", "buy")).lower() == "buy" else "SHORT"
            result.append({
                "symbol":      str(pos.get("symbol", "")).upper(),
                "side":        side,
                "qty":         float(pos.get("lots") or 0),
                "entry_price": float(pos.get("open_price") or 0),
                "current_sl":  float(pos.get("sl") or 0) or None,
                "current_tp":  float(pos.get("tp") or 0) or None,
                "pnl":         float(pos.get("pnl") or 0),
                "ticket":      str(ticket),
            })
        return result

    def get_positions_snapshot_meta(self, max_age_seconds: float = 90.0) -> dict[str, Any]:
        """Return freshness metadata for the DWX orders snapshot if supported."""
        if not self.connect():
            return {
                "source": None,
                "path": None,
                "mtime_utc": None,
                "age_seconds": None,
                "is_fresh": False,
                "reason": "dwx_not_connected",
            }
        if hasattr(self._mt5, "get_positions_snapshot_meta"):
            return self._mt5.get_positions_snapshot_meta(max_age_seconds=max_age_seconds)
        return {
            "source": "unknown",
            "path": None,
            "mtime_utc": None,
            "age_seconds": 0.0,
            "is_fresh": True,
            "reason": "",
        }

    def get_account_info(self) -> dict[str, Any]:
        """Return account balance/equity dict from the DWX adapter."""
        if not self.connect():
            return {}
        return self._mt5.get_account_info()

    # ── Order execution ───────────────────────────────────────────────────────

    def execute_trade(
        self,
        symbol: str,
        direction: str,
        lot_size: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """
        Place a market order via DWX. Matches MetaApiExecutor.execute_trade() signature.

        Returns:
            {'status': 'filled'|'failed', 'orderId': str, 'position_id': str,
             'price': float, 'error': str}
        """
        if not self.connect():
            return {
                "status": "failed",
                "error": "dwx_not_connected",
                "symbol": symbol,
                "account_id": self._profile_id,
            }

        order_direction = str(direction or "").upper().strip()
        if order_direction not in ("LONG", "SHORT"):
            return {
                "status": "failed",
                "error": f"unsupported direction={direction!r}",
                "symbol": symbol,
            }

        requested_lots = float(lot_size or 0.0)
        lots = requested_lots
        if lots > self._max_lots_safety:
            logger.warning(
                "[DWX_EXEC] profile=%s %s %s lots=%.5f > safety cap %.2f — capping",
                self._profile_id, symbol, direction, lots, self._max_lots_safety,
            )
            lots = self._max_lots_safety

        if lots <= 0:
            return {
                "status": "failed",
                "error": "lot_size=0",
                "symbol": symbol,
            }

        side = "buy" if order_direction == "LONG" else "sell"
        sl   = float(stop_loss or 0.0)
        tp   = float(take_profit or 0.0)

        logger.info(
            "[DWX_EXEC] profile=%s submitting %s %s lots=%.5f sl=%.5f tp=%.5f",
            self._profile_id, symbol, order_direction, lots, sl, tp,
        )

        ticket = self._mt5.open_order(
            symbol=symbol,
            side=side,
            lots=lots,
            price=0,        # market order
            sl=sl,
            tp=tp,
            comment=f"VG_{self._profile_id}_{symbol}_{order_direction}",
        )

        if not ticket:
            bridge_error = self._bridge_error_detail()
            return {
                "status": "failed",
                "error": bridge_error or "open_order returned empty ticket",
                "symbol": symbol,
                "account_id": self._profile_id,
                "requested_lots": requested_lots,
                "submitted_lots": lots,
            }

        # Resolve fill price from open_orders snapshot
        fill_price = 0.0
        positions = self._mt5.get_open_positions()
        if ticket in positions:
            fill_price = float(positions[ticket].get("open_price") or 0)

        logger.info(
            "[DWX_EXEC] profile=%s %s %s FILLED ticket=%s fill_price=%.5f",
            self._profile_id, symbol, order_direction, ticket, fill_price,
        )
        return {
            "status":      "filled",
            "orderId":     ticket,
            "order_id":    ticket,
            "position_id": ticket,
            "id":          ticket,
            "price":       fill_price,
            "error":       "",
            "symbol":      symbol,
            "account_id":  self._profile_id,
            "requested_lots": requested_lots,
            "submitted_lots": lots,
        }

    def close_position(
        self,
        position_id: str,
        symbol: str = "",
        reason: str = "ORCHESTRATOR_EXIT",
    ) -> dict[str, Any]:
        """
        Close an open position by ticket. Matches MetaApiExecutor.close_position() signature.
        """
        if not self.connect():
            return {
                "status": "failed",
                "error": "dwx_not_connected",
                "position_id": position_id,
            }

        success = self._mt5.close_order(str(position_id))
        if success:
            logger.info(
                "[DWX_EXEC] profile=%s closed ticket=%s symbol=%s reason=%s",
                self._profile_id, position_id, symbol, reason,
            )
        else:
            logger.warning(
                "[DWX_EXEC] profile=%s close FAILED ticket=%s",
                self._profile_id, position_id,
            )
        return {
            "status":      "closed" if success else "failed",
            "position_id": position_id,
            "symbol":      symbol,
            "error":       "" if success else (self._bridge_error_detail() or "close_order returned False"),
        }

    def modify_position(
        self,
        ticket: str,
        sl: float = 0,
        tp: float = 0,
    ) -> dict[str, Any]:
        """Modify SL/TP on an existing position."""
        if not self.connect():
            return {
                "status": "failed",
                "error": "dwx_not_connected",
                "ticket": ticket,
            }

        success = self._mt5.modify_order(str(ticket), sl=sl, tp=tp)
        logger.info(
            "[DWX_EXEC] profile=%s modify ticket=%s sl=%.5f tp=%.5f success=%s",
            self._profile_id, ticket, sl, tp, success,
        )
        return {
            "status": "modified" if success else "failed",
            "ticket": ticket,
            "sl":     sl,
            "tp":     tp,
            "error":  "" if success else (self._bridge_error_detail() or "modify_order returned False"),
        }

    def _bridge_error_detail(self) -> str:
        message = ""
        if hasattr(self._mt5, "get_last_bridge_message"):
            try:
                message = str(self._mt5.get_last_bridge_message() or "").strip()
            except Exception:
                message = ""
        if not message:
            return ""
        return f"dwx_bridge:{message}"

    def _load_max_lots_safety(self) -> float:
        try:
            runtime_path = Path(self._db_path).resolve().parents[1] / "config" / "vanguard_runtime.json"
            payload = json.loads(runtime_path.read_text())
            bridge_id = f"mt5_local_{self._profile_id}"
            bridge = ((payload.get("execution") or {}).get("bridges") or {}).get(bridge_id) or {}
            configured = bridge.get("max_lots_safety")
            if configured is not None:
                return max(0.0, float(configured))
        except Exception:
            pass
        return MAX_LOTS_SAFETY
