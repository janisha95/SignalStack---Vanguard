"""
test_vanguard_cache.py — Unit tests for V1 Vanguard Cache Pipeline.

Run: python3 -m pytest tests/test_vanguard_cache.py -v

Location: ~/SS/Vanguard/tests/test_vanguard_cache.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.helpers.clock import (
    now_utc, utc_to_et, et_to_utc, is_market_open,
    round_down_5m, round_down_1h, is_prefilter_due,
)
from vanguard.helpers.bars import (
    bar_end_iso, normalize_alpaca_bar, normalize_mt5_bar,
    aggregate_5m_to_1h, avg_volume_from_daily_bars, last_close_from_daily_bars,
)
from vanguard.helpers.db import VanguardDB, assert_db_isolation
from vanguard.data_adapters.alpaca_adapter import AlpacaAdapter

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> str:
    """Create a temp DB path in a valid vanguard_universe directory."""
    d = tempfile.mkdtemp()
    return os.path.join(d, "vanguard_universe.db")


def _alpaca_bar(t: str, o=100.0, h=101.0, l=99.0, c=100.5, v=5000) -> dict:
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "n": 100, "vw": 100.2}


def _mt5_bar(ts_unix: int, o=1.0, h=1.01, l=0.99, c=1.005, tv=200) -> dict:
    return {"time": ts_unix, "open": o, "high": h, "low": l, "close": c, "tick_volume": tv}


# ---------------------------------------------------------------------------
# TestClockHelpers
# ---------------------------------------------------------------------------

class TestClockHelpers(unittest.TestCase):

    def test_now_utc_is_aware(self):
        dt = now_utc()
        self.assertIsNotNone(dt.tzinfo)

    def test_utc_to_et_conversion(self):
        # 2026-03-28 14:30:00Z = 10:30 AM EDT
        dt_utc = datetime(2026, 3, 28, 14, 30, 0, tzinfo=UTC)
        dt_et = utc_to_et(dt_utc)
        self.assertEqual(dt_et.hour, 10)
        self.assertEqual(dt_et.minute, 30)

    def test_et_to_utc_conversion(self):
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        dt_et = datetime(2026, 3, 28, 10, 30, 0, tzinfo=ET)
        dt_utc = et_to_utc(dt_et)
        self.assertEqual(dt_utc.hour, 14)

    def test_is_market_open_during_hours(self):
        # Monday 2026-03-30 14:00 UTC = 10:00 AM EDT — market open
        dt = datetime(2026, 3, 30, 14, 0, 0, tzinfo=UTC)
        self.assertTrue(is_market_open(dt))

    def test_is_market_closed_weekend(self):
        # Saturday 2026-03-28 14:00 UTC
        dt = datetime(2026, 3, 28, 14, 0, 0, tzinfo=UTC)
        self.assertFalse(is_market_open(dt))

    def test_is_market_closed_before_open(self):
        # Monday 2026-03-30 13:00 UTC = 9:00 AM EDT — pre-market
        dt = datetime(2026, 3, 30, 13, 0, 0, tzinfo=UTC)
        self.assertFalse(is_market_open(dt))

    def test_is_market_closed_after_close(self):
        # Monday 2026-03-30 20:30 UTC = 4:30 PM EDT — after close
        dt = datetime(2026, 3, 30, 20, 30, 0, tzinfo=UTC)
        self.assertFalse(is_market_open(dt))

    def test_round_down_5m(self):
        dt = datetime(2026, 3, 28, 14, 37, 45, tzinfo=UTC)
        rounded = round_down_5m(dt)
        self.assertEqual(rounded.minute, 35)
        self.assertEqual(rounded.second, 0)

    def test_round_down_5m_on_boundary(self):
        dt = datetime(2026, 3, 28, 14, 35, 0, tzinfo=UTC)
        rounded = round_down_5m(dt)
        self.assertEqual(rounded.minute, 35)

    def test_round_down_1h(self):
        dt = datetime(2026, 3, 28, 14, 47, 22, tzinfo=UTC)
        rounded = round_down_1h(dt)
        self.assertEqual(rounded.minute, 0)
        self.assertEqual(rounded.second, 0)
        self.assertEqual(rounded.hour, 14)

    def test_prefilter_due_when_none(self):
        self.assertTrue(is_prefilter_due(None))


# ---------------------------------------------------------------------------
# TestBarNormalization
# ---------------------------------------------------------------------------

class TestBarNormalization(unittest.TestCase):

    def test_bar_end_iso_adds_5min(self):
        result = bar_end_iso("2026-03-28T14:30:00Z", 5)
        self.assertEqual(result, "2026-03-28T14:35:00Z")

    def test_bar_end_iso_crosses_hour(self):
        result = bar_end_iso("2026-03-28T14:55:00Z", 5)
        self.assertEqual(result, "2026-03-28T15:00:00Z")

    def test_normalize_alpaca_bar_schema(self):
        raw = _alpaca_bar("2026-03-28T14:30:00Z", o=100.0, h=101.0, l=99.5, c=100.8, v=12345)
        row = normalize_alpaca_bar(raw, "aapl")
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["bar_ts_utc"], "2026-03-28T14:35:00Z")  # end time = open + 5m
        self.assertAlmostEqual(row["open"], 100.0)
        self.assertAlmostEqual(row["high"], 101.0)
        self.assertAlmostEqual(row["low"], 99.5)
        self.assertAlmostEqual(row["close"], 100.8)
        self.assertEqual(row["volume"], 12345)
        self.assertEqual(row["asset_class"], "equity")
        self.assertEqual(row["data_source"], "alpaca")

    def test_normalize_alpaca_bar_custom_asset_class(self):
        raw = _alpaca_bar("2026-03-28T14:30:00Z")
        row = normalize_alpaca_bar(raw, "SPY", asset_class="etf", data_source="alpaca_sip")
        self.assertEqual(row["asset_class"], "etf")
        self.assertEqual(row["data_source"], "alpaca_sip")

    def test_normalize_mt5_bar_schema(self):
        # Unix timestamp 1743169800 = 2025-03-28 14:30:00 UTC
        ts = int(datetime(2025, 3, 28, 14, 30, 0, tzinfo=UTC).timestamp())
        raw = _mt5_bar(ts, o=1.0800, h=1.0850, l=1.0790, c=1.0830, tv=500)
        row = normalize_mt5_bar(raw, "EURUSD", asset_class="forex")
        self.assertEqual(row["symbol"], "EURUSD")
        expected_end = datetime(2025, 3, 28, 14, 35, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(row["bar_ts_utc"], expected_end)
        self.assertAlmostEqual(row["open"], 1.0800)
        self.assertEqual(row["volume"], 500)
        self.assertEqual(row["asset_class"], "forex")
        self.assertEqual(row["data_source"], "mt5")


# ---------------------------------------------------------------------------
# TestBarAggregation
# ---------------------------------------------------------------------------

class TestBarAggregation(unittest.TestCase):

    def _make_5m_bars(self, symbol: str, hour_open_utc: str, n: int = 12) -> list[dict]:
        """
        Generate n consecutive 5m bars starting at hour_open_utc.
        Each bar's bar_ts_utc is the END time (open + 5m).
        """
        base = datetime.fromisoformat(hour_open_utc.replace("Z", "+00:00"))
        bars = []
        for i in range(n):
            bar_open = base + timedelta(minutes=5 * i)
            bar_end  = bar_open + timedelta(minutes=5)
            bars.append({
                "symbol":      symbol,
                "bar_ts_utc":  bar_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open":        float(100 + i),
                "high":        float(101 + i),
                "low":         float(99 + i),
                "close":       float(100.5 + i),
                "volume":      1000,
                "asset_class": "equity",
                "data_source": "alpaca",
            })
        return bars

    def test_full_12_bar_hour(self):
        bars = self._make_5m_bars("AAPL", "2026-03-28T14:00:00Z", n=12)
        result = aggregate_5m_to_1h(bars)
        self.assertEqual(len(result), 1)
        h = result[0]
        self.assertEqual(h["symbol"], "AAPL")
        self.assertEqual(h["bar_ts_utc"], "2026-03-28T15:00:00Z")
        self.assertAlmostEqual(h["open"],  100.0)   # first bar's open
        self.assertAlmostEqual(h["close"], 111.5)   # last bar's close
        self.assertEqual(h["high"], 112.0)
        self.assertEqual(h["low"],  99.0)
        self.assertEqual(h["volume"], 12000)

    def test_two_hours(self):
        bars1 = self._make_5m_bars("MSFT", "2026-03-28T14:00:00Z", n=12)
        bars2 = self._make_5m_bars("MSFT", "2026-03-28T15:00:00Z", n=12)
        result = aggregate_5m_to_1h(bars1 + bars2)
        self.assertEqual(len(result), 2)
        ts_list = sorted(r["bar_ts_utc"] for r in result)
        self.assertEqual(ts_list[0], "2026-03-28T15:00:00Z")
        self.assertEqual(ts_list[1], "2026-03-28T16:00:00Z")

    def test_partial_hour(self):
        """3 bars should still produce 1 partial 1h bar."""
        bars = self._make_5m_bars("NVDA", "2026-03-28T14:00:00Z", n=3)
        result = aggregate_5m_to_1h(bars)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["volume"], 3000)

    def test_multiple_symbols(self):
        bars_aapl = self._make_5m_bars("AAPL", "2026-03-28T14:00:00Z", n=12)
        bars_msft = self._make_5m_bars("MSFT", "2026-03-28T14:00:00Z", n=12)
        result = aggregate_5m_to_1h(bars_aapl + bars_msft)
        symbols = {r["symbol"] for r in result}
        self.assertIn("AAPL", symbols)
        self.assertIn("MSFT", symbols)
        self.assertEqual(len(result), 2)

    def test_empty_input(self):
        result = aggregate_5m_to_1h([])
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# TestDailyBarHelpers
# ---------------------------------------------------------------------------

class TestDailyBarHelpers(unittest.TestCase):

    def test_avg_volume_empty(self):
        self.assertEqual(avg_volume_from_daily_bars([]), 0.0)

    def test_avg_volume_single(self):
        self.assertAlmostEqual(avg_volume_from_daily_bars([{"v": 1_000_000}]), 1_000_000.0)

    def test_avg_volume_multiple(self):
        bars = [{"v": 800_000}, {"v": 1_200_000}, {"v": 1_000_000}]
        self.assertAlmostEqual(avg_volume_from_daily_bars(bars), 1_000_000.0)

    def test_last_close_empty(self):
        self.assertEqual(last_close_from_daily_bars([]), 0.0)

    def test_last_close_uses_last_bar(self):
        bars = [{"c": 100.0}, {"c": 105.0}, {"c": 110.0}]
        self.assertAlmostEqual(last_close_from_daily_bars(bars), 110.0)


# ---------------------------------------------------------------------------
# TestVanguardDB
# ---------------------------------------------------------------------------

class TestVanguardDB(unittest.TestCase):

    def setUp(self):
        self.db_path = _tmp_db()
        self.db = VanguardDB(self.db_path)

    def test_isolation_guard_rejects_meridian_path(self):
        with self.assertRaises(AssertionError):
            assert_db_isolation("/path/to/v2_universe.db")

    def test_isolation_guard_rejects_non_vanguard(self):
        with self.assertRaises(AssertionError):
            assert_db_isolation("/tmp/foo.db")

    def test_isolation_guard_accepts_vanguard(self):
        assert_db_isolation("/path/to/vanguard_universe.db")  # no exception

    def test_tables_created(self):
        with self.db.connect() as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("vanguard_bars_5m", tables)
        self.assertIn("vanguard_bars_1h", tables)
        self.assertIn("vanguard_cache_meta", tables)

    def test_upsert_bars_5m(self):
        rows = [
            {
                "symbol": "AAPL", "bar_ts_utc": "2026-03-28T14:35:00Z",
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                "volume": 5000, "asset_class": "equity", "data_source": "alpaca",
            }
        ]
        n = self.db.upsert_bars_5m(rows)
        self.assertEqual(n, 1)
        self.assertEqual(self.db.count_bars_5m(), 1)
        self.assertEqual(self.db.count_symbols_5m(), 1)

    def test_upsert_bars_5m_idempotent(self):
        rows = [{
            "symbol": "AAPL", "bar_ts_utc": "2026-03-28T14:35:00Z",
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 5000, "asset_class": "equity", "data_source": "alpaca",
        }]
        self.db.upsert_bars_5m(rows)
        self.db.upsert_bars_5m(rows)  # second upsert — same PK
        self.assertEqual(self.db.count_bars_5m(), 1)

    def test_upsert_bars_1h(self):
        rows = [{
            "symbol": "MSFT", "bar_ts_utc": "2026-03-28T15:00:00Z",
            "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0,
            "volume": 60000,
        }]
        n = self.db.upsert_bars_1h(rows)
        self.assertEqual(n, 1)
        self.assertEqual(self.db.count_bars_1h(), 1)

    def test_upsert_empty_list(self):
        self.assertEqual(self.db.upsert_bars_5m([]), 0)
        self.assertEqual(self.db.upsert_bars_1h([]), 0)

    def test_meta_set_get(self):
        self.db.set_meta("test_key", "test_value")
        self.assertEqual(self.db.get_meta("test_key"), "test_value")

    def test_meta_missing_key(self):
        self.assertIsNone(self.db.get_meta("nonexistent"))

    def test_latest_bar_ts_utc(self):
        rows = [
            {"symbol": "A", "bar_ts_utc": "2026-03-28T14:35:00Z",
             "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100,
             "asset_class": "equity", "data_source": "alpaca"},
            {"symbol": "B", "bar_ts_utc": "2026-03-28T15:00:00Z",
             "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 200,
             "asset_class": "equity", "data_source": "alpaca"},
        ]
        self.db.upsert_bars_5m(rows)
        latest = self.db.latest_bar_ts_utc()
        self.assertEqual(latest, "2026-03-28T15:00:00Z")

    def test_count_symbols_multiple(self):
        rows = []
        for sym in ("AAPL", "MSFT", "NVDA"):
            rows.append({
                "symbol": sym, "bar_ts_utc": "2026-03-28T14:35:00Z",
                "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100,
                "asset_class": "equity", "data_source": "alpaca",
            })
        self.db.upsert_bars_5m(rows)
        self.assertEqual(self.db.count_symbols_5m(), 3)


# ---------------------------------------------------------------------------
# TestAlpacaAdapterPrefilter
# ---------------------------------------------------------------------------

class TestAlpacaAdapterPrefilter(unittest.TestCase):

    def _make_adapter(self):
        return AlpacaAdapter(api_key="fake_key", api_secret="fake_secret")

    def test_filter_assets_removes_otc(self):
        adapter = self._make_adapter()
        assets = [
            {"symbol": "AAPL", "exchange": "NASDAQ", "tradable": True},
            {"symbol": "PINKCO", "exchange": "OTC", "tradable": True},
        ]
        result = adapter.filter_assets(assets)
        self.assertIn("AAPL", result)
        self.assertNotIn("PINKCO", result)

    def test_filter_assets_removes_warrants(self):
        adapter = self._make_adapter()
        assets = [
            {"symbol": "AAPL", "exchange": "NASDAQ", "tradable": True},
            {"symbol": "RCKTW", "exchange": "NASDAQ", "tradable": True},  # warrant
        ]
        result = adapter.filter_assets(assets)
        self.assertIn("AAPL", result)
        self.assertNotIn("RCKTW", result)

    def test_filter_assets_removes_leveraged_etfs(self):
        adapter = self._make_adapter()
        assets = [
            {"symbol": "SPY",  "exchange": "NYSE",   "tradable": True},
            {"symbol": "TQQQ", "exchange": "NASDAQ", "tradable": True},
        ]
        result = adapter.filter_assets(assets)
        self.assertIn("SPY", result)
        self.assertNotIn("TQQQ", result)

    def test_filter_assets_removes_non_tradable(self):
        adapter = self._make_adapter()
        assets = [
            {"symbol": "AAPL", "exchange": "NASDAQ", "tradable": True},
            {"symbol": "DELISTED", "exchange": "NYSE", "tradable": False},
        ]
        result = adapter.filter_assets(assets)
        self.assertNotIn("DELISTED", result)

    def test_filter_assets_symbols_uppercased(self):
        adapter = self._make_adapter()
        assets = [{"symbol": "aapl", "exchange": "NASDAQ", "tradable": True}]
        result = adapter.filter_assets(assets)
        self.assertIn("AAPL", result)

    @patch("vanguard.data_adapters.alpaca_adapter.AlpacaAdapter._get")
    def test_get_daily_bars_batches(self, mock_get):
        """With 600 symbols, should make 2 batch requests."""
        mock_get.return_value = {"bars": {"AAPL": [_alpaca_bar("2026-03-28T05:00:00Z")]}, "next_page_token": None}
        adapter = self._make_adapter()
        symbols = [f"SYM{i:04d}" for i in range(600)]
        adapter.get_daily_bars(symbols, "2026-03-20", "2026-03-28")
        self.assertEqual(mock_get.call_count, 2)

    @patch("vanguard.data_adapters.alpaca_adapter.AlpacaAdapter._get")
    def test_get_5m_bars_handles_pagination(self, mock_get):
        """next_page_token should trigger a second call."""
        mock_get.side_effect = [
            {"bars": {"AAPL": [_alpaca_bar("2026-03-28T14:30:00Z")]}, "next_page_token": "tok123"},
            {"bars": {"AAPL": [_alpaca_bar("2026-03-28T14:35:00Z")]}, "next_page_token": None},
        ]
        adapter = self._make_adapter()
        start = datetime(2026, 3, 28, 14, 30, 0, tzinfo=UTC)
        end   = datetime(2026, 3, 28, 16, 0,  0, tzinfo=UTC)
        result = adapter.get_5m_bars(["AAPL"], start, end)
        self.assertEqual(len(result["AAPL"]), 2)
        self.assertEqual(mock_get.call_count, 2)

    @patch("vanguard.data_adapters.alpaca_adapter.AlpacaAdapter._get")
    def test_get_5m_bars_none_bars_handled(self, mock_get):
        """Response with 'bars': null should not crash."""
        mock_get.return_value = {"bars": None, "next_page_token": None}
        adapter = self._make_adapter()
        start = datetime(2026, 3, 28, 14, 0, 0, tzinfo=UTC)
        end   = datetime(2026, 3, 28, 16, 0, 0, tzinfo=UTC)
        result = adapter.get_5m_bars(["AAPL"], start, end)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# TestValidate
# ---------------------------------------------------------------------------

class TestValidate(unittest.TestCase):

    def setUp(self):
        self.db_path = _tmp_db()
        self.db = VanguardDB(self.db_path)

    def _populate(self, n_symbols: int = 1200, add_1h: bool = True) -> None:
        rows_5m = []
        rows_1h = []
        now_ts = "2026-03-28T15:00:00Z"
        for i in range(n_symbols):
            sym = f"SYM{i:04d}"
            rows_5m.append({
                "symbol": sym, "bar_ts_utc": now_ts,
                "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000,
                "asset_class": "equity", "data_source": "alpaca",
            })
            if add_1h:
                rows_1h.append({
                    "symbol": sym, "bar_ts_utc": now_ts,
                    "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 12000,
                })
        self.db.upsert_bars_5m(rows_5m)
        if add_1h:
            self.db.upsert_bars_1h(rows_1h)

    def test_empty_db_fails(self):
        from stages.vanguard_cache import validate
        result = validate(self.db, market_hours_check=False)
        self.assertFalse(result["pass"])

    def test_healthy_db_passes(self):
        from stages.vanguard_cache import validate
        self._populate(n_symbols=1200)
        result = validate(self.db, market_hours_check=False)
        self.assertTrue(result["pass"])

    def test_insufficient_symbols_fails(self):
        from stages.vanguard_cache import validate
        self._populate(n_symbols=500)  # below 1000 threshold
        result = validate(self.db, market_hours_check=False)
        self.assertFalse(result["checks"]["min_symbols_5m"]["pass"])

    def test_missing_1h_fails(self):
        from stages.vanguard_cache import validate
        self._populate(n_symbols=1200, add_1h=False)
        result = validate(self.db, market_hours_check=False)
        self.assertFalse(result["checks"]["bars_1h_nonempty"]["pass"])


# ---------------------------------------------------------------------------
# TestMT5AssetClass
# ---------------------------------------------------------------------------

class TestMT5AssetClass(unittest.TestCase):

    def test_classify_forex(self):
        from vanguard.data_adapters.mt5_data_adapter import _classify
        self.assertEqual(_classify("EURUSD"), "forex")
        self.assertEqual(_classify("GBPUSD"), "forex")

    def test_classify_metal(self):
        from vanguard.data_adapters.mt5_data_adapter import _classify
        self.assertEqual(_classify("XAUUSD"), "metal")
        self.assertEqual(_classify("XAGUSD"), "metal")

    def test_classify_crypto(self):
        from vanguard.data_adapters.mt5_data_adapter import _classify
        self.assertEqual(_classify("BTCUSD"), "crypto")
        self.assertEqual(_classify("ETHUSD"), "crypto")

    def test_classify_index(self):
        from vanguard.data_adapters.mt5_data_adapter import _classify
        self.assertEqual(_classify("NAS100"), "index")
        self.assertEqual(_classify("US500"), "index")

    def test_classify_unknown_defaults_to_forex(self):
        from vanguard.data_adapters.mt5_data_adapter import _classify
        self.assertEqual(_classify("UNKNOWN"), "forex")

    def test_mt5_adapter_stub_mode_on_mac(self):
        from vanguard.data_adapters.mt5_data_adapter import MT5DataAdapter, _MT5_AVAILABLE
        adapter = MT5DataAdapter()
        if not _MT5_AVAILABLE:
            self.assertFalse(adapter.available)
            self.assertEqual(adapter.get_symbols(), [])
            bars = adapter.get_bars_5m(
                "EURUSD",
                datetime(2026, 3, 28, 14, 0, 0, tzinfo=UTC),
                datetime(2026, 3, 28, 15, 0, 0, tzinfo=UTC),
            )
            self.assertEqual(bars, [])


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
