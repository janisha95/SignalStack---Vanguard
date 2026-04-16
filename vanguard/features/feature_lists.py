"""
Canonical Vanguard feature lists shared by training and inference.

Feature counts:
  EQUITY_FEATURES        : 29 base + 10 VP = 39
  FOREX_FEATURES         : 35 base + 10 VP = 45
  CRYPTO_FEATURES        : 29 base + 10 VP = 39
  METALS_ENERGY_FEATURES : 35 base + 10 VP = 45

VP features sourced from vanguard_features_vp via factor engine at inference.
"""
from __future__ import annotations

from vanguard.features.feature_computer import CRYPTO_FEATURES as SHARED_CRYPTO_FEATURES

# ── Shared VP block (identical for all asset classes) ─────────────────────────
VP_FEATURES = [
    "poc",
    "vah",
    "val",
    "poc_distance",
    "vah_distance",
    "val_distance",
    "vp_skew",
    "volume_delta",
    "cum_delta",
    "delta_divergence",
]

# ── Equity: 29 base + 10 VP = 39 ─────────────────────────────────────────────
EQUITY_FEATURES = [
    # ── 29 base features ──────────────────────────────────────────────────────
    "session_vwap_distance",
    "premium_discount_zone",
    "gap_pct",
    "session_opening_range_position",
    "daily_drawdown_from_high",
    "momentum_3bar",
    "momentum_12bar",
    "momentum_acceleration",
    "atr_expansion",
    "daily_adx",
    "relative_volume",
    "volume_burst_z",
    "down_volume_ratio",
    "effort_vs_result",
    "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark",
    "benchmark_momentum_12bar",
    "cross_asset_correlation",
    "session_phase",
    "ob_proximity_5m",
    "fvg_bullish_nearest",
    "fvg_bearish_nearest",
    "structure_break",
    "liquidity_sweep",
    "smc_premium_discount",
    "htf_trend_direction",
    "htf_structure_break",
    "htf_fvg_nearest",
    "htf_ob_proximity",
    # ── 10 VP features ────────────────────────────────────────────────────────
    *VP_FEATURES,
]

# ── Forex: 35 base + 10 VP = 45 ──────────────────────────────────────────────
FOREX_FEATURES = [
    # ── 35 base features ──────────────────────────────────────────────────────
    "session_vwap_distance",
    "premium_discount_zone",
    "gap_pct",
    "session_opening_range_position",
    "daily_drawdown_from_high",
    "momentum_3bar",
    "momentum_12bar",
    "momentum_acceleration",
    "atr_expansion",
    "daily_adx",
    "relative_volume",
    "volume_burst_z",
    "down_volume_ratio",
    "effort_vs_result",
    "spread_proxy",
    "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark",
    "benchmark_momentum_12bar",
    "cross_asset_correlation",
    "daily_conviction",
    "session_phase",
    "time_in_session_pct",
    "bars_since_session_open",
    "ob_proximity_5m",
    "fvg_bullish_nearest",
    "fvg_bearish_nearest",
    "structure_break",
    "liquidity_sweep",
    "smc_premium_discount",
    "htf_trend_direction",
    "htf_structure_break",
    "htf_fvg_nearest",
    "htf_ob_proximity",
    "bars_available",
    "nan_ratio",
    # ── 10 VP features ────────────────────────────────────────────────────────
    *VP_FEATURES,
]

# ── Metals/Energy: 35 base + 10 VP = 45 ──────────────────────────────────────
METALS_ENERGY_FEATURES = [
    # ── 35 base features ──────────────────────────────────────────────────────
    "session_vwap_distance",
    "premium_discount_zone",
    "gap_pct",
    "session_opening_range_position",
    "daily_drawdown_from_high",
    "momentum_3bar",
    "momentum_12bar",
    "momentum_acceleration",
    "atr_expansion",
    "daily_adx",
    "relative_volume",
    "volume_burst_z",
    "down_volume_ratio",
    "effort_vs_result",
    "spread_proxy",
    "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark",
    "benchmark_momentum_12bar",
    "cross_asset_correlation",
    "daily_conviction",
    "session_phase",
    "time_in_session_pct",
    "bars_since_session_open",
    "ob_proximity_5m",
    "fvg_bullish_nearest",
    "fvg_bearish_nearest",
    "structure_break",
    "liquidity_sweep",
    "smc_premium_discount",
    "htf_trend_direction",
    "htf_structure_break",
    "htf_fvg_nearest",
    "htf_ob_proximity",
    "bars_available",
    "nan_ratio",
    # ── 10 VP features ────────────────────────────────────────────────────────
    *VP_FEATURES,
]

# ── Crypto: 29 base + 10 VP = 39 ─────────────────────────────────────────────
# volume_delta exists in SHARED_CRYPTO_FEATURES already; exclude it from VP
# block to avoid duplication. All other 9 VP features are additive.
_CRYPTO_BASE = list(SHARED_CRYPTO_FEATURES)
_CRYPTO_VP_EXTRA = [f for f in VP_FEATURES if f not in _CRYPTO_BASE]
CRYPTO_FEATURES = _CRYPTO_BASE + _CRYPTO_VP_EXTRA
