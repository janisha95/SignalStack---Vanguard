"""
strategy_router.py — Routes instruments to strategies by asset class.

Pipeline:
  1. Load V3 features for the current cycle from vanguard_features table.
  2. Load LGBM model artifacts (cached in memory per process lifetime).
  3. Predict lgbm_long_prob + lgbm_short_prob for each symbol.
  4. Read strategy config from vanguard_strategies.json.
  5. For each asset class → filter instruments → apply ML gate → run strategies.
  6. Collect and return all result DataFrames.

Location: ~/SS/Vanguard/vanguard/helpers/strategy_router.py
"""
from __future__ import annotations

import json
import logging
import pickle
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent.parent   # ~/SS/Vanguard/
_DB_PATH    = str(_ROOT / "data" / "vanguard_universe.db")
_MODEL_DIR  = _ROOT / "models" / "vanguard" / "latest"
_CONFIG_DIR = _ROOT / "config"
_FEATURE_PROFILE_PATH = _CONFIG_DIR / "vanguard_feature_profiles.json"
_ML_THRESHOLD_PATH = _CONFIG_DIR / "vanguard_ml_thresholds.json"

# V4B feature list (same order used during training)
FEATURES = [
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

# ---------------------------------------------------------------------------
# Strategy registry (lazy import to avoid circular deps)
# ---------------------------------------------------------------------------

def _build_registry() -> dict[str, Any]:
    """Import all strategy classes and return {name: instance}."""
    from vanguard.strategies.smc_confluence import SmcConfluenceStrategy
    from vanguard.strategies.momentum import MomentumStrategy
    from vanguard.strategies.mean_reversion import MeanReversionStrategy
    from vanguard.strategies.risk_reward_edge import RiskRewardEdgeStrategy
    from vanguard.strategies.relative_strength import RelativeStrengthStrategy
    from vanguard.strategies.breakdown import BreakdownStrategy
    from vanguard.strategies.session_timing import SessionTimingStrategy
    from vanguard.strategies.htf_alignment import HtfAlignmentStrategy
    from vanguard.strategies.liquidity_grab_reversal import LiquidityGrabReversalStrategy
    from vanguard.strategies.session_open import SessionOpenStrategy
    from vanguard.strategies.cross_asset import CrossAssetStrategy
    from vanguard.strategies.momentum_breakout import MomentumBreakoutStrategy
    from vanguard.strategies.llm_strategy import LlmStrategy

    return {
        "smc":                     SmcConfluenceStrategy(),
        "momentum":                MomentumStrategy(),
        "mean_reversion":          MeanReversionStrategy(),
        "risk_reward":             RiskRewardEdgeStrategy(),
        "relative_strength":       RelativeStrengthStrategy(),
        "breakdown":               BreakdownStrategy(),
        "session_timing":          SessionTimingStrategy(),
        "htf_alignment":           HtfAlignmentStrategy(),
        "liquidity_grab_reversal": LiquidityGrabReversalStrategy(),
        "session_open":            SessionOpenStrategy(),
        "cross_asset":             CrossAssetStrategy(),
        "momentum_breakout":       MomentumBreakoutStrategy(),
        "llm":                     LlmStrategy(),
    }


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------

_MODEL_CACHE: dict[str, Any] = {}
_JSON_CACHE: dict[str, Any] = {}


def _load_model(name: str) -> Any:
    if name not in _MODEL_CACHE:
        path = _MODEL_DIR / f"{name}.pkl"
        if not path.exists():
            logger.warning(f"Model artifact not found: {path}")
            return None
        with open(path, "rb") as fh:
            _MODEL_CACHE[name] = pickle.load(fh)
        logger.info(f"Loaded model: {name} from {path}")
    return _MODEL_CACHE[name]


def _load_model_config(name: str) -> dict[str, Any]:
    cache_key = f"config:{name}"
    if cache_key not in _JSON_CACHE:
        path = _MODEL_DIR / f"{name}_config.json"
        if not path.exists():
            return {}
        with open(path) as fh:
            _JSON_CACHE[cache_key] = json.load(fh)
    return _JSON_CACHE.get(cache_key, {})


def _load_json(path: Path) -> dict[str, Any]:
    cache_key = str(path)
    if cache_key not in _JSON_CACHE:
        with open(path) as fh:
            _JSON_CACHE[cache_key] = json.load(fh)
    return _JSON_CACHE[cache_key]


def load_feature_profiles() -> dict[str, dict[str, Any]]:
    return _load_json(_FEATURE_PROFILE_PATH)


def load_ml_thresholds() -> dict[str, dict[str, float]]:
    return _load_json(_ML_THRESHOLD_PATH)


def load_feature_profile(profile_name: str | None) -> dict[str, Any]:
    if not profile_name:
        return {}
    profiles = load_feature_profiles()
    for _, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if profile.get("profile") == profile_name:
            return profile
    return {}


def _feature_columns_from_profile(profile: dict[str, Any]) -> list[str]:
    explicit = profile.get("features")
    if isinstance(explicit, list) and explicit:
        return [feature for feature in explicit if feature in FEATURES]

    drop_cols = set(profile.get("drop", []))
    universal_exclude = set(profile.get("universal_exclude", []))
    return [col for col in FEATURES if col not in drop_cols and col not in universal_exclude]


def _load_registry_row(model_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(_DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM vanguard_model_registry WHERE model_id = ?",
            (model_id,),
        ).fetchone()
    return dict(row) if row else None


def get_model_for_scoring(asset_class: str, direction: str) -> dict[str, Any]:
    direction = direction.lower()
    native_id = f"lgbm_{asset_class}_{direction}_v1"
    native = _load_registry_row(native_id)
    if native and native.get("readiness") in (
        "live_native",
        "validated_shadow",
        "trained_insufficient_validation",
    ):
        return {
            "model_id": native_id,
            "model_family": native.get("model_family") or f"lgbm_{asset_class}_{direction}",
            "model_source": "native",
            "readiness": native.get("readiness"),
            "feature_profile": native.get("feature_profile"),
            "tbm_profile": native.get("tbm_profile"),
        }

    fallback_id = f"lgbm_equity_{direction}_v1"
    fallback = _load_registry_row(fallback_id)
    if fallback and fallback.get("readiness") in (
        "live_native",
        "live_fallback",
        "trained_insufficient_validation",
    ):
        return {
            "model_id": fallback_id,
            "model_family": fallback.get("model_family") or f"lgbm_equity_{direction}",
            "model_source": "fallback_equity",
            "readiness": fallback.get("readiness"),
            "feature_profile": fallback.get("feature_profile"),
            "tbm_profile": fallback.get("tbm_profile"),
        }

    return {
        "model_id": None,
        "model_family": None,
        "model_source": "none",
        "readiness": "not_trained",
        "feature_profile": None,
        "tbm_profile": None,
    }


# ---------------------------------------------------------------------------
# Feature loading
# ---------------------------------------------------------------------------

def load_features(cycle_ts: str | None = None) -> pd.DataFrame:
    """
    Load V3 features for the most recent cycle (or a specific cycle_ts).
    Returns DataFrame with symbol as index, feature columns expanded from features_json.
    Also joins asset_class from vanguard_universe_members.
    """
    with sqlite3.connect(_DB_PATH) as con:
        con.row_factory = sqlite3.Row

        if cycle_ts is None:
            row = con.execute(
                "SELECT MAX(cycle_ts_utc) AS ts FROM vanguard_features"
            ).fetchone()
            cycle_ts = row["ts"] if row and row["ts"] else None

        if cycle_ts is None:
            logger.warning("No features found in vanguard_features")
            return pd.DataFrame()

        logger.info(f"Loading features for cycle: {cycle_ts}")
        rows = con.execute(
            "SELECT symbol, features_json FROM vanguard_features WHERE cycle_ts_utc = ?",
            (cycle_ts,),
        ).fetchall()

        if not rows:
            return pd.DataFrame()

        records = []
        for r in rows:
            feat = json.loads(r["features_json"])
            feat["symbol"] = r["symbol"]
            feat["cycle_ts_utc"] = cycle_ts
            records.append(feat)

        df = pd.DataFrame(records)

        # Join asset_class from universe members
        members = pd.read_sql_query(
            "SELECT symbol, asset_class FROM vanguard_universe_members", con
        )
        df = df.merge(members[["symbol", "asset_class"]], on="symbol", how="left")
        missing_ac = df["asset_class"].isna()
        if missing_ac.any():
            syms = df.loc[missing_ac, "symbol"].tolist()
            logger.warning(
                "[V5] %d symbol(s) missing from vanguard_universe_members, "
                "defaulting asset_class to 'equity': %s",
                len(syms), syms[:20],
            )
        df["asset_class"] = df["asset_class"].fillna("equity")

    return df


# ---------------------------------------------------------------------------
# LGBM prediction
# ---------------------------------------------------------------------------

def _score_direction(
    df: pd.DataFrame,
    asset_class: str,
    direction: str,
) -> pd.DataFrame:
    scored = df.copy()
    direction = direction.lower()
    prob_col = f"lgbm_{direction}_prob"
    model_family_col = f"{direction}_model_family"
    model_source_col = f"{direction}_model_source"
    model_readiness_col = f"{direction}_model_readiness"
    feature_profile_col = f"{direction}_feature_profile"
    tbm_profile_col = f"{direction}_tbm_profile"

    routing = get_model_for_scoring(asset_class, direction)
    if routing["model_id"] is None:
        scored[prob_col] = 0.0
        scored[model_family_col] = None
        scored[model_source_col] = "none"
        scored[model_readiness_col] = "not_trained"
        scored[feature_profile_col] = None
        scored[tbm_profile_col] = None
        return scored

    model = _load_model(routing["model_id"])
    if model is None:
        scored[prob_col] = 0.0
        scored[model_family_col] = routing["model_family"]
        scored[model_source_col] = "none"
        scored[model_readiness_col] = "not_trained"
        scored[feature_profile_col] = routing["feature_profile"]
        scored[tbm_profile_col] = routing["tbm_profile"]
        return scored

    model_config = _load_model_config(routing["model_id"])
    configured_features = model_config.get("features")
    if isinstance(configured_features, list) and configured_features:
        feature_cols = [feature for feature in configured_features if feature in FEATURES]
    else:
        profile = load_feature_profile(routing["feature_profile"])
        feature_cols = _feature_columns_from_profile(profile)
    X = scored.reindex(columns=feature_cols).fillna(0.0).to_numpy(dtype=np.float64)

    scored[prob_col] = model.predict_proba(X)[:, 1]
    scored[model_family_col] = routing["model_family"]
    scored[model_source_col] = routing["model_source"]
    scored[model_readiness_col] = routing["readiness"]
    scored[feature_profile_col] = routing["feature_profile"]
    scored[tbm_profile_col] = routing["tbm_profile"]
    return scored


def predict_probabilities(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add long/short probabilities and model provenance columns to df.
    Scoring is routed by asset_class + direction using model registry readiness.
    """
    frames: list[pd.DataFrame] = []
    for asset_class, ac_df in df.groupby("asset_class", dropna=False):
        resolved_asset_class = asset_class or "equity"
        scored = _score_direction(ac_df, resolved_asset_class, "long")
        scored = _score_direction(scored, resolved_asset_class, "short")
        frames.append(scored)
    if not frames:
        return df
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Main routing function
# ---------------------------------------------------------------------------

def route(
    df: pd.DataFrame,
    strategies_config: dict,
    selection_config: dict,
    regime: str,
    asset_class_filter: str | None = None,
    strategy_filter: str | None = None,
    enable_llm: bool = False,
) -> list[pd.DataFrame]:
    """
    Route instruments to strategies by asset class.

    Returns a list of per-strategy result DataFrames, each with columns:
        symbol, direction, strategy, strategy_score, strategy_rank,
        ml_prob, asset_class, regime, edge_score
    """
    registry = _build_registry()
    results: list[pd.DataFrame] = []

    rr_config = selection_config.get("risk_reward", {})
    ml_thresholds = load_ml_thresholds()

    for asset_class, ac_config in strategies_config.items():
        if asset_class_filter and asset_class != asset_class_filter:
            continue

        strategy_names: list[str] = ac_config.get("strategies", [])
        if not enable_llm:
            strategy_names = [s for s in strategy_names if s != "llm"]
        if strategy_filter:
            strategy_names = [s for s in strategy_names if s == strategy_filter]

        short_only_strategies: list[str] = ac_config.get("short_only_strategies", [])
        threshold_cfg = ml_thresholds.get(asset_class, {})
        long_threshold = float(threshold_cfg.get("long", ac_config.get("ml_gate_threshold", 0.50)))
        short_threshold = float(threshold_cfg.get("short", ac_config.get("ml_gate_threshold", 0.50)))

        if regime == "CAUTION":
            long_threshold  += 0.05
            short_threshold += 0.05
            logger.debug(
                "%s: CAUTION regime — ML gate raised +0.05 (long=%.2f, short=%.2f)",
                asset_class, long_threshold, short_threshold,
            )

        top_n_config = ac_config.get("top_n_per_strategy", 5)

        # Filter to this asset class
        ac_df = df[df["asset_class"] == asset_class].copy()
        if ac_df.empty:
            logger.debug(f"No instruments for asset_class={asset_class}")
            continue

        # Attach RR config columns (default tp/sl if not already present)
        tp_by_ac = rr_config.get("default_tp_pct_by_asset_class", {})
        sl_by_ac = rr_config.get("default_sl_pct_by_asset_class", {})
        if "tp_pct" not in ac_df.columns:
            ac_df["tp_pct"] = tp_by_ac.get(asset_class, 0.015)
        if "sl_pct" not in ac_df.columns:
            ac_df["sl_pct"] = sl_by_ac.get(asset_class, 0.008)

        # ML gate — produce LONG and SHORT candidate pools
        long_pool  = ac_df[ac_df["lgbm_long_prob"]  >= long_threshold].copy()
        short_pool = ac_df[ac_df["lgbm_short_prob"] >= short_threshold].copy()

        logger.debug(
            f"{asset_class}: total={len(ac_df)}, "
            f"long_pool={len(long_pool)}, short_pool={len(short_pool)}"
        )

        for strat_name in strategy_names:
            strategy = registry.get(strat_name)
            if strategy is None:
                logger.warning(f"Unknown strategy: {strat_name}")
                continue

            top_n: int
            if isinstance(top_n_config, dict):
                top_n = top_n_config.get(strat_name, 5)
            else:
                top_n = int(top_n_config)

            is_short_only = strat_name in short_only_strategies

            strat_results: list[pd.DataFrame] = []

            # LONG (skip if short-only strategy)
            if not is_short_only and len(long_pool) > 0:
                try:
                    scored = strategy.score(long_pool, "LONG", top_n)
                    if not scored.empty:
                            scored["asset_class"] = asset_class
                            scored["regime"] = regime
                            scored["strategy"] = strat_name
                            scored["ml_prob"] = scored.get(
                                "ml_prob", long_pool.set_index("symbol")
                                .reindex(scored["symbol"])["lgbm_long_prob"].values
                                if "symbol" in scored.columns else 0.0
                            )
                            long_meta = long_pool.set_index("symbol")
                            scored["model_family"] = long_meta.reindex(scored["symbol"])["long_model_family"].values
                            scored["model_source"] = long_meta.reindex(scored["symbol"])["long_model_source"].values
                            scored["model_readiness"] = long_meta.reindex(scored["symbol"])["long_model_readiness"].values
                            scored["feature_profile"] = long_meta.reindex(scored["symbol"])["long_feature_profile"].values
                            scored["tbm_profile"] = long_meta.reindex(scored["symbol"])["long_tbm_profile"].values
                            strat_results.append(scored)
                except Exception as exc:
                    logger.error(f"Strategy {strat_name} LONG failed: {exc}", exc_info=True)

            # SHORT
            if len(short_pool) > 0:
                try:
                    scored = strategy.score(short_pool, "SHORT", top_n)
                    if not scored.empty:
                            scored["asset_class"] = asset_class
                            scored["regime"] = regime
                            scored["strategy"] = strat_name
                            scored["ml_prob"] = scored.get(
                                "ml_prob", short_pool.set_index("symbol")
                                .reindex(scored["symbol"])["lgbm_short_prob"].values
                                if "symbol" in scored.columns else 0.0
                            )
                            short_meta = short_pool.set_index("symbol")
                            scored["model_family"] = short_meta.reindex(scored["symbol"])["short_model_family"].values
                            scored["model_source"] = short_meta.reindex(scored["symbol"])["short_model_source"].values
                            scored["model_readiness"] = short_meta.reindex(scored["symbol"])["short_model_readiness"].values
                            scored["feature_profile"] = short_meta.reindex(scored["symbol"])["short_feature_profile"].values
                            scored["tbm_profile"] = short_meta.reindex(scored["symbol"])["short_tbm_profile"].values
                            strat_results.append(scored)
                except Exception as exc:
                    logger.error(f"Strategy {strat_name} SHORT failed: {exc}", exc_info=True)

            results.extend(strat_results)

    return results
