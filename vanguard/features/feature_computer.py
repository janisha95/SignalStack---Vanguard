"""
Shared Vanguard feature computer.

This module is the single source of truth for V3 live features and V4A
training-backfill features. Both stages should call the same helpers here to
avoid train-serving skew.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from vanguard.factors import (
    market_context,
    momentum,
    price_location,
    quality,
    session_time,
    smc_5m,
    smc_htf_1h,
    volume,
)

logger = logging.getLogger("vanguard.feature_computer")

BENCHMARK_SYMBOLS = {
    "equity": "SPY",
    "index": "SPX",
    "forex": "DXY",
    "metal": "XAUUSD",
    "crypto": "BTCUSD",
    "energy": "WTI",
    "commodity": "WTI",
    "agriculture": "CORN",
}

BENCHMARK_FALLBACKS = {
    "forex": "EURUSD",
}

NA_FEATURES_BY_ASSET_CLASS = {
    "forex": set(),
    "crypto": set(),
}

FACTOR_MODULES = [
    price_location,
    momentum,
    volume,
    market_context,
    session_time,
    smc_5m,
    smc_htf_1h,
    quality,
]

FEATURE_NAMES = (
    price_location.FEATURE_NAMES
    + momentum.FEATURE_NAMES
    + volume.FEATURE_NAMES
    + market_context.FEATURE_NAMES
    + session_time.FEATURE_NAMES
    + smc_5m.FEATURE_NAMES
    + smc_htf_1h.FEATURE_NAMES
    + quality.FEATURE_NAMES
)

CRYPTO_FEATURES = [
    # === BTC as main driver (like SPY for equities) ===
    "cross_asset_correlation",      # correlation with BTC
    "btc_relative_momentum",        # momentum vs BTC
    "btc_adjusted_return",          # price move relative to BTC
    "btc_correlation_z",            # z-score of correlation

    # === Volatility & Regime ===
    "atr_expansion",
    "volatility_regime",
    "range_expansion_5m",

    # === Momentum & Acceleration ===
    "momentum_3bar",
    "momentum_12bar",
    "momentum_acceleration",
    "momentum_divergence",

    # === Volume & Order Flow ===
    "relative_volume",
    "volume_burst_z",
    "down_volume_ratio",
    "effort_vs_result",
    "volume_delta",

    # === Price Location & Premium/Discount ===
    "premium_discount_zone",
    "smc_premium_discount",

    # === Liquidity & Sweeps ===
    "liquidity_sweep",
    "liquidity_sweep_strength",

    # === FVG / Imbalance ===
    "fvg_bullish_nearest",
    "fvg_bearish_nearest",
    "fvg_size",

    # === Order Blocks & HTF Structure ===
    "ob_proximity_5m",
    "htf_ob_proximity",
    "htf_trend_direction",
    "htf_structure_break",
    "htf_fvg_nearest",

    # === Quality ===
    "nan_ratio",
]

_CRYPTO_DERIVED_FEATURES = [
    "btc_relative_momentum",
    "btc_adjusted_return",
    "btc_correlation_z",
    "volatility_regime",
    "range_expansion_5m",
    "momentum_divergence",
    "volume_delta",
    "liquidity_sweep_strength",
    "fvg_size",
]

FEATURE_NAMES = FEATURE_NAMES + _CRYPTO_DERIVED_FEATURES


def _symbol_variants(symbol: str | None) -> list[str]:
    if not symbol:
        return []
    variants: list[str] = []
    candidates = [str(symbol).strip().upper()]
    if "/" in candidates[0]:
        candidates.append(candidates[0].replace("/", ""))
    elif len(candidates[0]) == 6 and candidates[0].isalpha():
        candidates.append(f"{candidates[0][:3]}/{candidates[0][3:]}")
    for candidate in candidates:
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def benchmark_symbol_candidates(asset_class: str) -> list[str]:
    """Return benchmark symbols in priority order, including DB naming variants."""
    candidates: list[str] = []
    for raw_symbol in (
        BENCHMARK_SYMBOLS.get(asset_class),
        BENCHMARK_FALLBACKS.get(asset_class),
    ):
        for variant in _symbol_variants(raw_symbol):
            if variant not in candidates:
                candidates.append(variant)
    return candidates


def _safe_number(value: float, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _series_return(close: pd.Series, periods: int) -> float:
    if close is None or len(close) < periods + 1:
        return 0.0
    current = _safe_number(close.iloc[-1], default=0.0)
    prior = _safe_number(close.iloc[-(periods + 1)], default=0.0)
    if prior <= 0:
        return 0.0
    return (current - prior) / prior


def _augment_crypto_features(
    features: dict[str, float],
    df_5m: pd.DataFrame | None,
    benchmark_df: pd.DataFrame | None,
) -> dict[str, float]:
    """Add BTC-led crypto features without touching the core factor modules."""
    if df_5m is None or len(df_5m) == 0:
        for name in _CRYPTO_DERIVED_FEATURES:
            features.setdefault(name, 0.0)
        return features

    df = df_5m.copy().sort_values("bar_ts_utc").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    bench = None
    if benchmark_df is not None and len(benchmark_df) > 0:
        bench = benchmark_df.copy().sort_values("bar_ts_utc").reset_index(drop=True)
        for col in ("close",):
            if col not in bench.columns:
                bench[col] = 0.0
            bench[col] = pd.to_numeric(bench[col], errors="coerce").fillna(0.0)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_ = df["open"]
    volume_series = df["volume"]
    returns = close.pct_change().replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
    bench_returns = (
        bench["close"].pct_change().replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
        if bench is not None else pd.Series(dtype="float64")
    )

    btc_ret_12 = _series_return(bench["close"], 12) if bench is not None else 0.0
    btc_ret_3 = _series_return(bench["close"], 3) if bench is not None else 0.0
    symbol_ret_12 = _safe_number(features.get("momentum_12bar"), default=_series_return(close, 12))
    symbol_ret_3 = _safe_number(features.get("momentum_3bar"), default=_series_return(close, 3))
    features["btc_relative_momentum"] = round(symbol_ret_12 - btc_ret_12, 6)
    features["btc_adjusted_return"] = round(symbol_ret_3 - btc_ret_3, 6)
    features["momentum_divergence"] = round(symbol_ret_3 - symbol_ret_12, 6)

    if bench is not None and len(returns) >= 12 and len(bench_returns) >= 12:
        n = min(len(returns), len(bench_returns), 60)
        corr_window = pd.concat(
            [returns.tail(n).reset_index(drop=True), bench_returns.tail(n).reset_index(drop=True)],
            axis=1,
        ).dropna()
        rolling_corr = (
            corr_window.iloc[:, 0].rolling(12).corr(corr_window.iloc[:, 1]).dropna()
            if len(corr_window) >= 12 else pd.Series(dtype="float64")
        )
        corr = _safe_number(rolling_corr.iloc[-1], default=_safe_number(corr_window.iloc[:, 0].corr(corr_window.iloc[:, 1]), default=0.0)) if len(corr_window) else 0.0
        corr_mean = _safe_number(rolling_corr.mean(), default=corr)
        corr_std = _safe_number(rolling_corr.std(ddof=0), default=0.0)
        features["cross_asset_correlation"] = round(corr, 6)
        features["btc_correlation_z"] = round((corr - corr_mean) / corr_std, 6) if corr_std > 1e-8 else 0.0
    else:
        features["cross_asset_correlation"] = _safe_number(features.get("cross_asset_correlation"), default=0.0)
        features["btc_correlation_z"] = 0.0

    short_vol = _safe_number(returns.tail(20).std(ddof=0), default=0.0)
    long_vol = _safe_number(returns.tail(80).std(ddof=0), default=0.0)
    features["volatility_regime"] = round(short_vol / long_vol, 6) if long_vol > 1e-8 else 1.0

    bar_range = (high - low).abs()
    last_range = _safe_number(bar_range.iloc[-1], default=0.0)
    avg_range = _safe_number(bar_range.tail(20).mean(), default=0.0)
    features["range_expansion_5m"] = round(last_range / avg_range, 6) if avg_range > 1e-8 else 1.0

    signed_volume = volume_series.where(close >= open_, -volume_series).tail(20)
    gross_volume = _safe_number(volume_series.tail(20).abs().sum(), default=0.0)
    net_volume = _safe_number(signed_volume.sum(), default=0.0)
    features["volume_delta"] = round(net_volume / gross_volume, 6) if gross_volume > 1e-8 else 0.0

    sweep_value = _safe_number(features.get("liquidity_sweep"), default=0.0)
    if len(df) >= 21:
        prior_high = _safe_number(high.iloc[-21:-1].max(), default=_safe_number(high.iloc[-1], default=0.0))
        prior_low = _safe_number(low.iloc[-21:-1].min(), default=_safe_number(low.iloc[-1], default=0.0))
        denom = max(abs(_safe_number(close.iloc[-1], default=0.0)), 1e-8)
        if sweep_value > 0:
            features["liquidity_sweep_strength"] = round(max(0.0, (prior_low - _safe_number(low.iloc[-1], default=0.0)) / denom), 6)
        elif sweep_value < 0:
            features["liquidity_sweep_strength"] = round(max(0.0, (_safe_number(high.iloc[-1], default=0.0) - prior_high) / denom), 6)
        else:
            features["liquidity_sweep_strength"] = 0.0
    else:
        features["liquidity_sweep_strength"] = 0.0

    fvg_bull = abs(_safe_number(features.get("fvg_bullish_nearest"), default=0.0))
    fvg_bear = abs(_safe_number(features.get("fvg_bearish_nearest"), default=0.0))
    features["fvg_size"] = round(max(fvg_bull, fvg_bear), 6)

    for name in _CRYPTO_DERIVED_FEATURES:
        features.setdefault(name, 0.0)
    return features


def _apply_asset_class_overrides(
    features: dict[str, float],
    asset_class: str,
    df_5m: pd.DataFrame | None = None,
    benchmark_df: pd.DataFrame | None = None,
) -> dict[str, float]:
    adjusted = dict(features)
    for feature_name in NA_FEATURES_BY_ASSET_CLASS.get(asset_class, set()):
        adjusted[feature_name] = float("nan")
    if asset_class in {"forex", "crypto"} and df_5m is not None and len(df_5m) > 0:
        try:
            last_ts = pd.Timestamp(
                str(df_5m.sort_values("bar_ts_utc").iloc[-1]["bar_ts_utc"]),
                tz="UTC",
            )
            minutes = last_ts.hour * 60 + last_ts.minute
            day_pct = round(minutes / 1440.0, 6)
            adjusted["session_phase"] = day_pct
            adjusted["time_in_session_pct"] = day_pct
            adjusted["bars_since_session_open"] = float(minutes // 5)
        except Exception:
            pass
    if asset_class == "crypto":
        adjusted = _augment_crypto_features(adjusted, df_5m, benchmark_df)
    return adjusted


def standardize_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Apply the same cross-sectional standardization used by V4B scorer.

    This intentionally mirrors the V4B loop exactly: fill invalid values with 0,
    then z-score each feature column with population std (ddof=0).
    """
    if frame.empty:
        return frame.copy()

    x = frame.copy()
    for col in x.columns:
        x[col] = pd.to_numeric(x[col], errors="coerce").fillna(0.0)
    x = x.astype("float64")

    for col in x.columns:
        mean = float(x[col].mean())
        std = float(x[col].std(ddof=0))
        if std > 1e-8:
            x[col] = (x[col] - mean) / std
        else:
            x[col] = 0.0
    return x


