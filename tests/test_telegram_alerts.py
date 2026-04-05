"""
test_telegram_alerts.py — Unit tests for TelegramAlerts.

Run: python3 -m pytest tests/test_telegram_alerts.py -v
  or: python3 tests/test_telegram_alerts.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.execution.telegram_alerts import TelegramAlerts


class TestTelegramSend(unittest.TestCase):
    def setUp(self):
        self.tg = TelegramAlerts(
            bot_token="fake_token",
            chat_id="123456789",
            enabled=True,
        )

    @patch("vanguard.execution.telegram_alerts.requests.post")
    def test_send_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        result = self.tg.send("hello world")

        self.assertTrue(result)
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
        self.assertEqual(payload["chat_id"], "123456789")
        self.assertEqual(payload["text"], "hello world")

    @patch("vanguard.execution.telegram_alerts.requests.post")
    def test_send_http_error_returns_false(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        result = self.tg.send("test")
        self.assertFalse(result)

    @patch("vanguard.execution.telegram_alerts.requests.post")
    def test_send_exception_returns_false(self, mock_post):
        mock_post.side_effect = Exception("network error")

        result = self.tg.send("test")
        self.assertFalse(result)


class TestTelegramDisabled(unittest.TestCase):
    def setUp(self):
        self.tg = TelegramAlerts(
            bot_token="fake_token",
            chat_id="123456789",
            enabled=False,
        )

    @patch("vanguard.execution.telegram_alerts.requests.post")
    def test_disabled_no_http_call(self, mock_post):
        result = self.tg.send("should not be sent")

        self.assertTrue(result)  # returns True silently
        mock_post.assert_not_called()

    @patch("vanguard.execution.telegram_alerts.requests.post")
    def test_disabled_alert_no_http_call(self, mock_post):
        self.tg.alert_trade_executed("AAPL", "LONG", 100, None, "meridian", "ttp_demo_1m")
        mock_post.assert_not_called()


class TestAlertMessages(unittest.TestCase):
    """Test that alert methods produce correctly formatted messages and call send()."""

    def setUp(self):
        self.tg = TelegramAlerts(
            bot_token="fake_token",
            chat_id="123456789",
            enabled=True,
        )

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_trade_executed_long(self, mock_send):
        self.tg.alert_trade_executed("AAPL", "LONG", 100, 185.50, "meridian", "ttp_demo_1m")

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        self.assertIn("🟢", msg)
        self.assertIn("AAPL", msg)
        self.assertIn("LONG", msg)
        self.assertIn("100", msg)
        self.assertIn("meridian", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_trade_executed_short(self, mock_send):
        self.tg.alert_trade_executed("TSLA", "SHORT", 50, None, "s1", "ttp_demo_1m")

        msg = mock_send.call_args[0][0]
        self.assertIn("🔴", msg)
        self.assertIn("TSLA", msg)
        self.assertIn("SHORT", msg)
        self.assertIn("MKT", msg)  # price=None → "MKT"

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_trade_failed(self, mock_send):
        self.tg.alert_trade_failed("NVDA", "LONG", "HTTP 500: server error", "vanguard")

        msg = mock_send.call_args[0][0]
        self.assertIn("❌", msg)
        self.assertIn("NVDA", msg)
        self.assertIn("HTTP 500", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_eod_flatten(self, mock_send):
        self.tg.alert_eod_flatten(positions_closed=12, account="ttp_demo_1m")

        msg = mock_send.call_args[0][0]
        self.assertIn("🏁", msg)
        self.assertIn("12", msg)
        self.assertIn("ttp_demo_1m", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_daily_summary_profit(self, mock_send):
        self.tg.alert_daily_summary("ttp_demo_1m", trades=20, pnl=1250.75, win_rate=0.65)

        msg = mock_send.call_args[0][0]
        self.assertIn("📈", msg)
        self.assertIn("+1250.75", msg)
        self.assertIn("65.0%", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_daily_summary_loss(self, mock_send):
        self.tg.alert_daily_summary("ttp_demo_1m", trades=10, pnl=-500.00, win_rate=0.30)

        msg = mock_send.call_args[0][0]
        self.assertIn("📉", msg)
        self.assertIn("-500.00", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_picks_shows_longs_and_shorts(self, mock_send):
        longs  = [{"symbol": "AAPL", "edge": 0.85}, {"symbol": "MSFT", "edge": 0.80}]
        shorts = [{"symbol": "TSLA", "edge": 0.70}]
        self.tg.alert_picks(longs, shorts, "meridian")

        msg = mock_send.call_args[0][0]
        self.assertIn("MERIDIAN", msg)
        self.assertIn("AAPL", msg)
        self.assertIn("TSLA", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_picks_empty(self, mock_send):
        self.tg.alert_picks([], [], "s1")

        msg = mock_send.call_args[0][0]
        self.assertIn("(none)", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_system_error(self, mock_send):
        self.tg.alert_system_error("execute_daily_picks", "DB connection failed")

        msg = mock_send.call_args[0][0]
        self.assertIn("🚨", msg)
        self.assertIn("execute_daily_picks", msg)
        self.assertIn("DB connection failed", msg)

    @patch.object(TelegramAlerts, "send", return_value=True)
    def test_alert_execution_summary(self, mock_send):
        self.tg.alert_execution_summary(
            source="meridian",
            account="ttp_demo_1m",
            submitted=20,
            filled=18,
            failed=2,
            forward_tracked=0,
            execution_mode="paper",
        )
        msg = mock_send.call_args[0][0]
        self.assertIn("MERIDIAN", msg)
        self.assertIn("20", msg)
        self.assertIn("18", msg)
        self.assertIn("2", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
