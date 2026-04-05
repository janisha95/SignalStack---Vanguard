# STAGE V7.1: Executor Bridge — Build First

**Status:** APPROVED — BUILD NOW
**Project:** Vanguard
**Date:** Mar 29, 2026
**Extracted from:** VANGUARD_STAGE_V7_SPEC.md
**Purpose:** Place Meridian + S1 daily picks on TTP $1M demo via SignalStack

---

## READ ORDER FOR BUILD AGENT

1. `AGENTS.md` — project overview, principles, repo structure
2. `ROADMAP.md` — current phase (Phase 0), build order
3. This file (`VANGUARD_STAGE_V7_1_EXECUTOR_SPEC.md`) — what to build
4. `VANGUARD_SUPPORTING_SPECS.md` — DB schema (vanguard_execution_log table)

---

## What V7.1 Does

Three modules + one script:

1. **signalstack_adapter.py** — HTTP POST to SignalStack webhook URL
2. **telegram_alerts.py** — Send trade alerts via Telegram bot
3. **bridge.py** — Universal dispatcher: takes a trade object, routes to correct adapter
4. **execute_daily_picks.py** — Reads Meridian/S1 shortlists, converts to trade objects, feeds to bridge

The executor is SYSTEM-AGNOSTIC. It takes a universal trade object and
doesn't care if the pick came from Meridian, S1, or Vanguard.

---

## Universal Trade Object

```python
@dataclass
class TradeOrder:
    symbol: str                    # e.g. "AAPL"
    direction: str                 # "LONG" or "SHORT"
    shares: int                    # number of shares
    order_type: str = "market"     # "market", "limit", "stop", "stop_limit"
    limit_price: float = None      # for limit orders
    stop_price: float = None       # for stop orders
    source_system: str = ""        # "meridian", "s1", "vanguard"
    account_id: str = ""           # "ttp_demo_1m"
    execution_mode: str = "paper"  # "off", "paper", "live"
```

---

## Module 1: signalstack_adapter.py

Location: `~/SS/Vanguard/vanguard/execution/signalstack_adapter.py`

### SignalStack Webhook Payload Format

```python
import requests
import json
import time
import logging

logger = logging.getLogger(__name__)

class SignalStackAdapter:
    """
    HTTP POST client for SignalStack webhook.
    Sends JSON payloads to execute trades on TTP via Trader Evolution.
    """

    def __init__(self, webhook_url: str, timeout: int = 5, max_retries: int = 2,
                 retry_delay: float = 1.0, log_payloads: bool = True):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.log_payloads = log_payloads

    def _direction_to_action(self, direction: str, operation: str) -> str:
        """
        Map direction + operation to SignalStack action.

        Actions:
          buy          — open long
          sell         — close long
          sell_short   — open short
          buy_to_cover — close short
          close        — close any position (direction-agnostic)
        """
        if operation == "open":
            return "buy" if direction == "LONG" else "sell_short"
        elif operation == "close":
            return "sell" if direction == "LONG" else "buy_to_cover"
        elif operation == "flatten":
            return "close"  # direction-agnostic
        else:
            raise ValueError(f"Unknown operation: {operation}")

    def build_payload(self, symbol: str, action: str, quantity: int,
                      limit_price: float = None, stop_price: float = None) -> dict:
        """Build SignalStack JSON payload."""
        payload = {
            "symbol": symbol,
            "action": action,
            "quantity": quantity
        }
        if limit_price is not None:
            payload["limit_price"] = limit_price
        if stop_price is not None:
            payload["stop_price"] = stop_price
        return payload

    def send_order(self, symbol: str, direction: str, quantity: int,
                   operation: str = "open", limit_price: float = None,
                   stop_price: float = None) -> dict:
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
        action = self._direction_to_action(direction, operation)
        payload = self.build_payload(symbol, action, quantity, limit_price, stop_price)

        if self.log_payloads:
            logger.info(f"SignalStack payload: {json.dumps(payload)}")

        for attempt in range(self.max_retries + 1):
            try:
                start = time.time()
                response = requests.post(
                    self.webhook_url,
                    json=payload,
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json"}
                )
                latency_ms = int((time.time() - start) * 1000)

                result = {
                    "success": response.status_code in (200, 201, 202),
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "latency_ms": latency_ms,
                    "payload": payload,
                    "error": None if response.status_code in (200, 201, 202)
                             else f"HTTP {response.status_code}: {response.text[:200]}"
                }

                if result["success"]:
                    logger.info(f"SignalStack OK: {symbol} {action} {quantity} ({latency_ms}ms)")
                    return result
                else:
                    logger.warning(f"SignalStack FAIL attempt {attempt+1}: {result['error']}")

            except requests.exceptions.Timeout:
                logger.warning(f"SignalStack TIMEOUT attempt {attempt+1}: {symbol}")
                result = {
                    "success": False, "status_code": 0, "response_body": "",
                    "latency_ms": self.timeout * 1000, "payload": payload,
                    "error": f"Timeout after {self.timeout}s"
                }
            except requests.exceptions.RequestException as e:
                logger.error(f"SignalStack ERROR attempt {attempt+1}: {e}")
                result = {
                    "success": False, "status_code": 0, "response_body": "",
                    "latency_ms": 0, "payload": payload,
                    "error": str(e)
                }

            if attempt < self.max_retries:
                time.sleep(self.retry_delay)

        return result

    def flatten_position(self, symbol: str, quantity: int) -> dict:
        """Close any position — direction-agnostic."""
        return self.send_order(symbol, "LONG", quantity, operation="flatten")
```

