"""
bridge.py — Universal execution dispatcher for Vanguard.

Takes TradeOrder objects, routes to the correct adapter (SignalStack, MT5),
and logs all executions to vanguard_execution_log in vanguard_universe.db.

DEPRECATED: vanguard_execution_log is the legacy execution table used by this
bridge path. The canonical execution table is execution_log, written directly
by vanguard_orchestrator.py. New code should NOT read from vanguard_execution_log
for live portfolio state.

Location: ~/SS/Vanguard/vanguard/execution/bridge.py
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeOrder:
    """Universal trade object — system-agnostic."""
    symbol: str
    direction: str                          # "LONG" or "SHORT"
    shares: int
    order_type: str = "market"              # "market", "limit", "stop", "stop_limit"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    source_system: str = ""                 # "meridian", "s1", "vanguard"
    account_id: str = ""
    execution_mode: str = "paper"           # "off", "paper", "live"
    signal_tier: str = ""                   # e.g. "tier_dual", "tier_meridian_long"
    signal_metadata: dict = field(default_factory=dict)  # tier-specific scores


class ExecutionBridge:
    """
    Universal execution dispatcher.
    Routes TradeOrder objects to the correct adapter (SignalStack, MT5).
    Logs all executions to DB.
    """

    def __init__(
        self,
        db_path: str,
        signalstack=None,
        telegram=None,
    ):
        assert "vanguard_universe" in db_path, (
            f"DB isolation violation: {db_path} is not the Vanguard DB"
        )
        self.db_path = db_path
        self.signalstack = signalstack
        self.telegram = telegram
        self._init_db()

    def _init_db(self) -> None:
        """Create execution log table and migrate new columns if needed."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_execution_log (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_ts_utc      TEXT NOT NULL,
                    account_id        TEXT NOT NULL,
                    symbol            TEXT NOT NULL,
                    direction         TEXT NOT NULL,
                    action            TEXT NOT NULL,
                    shares_or_lots    REAL,
                    order_type        TEXT,
                    limit_price       REAL,
                    stop_price        REAL,
                    fill_price        REAL,
                    fill_quantity     REAL,
                    execution_bridge  TEXT,
                    bridge_order_id   TEXT,
                    status            TEXT,
                    error_message     TEXT,
                    latency_ms        INTEGER,
                    source_system     TEXT,
                    signal_tier       TEXT,
                    signal_metadata   TEXT,
                    created_at_utc    TEXT DEFAULT (datetime('now'))
                )
            """)
            # Add new columns to existing tables (safe no-op if columns already exist)
            for col, typedef in (
                ("signal_tier",     "TEXT"),
                ("signal_metadata", "TEXT"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE vanguard_execution_log ADD COLUMN {col} {typedef};"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()

    def execute(self, order: TradeOrder) -> dict:
        """
        Execute a single trade order.
        Routes to correct adapter based on execution_mode and configured adapters.

        execution_mode:
          "off"   — forward tracking only, no real order sent
          "paper" — send to SignalStack (paper account)
          "live"  — send to SignalStack (live account)
        """
        cycle_ts = datetime.now(timezone.utc).isoformat()

        if order.execution_mode == "off":
            logger.info(
                f"FORWARD TRACK: {order.symbol} {order.direction} {order.shares} "
                f"({order.source_system}/{order.signal_tier})"
            )
            result = {
                "success": True,
                "status_code": 0,
                "latency_ms": 0,
                "error": None,
                "payload": asdict(order),
            }
            self._log_execution(cycle_ts, order, result, bridge="none", status="FORWARD_TRACKED")
            return result

        # Route to SignalStack (paper or live)
        if self.signalstack:
            result = self.signalstack.send_order(
                symbol=order.symbol,
                direction=order.direction,
                quantity=order.shares,
                operation="open",
                limit_price=order.limit_price,
                stop_price=order.stop_price,
            )

            status = "FILLED" if result["success"] else "FAILED"
            self._log_execution(cycle_ts, order, result, bridge="signalstack", status=status)

            if self.telegram:
                if result["success"]:
                    self.telegram.alert_trade_executed(
                        order.symbol,
                        order.direction,
                        order.shares,
                        order.limit_price,
                        order.source_system,
                        order.account_id,
                    )
                else:
                    self.telegram.alert_trade_failed(
                        order.symbol,
                        order.direction,
                        result.get("error", "Unknown"),
                        order.source_system,
                    )

            return result

        err_msg = "No execution adapter configured"
        logger.error(f"{err_msg} for order: {order}")
        return {"success": False, "status_code": 0, "latency_ms": 0, "error": err_msg}

    def execute_batch(self, orders: list[TradeOrder]) -> list[dict]:
        """Execute a batch of orders sequentially."""
        results = []
        for order in orders:
            result = self.execute(order)
            results.append(result)
        return results

    def flatten_position(self, symbol: str, quantity: int, account_id: str) -> dict:
        """Close a position — direction-agnostic (sends SignalStack 'close' action)."""
        cycle_ts = datetime.now(timezone.utc).isoformat()
        if self.signalstack:
            result = self.signalstack.flatten_position(symbol, quantity)
            status = "FLATTENED" if result["success"] else "FLATTEN_FAILED"
            order = TradeOrder(
                symbol=symbol,
                direction="FLAT",
                shares=quantity,
                account_id=account_id,
                source_system="eod_flatten",
                execution_mode="paper",
                signal_tier="eod_flatten",
            )
            self._log_execution(cycle_ts, order, result, bridge="signalstack", status=status)
            return result
        return {"success": False, "status_code": 0, "latency_ms": 0, "error": "No adapter"}

    def _log_execution(
        self,
        cycle_ts: str,
        order: TradeOrder,
        result: dict,
        bridge: str,
        status: str,
    ) -> None:
        """Write execution result to vanguard_execution_log."""
        try:
            payload = result.get("payload", {})
            action = payload.get("action") or payload.get("direction") or ""
            metadata_json = json.dumps(order.signal_metadata) if order.signal_metadata else None
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO vanguard_execution_log
                    (cycle_ts_utc, account_id, symbol, direction, action,
                     shares_or_lots, order_type, limit_price, stop_price,
                     fill_price, fill_quantity, execution_bridge,
                     status, error_message, latency_ms, source_system,
                     signal_tier, signal_metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cycle_ts,
                    order.account_id,
                    order.symbol,
                    order.direction,
                    action,
                    order.shares,
                    order.order_type,
                    order.limit_price,
                    order.stop_price,
                    result.get("fill_price"),
                    result.get("fill_quantity"),
                    bridge,
                    status,
                    result.get("error"),
                    result.get("latency_ms", 0),
                    order.source_system,
                    order.signal_tier,
                    metadata_json,
                ))
                conn.commit()
        except Exception as exc:
            logger.error(f"Failed to log execution to DB: {exc}")
