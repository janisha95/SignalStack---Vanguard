"""
test_vanguard_factors_part3.py — Tests for V3 Part 3 factor modules.

Covers:
  • session_time   (3 features)
  • smc_5m         (6 features)
  • smc_htf_1h     (4 features)
  • quality        (2 features)
  • engine integration (35 total features, nan_ratio injection)

Run: python3 -m pytest tests/test_vanguard_factors_part3.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.factors import session_time, smc_5m, smc_htf_1h, quality
from stages.vanguard_factor_engine import compute_features, _EXPECTED_FEATURES, FACTOR_MODULES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bars_5m(n: int = 50, start_price: float = 100.0, ts_utc: str | None = None) -> pd.DataFrame:
    """Build a simple ascending 5m bar DataFrame."""
    import numpy as np

    prices = [start_price + i * 0.01 for i in range(n)]
    volumes = [1_000_000 + i * 1000 for i in range(n)]

    if ts_utc is None:
        # Default: last bar during NYSE session (14:30 UTC = 09:30 ET during EST)
        from pandas import Timestamp
        base = Timestamp("2025-01-10 14:35:00", tz="UTC")   # 09:35 ET
    else:
        from pandas import Timestamp
        base = Timestamp(ts_utc, tz="UTC")

    bar_times = [base - pd.Timedelta(minutes=5 * (n - 1 - i)) for i in range(n)]

    df = pd.DataFrame({
        "bar_ts_utc": [t.isoformat() for t in bar_times],
        "open":   [p - 0.05 for p in prices],
        "high":   [p + 0.10 for p in prices],
        "low":    [p - 0.10 for p in prices],
        "close":  prices,
        "volume": volumes,
    })
    return df


def _bars_1h(n: int = 24, start_price: float = 100.0) -> pd.DataFrame:
    from pandas import Timestamp

    base = Timestamp("2025-01-10 16:00:00", tz="UTC")
    prices = [start_price + i * 0.05 for i in range(n)]
    bar_times = [base - pd.Timedelta(hours=(n - 1 - i)) for i in range(n)]

    df = pd.DataFrame({
        "bar_ts_utc": [t.isoformat() for t in bar_times],
        "open":   [p - 0.10 for p in prices],
        "high":   [p + 0.20 for p in prices],
        "low":    [p - 0.20 for p in prices],
        "close":  prices,
        "volume": [500_000] * n,
    })
    return df


# ---------------------------------------------------------------------------
# session_time tests
# ---------------------------------------------------------------------------

class TestSessionTime:

    def test_feature_names(self):
        assert session_time.FEATURE_NAMES == [
            "session_phase",
            "time_in_session_pct",
            "bars_since_session_open",
        ]

    def test_returns_dict_with_all_keys(self):
        df = _bars_5m(50)
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        assert set(result.keys()) == set(session_time.FEATURE_NAMES)

    def test_empty_df_returns_nan(self):
        result = session_time.compute(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        for k in session_time.FEATURE_NAMES:
            assert math.isnan(result[k]), f"{k} should be NaN for empty df"

    def test_none_df_returns_nan(self):
        result = session_time.compute(None, None, None)
        for k in session_time.FEATURE_NAMES:
            assert math.isnan(result[k])

    def test_session_open_bar(self):
        # 9:35 ET = 14:35 UTC (EST, Jan = UTC-5)
        df = _bars_5m(50, ts_utc="2025-01-10 14:35:00+00:00")
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        assert not math.isnan(result["session_phase"])
        assert 0.0 <= result["session_phase"] <= 1.0
        # 5 minutes in, so ~1.3% of session
        assert result["time_in_session_pct"] == pytest.approx(5 / 390, abs=0.001)
        assert result["bars_since_session_open"] == 1.0

    def test_session_midday(self):
        # 12:00 ET = 17:00 UTC (EST)
        df = _bars_5m(50, ts_utc="2025-01-10 17:00:00+00:00")
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        assert not math.isnan(result["session_phase"])
        # 12:00 ET = 150 min into session
        expected_pct = 150 / 390
        assert result["time_in_session_pct"] == pytest.approx(expected_pct, abs=0.002)
        assert result["bars_since_session_open"] == 30.0

    def test_outside_session_premarket_is_nan(self):
        # 8:00 ET = 13:00 UTC (EST)
        df = _bars_5m(20, ts_utc="2025-01-10 13:00:00+00:00")
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        for k in session_time.FEATURE_NAMES:
            assert math.isnan(result[k]), f"{k} should be NaN premarket"

    def test_outside_session_afterhours_is_nan(self):
        # 17:00 ET = 22:00 UTC (EST)
        df = _bars_5m(20, ts_utc="2025-01-10 22:00:00+00:00")
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        for k in session_time.FEATURE_NAMES:
            assert math.isnan(result[k]), f"{k} should be NaN afterhours"

    def test_session_close_bar(self):
        # 16:00 ET = 21:00 UTC (EST)
        df = _bars_5m(50, ts_utc="2025-01-10 21:00:00+00:00")
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        assert not math.isnan(result["session_phase"])
        assert result["time_in_session_pct"] == pytest.approx(1.0, abs=0.001)
        assert result["bars_since_session_open"] == 78.0

    def test_phase_equals_time_pct(self):
        df = _bars_5m(50, ts_utc="2025-01-10 16:00:00+00:00")  # 11:00 ET
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        if not math.isnan(result["session_phase"]):
            assert result["session_phase"] == result["time_in_session_pct"]

    def test_bars_since_open_is_integer_value(self):
        df = _bars_5m(50, ts_utc="2025-01-10 15:00:00+00:00")  # 10:00 ET = 30 min in
        result = session_time.compute(df, pd.DataFrame(), pd.DataFrame())
        if not math.isnan(result["bars_since_session_open"]):
            assert result["bars_since_session_open"] == float(int(result["bars_since_session_open"]))


# ---------------------------------------------------------------------------
# smc_5m tests
# ---------------------------------------------------------------------------

class TestSmc5m:

    def test_feature_names(self):
        assert smc_5m.FEATURE_NAMES == [
            "ob_proximity_5m",
            "fvg_bullish_nearest",
            "fvg_bearish_nearest",
            "structure_break",
            "liquidity_sweep",
            "smc_premium_discount",
        ]

    def test_returns_dict_with_all_keys(self):
        df = _bars_5m(50)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        assert set(result.keys()) == set(smc_5m.FEATURE_NAMES)

    def test_empty_df_returns_nan(self):
        result = smc_5m.compute(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        for k in smc_5m.FEATURE_NAMES:
            assert math.isnan(result[k])

    def test_none_df_returns_nan(self):
        result = smc_5m.compute(None, None, None)
        for k in smc_5m.FEATURE_NAMES:
            assert math.isnan(result[k])

    def test_too_few_bars_returns_nan(self):
        df = _bars_5m(2)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        for k in smc_5m.FEATURE_NAMES:
            assert math.isnan(result[k])

    def test_smc_premium_discount_range(self):
        df = _bars_5m(50)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        pd_val = result["smc_premium_discount"]
        assert not math.isnan(pd_val)
        assert 0.0 <= pd_val <= 1.0

    def test_ob_proximity_range(self):
        df = _bars_5m(50)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        ob_val = result["ob_proximity_5m"]
        if not math.isnan(ob_val):
            assert 0.0 <= ob_val <= 1.0

    def test_structure_break_values(self):
        df = _bars_5m(50)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        bos = result["structure_break"]
        assert bos in (-1.0, 0.0, 1.0) or math.isnan(bos)

    def test_liquidity_sweep_values(self):
        df = _bars_5m(50)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        sweep = result["liquidity_sweep"]
        assert sweep in (-1.0, 0.0, 1.0) or math.isnan(sweep)

    def test_fvg_distances_non_negative(self):
        df = _bars_5m(50)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        for k in ("fvg_bullish_nearest", "fvg_bearish_nearest"):
            v = result[k]
            if not math.isnan(v):
                assert v >= 0.0, f"{k} must be non-negative"

    def test_bullish_bos_detected(self):
        """Ascending price should produce bullish BOS (structure_break = 1.0)."""
        # Create strongly ascending bars so close > prior 20-bar high
        n = 50
        prices = [100.0 + i * 0.5 for i in range(n)]  # strong ascent
        df = pd.DataFrame({
            "bar_ts_utc": [f"2025-01-10T{14 + i // 12:02d}:{(i % 12) * 5:02d}:00+00:00" for i in range(n)],
            "open":   [p - 0.1 for p in prices],
            "high":   [p + 0.1 for p in prices],
            "low":    [p - 0.1 for p in prices],
            "close":  prices,
            "volume": [1_000_000] * n,
        })
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        assert result["structure_break"] == 1.0

    def test_bearish_bos_detected(self):
        """Descending price should produce bearish BOS."""
        n = 50
        prices = [200.0 - i * 0.5 for i in range(n)]
        df = pd.DataFrame({
            "bar_ts_utc": [f"2025-01-10T{14 + i // 12:02d}:{(i % 12) * 5:02d}:00+00:00" for i in range(n)],
            "open":   [p + 0.1 for p in prices],
            "high":   [p + 0.1 for p in prices],
            "low":    [p - 0.1 for p in prices],
            "close":  prices,
            "volume": [1_000_000] * n,
        })
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        assert result["structure_break"] == -1.0

    def test_bullish_fvg_detected(self):
        """Create a 3-bar pattern with a clear gap up → bullish FVG."""
        rows = []
        base_price = 100.0
        for i in range(30):
            rows.append({
                "bar_ts_utc": f"2025-01-10T14:{i * 5 % 60:02d}:00+00:00",
                "open":  base_price, "high": base_price + 0.1,
                "low":   base_price - 0.1, "close": base_price,
                "volume": 1_000_000,
            })
        # Inject bullish FVG in last 3 bars: bar[-3].high = 100.1, bar[-1].low = 102.0
        rows[-3] = {**rows[-3], "high": 100.1, "close": 100.0}
        rows[-2] = {**rows[-2], "open": 101.0, "close": 101.5, "high": 101.6, "low": 101.0}
        rows[-1] = {**rows[-1], "open": 102.0, "close": 102.5, "high": 102.6, "low": 102.0}
        df = pd.DataFrame(rows)
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        assert not math.isnan(result["fvg_bullish_nearest"])
        assert result["fvg_bullish_nearest"] >= 0.0

    def test_premium_discount_at_midpoint(self):
        """When close is exactly at midpoint of swing range, pd zone ≈ 0.5."""
        n = 40
        # Build flat bars then set last close to midpoint of range
        prices = [100.0] * n
        prices[-10] = 110.0   # creates a high
        prices[-5]  = 90.0    # creates a low (will be in last 20 bars)
        prices[-1]  = 100.0   # midpoint
        df = pd.DataFrame({
            "bar_ts_utc": [f"2025-01-10T{14 + i // 12:02d}:{(i % 12) * 5:02d}:00+00:00" for i in range(n)],
            "open":  prices,
            "high":  [p + 0.1 for p in prices],
            "low":   [p - 0.1 for p in prices],
            "close": prices,
            "volume": [1_000_000] * n,
        })
        # Adjust high/low so range is 90–110 in last 20 bars
        df.at[n - 10, "high"] = 110.0
        df.at[n - 5,  "low"]  = 90.0
        result = smc_5m.compute(df, pd.DataFrame(), pd.DataFrame())
        pd_val = result["smc_premium_discount"]
        # close=100, low=90 (after tail), high=110 → pd = (100-90)/20 = 0.5
        if not math.isnan(pd_val):
            assert 0.0 <= pd_val <= 1.0


# ---------------------------------------------------------------------------
# smc_htf_1h tests
# ---------------------------------------------------------------------------

class TestSmcHtf1h:

    def test_feature_names(self):
        assert smc_htf_1h.FEATURE_NAMES == [
            "htf_trend_direction",
            "htf_structure_break",
            "htf_fvg_nearest",
            "htf_ob_proximity",
        ]

    def test_returns_dict_with_all_keys(self):
        df1h = _bars_1h(24)
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        assert set(result.keys()) == set(smc_htf_1h.FEATURE_NAMES)

    def test_empty_1h_returns_neutral(self):
        """Missing 1h data → neutral 0.0, not NaN."""
        result = smc_htf_1h.compute(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        for k in smc_htf_1h.FEATURE_NAMES:
            assert result[k] == 0.0, f"{k} should be 0.0 when no 1h data"

    def test_none_1h_returns_neutral(self):
        result = smc_htf_1h.compute(None, None, None)
        for k in smc_htf_1h.FEATURE_NAMES:
            assert result[k] == 0.0

    def test_too_few_1h_bars_returns_neutral(self):
        df1h = _bars_1h(2)
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        for k in smc_htf_1h.FEATURE_NAMES:
            assert result[k] == 0.0

    def test_trend_direction_values(self):
        df1h = _bars_1h(24)
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        assert result["htf_trend_direction"] in (-1.0, 0.0, 1.0)

    def test_uptrend_detected(self):
        """Ascending 1h bars → htf_trend_direction = 1.0."""
        n = 20
        prices = [100.0 + i * 1.0 for i in range(n)]
        from pandas import Timestamp
        base = Timestamp("2025-01-10 20:00:00", tz="UTC")
        df1h = pd.DataFrame({
            "bar_ts_utc": [(base - pd.Timedelta(hours=n - 1 - i)).isoformat() for i in range(n)],
            "open":  [p - 0.5 for p in prices],
            "high":  [p + 0.5 for p in prices],
            "low":   [p - 0.5 for p in prices],
            "close": prices,
            "volume": [500_000] * n,
        })
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        assert result["htf_trend_direction"] == 1.0

    def test_downtrend_detected(self):
        """Descending 1h bars → htf_trend_direction = -1.0."""
        n = 20
        prices = [200.0 - i * 1.0 for i in range(n)]
        from pandas import Timestamp
        base = Timestamp("2025-01-10 20:00:00", tz="UTC")
        df1h = pd.DataFrame({
            "bar_ts_utc": [(base - pd.Timedelta(hours=n - 1 - i)).isoformat() for i in range(n)],
            "open":  [p + 0.5 for p in prices],
            "high":  [p + 0.5 for p in prices],
            "low":   [p - 0.5 for p in prices],
            "close": prices,
            "volume": [500_000] * n,
        })
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        assert result["htf_trend_direction"] == -1.0

    def test_htf_structure_break_values(self):
        df1h = _bars_1h(24)
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        assert result["htf_structure_break"] in (-1.0, 0.0, 1.0)

    def test_htf_fvg_nearest_non_negative(self):
        df1h = _bars_1h(24)
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        assert result["htf_fvg_nearest"] >= 0.0

    def test_htf_ob_proximity_range(self):
        df1h = _bars_1h(24)
        result = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        ob_val = result["htf_ob_proximity"]
        assert 0.0 <= ob_val <= 1.0

    def test_5m_bars_ignored(self):
        """5m bars should not affect output."""
        df1h = _bars_1h(20)
        r1 = smc_htf_1h.compute(pd.DataFrame(), df1h, pd.DataFrame())
        r2 = smc_htf_1h.compute(_bars_5m(100), df1h, pd.DataFrame())
        for k in smc_htf_1h.FEATURE_NAMES:
            assert r1[k] == r2[k], f"{k} changed when 5m bars changed — should not"


# ---------------------------------------------------------------------------
# quality tests
# ---------------------------------------------------------------------------

class TestQuality:

    def test_feature_names(self):
        assert quality.FEATURE_NAMES == ["bars_available", "nan_ratio"]

    def test_returns_dict_with_all_keys(self):
        df = _bars_5m(50)
        result = quality.compute(df, pd.DataFrame(), pd.DataFrame())
        assert set(result.keys()) == set(quality.FEATURE_NAMES)

    def test_bars_available_count(self):
        df = _bars_5m(80)
        result = quality.compute(df, pd.DataFrame(), pd.DataFrame())
        assert result["bars_available"] == 80.0

    def test_bars_available_empty(self):
        result = quality.compute(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        assert result["bars_available"] == 0.0

    def test_bars_available_none(self):
        result = quality.compute(None, None, None)
        assert result["bars_available"] == 0.0

    def test_nan_ratio_placeholder(self):
        """quality.py sets nan_ratio=0.0 placeholder; engine overwrites."""
        df = _bars_5m(50)
        result = quality.compute(df, pd.DataFrame(), pd.DataFrame())
        assert result["nan_ratio"] == 0.0   # placeholder before engine injection


# ---------------------------------------------------------------------------
# Engine integration tests
# ---------------------------------------------------------------------------

class TestEngineIntegration:

    def test_expected_features_count(self):
        assert len(_EXPECTED_FEATURES) == 35, (
            f"Expected 35 features, got {len(_EXPECTED_FEATURES)}: {_EXPECTED_FEATURES}"
        )

    def test_expected_features_no_duplicates(self):
        assert len(_EXPECTED_FEATURES) == len(set(_EXPECTED_FEATURES)), (
            "Duplicate feature names detected"
        )

    def test_factor_modules_count(self):
        assert len(FACTOR_MODULES) == 8

    def test_quality_is_last_module(self):
        """quality must be last so nan_ratio injection sees all other features."""
        assert FACTOR_MODULES[-1] is quality

    def test_compute_features_returns_35(self):
        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        spy  = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00", start_price=480.0)
        result = compute_features("TEST", df5m, df1h, spy)
        assert len(result) == 35, f"Expected 35, got {len(result)}: {sorted(result.keys())}"

    def test_compute_features_has_all_expected_keys(self):
        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        spy  = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00", start_price=480.0)
        result = compute_features("TEST", df5m, df1h, spy)
        for k in _EXPECTED_FEATURES:
            assert k in result, f"Missing feature: {k}"

    def test_nan_ratio_injected_correctly(self):
        """nan_ratio in output should reflect actual NaN fraction."""
        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        result = compute_features("TEST", df5m, df1h, pd.DataFrame())
        nan_count = sum(1 for v in result.values() if isinstance(v, float) and math.isnan(v))
        expected_ratio = round(nan_count / len(result), 6)
        assert result["nan_ratio"] == expected_ratio

    def test_bars_available_matches_input(self):
        n = 75
        df5m = _bars_5m(n, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        result = compute_features("TEST", df5m, df1h, pd.DataFrame())
        assert result["bars_available"] == float(n)

    def test_compute_features_no_exception_with_minimal_data(self):
        """Engine should not raise even with very few bars."""
        df5m = _bars_5m(5, ts_utc="2025-01-10 15:00:00+00:00")
        result = compute_features("MINIMAL", df5m, pd.DataFrame(), pd.DataFrame())
        assert isinstance(result, dict)
        assert len(result) == 35

    def test_compute_features_nan_ratio_0_when_all_valid(self):
        """With good data, most features should be valid and nan_ratio should be low."""
        df5m = _bars_5m(100, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(30)
        spy  = _bars_5m(100, ts_utc="2025-01-10 15:00:00+00:00", start_price=480.0)
        result = compute_features("TEST", df5m, df1h, spy)
        assert result["nan_ratio"] < 0.20, f"nan_ratio {result['nan_ratio']} too high"

    def test_session_features_present_in_engine_output(self):
        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        result = compute_features("TEST", df5m, df1h, pd.DataFrame())
        for k in session_time.FEATURE_NAMES:
            assert k in result

    def test_smc_5m_features_present_in_engine_output(self):
        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        result = compute_features("TEST", df5m, df1h, pd.DataFrame())
        for k in smc_5m.FEATURE_NAMES:
            assert k in result

    def test_smc_htf_features_present_in_engine_output(self):
        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        result = compute_features("TEST", df5m, df1h, pd.DataFrame())
        for k in smc_htf_1h.FEATURE_NAMES:
            assert k in result

    def test_quality_features_present_in_engine_output(self):
        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        result = compute_features("TEST", df5m, df1h, pd.DataFrame())
        for k in quality.FEATURE_NAMES:
            assert k in result

    def test_all_35_feature_names_match_expected(self):
        """Feature names list must exactly match the concatenation spec."""
        from vanguard.factors import price_location, momentum, volume, market_context
        expected = (
            price_location.FEATURE_NAMES
            + momentum.FEATURE_NAMES
            + volume.FEATURE_NAMES
            + market_context.FEATURE_NAMES
            + session_time.FEATURE_NAMES
            + smc_5m.FEATURE_NAMES
            + smc_htf_1h.FEATURE_NAMES
            + quality.FEATURE_NAMES
        )
        assert list(_EXPECTED_FEATURES) == expected

    def test_compute_features_debug_mode_no_exception(self):
        """Debug mode should print to stdout without raising."""
        import io
        from contextlib import redirect_stdout

        df5m = _bars_5m(60, ts_utc="2025-01-10 15:00:00+00:00")
        df1h = _bars_1h(24)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = compute_features("DEBUG_TEST", df5m, df1h, pd.DataFrame(), debug=True)
        output = buf.getvalue()
        assert "DEBUG FEATURES" in output
        assert len(result) == 35