---

## Module 2: telegram_alerts.py

Location: `~/SS/Vanguard/vanguard/execution/telegram_alerts.py`

```python
import requests
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class TelegramAlerts:
    """Send trade alerts and reports via Telegram bot."""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            return True
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode
                },
                timeout=10
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    def alert_trade_executed(self, symbol, direction, shares, price, source, account):
        emoji = "🟢" if direction == "LONG" else "🔴"
        msg = (
            f"{emoji} <b>TRADE EXECUTED</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Direction: {direction}\n"
            f"Shares: {shares}\n"
            f"Price: ${price or 'MKT'}\n"
            f"Source: {source}\n"
            f"Account: {account}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_trade_failed(self, symbol, direction, error, source):
        msg = (
            f"❌ <b>TRADE FAILED</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Direction: {direction}\n"
            f"Error: {error}\n"
            f"Source: {source}"
        )
        return self.send(msg)

    def alert_eod_flatten(self, positions_closed, account):
        msg = (
            f"🏁 <b>EOD FLATTEN</b>\n"
            f"Account: {account}\n"
            f"Positions closed: {positions_closed}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_daily_summary(self, account, trades, pnl, win_rate):
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"{emoji} <b>DAILY SUMMARY</b>\n"
            f"Account: {account}\n"
            f"Trades: {trades}\n"
            f"P&L: ${pnl:+.2f}\n"
            f"Win Rate: {win_rate:.1%}\n"
            f"Date: {datetime.now().strftime('%Y-%m-%d')}"
        )
        return self.send(msg)

    def alert_picks(self, longs, shorts, source):
        """Send pick summary (when execution is OFF)."""
        long_list = "\n".join([f"  • {s['symbol']} (edge: {s.get('edge', 'N/A')})" for s in longs[:5]])
        short_list = "\n".join([f"  • {s['symbol']} (edge: {s.get('edge', 'N/A')})" for s in shorts[:5]])
        msg = (
            f"📋 <b>PICKS ({source.upper()})</b>\n\n"
            f"<b>LONG:</b>\n{long_list}\n\n"
            f"<b>SHORT:</b>\n{short_list}\n\n"
            f"Time: {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_system_error(self, stage, error):
        msg = (
            f"🚨 <b>SYSTEM ERROR</b>\n"
            f"Stage: {stage}\n"
            f"Error: {error}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)
```

---

## Module 3: bridge.py

Location: `~/SS/Vanguard/vanguard/execution/bridge.py`

