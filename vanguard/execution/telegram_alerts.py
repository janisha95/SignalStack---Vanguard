"""
telegram_alerts.py — Telegram bot alerts for Vanguard trade execution.

Location: ~/SS/Vanguard/vanguard/execution/telegram_alerts.py
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TelegramAlerts:
    """Send trade alerts and reports via Telegram bot."""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a plain text or HTML message. Returns True on success."""
        if not self.enabled:
            return True
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                },
                timeout=10,
            )
            if response.status_code != 200:
                logger.error(
                    f"Telegram send failed: HTTP {response.status_code} — {response.text[:200]}"
                )
                return False
            return True
        except Exception as exc:
            logger.error(f"Telegram send failed: {exc}")
            return False

    def alert_trade_executed(
        self,
        symbol: str,
        direction: str,
        shares: int,
        price: Optional[float],
        source: str,
        account: str,
    ) -> bool:
        emoji = "🟢" if direction.upper() == "LONG" else "🔴"
        price_str = f"${price:.2f}" if price else "MKT"
        msg = (
            f"{emoji} <b>TRADE EXECUTED</b>\n"
            f"Symbol:    <code>{symbol}</code>\n"
            f"Direction: {direction}\n"
            f"Shares:    {shares:,}\n"
            f"Price:     {price_str}\n"
            f"Source:    {source}\n"
            f"Account:   {account}\n"
            f"Time:      {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_trade_failed(
        self,
        symbol: str,
        direction: str,
        error: str,
        source: str,
    ) -> bool:
        msg = (
            f"❌ <b>TRADE FAILED</b>\n"
            f"Symbol:    <code>{symbol}</code>\n"
            f"Direction: {direction}\n"
            f"Error:     {error}\n"
            f"Source:    {source}\n"
            f"Time:      {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_eod_flatten(self, positions_closed: int, account: str) -> bool:
        msg = (
            f"🏁 <b>EOD FLATTEN</b>\n"
            f"Account:           {account}\n"
            f"Positions closed:  {positions_closed}\n"
            f"Time:              {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_daily_summary(
        self,
        account: str,
        trades: int,
        pnl: float,
        win_rate: float,
    ) -> bool:
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"{emoji} <b>DAILY SUMMARY</b>\n"
            f"Account:   {account}\n"
            f"Trades:    {trades}\n"
            f"P&L:       ${pnl:+.2f}\n"
            f"Win Rate:  {win_rate:.1%}\n"
            f"Date:      {datetime.now().strftime('%Y-%m-%d')}"
        )
        return self.send(msg)

    def alert_picks(
        self,
        longs: list[dict],
        shorts: list[dict],
        source: str,
    ) -> bool:
        """Send pick summary (used when execution_mode is 'off')."""
        long_lines = "\n".join(
            f"  • {s['symbol']} (edge: {s.get('edge', s.get('final_score', 'N/A'))})"
            for s in longs[:5]
        )
        short_lines = "\n".join(
            f"  • {s['symbol']} (edge: {s.get('edge', s.get('final_score', 'N/A'))})"
            for s in shorts[:5]
        )
        long_section = long_lines if long_lines else "  (none)"
        short_section = short_lines if short_lines else "  (none)"
        msg = (
            f"📋 <b>PICKS ({source.upper()})</b>\n\n"
            f"<b>LONG ({len(longs)}):</b>\n{long_section}\n\n"
            f"<b>SHORT ({len(shorts)}):</b>\n{short_section}\n\n"
            f"Time: {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_execution_summary(
        self,
        source: str,
        account: str,
        submitted: int,
        filled: int,
        failed: int,
        forward_tracked: int,
        execution_mode: str,
    ) -> bool:
        """Send batch execution summary after execute_daily_picks run."""
        emoji = "✅" if failed == 0 else "⚠️"
        mode_tag = f" [{execution_mode.upper()}]" if execution_mode != "paper" else ""
        msg = (
            f"{emoji} <b>EXECUTION RUN{mode_tag}</b>\n"
            f"Source:           {source.upper()}\n"
            f"Account:          {account}\n"
            f"Submitted:        {submitted}\n"
            f"Filled:           {filled}\n"
            f"Failed:           {failed}\n"
            f"Forward tracked:  {forward_tracked}\n"
            f"Time:             {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)

    def alert_tier_summary(
        self,
        tier_orders: dict,
        account: str,
        execution_mode: str,
    ) -> bool:
        """
        Send per-tier execution summary.

        tier_orders: dict mapping tier_name → list of TradeOrder objects
        """
        TIER_LABELS = {
            "tier_dual":           "Tier Dual (S1)",
            "tier_nn":             "Tier NN (S1)",
            "tier_rf":             "Tier RF (S1)",
            "tier_scorer_long":    "ML Scorer Longs (S1)",
            "tier_s1_short":       "S1 Shorts",
            "tier_meridian_long":  "Meridian Longs",
            "tier_meridian_short": "Meridian Shorts",
        }
        mode_tag = f" [{execution_mode.upper()}]" if execution_mode != "paper" else ""
        lines = [f"📊 <b>EXECUTION SUMMARY{mode_tag}</b>\n"]

        total = 0
        for tier_name, orders in tier_orders.items():
            if not orders:
                continue
            label = TIER_LABELS.get(tier_name, tier_name)
            direction = orders[0].direction if orders else ""
            symbols = ", ".join(o.symbol for o in orders)
            lines.append(f"<b>{label}:</b> {len(orders)} trades")
            lines.append(f"  {direction}: {symbols}")
            total += len(orders)

        if total == 0:
            lines.append("No trades placed.")
        else:
            lines.append(f"\n<b>Total: {total} trades | Account: {account}</b>")

        lines.append(f"Time: {datetime.now().strftime('%H:%M:%S ET')}")
        return self.send("\n".join(lines))

    def alert_system_error(self, stage: str, error: str) -> bool:
        msg = (
            f"🚨 <b>SYSTEM ERROR</b>\n"
            f"Stage:  {stage}\n"
            f"Error:  {error}\n"
            f"Time:   {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send(msg)
