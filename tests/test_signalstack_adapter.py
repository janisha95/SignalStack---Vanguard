"""
test_signalstack_adapter.py — Unit tests for SignalStackAdapter.

Run: python3 -m pytest tests/test_signalstack_adapter.py -v
  or: python3 tests/test_signalstack_adapter.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.execution.signalstack_adapter import SignalStackAdapter


class TestDirectionToAction(unittest.TestCase):
    def setUp(self):
        self.adapter = SignalStackAdapter(webhook_url="http://fake.test/webhook")

    def test_long_open(self):
        self.assertEqual(self.adapter._direction_to_action("LONG", "open"), "buy")

    def test_short_open(self):
        self.assertEqual(self.adapter._direction_to_action("SHORT", "open"), "sell_short")

    def test_long_close(self):
        self.assertEqual(self.adapter._direction_to_action("LONG", "close"), "sell")

    def test_short_close(self):
        self.assertEqual(self.adapter._direction_to_action("SHORT", "close"), "buy_to_cover")

    def test_flatten(self):
        self.assertEqual(self.adapter._direction_to_action("LONG", "flatten"), "close")
        self.assertEqual(self.adapter._direction_to_action("SHORT", "flatten"), "close")

    def test_unknown_operation(self):
        with self.assertRaises(ValueError):
            self.adapter._direction_to_action("LONG", "unknown_op")


class TestBuildPayload(unittest.TestCase):
    def setUp(self):
        self.adapter = SignalStackAdapter(webhook_url="http://fake.test/webhook")

    def test_market_order_payload(self):
        payload = self.adapter.build_payload("AAPL", "buy", 100)
        self.assertEqual(payload, {"symbol": "AAPL", "action": "buy", "quantity": 100})

    def test_limit_order_payload(self):
        payload = self.adapter.build_payload("NVDA", "buy", 50, limit_price=123.45)
        self.assertIn("limit_price", payload)
        self.assertEqual(payload["limit_price"], 123.45)
        self.assertNotIn("stop_price", payload)

    def test_stop_order_payload(self):
        payload = self.adapter.build_payload("TSLA", "sell_short", 10, stop_price=200.0)
        self.assertIn("stop_price", payload)
        self.assertNotIn("limit_price", payload)

    def test_no_none_fields(self):
        payload = self.adapter.build_payload("SPY", "buy", 5)
        self.assertNotIn("limit_price", payload)
        self.assertNotIn("stop_price", payload)


class TestSendOrderSuccess(unittest.TestCase):
    def setUp(self):
        self.adapter = SignalStackAdapter(
            webhook_url="http://fake.test/webhook",
            max_retries=2,
            retry_delay=0.0,
        )

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_successful_send(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"status":"ok"}'
        mock_post.return_value = mock_resp

        result = self.adapter.send_order("AAPL", "LONG", 100)

        self.assertTrue(result["success"])
        self.assertEqual(result["status_code"], 200)
        self.assertIsNone(result["error"])
        self.assertIn("payload", result)
        self.assertEqual(result["payload"]["action"], "buy")
        self.assertEqual(result["payload"]["symbol"], "AAPL")
        self.assertEqual(result["payload"]["quantity"], 100)
        mock_post.assert_called_once()

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_201_accepted(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.text = "accepted"
        mock_post.return_value = mock_resp

        result = self.adapter.send_order("MSFT", "SHORT", 50)
        self.assertTrue(result["success"])

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_short_sell_action(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_post.return_value = mock_resp

        result = self.adapter.send_order("TSLA", "SHORT", 25)
        self.assertEqual(result["payload"]["action"], "sell_short")


class TestSendOrderFailure(unittest.TestCase):
    def setUp(self):
        self.adapter = SignalStackAdapter(
            webhook_url="http://fake.test/webhook",
            max_retries=1,
            retry_delay=0.0,
        )

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_http_error_retries(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        result = self.adapter.send_order("AAPL", "LONG", 100)

        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 500)
        self.assertIsNotNone(result["error"])
        # max_retries=1 → 2 total attempts
        self.assertEqual(mock_post.call_count, 2)

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_timeout_returns_error(self, mock_post):
        import requests as _req
        mock_post.side_effect = _req.exceptions.Timeout()

        result = self.adapter.send_order("AAPL", "LONG", 100)

        self.assertFalse(result["success"])
        self.assertIn("Timeout", result["error"])

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_connection_error(self, mock_post):
        import requests as _req
        mock_post.side_effect = _req.exceptions.ConnectionError("refused")

        result = self.adapter.send_order("AAPL", "LONG", 100)

        self.assertFalse(result["success"])
        self.assertIsNotNone(result["error"])

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_latency_recorded(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_post.return_value = mock_resp

        result = self.adapter.send_order("NVDA", "LONG", 10)
        self.assertIn("latency_ms", result)
        self.assertIsInstance(result["latency_ms"], int)
        self.assertGreaterEqual(result["latency_ms"], 0)


class TestFlattenPosition(unittest.TestCase):
    def setUp(self):
        self.adapter = SignalStackAdapter(
            webhook_url="http://fake.test/webhook",
            max_retries=0,
            retry_delay=0.0,
        )

    @patch("vanguard.execution.signalstack_adapter.requests.post")
    def test_flatten_sends_close_action(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_post.return_value = mock_resp

        result = self.adapter.flatten_position("AAPL", 100)

        self.assertTrue(result["success"])
        self.assertEqual(result["payload"]["action"], "close")
        self.assertEqual(result["payload"]["symbol"], "AAPL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