```python
import sqlite3
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class TradeOrder:
    symbol: str
    direction: str              # "LONG" or "SHORT"
    shares: int
    order_type: str = "market"  # "market", "limit", "stop", "stop_limit"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    source_system: str = ""     # "meridian", "s1", "vanguard"
    account_id: str = ""
    execution_mode: str = "paper"

class ExecutionBridge:
    """
    Universal execution dispatcher.
    Routes TradeOrder objects to the correct adapter (SignalStack, MT5).
    Logs all executions to DB.
    """

    def __init__(self, db_path: str, signalstack=None, telegram=None):
        self.db_path = db_path
        self.signalstack = signalstack
        self.telegram = telegram
        self._init_db()

    def _init_db(self):
        """Create execution log table if not exists."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_execution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_ts_utc TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    action TEXT NOT NULL,
                    shares_or_lots REAL,
                    order_type TEXT,
                    limit_price REAL,
                    stop_price REAL,
                    fill_price REAL,
                    fill_quantity REAL,
                    execution_bridge TEXT,
                    bridge_order_id TEXT,
                    status TEXT,
                    error_message TEXT,
                    latency_ms INTEGER,
                    source_system TEXT,
                    created_at_utc TEXT DEFAULT (datetime('now'))
                )
            """)

    def execute(self, order: TradeOrder) -> dict:
        """
        Execute a single trade order.
        Routes to correct adapter based on account config.
        """
        cycle_ts = datetime.now(timezone.utc).isoformat()

        if order.execution_mode == "off":
            logger.info(f"FORWARD TRACK: {order.symbol} {order.direction} {order.shares} ({order.source_system})")
            result = {"success": True, "status_code": 0, "latency_ms": 0,
                      "error": None, "payload": asdict(order)}
            self._log_execution(cycle_ts, order, result, bridge="none", status="FORWARD_TRACKED")
            return result

        # Route to SignalStack
        if self.signalstack:
            result = self.signalstack.send_order(
                symbol=order.symbol,
                direction=order.direction,
                quantity=order.shares,
                operation="open",
                limit_price=order.limit_price,
                stop_price=order.stop_price
            )

            status = "FILLED" if result["success"] else "FAILED"
            self._log_execution(cycle_ts, order, result, bridge="signalstack", status=status)

            # Telegram alert
            if self.telegram:
                if result["success"]:
                    self.telegram.alert_trade_executed(
                        order.symbol, order.direction, order.shares,
                        order.limit_price, order.source_system, order.account_id
                    )
                else:
                    self.telegram.alert_trade_failed(
                        order.symbol, order.direction,
                        result.get("error", "Unknown"), order.source_system
                    )

            return result

        logger.error(f"No adapter configured for order: {order}")
        return {"success": False, "error": "No execution adapter configured"}

    def execute_batch(self, orders: list[TradeOrder]) -> list[dict]:
        """Execute a batch of orders sequentially."""
        results = []
        for order in orders:
            result = self.execute(order)
            results.append(result)
        return results

    def flatten_position(self, symbol: str, quantity: int, account_id: str) -> dict:
        """Close a position — direction-agnostic."""
        cycle_ts = datetime.now(timezone.utc).isoformat()
        if self.signalstack:
            result = self.signalstack.flatten_position(symbol, quantity)
            status = "FLATTENED" if result["success"] else "FLATTEN_FAILED"
            order = TradeOrder(symbol=symbol, direction="FLAT", shares=quantity,
                              account_id=account_id, source_system="eod_flatten")
            self._log_execution(cycle_ts, order, result, bridge="signalstack", status=status)
            return result
        return {"success": False, "error": "No adapter"}

    def _log_execution(self, cycle_ts, order, result, bridge, status):
        """Write execution result to DB."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO vanguard_execution_log
                    (cycle_ts_utc, account_id, symbol, direction, action,
                     shares_or_lots, order_type, limit_price, stop_price,
                     fill_price, fill_quantity, execution_bridge,
                     status, error_message, latency_ms, source_system)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cycle_ts, order.account_id, order.symbol, order.direction,
                    result.get("payload", {}).get("action", ""),
                    order.shares, order.order_type, order.limit_price, order.stop_price,
                    result.get("fill_price"), result.get("fill_quantity"),
                    bridge, status, result.get("error"),
                    result.get("latency_ms", 0), order.source_system
                ))
        except Exception as e:
            logger.error(f"Failed to log execution: {e}")
```

---

## Script: execute_daily_picks.py

Location: `~/SS/Vanguard/scripts/execute_daily_picks.py`

