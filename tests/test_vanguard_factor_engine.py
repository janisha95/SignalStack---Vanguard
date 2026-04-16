"""
test_vanguard_factor_engine.py — Unit tests for V3 Vanguard Factor Engine.

Run: python3 -m pytest tests/test_vanguard_factor_engine.py -v

Location: ~/SS/Vanguard/tests/test_vanguard_factor_engine.py
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
from vanguard.factors import price_location, momentum
from vanguard.factors.price_location import FEATURE_NAMES as PL_FEATURES
from vanguard.factors.momentum import FEATURE_NAMES as MOM_FEATURES
from stages.vanguard_factor_engine import (
    compute_features,
    run,
    validate,
    _load_bars_df,
    _EXPECTED_FEATURES,
    FACTOR_MODULES,
    _merge_matrix_features,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db() -> str:
    d = tempfile.mkdtemp()
    return os.path.join(d, "vanguard_universe.db")


def _make_df(n: int, base_ts: datetime = None, interval_min: int = 5,
             o: float = 100.0, h: float = 101.0, l: float = 99.0,
             c: float = 100.0, v: int = 1000) -> pd.DataFrame:
    """Generate a simple OHLCV DataFrame with n bars."""
    if base_ts is None:
        base_ts = datetime(2026, 3, 28, 14, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        ts = base_ts + timedelta(minutes=i * interval_min)
        rows.append({
            "bar_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })
    return pd.DataFrame(rows)


def _make_trending_df(n: int, start_price: float = 100.0) -> pd.DataFrame:
    """Generate a steadily rising bar series."""
    base_ts = datetime(2026, 3, 28, 9, 35, 0, tzinfo=UTC)
    rows = []
    price = start_price
    for i in range(n):
        ts = base_ts + timedelta(minutes=i * 5)
        price += 0.1
        rows.append({
            "bar_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": price - 0.05, "high": price + 0.1,
            "low": price - 0.1, "close": price, "volume": 1000,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# TestPriceLocation
# ---------------------------------------------------------------------------

class TestPriceLocation(unittest.TestCase):

    def _df(self, n: int = 60, **kwargs) -> pd.DataFrame:
        return _make_df(n, **kwargs)

    def test_returns_all_five_features(self):
        df = self._df()
        result = price_location.compute(df, None, None)
        for k in PL_FEATURES:
            self.assertIn(k, result, f"Missing feature: {k}")

    def test_nan_on_empty_df(self):
        result = price_location.compute(pd.DataFrame(), None, None)
        for k in PL_FEATURES:
            self.assertIn(k, result)
            self.assertTrue(math.isnan(result[k]), f"{k} should be NaN on empty input")

    def test_nan_on_none_df(self):
        result = price_location.compute(None, None, None)
        for k in PL_FEATURES:
            self.assertTrue(math.isnan(result[k]))

    def test_session_vwap_distance_zero_for_flat_bars(self):
        # Flat bars: open=high=low=close=100, VWAP=100, close=100 → distance=0
        df = _make_df(60, h=100.0, l=100.0, c=100.0, o=100.0)
        result = price_location.compute(df, None, None)
        self.assertAlmostEqual(result["session_vwap_distance"], 0.0, places=4)

    def test_premium_discount_zone_at_high(self):
        # close = session_high → zone = 1.0
        df = _make_df(60, h=110.0, l=90.0, c=110.0)
        result = price_location.compute(df, None, None)
        self.assertAlmostEqual(result["premium_discount_zone"], 1.0, places=3)

    def test_premium_discount_zone_at_low(self):
        # close = session_low → zone = 0.0
        df = _make_df(60, h=110.0, l=90.0, c=90.0)
        result = price_location.compute(df, None, None)
        self.assertAlmostEqual(result["premium_discount_zone"], 0.0, places=3)

    def test_premium_discount_zone_clipped(self):
        result = price_location.compute(_make_df(60), None, None)
        pdz = result["premium_discount_zone"]
        self.assertGreaterEqual(pdz, 0.0)
        self.assertLessEqual(pdz, 1.0)

    def test_gap_pct_zero_when_no_prev_data(self):
        # No 1h bars → gap = 0.0 (fallback)
        df = _make_df(60)
        result = price_location.compute(df, None, None)
        self.assertAlmostEqual(result["gap_pct"], 0.0, places=4)

    def test_gap_pct_positive_gap_up(self):
        df_5m = _make_df(60, o=110.0, c=110.0)  # session open = 110
        # 1h bars: last two bars, prev close = 100
        df_1h = _make_df(3, interval_min=60, c=100.0)
        result = price_location.compute(df_5m, df_1h, None)
        # gap = 110 / 100 - 1 = 0.10
        self.assertAlmostEqual(result["gap_pct"], 0.10, places=3)

    def test_daily_drawdown_at_session_high_is_zero(self):
        # Ascending price; last bar is the highest → drawdown = 0
        df = _make_trending_df(60)
        result = price_location.compute(df, None, None)
        self.assertAlmostEqual(result["daily_drawdown_from_high"], 0.0, places=4)

    def test_daily_drawdown_negative_when_below_high(self):
        # Descending bars: price falls over time → last bar below cummax
        base = datetime(2026, 3, 28, 9, 35, 0, tzinfo=UTC)
        rows = []
        for i in range(60):
            price = 100.0 - i * 0.1
            ts = base + timedelta(minutes=i * 5)
            rows.append({
                "bar_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": price, "high": price + 0.05,
                "low": price - 0.05, "close": price, "volume": 1000,
            })
        df = pd.DataFrame(rows)
        result = price_location.compute(df, None, None)
        self.assertLess(result["daily_drawdown_from_high"], 0.0)

    def test_opening_range_position_in_range(self):
        result = price_location.compute(_make_df(60), None, None)
        orp = result["session_opening_range_position"]
        self.assertFalse(math.isnan(orp))


# ---------------------------------------------------------------------------
# TestMomentum
# ---------------------------------------------------------------------------

class TestMomentum(unittest.TestCase):

    def _df(self, n: int = 60, **kwargs) -> pd.DataFrame:
        return _make_df(n, **kwargs)

    def test_returns_all_five_features(self):
        df = _make_trending_df(60)
        result = momentum.compute(df, None, None)
        for k in MOM_FEATURES:
            self.assertIn(k, result, f"Missing: {k}")

    def test_nan_on_empty(self):
        result = momentum.compute(pd.DataFrame(), None, None)
        for k in MOM_FEATURES:
            self.assertTrue(math.isnan(result[k]))

    def test_nan_on_none(self):
        result = momentum.compute(None, None, None)
        for k in MOM_FEATURES:
            self.assertTrue(math.isnan(result[k]))

    def test_nan_when_too_few_bars(self):
        df = _make_df(3)  # only 3 bars — below all thresholds
        result = momentum.compute(df, None, None)
        for k in MOM_FEATURES:
            self.assertTrue(math.isnan(result[k]))

    def test_momentum_3bar_positive_uptrend(self):
        df = _make_trending_df(20)
        result = momentum.compute(df, None, None)
        self.assertFalse(math.isnan(result["momentum_3bar"]))
        self.assertGreater(result["momentum_3bar"], 0.0)

    def test_momentum_3bar_zero_flat_bars(self):
        df = _make_df(20, c=100.0)  # flat: all closes = 100
        result = momentum.compute(df, None, None)
        self.assertAlmostEqual(result["momentum_3bar"], 0.0, places=4)

    def test_momentum_12bar_nan_when_insufficient(self):
        df = _make_df(10)  # < 13 bars needed for momentum_12bar
        result = momentum.compute(df, None, None)
        self.assertTrue(math.isnan(result["momentum_12bar"]))

    def test_momentum_12bar_available_with_enough_bars(self):
        df = _make_trending_df(20)
        result = momentum.compute(df, None, None)
        self.assertFalse(math.isnan(result["momentum_12bar"]))

    def test_momentum_acceleration_needs_10_bars(self):
        df = _make_df(9)  # below threshold
        result = momentum.compute(df, None, None)
        self.assertTrue(math.isnan(result["momentum_acceleration"]))

    def test_atr_expansion_nan_below_51_bars(self):
        df = _make_df(50)  # need 51 for ATR(50)
        result = momentum.compute(df, None, None)
        self.assertTrue(math.isnan(result["atr_expansion"]))

    def test_atr_expansion_positive_with_enough_bars(self):
        df = _make_trending_df(60)
        result = momentum.compute(df, None, None)
        self.assertFalse(math.isnan(result["atr_expansion"]))
        self.assertGreater(result["atr_expansion"], 0.0)

    def test_atr_expansion_around_1_for_constant_volatility(self):
        # Consistent volatility: ATR(14) ≈ ATR(50) → expansion ≈ 1.0
        df = _make_df(60, h=101.0, l=99.0, c=100.0)  # constant range = 2
        result = momentum.compute(df, None, None)
        if not math.isnan(result["atr_expansion"]):
            self.assertAlmostEqual(result["atr_expansion"], 1.0, places=1)

    def test_daily_adx_uses_1h_bars_when_available(self):
        df_5m = _make_df(60)
        df_1h = _make_trending_df(20)
        # rename df_1h interval to 60 min
        base = datetime(2026, 3, 28, 9, 0, 0, tzinfo=UTC)
        rows = []
        price = 100.0
        for i in range(20):
            price += 0.5
            ts = base + timedelta(hours=i)
            rows.append({"bar_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "open": price-0.25, "high": price+0.5,
                         "low": price-0.5, "close": price, "volume": 1000})
        df_1h = pd.DataFrame(rows)
        result = momentum.compute(df_5m, df_1h, None)
        self.assertFalse(math.isnan(result["daily_adx"]))
        self.assertGreaterEqual(result["daily_adx"], 0.0)
        self.assertLessEqual(result["daily_adx"], 100.0)

    def test_daily_adx_falls_back_to_5m_when_no_1h(self):
        df = _make_trending_df(60)
        result = momentum.compute(df, None, None)
        self.assertFalse(math.isnan(result["daily_adx"]))


# ---------------------------------------------------------------------------
# TestComputeFeatures — engine integration
# ---------------------------------------------------------------------------

class TestComputeFeatures(unittest.TestCase):

    def test_all_expected_features_present(self):
        df = _make_trending_df(60)
        result = compute_features("AAPL", df, None, None)
        for k in _EXPECTED_FEATURES:
            self.assertIn(k, result)

    def test_module_failure_fills_nan_not_crash(self):
        """If a module raises, other modules' features are preserved."""
        import types

        # Create a broken module
        broken = types.ModuleType("broken_module")
        broken.FEATURE_NAMES = ["broken_feat"]
        broken.compute = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("oops"))

        df = _make_trending_df(60)
        # Temporarily patch FACTOR_MODULES
        import stages.vanguard_factor_engine as eng
        orig = eng.FACTOR_MODULES[:]
        eng.FACTOR_MODULES = [price_location, broken]
        try:
            result = compute_features("SPY", df, None, None)
        finally:
            eng.FACTOR_MODULES = orig

        # price_location features should still be present
        for k in PL_FEATURES:
            self.assertIn(k, result)
        # broken feature defaults to NaN
        self.assertTrue(math.isnan(result.get("broken_feat", float("nan"))))

    def test_empty_df_returns_all_nan(self):
        result = compute_features("AAPL", pd.DataFrame(), None, None)
        for k in _EXPECTED_FEATURES:
            self.assertIn(k, result)
            self.assertTrue(math.isnan(result[k]))

    def test_crypto_merge_preserves_base_volume_delta_when_vp_missing(self):
        features = {"volume_delta": 0.42, "momentum_3bar": 1.0}
        vp_vals = {
            "poc": None,
            "vah": None,
            "val": None,
            "poc_distance": None,
            "vah_distance": None,
            "val_distance": None,
            "vp_skew": None,
            "volume_delta": None,
            "cum_delta": None,
            "delta_divergence": None,
        }
        merged = _merge_matrix_features("crypto", features, vp_vals)
        self.assertEqual(merged["volume_delta"], 0.42)

    def test_forex_merge_still_allows_vp_volume_delta_override(self):
        features = {"volume_delta": 0.42, "momentum_3bar": 1.0}
        vp_vals = {
            "poc": None,
            "vah": None,
            "val": None,
            "poc_distance": None,
            "vah_distance": None,
            "val_distance": None,
            "vp_skew": None,
            "volume_delta": -0.17,
            "cum_delta": None,
            "delta_divergence": None,
        }
        merged = _merge_matrix_features("forex", features, vp_vals)
        self.assertEqual(merged["volume_delta"], -0.17)