def compute_feature_dict(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame | None = None,
    benchmark_df: pd.DataFrame | None = None,
    *,
    asset_class: str = "equity",
    symbol: str = "",
    debug: bool = False,
    fillna_zero: bool = True,
) -> dict[str, float]:
    """Run all registered factor modules for one symbol/context window."""
    combined: dict[str, float] = {}
    df_1h = df_1h if df_1h is not None else pd.DataFrame()
    benchmark_df = benchmark_df if benchmark_df is not None else pd.DataFrame()

    for module in FACTOR_MODULES:
        module_name = module.__name__.split(".")[-1]
        try:
            features = module.compute(df_5m, df_1h, benchmark_df)
            combined.update(features)
        except Exception as exc:
            logger.warning("Module %s failed for %s: %s", module_name, symbol or "UNKNOWN", exc)
            for name in getattr(module, "FEATURE_NAMES", []):
                combined.setdefault(name, float("nan"))

    if "nan_ratio" in combined:
        nan_count_actual = sum(
            1
            for value in combined.values()
            if isinstance(value, float) and math.isnan(value)
        )
        total = len(combined)
        combined["nan_ratio"] = round(nan_count_actual / total, 6) if total else 0.0

    combined = _apply_asset_class_overrides(combined, asset_class, df_5m, benchmark_df)

    if fillna_zero:
        combined = {
            key: (
                0.0
                if value is None or (isinstance(value, float) and math.isnan(value))
                else value
            )
            for key, value in combined.items()
        }

    if debug:
        print(f"\n=== DEBUG FEATURES: {symbol or 'UNKNOWN'} ===")
        for key, value in combined.items():
            print(f"  {key:<40}: {value}")
        nan_count = sum(
            1
            for value in combined.values()
            if isinstance(value, float) and math.isnan(value)
        )
        print(f"  NaN count: {nan_count}/{len(combined)}")

    return combined