```python
"""
Execute daily picks from Meridian or S1 on TTP demo via SignalStack.

Usage:
  python3 scripts/execute_daily_picks.py --source meridian --account ttp_demo_1m
  python3 scripts/execute_daily_picks.py --source s1 --account ttp_demo_1m
  python3 scripts/execute_daily_picks.py --source meridian --dry-run
  python3 scripts/execute_daily_picks.py --source both --account ttp_demo_1m --max-trades 25
"""

# This script:
# 1. Reads the latest shortlist from Meridian (v2_universe.db) or S1
# 2. Converts each pick to a TradeOrder
# 3. Feeds to ExecutionBridge
# 4. Logs results + sends Telegram summary
#
# CLI args:
#   --source: "meridian", "s1", or "both"
#   --account: account ID from vanguard_accounts.json
#   --max-trades: max trades to execute (default 25)
#   --max-per-side: max longs or shorts (default 15)
#   --shares-per-trade: fixed shares per trade (default 100)
#   --dry-run: log but don't execute
#   --execution-mode: "off", "paper", "live" (default "paper")
#
# Meridian shortlist location:
#   ~/SS/Meridian/data/v2_universe.db
#   Table: v2_shortlist (or wherever Meridian writes final picks)
#   Columns: symbol, direction, rank, edge_score, ml_prob, etc.
#
# S1 shortlist location:
#   ~/SS/Advance/data/s1_universe.db (or equivalent)
#   Table depends on S1's output format
#
# The script reads from these DBs READ-ONLY. All writes go to
# ~/SS/Vanguard/data/vanguard_universe.db (vanguard_execution_log table).
```

---

## Config Files Needed

### config/vanguard_execution_config.json

```json
{
    "signalstack": {
        "webhook_url": "ENV:SIGNALSTACK_WEBHOOK_URL",
        "timeout_seconds": 5,
        "max_retries": 2,
        "retry_delay_seconds": 1.0,
        "order_type": "market",
        "log_payloads": true
    },
    "telegram": {
        "bot_token": "ENV:TELEGRAM_BOT_TOKEN",
        "chat_id": "ENV:TELEGRAM_CHAT_ID",
        "enabled": true
    },
    "defaults": {
        "execution_mode": "paper",
        "shares_per_trade": 100,
        "max_trades_per_run": 25,
        "max_per_side": 15
    }
}
```

### config/vanguard_accounts.json (minimal for Phase 0)

```json
{
    "accounts": [
        {
            "account_id": "ttp_demo_1m",
            "enabled": true,
            "prop_firm": "ttp",
            "account_type": "demo",
            "account_balance": 1000000,
            "currency": "USD",
            "execution_mode": "paper",
            "execution_bridge": "signalstack"
        }
    ]
}
```

---

## File Structure (Phase 0 Only)

```
~/SS/Vanguard/
├── AGENTS.md
├── ROADMAP.md
├── docs/
│   ├── VANGUARD_STAGE_V7_1_EXECUTOR_SPEC.md   # This file
│   └── VANGUARD_SUPPORTING_SPECS.md
├── vanguard/
│   ├── __init__.py
│   └── execution/
│       ├── __init__.py
│       ├── bridge.py                   # Execution dispatcher
│       ├── signalstack_adapter.py      # SignalStack webhook client
│       └── telegram_alerts.py          # Telegram bot notifications
├── scripts/
│   └── execute_daily_picks.py          # Meridian/S1 → executor
├── config/
│   ├── vanguard_accounts.json
│   └── vanguard_execution_config.json
├── data/
│   └── vanguard_universe.db            # execution_log table
└── tests/
    ├── test_signalstack_adapter.py
    └── test_telegram_alerts.py
```

---

## Acceptance Criteria

- [ ] `vanguard/execution/signalstack_adapter.py` exists and runs
- [ ] SignalStack webhook payloads match format: `{symbol, action, quantity}`
- [ ] Actions: buy, sell, sell_short, buy_to_cover, close
- [ ] Retry logic: 2 retries with 1s delay
- [ ] Response logging with latency_ms
- [ ] `vanguard/execution/telegram_alerts.py` exists and runs
- [ ] Telegram alerts: trade executed, trade failed, EOD flatten, daily summary, picks, system error
- [ ] `vanguard/execution/bridge.py` exists and runs
- [ ] Universal TradeOrder dataclass
- [ ] Routes to SignalStack adapter
- [ ] Logs all executions to vanguard_execution_log table
- [ ] Execution modes: off (forward track), paper, live
- [ ] `scripts/execute_daily_picks.py` exists and runs
- [ ] Reads Meridian shortlist from v2_universe.db (READ ONLY)
- [ ] Converts picks to TradeOrder objects
- [ ] Feeds to ExecutionBridge
- [ ] CLI: --source, --account, --max-trades, --dry-run, --execution-mode
- [ ] All writes go to vanguard_universe.db ONLY
- [ ] Config loaded from JSON files + environment variables
- [ ] No hardcoded webhook URLs, tokens, or paths
- [ ] Tests exist for signalstack_adapter and telegram_alerts
