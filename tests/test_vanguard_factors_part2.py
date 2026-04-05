"""
test_vanguard_factors_part2.py — Unit tests for V3 Part 2 factor modules.

Covers:
  • vanguard/factors/volume.py
  • vanguard/factors/market_context.py
  • Engine registry includes Part 2 modules
  • Total feature count = 20

Run: python3 -m pytest tests/test_vanguard_factors_part2.py -v

Location: ~/SS/Vanguard/tests/test_vanguard_factors_part2.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from vanguard.helpers.db import VanguardDB
from vanguard.factors import volume, market_context
from vanguard.factors.volume import FEATURE_NAMES as VOL_FEATURES
from vanguard.factors.market_context import FEATURE_NAMES as CTX_FEATURES
from stages.vanguard_factor_engine import (
    FACTOR_MODULES,
    _EXPECTED_FEATURES,
    run,
    validate,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> str:
    d = tempfile.mkdtemp()
    return os.path.join(d, "vanguard_universe.db")


def _make_df(
    n: int,
    base_ts: datetime = None,
    interval_min: int = 5,
    o: float = 100.0,
    h: float = 101.0,
    l: float = 99.0,
    c: float = 100.0,
    v: int = 1000,
) -> pd.DataFrame:
    if base_ts is None:
        base_ts = datetime(2026, 3, 28, 9, 35, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        ts = base_ts + timedelta(minutes=i * interval_min)
        rows.append({
            "bar_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })
    return pd.DataFrame(rows)


def _make_trending_df(n: int, start: float = 100.0, step: float = 0.1) -> pd.DataFrame:
    base_ts = datetime(2026, 3, 28, 9, 35, 0, tzinfo=UTC)
    rows = []
    price = start
    for i in range(n):
        price += step
        ts = base_ts + timedelta(minutes=i * 5)
        rows.append({
            "bar_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": price - step / 2, "high": price + step,
            "low": price - step, "close": price, "volume": 1000,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# TestVolume
# ---------------------------------------------------------------------------

class TestVolume(unittest.TestCase):

    def test_returns_all_five_features(self):
        df = _make_df(25)
        result = volume.compute(df, None, None)
        for k in VOL_FEATURES:
            self.assertIn(k, result, f"Missing feature: {k}")

    def test_nan_on_empty(self):
        result = volume.compute(pd.DataFrame(), None, None)
        for k in VOL_FEATURES:
            self.assertTrue(math.isnan(result[k]), f"{k} should be NaN on empty")

    def test_nan_on_none(self):
        result = volume.compute(None, None, None)
        for k in VOL_FEATURES:
            self.assertTrue(math.isnan(result[k]))

    # ------------------------------------------------------------------
    # relative_volume
    # ------------------------------------------------------------------

    def test_relative_volume_equals_1_for_flat_volume(self):
        df = _make_df(25, v=1000)
        result = volume.compute(df, None, None)
        self.assertFalse(math.isnan(result["relative_volume"]))
        self.assertAlmostEqual(result["relative_volume"], 1.0, places=3)

    def test_relative_volume_above_1_for_high_last_bar(self):
        df = _make_df(25, v=1000)
        # Spike last bar to 3000 → rel_vol > 1
        df.iloc[-1, df.columns.get_loc("volume")] = 3000
        result = volume.compute(df, None, None)
        self.assertGreater(result["relative_volume"], 1.0)

    def test_relative_volume_below_1_for_low_last_bar(self):
        df = _make_df(25, v=1000)
        df.iloc[-1, df.columns.get_loc("volume")] = 100
        result = volume.compute(df, None, None)
        self.assertLess(result["relative_volume"], 1.0)

    # ------------------------------------------------------------------
    # volume_burst_z
    # ------------------------------------------------------------------

    def test_volume_burst_z_zero_for_flat_volume(self):
        # All bars same volume → std = 0 → z = 0
        df = _make_df(25, v=1000)
        result = volume.compute(df, None, None)
        self.assertAlmostEqual(result["volume_burst_z"], 0.0, places=3)

    def test_volume_burst_z_positive_for_spike(self):
        df = _make_df(25, v=1000)
        df.iloc[-1, df.columns.get_loc("volume")] = 10_000
        result = volume.compute(df, None, None)
        self.assertGreater(result["volume_burst_z"], 0.0)

    def test_volume_burst_z_negative_for_trough(self):
        df = _make_df(25, v=1000)
        df.iloc[-1, df.columns.get_loc("volume")] = 1
        result = volume.compute(df, None, None)
        self.assertLess(result["volume_burst_z"], 0.0)

    # ------------------------------------------------------------------
    # down_volume_ratio
    # ------------------------------------------------------------------

    def test_down_volume_ratio_zero_for_all_up_bars(self):
        # open < close for all bars → no down volume → ratio = 0
        df = _make_df(25, o=99.0, c=100.0, v=1000)
        result = volume.compute(df, None, None)
        self.assertAlmostEqual(result["down_volume_ratio"], 0.0, places=3)

    def test_down_volume_ratio_one_for_all_down_bars(self):
        # open > close for all bars → all down volume → ratio = 1
        df = _make_df(25, o=101.0, c=100.0, v=1000)
        result = volume.compute(df, None, None)
        self.assertAlmostEqual(result["down_volume_ratio"], 1.0, places=3)

    def test_down_volume_ratio_between_0_and_1(self):
        df = _make_trending_df(25)
        result = volume.compute(df, None, None)
        dvr = result["down_volume_ratio"]
        self.assertFalse(math.isnan(dvr))
        self.assertGreaterEqual(dvr, 0.0)
        self.assertLessEqual(dvr, 1.0)

    # ------------------------------------------------------------------
    # effort_vs_result
    # ------------------------------------------------------------------

    def test_effort_vs_result_positive(self):
        df = _make_df(25, o=99.0, h=101.0, l=98.0, c=100.0, v=1000)
        result = volume.compute(df, None, None)
        evr = result["effort_vs_result"]
        self.assertFalse(math.isnan(evr))
        self.assertGreater(evr, 0.0)

    def test_effort_vs_result_nan_when_zero_volume(self):
        df = _make_df(25, v=0)
        result = volume.compute(df, None, None)
        self.assertTrue(math.isnan(result["effort_vs_result"]))

    def test_effort_vs_result_zero_for_doji(self):
        # open == close → price_change = 0 → effort = 0
        df = _make_df(25, o=100.0, c=100.0, v=1000)
        result = volume.compute(df, None, None)
        self.assertAlmostEqual(result["effort_vs_result"], 0.0, places=4)

    # ------------------------------------------------------------------
    # spread_proxy
    # ------------------------------------------------------------------

    def test_spread_proxy_equals_range_over_close(self):
        df = _make_df(25, h=102.0, l=98.0, c=100.0)
        result = volume.compute(df, None, None)
        # (102 - 98) / 100 = 0.04
        self.assertAlmostEqual(result["spread_proxy"], 0.04, places=4)

    def test_spread_proxy_zero_when_high_equals_low(self):
        df = _make_df(25, h=100.0, l=100.0, c=100.0)
        result = volume.compute(df, None, None)
        self.assertAlmostEqual(result["spread_proxy"], 0.0, places=4)

    def test_spread_proxy_positive(self):
        df = _make_df(25)
        result = volume.compute(df, None, None)
        self.assertGreater(result["spread_proxy"], 0.0)


# ---------------------------------------------------------------------------
# TestMarketContext
# ---------------------------------------------------------------------------

class TestMarketContext(unittest.TestCase):

    def _spy(self, n: int = 80, c: float = 500.0) -> pd.DataFrame:
        return _make_df(n, c=c, h=c + 1, l=c - 1, o=c - 0.5)

    def test_returns_all_five_features(self):
        df = _make_df(80)
        result = market_context.compute(df, None, self._spy())
        for k in CTX_FEATURES:
            self.assertIn(k, result, f"Missing: {k}")

    def test_nan_on_empty(self):
        result = market_context.compute(pd.DataFrame(), None, None)
        for k in CTX_FEATURES:
            self.assertTrue(math.isnan(result[k]))

    def test_nan_on_none(self):
        result = market_context.compute(None, None, None)
        for k in CTX_FEATURES:
            self.assertTrue(math.isnan(result[k]))

    # ------------------------------------------------------------------
    # rs_vs_benchmark_intraday
    # ------------------------------------------------------------------

    def test_rs_intraday_zero_when_spy_unavailable(self):
        # ticker has enough bars; no SPY → neutral 0.0
        df = _make_df(25, c=100.0)
        result = market_context.compute(df, None, None)
        self.assertAlmostEqual(result["rs_vs_benchmark_intraday"], 0.0, places=4)

    def test_rs_intraday_zero_when_equal_returns(self):
        # Ticker and SPY have identical flat price → same return → rs = 0
        df  = _make_df(25, c=100.0)
        spy = _make_df(25, c=500.0)
        result = market_context.compute(df, None, spy)
        self.assertAlmostEqual(result["rs_vs_benchmark_intraday"], 0.0, places=4)

    def test_rs_intraday_positive_when_ticker_outperforms(self):
        # Ticker rises 5%, SPY flat → rs > 0
        ticker_rows = []
        spy_rows    = []
        base = datetime(2026, 3, 28, 9, 35, 0, tzinfo=UTC)
        for i in range(25):
            ts = (base + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            ticker_price = 100.0 + i * 0.25   # rising
            spy_price    = 500.0               # flat
            ticker_rows.append({"bar_ts_utc": ts, "open": ticker_price,
                                 "high": ticker_price + 0.1, "low": ticker_price - 0.1,
                                 "close": ticker_price, "volume": 1000})
            spy_rows.append({"bar_ts_utc": ts, "open": spy_price,
                             "high": spy_price + 0.1, "low": spy_price - 0.1,
                             "close": spy_price, "volume": 5000})
        result = market_context.compute(
            pd.DataFrame(ticker_rows), None, pd.DataFrame(spy_rows)
        )
        self.assertGreater(result["rs_vs_benchmark_intraday"], 0.0)

    def test_rs_intraday_nan_when_too_few_ticker_bars(self):
        df  = _make_df(10)   # < 21 bars needed
        spy = _make_df(80)
        result = market_context.compute(df, None, spy)
        self.assertTrue(math.isnan(result["rs_vs_benchmark_intraday"]))

    # ------------------------------------------------------------------
    # daily_rs_vs_benchmark
    # ------------------------------------------------------------------

    def test_daily_rs_nan_when_too_few_bars(self):
        df  = _make_df(50)   # < 61 bars needed
        spy = _make_df(80)
        result = market_context.compute(df, None, spy)
        self.assertTrue(math.isnan(result["daily_rs_vs_benchmark"]))

    def test_daily_rs_available_with_enough_bars(self):
        df  = _make_trending_df(70)
        spy = _make_df(80)
        result = market_context.compute(df, None, spy)
        self.assertFalse(math.isnan(result["daily_rs_vs_benchmark"]))

    # ------------------------------------------------------------------
    # benchmark_momentum_12bar
    # ------------------------------------------------------------------

    def test_benchmark_momentum_zero_when_spy_flat(self):
        df  = _make_df(25)
        spy = _make_df(25, c=500.0)  # flat SPY
        result = market_context.compute(df, None, spy)
        self.assertAlmostEqual(result["benchmark_momentum_12bar"], 0.0, places=4)

    def test_benchmark_momentum_zero_when_no_spy(self):
        df = _make_df(25)
        result = market_context.compute(df, None, None)
        self.assertAlmostEqual(result["benchmark_momentum_12bar"], 0.0, places=4)

    def test_benchmark_momentum_positive_for_rising_spy(self):
        df  = _make_df(25)
        spy = _make_trending_df(25, start=500.0)
        result = market_context.compute(df, None, spy)
        self.assertGreater(result["benchmark_momentum_12bar"], 0.0)

    # ------------------------------------------------------------------
    # Placeholder features
    # ------------------------------------------------------------------

    def test_cross_asset_correlation_is_placeholder_zero(self):
        result = market_context.compute(_make_df(25), None, None)
        self.assertAlmostEqual(result["cross_asset_correlation"], 0.0, places=6)

    def test_daily_conviction_is_placeholder_zero(self):
        result = market_context.compute(_make_df(25), None, None)
        self.assertAlmostEqual(result["daily_conviction"], 0.0, places=6)


# ---------------------------------------------------------------------------
# TestEngineRegistryPart2
# ---------------------------------------------------------------------------

class TestEngineRegistryPart2(unittest.TestCase):

    def test_volume_module_registered(self):
        self.assertIn(volume, FACTOR_MODULES)

    def test_market_context_module_registered(self):
        self.assertIn(market_context, FACTOR_MODULES)

    def test_total_expected_features_is_20(self):
        self.assertEqual(len(_EXPECTED_FEATURES), 20)

    def test_all_vol_features_in_expected(self):
        for k in VOL_FEATURES:
            self.assertIn(k, _EXPECTED_FEATURES)

    def test_all_ctx_features_in_expected(self):
        for k in CTX_FEATURES:
            self.assertIn(k, _EXPECTED_FEATURES)

    def test_no_duplicate_feature_names(self):
        self.assertEqual(len(_EXPECTED_FEATURES), len(set(_EXPECTED_FEATURES)))


# ---------------------------------------------------------------------------
# TestEnginePart2Integration — DB run with 20 features
# ---------------------------------------------------------------------------

class TestEnginePart2Integration(unittest.TestCase):

    def _seed_db(self, db_path: str, symbols: list[str]) -> None:
        db  = VanguardDB(db_path)
        now = datetime(2026, 3, 28, 14, 0, 0, tzinfo=UTC)
        rows_5m = []
        rows_1h = []
        for sym in symbols:
            price = 100.0
            for i in range(80):
                price += 0.05
                ts = (now - timedelta(minutes=5 * (79 - i))).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows_5m.append({
                    "symbol": sym, "bar_ts_utc": ts,
                    "open": price - 0.025, "high": price + 0.05,
                    "low": price - 0.05, "close": price,
                    "volume": 1000, "asset_class": "equity", "data_source": "alpaca",
                })
            for i in range(24):
                ts = (now - timedelta(hours=(23 - i))).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows_1h.append({
                    "symbol": sym, "bar_ts_utc": ts,
                    "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                    "volume": 5000,
                })
        db.upsert_bars_5m(rows_5m)
        db.upsert_bars_1h(rows_1h)
        health = [
            {"symbol": s, "cycle_ts_utc": "2026-03-28T14:00:00Z",
             "status": "ACTIVE", "relative_volume": 1.0, "spread_bps": 20.0,
             "bars_available": 80, "last_bar_ts_utc": "2026-03-28T13:55:00Z"}
            for s in symbols
        ]
        db.upsert_health(health)

    def test_run_produces_20_features_per_symbol(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY", "AAPL"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            results = run(symbols=["SPY", "AAPL"], dry_run=False)
        self.assertEqual(len(results), 2)
        for r in results:
            feats = json.loads(r["features_json"])
            self.assertEqual(
                len(feats), 20,
                f"{r['symbol']}: expected 20 features, got {len(feats)}: {list(feats.keys())}"
            )

    def test_all_20_feature_names_present(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            results = run(symbols=["SPY"], dry_run=False)
        feats = json.loads(results[0]["features_json"])
        for k in _EXPECTED_FEATURES:
            self.assertIn(k, feats, f"Missing feature: {k}")

    def test_nan_rate_acceptable(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY", "AAPL", "MSFT"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            results = run(symbols=["SPY", "AAPL", "MSFT"], dry_run=False)
        for r in results:
            feats    = json.loads(r["features_json"])
            nan_ct   = sum(1 for v in feats.values() if isinstance(v, float) and math.isnan(v))
            nan_rate = nan_ct / len(feats)
            self.assertLess(nan_rate, 0.5,
                f"{r['symbol']}: NaN rate {nan_rate:.0%} too high")

    def test_validate_passes_after_run(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY", "AAPL"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            run(symbols=["SPY", "AAPL"], dry_run=False)
            ok = validate()
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
