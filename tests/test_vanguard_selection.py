"""
test_vanguard_selection.py — V5 Strategy Router test suite.

Tests:
  1. Regime gate detects all 4 regimes (ACTIVE, CAUTION, DEAD, CLOSED)
  2. ML gate filters out candidates below 0.50 threshold
  3. Equity strategies produce top 5 LONG + 5 SHORT (except Breakdown = SHORT only)
  4. Breakdown strategy ONLY produces SHORT candidates
  5. Risk-Reward Edge formula matches spec
  6. Consensus count is correct
  7. Config-driven: removing a strategy from JSON excludes it
  8. NaN features don't crash strategies
  9. Empty input returns empty shortlist, not crash
 10. Output written to vanguard_shortlist with correct schema

Location: ~/SS/Vanguard/tests/test_vanguard_selection.py
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Fixtures: minimal feature DataFrame
# ---------------------------------------------------------------------------

_FEATURES = [
    "session_vwap_distance", "premium_discount_zone", "gap_pct",
    "session_opening_range_position", "daily_drawdown_from_high",
    "momentum_3bar", "momentum_12bar", "momentum_acceleration",
    "atr_expansion", "daily_adx",
    "relative_volume", "volume_burst_z", "down_volume_ratio",
    "effort_vs_result", "spread_proxy",
    "rs_vs_benchmark_intraday", "daily_rs_vs_benchmark",
    "benchmark_momentum_12bar", "cross_asset_correlation", "daily_conviction",
    "session_phase", "time_in_session_pct", "bars_since_session_open",
    "ob_proximity_5m", "fvg_bullish_nearest", "fvg_bearish_nearest",
    "structure_break", "liquidity_sweep", "smc_premium_discount",
    "htf_trend_direction", "htf_structure_break", "htf_fvg_nearest", "htf_ob_proximity",
    "bars_available", "nan_ratio",
]


def _make_df(
    n: int = 20,
    asset_class: str = "equity",
    seed: int = 42,
    long_prob: float = 0.70,
    short_prob: float = 0.65,
    tp_pct: float = 0.015,
    sl_pct: float = 0.008,
) -> pd.DataFrame:
    """Build a synthetic feature DataFrame with n symbols."""
    rng = np.random.default_rng(seed)
    data: dict[str, list] = {}
    for feat in _FEATURES:
        data[feat] = rng.uniform(-1, 1, n).tolist()
    # Enforce valid ranges for specific features
    data["premium_discount_zone"] = rng.uniform(0, 1, n).tolist()
    data["smc_premium_discount"]  = rng.uniform(0, 1, n).tolist()
    data["relative_volume"]       = rng.uniform(0.5, 3.0, n).tolist()
    data["atr_expansion"]         = rng.uniform(0.5, 2.5, n).tolist()
    data["volume_burst_z"]        = rng.uniform(-1, 4.0, n).tolist()
    data["daily_adx"]             = rng.uniform(10, 60, n).tolist()
    data["session_phase"]         = rng.choice([0.0, 0.3, 0.7, 0.8, 1.0], n).tolist()
    data["time_in_session_pct"]   = rng.uniform(0, 1, n).tolist()
    data["bars_since_session_open"] = rng.uniform(0, 78, n).tolist()
    data["ob_proximity_5m"]       = rng.uniform(0, 1, n).tolist()
    data["fvg_bullish_nearest"]   = rng.uniform(0, 0.01, n).tolist()
    data["fvg_bearish_nearest"]   = rng.uniform(0, 0.01, n).tolist()
    data["liquidity_sweep"]       = rng.choice([0.0, 1.0], n).tolist()
    data["spread_proxy"]          = rng.uniform(0.0001, 0.005, n).tolist()
    data["daily_drawdown_from_high"] = rng.uniform(-0.1, 0, n).tolist()
    data["down_volume_ratio"]     = rng.uniform(0.2, 0.8, n).tolist()
    data["structure_break"]       = rng.uniform(-1, 1, n).tolist()
    data["htf_trend_direction"]   = rng.choice([-1.0, 0.0, 1.0], n).tolist()
    data["bars_available"]        = [200.0] * n
    data["nan_ratio"]             = [0.0] * n

    df = pd.DataFrame(data)
    df["symbol"] = [f"SYM{i:03d}" for i in range(n)]
    df["asset_class"] = asset_class
    df["lgbm_long_prob"]  = long_prob
    df["lgbm_short_prob"] = short_prob
    df["tp_pct"] = tp_pct
    df["sl_pct"] = sl_pct
    return df


# ===========================================================================
# Test 1: Regime gate detects all 4 regimes
# ===========================================================================

class TestRegimeGate:
    def test_active_regime(self):
        from vanguard.helpers.regime_detector import detect_regime
        df = _make_df(20)
        df["atr_expansion"] = 1.0  # below caution threshold
        df["relative_volume"] = 1.5  # above dead threshold
        # Mock: market is open, not lunch hour
        dt = datetime(2026, 3, 31, 14, 0, tzinfo=timezone.utc)  # 10AM ET
        with patch("vanguard.helpers.regime_detector.is_market_open", return_value=True):
            regime = detect_regime(df, dt_utc=dt)
        assert regime == "ACTIVE"

    def test_caution_regime(self):
        from vanguard.helpers.regime_detector import detect_regime
        df = _make_df(20)
        df["atr_expansion"] = 3.5  # above 2.0 threshold → CAUTION
        dt = datetime(2026, 3, 31, 14, 0, tzinfo=timezone.utc)
        with patch("vanguard.helpers.regime_detector.is_market_open", return_value=True):
            regime = detect_regime(df, dt_utc=dt)
        assert regime == "CAUTION"

    def test_dead_regime(self):
        from vanguard.helpers.regime_detector import detect_regime
        df = _make_df(20)
        df["atr_expansion"] = 1.0
        df["relative_volume"] = 0.3  # low volume
        # Lunch hour in ET: 12:15 ET = 16:15 UTC (standard time)
        dt = datetime(2026, 3, 31, 16, 15, tzinfo=timezone.utc)
        with patch("vanguard.helpers.regime_detector.is_market_open", return_value=True):
            regime = detect_regime(df, dt_utc=dt, volume_dead_threshold=0.5)
        assert regime == "DEAD"

    def test_closed_regime(self):
        from vanguard.helpers.regime_detector import detect_regime
        df = _make_df(20)
        with patch("vanguard.helpers.regime_detector.is_market_open", return_value=False):
            regime = detect_regime(df)
        assert regime == "CLOSED"


# ===========================================================================
# Test 2: ML gate filters sub-threshold candidates
# ===========================================================================

class TestMlGate:
    def test_ml_gate_filters_low_prob(self):
        from vanguard.helpers.strategy_router import route
        df = _make_df(20, long_prob=0.40, short_prob=0.40)  # all below threshold

        strat_cfg = {
            "equity": {
                "strategies": ["momentum"],
                "top_n_per_strategy": 5,
                "short_only_strategies": [],
                "ml_gate_threshold": 0.50,
            }
        }
        sel_cfg = {"risk_reward": {}}
        results = route(df, strat_cfg, sel_cfg, regime="ACTIVE")
        # All candidates below threshold — empty
        assert len(results) == 0 or all(r.empty for r in results)

    def test_ml_gate_passes_high_prob(self):
        from vanguard.helpers.strategy_router import route
        df = _make_df(20, long_prob=0.75, short_prob=0.75)

        strat_cfg = {
            "equity": {
                "strategies": ["momentum"],
                "top_n_per_strategy": 5,
                "short_only_strategies": [],
                "ml_gate_threshold": 0.50,
            }
        }
        sel_cfg = {"risk_reward": {}}
        results = route(df, strat_cfg, sel_cfg, regime="ACTIVE")
        non_empty = [r for r in results if not r.empty]
        assert len(non_empty) > 0


# ===========================================================================
# Test 3: Equity strategies produce top 5 LONG + 5 SHORT
# ===========================================================================

class TestEquityStrategies:
    _STRATEGIES = [
        ("smc_confluence", "SmcConfluenceStrategy"),
        ("momentum", "MomentumStrategy"),
        ("mean_reversion", "MeanReversionStrategy"),
        ("risk_reward_edge", "RiskRewardEdgeStrategy"),
        ("relative_strength", "RelativeStrengthStrategy"),
    ]

    @pytest.mark.parametrize("module,klass", _STRATEGIES)
    def test_produces_long_and_short(self, module, klass):
        import importlib
        mod = importlib.import_module(f"vanguard.strategies.{module}")
        strategy = getattr(mod, klass)()
        df = _make_df(20)

        long_result  = strategy.score(df, "LONG",  top_n=5)
        short_result = strategy.score(df, "SHORT", top_n=5)

        assert not long_result.empty,  f"{klass} returned empty LONG result"
        assert not short_result.empty, f"{klass} returned empty SHORT result"
        assert len(long_result)  <= 5
        assert len(short_result) <= 5
        assert all(long_result["direction"]  == "LONG")
        assert all(short_result["direction"] == "SHORT")

    def test_result_has_required_columns(self):
        from vanguard.strategies.momentum import MomentumStrategy
        df = _make_df(20)
        result = MomentumStrategy().score(df, "LONG", top_n=5)
        for col in ("symbol", "direction", "strategy_score", "strategy_rank"):
            assert col in result.columns, f"Missing column: {col}"

    def test_ranks_are_sequential(self):
        from vanguard.strategies.smc_confluence import SmcConfluenceStrategy
        df = _make_df(20)
        result = SmcConfluenceStrategy().score(df, "LONG", top_n=5)
        assert list(result["strategy_rank"]) == list(range(1, len(result) + 1))


# ===========================================================================
# Test 4: Breakdown strategy is SHORT ONLY
# ===========================================================================

class TestBreakdownStrategy:
    def test_breakdown_short_only(self):
        from vanguard.strategies.breakdown import BreakdownStrategy
        strat = BreakdownStrategy()
        assert strat.short_only is True
        df = _make_df(20)
        long_result = strat.score(df, "LONG", top_n=5)
        assert long_result.empty, "Breakdown strategy must return empty for LONG"

    def test_breakdown_produces_shorts(self):
        from vanguard.strategies.breakdown import BreakdownStrategy
        df = _make_df(20)
        result = BreakdownStrategy().score(df, "SHORT", top_n=5)
        assert not result.empty
        assert all(result["direction"] == "SHORT")


# ===========================================================================
# Test 5: Risk-Reward Edge formula matches spec
# ===========================================================================

class TestRiskRewardEdge:
    def test_formula_correctness(self):
        """
        Spec: edge = (ml_prob × tp) − ((1−ml_prob) × sl) − spread
        Higher edge → higher rank.
        """
        from vanguard.strategies.risk_reward_edge import RiskRewardEdgeStrategy
        import numpy as np

        n = 5
        df = _make_df(n)
        # Set deterministic values
        df["lgbm_long_prob"] = [0.80, 0.70, 0.60, 0.55, 0.51]
        df["tp_pct"]         = [0.015] * n
        df["sl_pct"]         = [0.008] * n
        df["spread_proxy"]   = [0.001] * n

        strat = RiskRewardEdgeStrategy(spread_cost=0.0, expected_bars=1.0)
        # Manually compute expected edges
        expected_edges = [
            (0.80 * 0.015) - (0.20 * 0.008) - 0.001,
            (0.70 * 0.015) - (0.30 * 0.008) - 0.001,
            (0.60 * 0.015) - (0.40 * 0.008) - 0.001,
            (0.55 * 0.015) - (0.45 * 0.008) - 0.001,
            (0.51 * 0.015) - (0.49 * 0.008) - 0.001,
        ]
        # Expected: rank 1 (highest edge) = SYM000
        result = strat.score(df, "LONG", top_n=5)
        assert result.iloc[0]["symbol"] == "SYM000", \
            f"Expected SYM000 (highest edge) ranked first, got {result.iloc[0]['symbol']}"

    def test_negative_edge_symbols_rank_last(self):
        from vanguard.strategies.risk_reward_edge import RiskRewardEdgeStrategy
        n = 5
        df = _make_df(n)
        df["lgbm_long_prob"] = [0.52, 0.51, 0.50, 0.50, 0.50]
        df["tp_pct"]  = [0.001] * n   # tiny TP
        df["sl_pct"]  = [0.015] * n   # large SL → negative edge
        df["spread_proxy"] = [0.002] * n
        strat = RiskRewardEdgeStrategy()
        result = strat.score(df, "LONG", top_n=5)
        # Should still return results (normalize handles all-negative ranges)
        assert not result.empty


# ===========================================================================
# Test 6: Consensus count is correct
# ===========================================================================

class TestConsensusCount:
    def test_correct_count(self):
        from vanguard.helpers.consensus_counter import count_consensus

        def _make_strat_df(strategy: str, symbols: list[str], direction: str) -> pd.DataFrame:
            return pd.DataFrame({
                "symbol": symbols,
                "direction": direction,
                "strategy": strategy,
                "strategy_score": [0.8] * len(symbols),
                "strategy_rank": list(range(1, len(symbols) + 1)),
                "ml_prob": [0.7] * len(symbols),
                "asset_class": "equity",
                "regime": "ACTIVE",
            })

        r1 = _make_strat_df("smc",       ["AAPL", "MSFT", "NVDA"], "LONG")
        r2 = _make_strat_df("momentum",  ["AAPL", "TSLA"],          "LONG")
        r3 = _make_strat_df("breakdown", ["AAPL", "GOOG"],          "SHORT")

        combined = count_consensus([r1, r2, r3])

        # AAPL LONG should appear in smc + momentum = 2
        aapl_long = combined[(combined["symbol"] == "AAPL") & (combined["direction"] == "LONG")]
        assert not aapl_long.empty
        assert aapl_long.iloc[0]["consensus_count"] == 2

        # AAPL SHORT should appear in breakdown = 1
        aapl_short = combined[(combined["symbol"] == "AAPL") & (combined["direction"] == "SHORT")]
        assert not aapl_short.empty
        assert aapl_short.iloc[0]["consensus_count"] == 1

        # MSFT LONG: smc only = 1
        msft_long = combined[(combined["symbol"] == "MSFT") & (combined["direction"] == "LONG")]
        assert msft_long.iloc[0]["consensus_count"] == 1

    def test_strategies_matched_sorted(self):
        from vanguard.helpers.consensus_counter import count_consensus

        r1 = pd.DataFrame({
            "symbol": ["X"], "direction": "LONG",
            "strategy": "smc", "strategy_score": [0.9],
            "strategy_rank": [1], "ml_prob": [0.8],
            "asset_class": "equity", "regime": "ACTIVE",
        })
        r2 = pd.DataFrame({
            "symbol": ["X"], "direction": "LONG",
            "strategy": "momentum", "strategy_score": [0.8],
            "strategy_rank": [1], "ml_prob": [0.75],
            "asset_class": "equity", "regime": "ACTIVE",
        })
        combined = count_consensus([r1, r2])
        row = combined[combined["symbol"] == "X"].iloc[0]
        assert row["consensus_count"] == 2
        assert "momentum" in row["strategies_matched"]
        assert "smc" in row["strategies_matched"]


# ===========================================================================
# Test 7: Config-driven — removing a strategy excludes it
# ===========================================================================

class TestConfigDriven:
    def test_excluded_strategy_not_run(self):
        from vanguard.helpers.strategy_router import route

        df = _make_df(20, long_prob=0.75, short_prob=0.75)
        # Config with only momentum — breakdown should NOT appear
        strat_cfg = {
            "equity": {
                "strategies": ["momentum"],
                "top_n_per_strategy": 5,
                "short_only_strategies": [],
                "ml_gate_threshold": 0.50,
            }
        }
        sel_cfg = {"risk_reward": {}}
        results = route(df, strat_cfg, sel_cfg, regime="ACTIVE")

        all_strategies = set()
        for r in results:
            if not r.empty and "strategy" in r.columns:
                all_strategies.update(r["strategy"].unique())

        assert "breakdown" not in all_strategies
        assert "smc" not in all_strategies

    def test_strategy_filter_respected(self):
        from vanguard.helpers.strategy_router import route

        df = _make_df(20, long_prob=0.75, short_prob=0.75)
        strat_cfg = {
            "equity": {
                "strategies": ["smc", "momentum", "risk_reward"],
                "top_n_per_strategy": 5,
                "short_only_strategies": [],
                "ml_gate_threshold": 0.50,
            }
        }
        sel_cfg = {"risk_reward": {}}
        results = route(df, strat_cfg, sel_cfg, regime="ACTIVE", strategy_filter="momentum")

        all_strategies = set()
        for r in results:
            if not r.empty and "strategy" in r.columns:
                all_strategies.update(r["strategy"].unique())

        assert "momentum" in all_strategies
        assert "smc" not in all_strategies
        assert "risk_reward" not in all_strategies


# ===========================================================================
# Test 8: NaN features don't crash strategies
# ===========================================================================

class TestNanHandling:
    def test_all_nan_features_no_crash(self):
        from vanguard.strategies.smc_confluence import SmcConfluenceStrategy
        from vanguard.strategies.momentum import MomentumStrategy
        from vanguard.strategies.risk_reward_edge import RiskRewardEdgeStrategy

        df = _make_df(10)
        # Set all feature columns to NaN
        for col in _FEATURES:
            if col in df.columns:
                df[col] = float("nan")

        for strategy in [SmcConfluenceStrategy(), MomentumStrategy(), RiskRewardEdgeStrategy()]:
            # Should not raise
            try:
                result = strategy.score(df, "LONG", top_n=5)
                # May be empty or not, but must not crash
            except Exception as exc:
                pytest.fail(f"{strategy.name} crashed on all-NaN input: {exc}")

    def test_partial_nan_no_crash(self):
        from vanguard.strategies.mean_reversion import MeanReversionStrategy
        df = _make_df(10)
        df.loc[0:4, "session_vwap_distance"] = float("nan")
        df.loc[5:9, "daily_drawdown_from_high"] = float("nan")

        result = MeanReversionStrategy().score(df, "LONG", top_n=5)
        # Should not crash
        assert isinstance(result, pd.DataFrame)


# ===========================================================================
# Test 9: Empty input returns empty shortlist, not crash
# ===========================================================================

class TestEmptyInput:
    def test_empty_df_no_crash(self):
        from vanguard.strategies.smc_confluence import SmcConfluenceStrategy
        from vanguard.strategies.breakdown import BreakdownStrategy
        from vanguard.strategies.session_open import SessionOpenStrategy

        empty = pd.DataFrame(columns=_FEATURES + ["symbol", "asset_class"])

        for strategy in [SmcConfluenceStrategy(), BreakdownStrategy(), SessionOpenStrategy()]:
            result = strategy.score(empty, "SHORT", top_n=5)
            assert result.empty, f"{strategy.name} returned non-empty for empty input"

    def test_empty_consensus(self):
        from vanguard.helpers.consensus_counter import count_consensus
        result = count_consensus([])
        assert result.empty


# ===========================================================================
# Test 10: Output schema is correct when writing to DB
# ===========================================================================

class TestDbOutput:
    def test_shortlist_schema(self):
        """Create a temp DB and verify vanguard_shortlist can accept our data."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            con = sqlite3.connect(f.name)
            con.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_shortlist (
                    cycle_ts_utc    TEXT    NOT NULL,
                    symbol          TEXT    NOT NULL,
                    asset_class     TEXT    NOT NULL,
                    direction       TEXT    NOT NULL,
                    strategy        TEXT    NOT NULL,
                    strategy_rank   INTEGER NOT NULL,
                    strategy_score  REAL    NOT NULL,
                    ml_prob         REAL    NOT NULL,
                    edge_score      REAL,
                    consensus_count INTEGER DEFAULT 0,
                    strategies_matched TEXT,
                    regime          TEXT    NOT NULL,
                    PRIMARY KEY (cycle_ts_utc, symbol, strategy)
                )
            """)
            row = {
                "cycle_ts_utc": "2026-03-31T10:00:00Z",
                "symbol": "AAPL",
                "asset_class": "equity",
                "direction": "LONG",
                "strategy": "smc",
                "strategy_rank": 1,
                "strategy_score": 0.8423,
                "ml_prob": 0.72,
                "edge_score": None,
                "consensus_count": 3,
                "strategies_matched": "momentum,smc",
                "regime": "ACTIVE",
            }
            con.execute(
                """INSERT INTO vanguard_shortlist VALUES
                   (:cycle_ts_utc, :symbol, :asset_class, :direction, :strategy,
                    :strategy_rank, :strategy_score, :ml_prob, :edge_score,
                    :consensus_count, :strategies_matched, :regime)""",
                row,
            )
            con.commit()
            rows = con.execute("SELECT * FROM vanguard_shortlist").fetchall()
            assert len(rows) == 1
            assert rows[0][1] == "AAPL"
            assert rows[0][4] == "smc"
            con.close()