def compute_all_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute one row of shared Vanguard features from a 5m context DataFrame.

    Context side-inputs are supplied through ``df.attrs``:
      - df_1h: optional 1h context DataFrame
      - benchmark_df: optional benchmark 5m DataFrame
      - asset_class: asset class label
      - symbol: symbol string
      - debug: bool
      - normalize_cross_section: bool, optional cross-sectional z-score pass

    Returns a one-row DataFrame with FEATURE_NAMES columns.
    """
    features = compute_feature_dict(
        df,
        df_1h=df.attrs.get("df_1h"),
        benchmark_df=df.attrs.get("benchmark_df"),
        asset_class=str(df.attrs.get("asset_class") or "equity"),
        symbol=str(df.attrs.get("symbol") or ""),
        debug=bool(df.attrs.get("debug", False)),
        fillna_zero=bool(df.attrs.get("fillna_zero", True)),
    )
    frame = pd.DataFrame([{name: features.get(name, 0.0) for name in FEATURE_NAMES}])
    if bool(df.attrs.get("normalize_cross_section", False)) and len(frame) > 1:
        frame = standardize_feature_frame(frame)
    return frame


def verify_feature_parity(live_df: pd.DataFrame, training_df: pd.DataFrame) -> dict[str, Any]:
    """
    Compare live and training feature frames over common columns/keys.

    This is a smoke-test helper for validating that V3 and V4A now call the
    same feature computer.
    """
    if live_df.empty and training_df.empty:
        return {"ok": True, "rows_compared": 0, "max_abs_diff": 0.0, "feature_diffs": {}}

    key_cols = [
        col
        for col in ("symbol", "asset_class", "cycle_ts_utc", "asof_ts_utc", "bar_ts_utc")
        if col in live_df.columns and col in training_df.columns
    ]
    common_cols = [
        col
        for col in FEATURE_NAMES
        if col in live_df.columns and col in training_df.columns
    ]

    left = live_df.copy()
    right = training_df.copy()
    if key_cols:
        merged = left[key_cols + common_cols].merge(
            right[key_cols + common_cols],
            on=key_cols,
            how="inner",
            suffixes=("_live", "_training"),
        )
    else:
        rows = min(len(left), len(right))
        merged = pd.concat(
            [
                left[common_cols].head(rows).reset_index(drop=True).add_suffix("_live"),
                right[common_cols].head(rows).reset_index(drop=True).add_suffix("_training"),
            ],
            axis=1,
        )

    feature_diffs: dict[str, float] = {}
    max_abs_diff = 0.0
    for col in common_cols:
        live_values = pd.to_numeric(merged[f"{col}_live"], errors="coerce").fillna(0.0)
        train_values = pd.to_numeric(merged[f"{col}_training"], errors="coerce").fillna(0.0)
        diff = float((live_values - train_values).abs().max() or 0.0)
        feature_diffs[col] = diff
        max_abs_diff = max(max_abs_diff, diff)

    return {
        "ok": max_abs_diff <= 1e-9,
        "rows_compared": int(len(merged)),
        "columns_compared": len(common_cols),
        "max_abs_diff": max_abs_diff,
        "feature_diffs": feature_diffs,
    }