# ---------------------------------------------------------------------------
# TestFactorEngineIntegration — DB read/write
# ---------------------------------------------------------------------------

class TestFactorEngineIntegration(unittest.TestCase):

    def _seed_db(self, db_path: str, symbols: list[str]) -> None:
        """Insert 60 5m bars and 24 1h bars for each symbol into temp DB."""
        db   = VanguardDB(db_path)
        now  = datetime(2026, 3, 28, 14, 0, 0, tzinfo=UTC)
        rows_5m = []
        rows_1h = []

        for sym in symbols:
            # 5m bars
            price = 100.0
            for i in range(60):
                price += 0.05
                ts = (now - timedelta(minutes=5 * (59 - i))).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows_5m.append({
                    "symbol": sym, "bar_ts_utc": ts,
                    "open": price - 0.025, "high": price + 0.05,
                    "low": price - 0.05, "close": price,
                    "volume": 1000, "asset_class": "equity", "data_source": "alpaca",
                })
            # 1h bars
            for i in range(24):
                ts = (now - timedelta(hours=(23 - i))).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows_1h.append({
                    "symbol": sym, "bar_ts_utc": ts,
                    "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                    "volume": 5000,
                })
        db.upsert_bars_5m(rows_5m)
        db.upsert_bars_1h(rows_1h)

        # Seed health rows so get_active_symbols() returns something
        health_rows = [
            {"symbol": s, "cycle_ts_utc": "2026-03-28T14:00:00Z",
             "status": "ACTIVE", "relative_volume": 1.0, "spread_bps": 20.0,
             "bars_available": 60, "last_bar_ts_utc": "2026-03-28T13:55:00Z"}
            for s in symbols
        ]
        db.upsert_health(health_rows)

    def test_run_produces_feature_rows(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY", "AAPL", "MSFT"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            results = run(symbols=["SPY", "AAPL", "MSFT"], dry_run=False)
        self.assertEqual(len(results), 3)
        syms = {r["symbol"] for r in results}
        self.assertIn("SPY", syms)

    def test_run_dry_run_does_not_write(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY", "AAPL"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            run(symbols=["SPY", "AAPL"], dry_run=True)
        db = VanguardDB(db_path)
        conn = db.connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM vanguard_features").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 0)

    def test_run_features_json_valid(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            results = run(symbols=["SPY"], dry_run=False)
        feat = json.loads(results[0]["features_json"])
        for k in _EXPECTED_FEATURES:
            self.assertIn(k, feat)

    def test_run_reads_active_symbols_from_health(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY", "AAPL"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            # No --symbols: should read from vanguard_health
            results = run(dry_run=True)
        self.assertEqual(len(results), 2)

    def test_validate_passes_after_run(self):
        db_path = _tmp_db()
        self._seed_db(db_path, ["SPY", "AAPL"])
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            run(symbols=["SPY", "AAPL"], dry_run=False)
            ok = validate()
        self.assertTrue(ok)

    def test_validate_fails_when_empty(self):
        db_path = _tmp_db()
        VanguardDB(db_path)  # just create tables
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            ok = validate()
        self.assertFalse(ok)

    def test_run_no_active_symbols_returns_empty(self):
        db_path = _tmp_db()
        VanguardDB(db_path)  # tables but no health data
        with patch("stages.vanguard_factor_engine.DB_PATH", db_path):
            results = run(dry_run=True)
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# TestModuleRegistry
# ---------------------------------------------------------------------------

class TestModuleRegistry(unittest.TestCase):

    def test_both_part1_modules_registered(self):
        self.assertIn(price_location, FACTOR_MODULES)
        self.assertIn(momentum, FACTOR_MODULES)

    def test_feature_names_cover_expected(self):
        registered = set()
        for mod in FACTOR_MODULES:
            registered.update(getattr(mod, "FEATURE_NAMES", []))
        for k in _EXPECTED_FEATURES:
            self.assertIn(k, registered, f"Feature {k} not in any module")


if __name__ == "__main__":
    unittest.main()
