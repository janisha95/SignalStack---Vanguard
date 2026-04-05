"""
test_vanguard_prefilter.py — Unit tests for V2 Vanguard Health Monitor.

Run: python3 -m pytest tests/test_vanguard_prefilter.py -v

Location: ~/SS/Vanguard/tests/test_vanguard_prefilter.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.helpers.db import VanguardDB
from stages.vanguard_prefilter import (
    check_symbol,
    run,
    validate,
    STATUS_ACTIVE,
    STATUS_STALE,
    STATUS_LOW_VOLUME,
    STATUS_WIDE_SPREAD,
    STATUS_WARM_UP,
    STATUS_HALTED,
    MIN_BARS_WARMUP,
    MAX_STALE_MINUTES,
    MIN_REL_VOL,
    MAX_SPREAD_PCT,
)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> str:
    d = tempfile.mkdtemp()
    return os.path.join(d, "vanguard_universe.db")


def _make_bar(
    ts_utc: str,
    o: float = 100.0,
    h: float = 100.5,
    l: float = 99.5,
    c: float = 100.0,
    v: int = 1000,
) -> dict:
    return {"bar_ts_utc": ts_utc, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _bars(n: int, base_ts: datetime, interval_min: int = 5, **kwargs) -> list[dict]:
    """Generate n bars ending at base_ts, going back in time."""
    bars = []
    for i in range(n - 1, -1, -1):
        ts = base_ts - timedelta(minutes=i * interval_min)
        bars.append(_make_bar(ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **kwargs))
    return bars


# ---------------------------------------------------------------------------
# TestCheckSymbol — unit tests for the pure check_symbol function
# ---------------------------------------------------------------------------

class TestCheckSymbolNoData(unittest.TestCase):

    def _now(self) -> datetime:
        return datetime(2026, 3, 28, 14, 0, 0, tzinfo=UTC)  # Friday 10 AM ET (market open)

    def test_no_bars_returns_halted(self):
        result = check_symbol("AAPL", [], self._now(), market_is_open=True)
        self.assertEqual(result["status"], STATUS_HALTED)
        self.assertEqual(result["bars_available"], 0)
        self.assertIsNone(result["last_bar_ts_utc"])

    def test_no_bars_outside_hours_returns_halted(self):
        result = check_symbol("AAPL", [], self._now(), market_is_open=False)
        self.assertEqual(result["status"], STATUS_HALTED)


class TestCheckSymbolStaleness(unittest.TestCase):

    def _now_open(self) -> datetime:
        return datetime(2026, 3, 28, 14, 30, 0, tzinfo=UTC)  # 10:30 AM ET

    def _fresh_bar(self, minutes_ago: float = 4.0) -> list[dict]:
        """Single fresh bar."""
        ts = self._now_open() - timedelta(minutes=minutes_ago)
        bars = _bars(MIN_BARS_WARMUP + 5, ts - timedelta(minutes=5 * (MIN_BARS_WARMUP + 4)))
        # Replace last bar with the fresh one
        bars[-1] = _make_bar(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return bars

    def test_fresh_bar_passes_staleness(self):
        # Last bar 4 min ago, threshold is 10 — should NOT trigger STALE
        bars = self._fresh_bar(4.0)
        result = check_symbol("SPY", bars, self._now_open(), market_is_open=True)
        self.assertNotEqual(result["status"], STATUS_STALE)

    def test_stale_bar_during_market_hours(self):
        now = self._now_open()
        # Last bar 15 minutes ago (past the 10-min threshold)
        old_ts = now - timedelta(minutes=15)
        bars = _bars(MIN_BARS_WARMUP + 5, old_ts)
        result = check_symbol("AAPL", bars, now, market_is_open=True)
        self.assertEqual(result["status"], STATUS_STALE)

    def test_stale_bar_outside_hours_passes(self):
        now = datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)  # Saturday night
        # Last bar 3 hours ago — should NOT trigger STALE outside market hours
        old_ts = now - timedelta(hours=3)
        bars = _bars(MIN_BARS_WARMUP + 5, old_ts)
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertNotEqual(result["status"], STATUS_STALE)


class TestCheckSymbolVolume(unittest.TestCase):

    def _now(self) -> datetime:
        return datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)  # outside hours

    def test_normal_volume_passes(self):
        now = self._now()
        bars = _bars(MIN_BARS_WARMUP + 5, now - timedelta(hours=2), v=1000)
        result = check_symbol("SPY", bars, now, market_is_open=False)
        # All volumes equal → rel_vol = 1.0 → passes
        self.assertNotEqual(result["status"], STATUS_LOW_VOLUME)
        self.assertAlmostEqual(result["relative_volume"], 1.0, places=2)

    def test_low_volume_triggers_status(self):
        now = self._now()
        # avg volume = 1000; last bar volume = 10 → rel_vol = 0.01
        bars = _bars(MIN_BARS_WARMUP + 5, now - timedelta(hours=2), v=1000)
        bars[-1] = _make_bar(bars[-1]["bar_ts_utc"], v=10)
        result = check_symbol("SPY", bars, now, market_is_open=False)
        self.assertEqual(result["status"], STATUS_LOW_VOLUME)
        self.assertLess(result["relative_volume"], MIN_REL_VOL)

    def test_zero_avg_volume_gives_low_volume(self):
        now = self._now()
        bars = _bars(MIN_BARS_WARMUP + 5, now - timedelta(hours=2), v=0)
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertEqual(result["status"], STATUS_LOW_VOLUME)


class TestCheckSymbolSpread(unittest.TestCase):

    def _now(self) -> datetime:
        return datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)

    def test_tight_spread_passes(self):
        now = self._now()
        # spread = (100.5 - 99.5) / 100 = 0.01 = 1% < 5%
        bars = _bars(MIN_BARS_WARMUP + 5, now - timedelta(hours=2),
                     h=100.5, l=99.5, c=100.0, v=500)
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertNotEqual(result["status"], STATUS_WIDE_SPREAD)
        self.assertAlmostEqual(result["spread_bps"], 100.0, places=0)  # 100 bps

    def test_wide_spread_triggers_status(self):
        now = self._now()
        # spread = (110 - 90) / 100 = 0.20 = 20% >> 5%
        bars = _bars(MIN_BARS_WARMUP + 5, now - timedelta(hours=2),
                     h=110.0, l=90.0, c=100.0, v=500)
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertEqual(result["status"], STATUS_WIDE_SPREAD)
        self.assertGreater(result["spread_bps"], MAX_SPREAD_PCT * 10_000)

    def test_exact_spread_threshold_fails(self):
        now = self._now()
        # spread = exactly 5% → should fail (>= threshold)
        bars = _bars(MIN_BARS_WARMUP + 5, now - timedelta(hours=2),
                     h=105.0, l=100.0, c=100.0, v=500)
        # (105 - 100) / 100 = 0.05 = exactly MAX_SPREAD_PCT
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertEqual(result["status"], STATUS_WIDE_SPREAD)


class TestCheckSymbolWarmUp(unittest.TestCase):

    def _now(self) -> datetime:
        return datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)

    def test_insufficient_bars_triggers_warmup(self):
        now = self._now()
        bars = _bars(10, now - timedelta(hours=1))  # only 10 bars
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertEqual(result["status"], STATUS_WARM_UP)
        self.assertEqual(result["bars_available"], 10)

    def test_exactly_min_bars_passes_warmup(self):
        now = self._now()
        bars = _bars(MIN_BARS_WARMUP, now - timedelta(hours=4))
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertNotEqual(result["status"], STATUS_WARM_UP)

    def test_one_bar_triggers_warmup(self):
        now = self._now()
        bars = [_make_bar((now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"), v=500)]
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        self.assertEqual(result["status"], STATUS_WARM_UP)


class TestCheckSymbolSession(unittest.TestCase):

    def _market_open_now(self) -> datetime:
        """Simulate a Friday at 14:30 UTC = 10:30 ET (market open)."""
        return datetime(2026, 3, 27, 14, 30, 0, tzinfo=UTC)

    def test_session_bar_present_passes(self):
        """Bar from current session → session check passes."""
        now = self._market_open_now()
        # last bar 5 min ago, session started at 13:30 UTC (9:30 ET)
        bars = _bars(MIN_BARS_WARMUP + 5, now - timedelta(minutes=5))
        # patch last_market_open_utc to return session start
        session_open = datetime(2026, 3, 27, 13, 30, 0, tzinfo=UTC)
        with patch("stages.vanguard_prefilter.last_market_open_utc", return_value=session_open):
            result = check_symbol("SPY", bars, now, market_is_open=True)
        self.assertNotEqual(result["status"], STATUS_HALTED)

    def test_no_session_bar_triggers_halted(self):
        """
        Fresh bars but none from today's session → HALTED.
        Edge case: market just opened at 13:30 UTC, last bar is 13:28 UTC
        (5 min old, passes staleness), but session_open is 13:31 UTC
        so no bar qualifies as a session bar.
        """
        # Market opened at 13:31 UTC; last bar is 13:28 UTC (3 min old)
        session_open = datetime(2026, 3, 27, 13, 31, 0, tzinfo=UTC)
        last_bar_ts  = datetime(2026, 3, 27, 13, 28, 0, tzinfo=UTC)
        now          = datetime(2026, 3, 27, 13, 33, 0, tzinfo=UTC)  # 2 min after session open

        # All bars end at or before 13:28 — none >= 13:31
        bars = _bars(MIN_BARS_WARMUP + 5, last_bar_ts)

        with patch("stages.vanguard_prefilter.last_market_open_utc", return_value=session_open):
            result = check_symbol("SPY", bars, now, market_is_open=True)
        self.assertEqual(result["status"], STATUS_HALTED)

    def test_session_check_skipped_outside_hours(self):
        """Outside market hours → session check not applied."""
        now = datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)
        past = now - timedelta(days=2)
        bars = _bars(MIN_BARS_WARMUP + 5, past)
        result = check_symbol("SPY", bars, now, market_is_open=False)
        # Should not be HALTED due to session check (outside hours)
        self.assertEqual(result["status"], STATUS_ACTIVE)


class TestCheckSymbolActive(unittest.TestCase):

    def test_all_checks_pass_returns_active(self):
        now = datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)  # outside hours
        bars = _bars(MIN_BARS_WARMUP + 10, now - timedelta(hours=2))
        result = check_symbol("SPY", bars, now, market_is_open=False)
        self.assertEqual(result["status"], STATUS_ACTIVE)
        self.assertIsNotNone(result["relative_volume"])
        self.assertIsNotNone(result["spread_bps"])
        self.assertEqual(result["bars_available"], MIN_BARS_WARMUP + 10)

    def test_result_has_all_required_fields(self):
        now = datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)
        bars = _bars(MIN_BARS_WARMUP + 10, now - timedelta(hours=2))
        result = check_symbol("AAPL", bars, now, market_is_open=False)
        for key in ("symbol", "status", "relative_volume", "spread_bps",
                    "bars_available", "last_bar_ts_utc"):
            self.assertIn(key, result, f"Missing key: {key}")


# ---------------------------------------------------------------------------
# TestRunAndDB — integration tests using a real temp DB
# ---------------------------------------------------------------------------

class TestRunIntegration(unittest.TestCase):

    def setUp(self):
        self.db_path = _tmp_db()
        self.db = VanguardDB(self.db_path)

        # Insert bars for SPY and AAPL (>= 50 bars each)
        now = datetime(2026, 3, 28, 23, 0, 0, tzinfo=UTC)
        spy_bars = []
        aapl_bars = []
        for i in range(60):
            ts = (now - timedelta(minutes=5 * (59 - i))).strftime("%Y-%m-%dT%H:%M:%SZ")
            spy_bars.append({
                "symbol": "SPY", "bar_ts_utc": ts,
                "open": 500.0, "high": 501.0, "low": 499.0, "close": 500.0,
                "volume": 1000, "asset_class": "equity", "data_source": "alpaca",
            })
            aapl_bars.append({
                "symbol": "AAPL", "bar_ts_utc": ts,
                "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.0,
                "volume": 500, "asset_class": "equity", "data_source": "alpaca",
            })
        self.db.upsert_bars_5m(spy_bars + aapl_bars)

    def test_run_returns_results_for_all_symbols(self):
        with patch("stages.vanguard_prefilter.DB_PATH", self.db_path):
            with patch("stages.vanguard_prefilter.is_market_open", return_value=False):
                results = run(dry_run=True)
        symbols = {r["symbol"] for r in results}
        self.assertIn("SPY", symbols)
        self.assertIn("AAPL", symbols)

    def test_run_dry_run_does_not_write(self):
        with patch("stages.vanguard_prefilter.DB_PATH", self.db_path):
            with patch("stages.vanguard_prefilter.is_market_open", return_value=False):
                run(dry_run=True)
        # DB should have no health rows
        cycle = self.db.get_latest_health_cycle()
        self.assertIsNone(cycle)

    def test_run_writes_health_rows(self):
        with patch("stages.vanguard_prefilter.DB_PATH", self.db_path):
            with patch("stages.vanguard_prefilter.is_market_open", return_value=False):
                run(dry_run=False)
        cycle = self.db.get_latest_health_cycle()
        self.assertIsNotNone(cycle)
        active = self.db.get_active_symbols(cycle)
        self.assertIn("SPY", active)
        self.assertIn("AAPL", active)

    def test_run_symbols_filter(self):
        with patch("stages.vanguard_prefilter.DB_PATH", self.db_path):
            with patch("stages.vanguard_prefilter.is_market_open", return_value=False):
                results = run(symbols=["SPY"], dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["symbol"], "SPY")

    def test_validate_passes_after_run(self):
        with patch("stages.vanguard_prefilter.DB_PATH", self.db_path):
            with patch("stages.vanguard_prefilter.is_market_open", return_value=False):
                run(dry_run=False)
            ok = validate()
        self.assertTrue(ok)

    def test_validate_fails_when_no_data(self):
        with patch("stages.vanguard_prefilter.DB_PATH", self.db_path):
            ok = validate()
        self.assertFalse(ok)


class TestVanguardHealthDB(unittest.TestCase):

    def setUp(self):
        self.db_path = _tmp_db()
        self.db = VanguardDB(self.db_path)

    def test_upsert_and_get_active_symbols(self):
        rows = [
            {"symbol": "SPY", "cycle_ts_utc": "2026-03-28T14:00:00Z",
             "status": "ACTIVE", "relative_volume": 1.2, "spread_bps": 50.0,
             "bars_available": 60, "last_bar_ts_utc": "2026-03-28T13:55:00Z"},
            {"symbol": "AAPL", "cycle_ts_utc": "2026-03-28T14:00:00Z",
             "status": "LOW_VOLUME", "relative_volume": 0.1, "spread_bps": 30.0,
             "bars_available": 60, "last_bar_ts_utc": "2026-03-28T13:55:00Z"},
        ]
        written = self.db.upsert_health(rows)
        self.assertEqual(written, 2)

        active = self.db.get_active_symbols()
        self.assertEqual(active, ["SPY"])

    def test_count_health_by_status(self):
        rows = [
            {"symbol": "SPY",  "cycle_ts_utc": "2026-03-28T14:00:00Z",
             "status": "ACTIVE", "relative_volume": 1.0, "spread_bps": 20.0,
             "bars_available": 60, "last_bar_ts_utc": "2026-03-28T13:55:00Z"},
            {"symbol": "AAPL", "cycle_ts_utc": "2026-03-28T14:00:00Z",
             "status": "ACTIVE", "relative_volume": 0.8, "spread_bps": 30.0,
             "bars_available": 60, "last_bar_ts_utc": "2026-03-28T13:55:00Z"},
            {"symbol": "TSLA", "cycle_ts_utc": "2026-03-28T14:00:00Z",
             "status": "STALE", "relative_volume": None, "spread_bps": None,
             "bars_available": 60, "last_bar_ts_utc": "2026-03-28T12:00:00Z"},
        ]
        self.db.upsert_health(rows)
        counts = self.db.count_health_by_status("2026-03-28T14:00:00Z")
        self.assertEqual(counts.get("ACTIVE", 0), 2)
        self.assertEqual(counts.get("STALE", 0), 1)

    def test_get_latest_health_cycle(self):
        for ts in ["2026-03-28T13:00:00Z", "2026-03-28T14:00:00Z", "2026-03-28T12:00:00Z"]:
            self.db.upsert_health([{
                "symbol": "SPY", "cycle_ts_utc": ts,
                "status": "ACTIVE", "relative_volume": 1.0, "spread_bps": 10.0,
                "bars_available": 60, "last_bar_ts_utc": ts,
            }])
        latest = self.db.get_latest_health_cycle()
        self.assertEqual(latest, "2026-03-28T14:00:00Z")

    def test_upsert_health_empty_list(self):
        written = self.db.upsert_health([])
        self.assertEqual(written, 0)


if __name__ == "__main__":
    unittest.main()
