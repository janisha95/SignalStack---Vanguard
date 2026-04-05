"""
vanguard_model_trainer.py — V4B Vanguard Model Trainer.

Trains per-asset-class LightGBM binary classifiers on V4A training data:
  - lgbm_{asset_class}_long_v1
  - lgbm_{asset_class}_short_v1

Walk-forward validation: sorted by time, train on first 70%, test on last 30%.
Saves model artifacts and config to models/vanguard/latest/.

CLI:
  python3 stages/vanguard_model_trainer.py                     # train both
  python3 stages/vanguard_model_trainer.py --model long        # long only
  python3 stages/vanguard_model_trainer.py --model short       # short only
  python3 stages/vanguard_model_trainer.py --validate          # validate existing
  python3 stages/vanguard_model_trainer.py --feature-importance

Location: ~/SS/Vanguard/stages/vanguard_model_trainer.py
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import sqlite3
import sys
import time
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import lightgbm as lgb
from scipy.stats import spearmanr
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import (
    roc_auc_score,
    accuracy_score,
    precision_score,
    recall_score,
    mean_absolute_error,
    mean_squared_error,
)

from stages.vanguard_scorer import MODEL_SPECS as SCORER_MODEL_SPECS
from vanguard.features.feature_computer import CRYPTO_FEATURES as SHARED_CRYPTO_FEATURES
from vanguard.features.feature_computer import compute_all_features
from vanguard.helpers.db import VanguardDB
from vanguard.helpers.clock import now_utc, iso_utc

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("vanguard_model_trainer")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH   = str(Path(__file__).resolve().parent.parent / "data" / "vanguard_universe.db")
MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "vanguard" / "latest"
TBM_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "vanguard_tbm_params.json"
FEATURE_PROFILE_PATH = Path(__file__).resolve().parent.parent / "config" / "vanguard_feature_profiles.json"

TRAIN_FRAC    = 0.70   # first 70% of rows (by time) → training
WR_THRESHOLD  = 0.55   # probability threshold for win-rate reporting
MIN_TRAINING_ROWS = 5000
MIN_VALIDATION_WINDOWS = 5
PROMOTION_MIN_IC = 0.02
PROMOTION_MIN_WR = 1.0 / 3.0
PROMOTION_MAX_IC_STD = 0.03
REGRESSOR_TARGET_CLIP = 0.05

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
]  # 35 features — same order as V3 factor modules

UNIVERSAL_EXCLUDE = {
    "nan_ratio",
    "asset_class_encoded",
    "daily_conviction",
    "time_in_session_pct",
    "bars_since_session_open",
    "spread_proxy",
}

CRYPTO_REGRESSOR_EXCLUDE = {
    "session_vwap_distance",
    "gap_pct",
    "session_opening_range_position",
    "daily_drawdown_from_high",
    "daily_adx",
    "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark",
    "benchmark_momentum_12bar",
    "session_phase",
    "time_in_session_pct",
    "bars_since_session_open",
    "bars_available",
}

_LABEL_MAP = {
    "long":  "label_long",
    "short": "label_short",
}

READINESS_NOT_TRAINED = "not_trained"
READINESS_TRAINED_INSUFFICIENT = "trained_insufficient_validation"
READINESS_VALIDATED_SHADOW = "validated_shadow"
READINESS_LIVE_NATIVE = "live_native"
READINESS_LIVE_FALLBACK = "live_fallback"
READINESS_DISABLED = "disabled"

# ---------------------------------------------------------------------------
# LightGBM hyperparameters
# ---------------------------------------------------------------------------

def _make_lgbm_params(scale_pos_weight: float) -> dict:
    return {
        "objective":           "binary",
        "metric":              "binary_logloss",
        "boosting_type":       "gbdt",
        "num_leaves":          31,
        "learning_rate":       0.05,
        "feature_fraction":    0.8,
        "bagging_fraction":    0.8,
        "bagging_freq":        5,
        "min_child_samples":   50,
        "n_estimators":        500,
        "verbose":             -1,
        "seed":                42,
        "scale_pos_weight":    scale_pos_weight,
    }


def _make_regressor_lgbm_params() -> dict:
    return {
        "objective":         "regression",
        "metric":            "rmse",
        "boosting_type":     "gbdt",
        "num_leaves":        31,
        "learning_rate":     0.05,
        "feature_fraction":  0.8,
        "bagging_fraction":  0.8,
        "bagging_freq":      5,
        "min_child_samples": 50,
        "n_estimators":      500,
        "verbose":           -1,
        "random_state":      42,
    }


def _scorer_spec(asset_class: str) -> dict:
    spec = SCORER_MODEL_SPECS.get(asset_class)
    if not spec:
        raise ValueError(f"No scorer MODEL_SPECS entry for asset_class={asset_class}")
    return spec


def _shared_crypto_feature_names(spec_features: list[str] | None = None) -> list[str]:
    """Resolve crypto features against the shared feature module output."""
    seed = pd.DataFrame(columns=["bar_ts_utc", "open", "high", "low", "close", "volume"])
    seed.attrs["asset_class"] = "crypto"
    seed.attrs["symbol"] = "BTCUSD"
    shared_frame = compute_all_features(seed)
    shared_names = list(shared_frame.columns)
    candidate_names = list(spec_features or SHARED_CRYPTO_FEATURES)
    selected = [name for name in candidate_names if name in shared_names]
    if not selected and not spec_features:
        selected = [
            name
            for name in shared_names
            if name not in CRYPTO_REGRESSOR_EXCLUDE
        ]
    if not selected:
        raise RuntimeError("No crypto features resolved from shared feature module")
    return selected


def _requested_feature_names_from_scorer(
    asset_class: str,
    feature_profiles: dict[str, dict] | None = None,
) -> list[str]:
    """
    Prefer the exact scorer MODEL_SPECS feature list so training/inference stay aligned.
    """
    spec = SCORER_MODEL_SPECS.get(asset_class) or {}
    spec_features = spec.get("features")
    if isinstance(spec_features, list) and spec_features:
        if asset_class == "crypto":
            return _shared_crypto_feature_names(spec_features)
        return list(spec_features)
    if asset_class == "crypto":
        return _shared_crypto_feature_names()
    return feature_names_for_asset_class(asset_class, feature_profiles)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_training_data(db_path: str) -> pd.DataFrame:
    """Load vanguard_training_data sorted by time."""
    conn = VanguardDB(db_path).connect()
    try:
        df = pd.read_sql(
            "SELECT * FROM vanguard_training_data ORDER BY asof_ts_utc",
            conn,
        )
    finally:
        conn.close()
    logger.info("Loaded %d training rows, %d symbols", len(df), df["symbol"].nunique())
    return df


def load_tbm_profiles(config_path: str | Path = TBM_CONFIG_PATH) -> dict[str, dict]:
    with open(config_path) as fh:
        return json.load(fh)


def load_feature_profiles(config_path: str | Path = FEATURE_PROFILE_PATH) -> dict[str, dict]:
    with open(config_path) as fh:
        return json.load(fh)


def feature_names_for_asset_class(
    asset_class: str,
    feature_profiles: dict[str, dict] | None = None,
) -> list[str]:
    feature_profiles = feature_profiles or load_feature_profiles()
    profile = feature_profiles.get(asset_class, feature_profiles.get("equity", {}))
    explicit = profile.get("features")
    if isinstance(explicit, list) and explicit:
        logger.info(
            "[V4B] Feature profile for %s: %d requested curated features",
            asset_class,
            len(explicit),
        )
        return list(explicit)

    drops = set(profile.get("drop", []))
    universal_exclude = set(feature_profiles.get("universal_exclude", [])) or set(UNIVERSAL_EXCLUDE)
    feature_names = [
        feature for feature in FEATURES
        if feature not in drops and feature not in universal_exclude
    ]
    logger.info(
        "[V4B] Feature profile fallback for %s: %d features after excludes",
        asset_class,
        len(feature_names),
    )
    return feature_names


def resolve_available_feature_names(
    df: pd.DataFrame,
    feature_names: list[str],
    asset_class: str,
) -> list[str]:
    available = [feature for feature in feature_names if feature in df.columns]
    missing = [feature for feature in feature_names if feature not in df.columns]
    if missing:
        logger.warning(
            "[V4B] %s profile has %d unavailable features in training data: %s",
            asset_class,
            len(missing),
            missing,
        )
    if not available:
        raise RuntimeError(f"No usable features available for asset_class={asset_class}")
    logger.info(
        "[V4B] %s using %d available training features",
        asset_class,
        len(available),
    )
    return available


def load_training_data_for_asset_class(db_path: str, asset_class: str) -> pd.DataFrame:
    print(f"[V4B] Loading training rows for asset_class={asset_class}...", flush=True)
    conn = VanguardDB(db_path).connect()
    try:
        filtered = pd.read_sql(
            "SELECT * FROM vanguard_training_data WHERE asset_class = ? ORDER BY asof_ts_utc",
            conn,
            params=(asset_class,),
        )
    finally:
        conn.close()
    logger.info(
        "Filtered training rows | asset_class=%s | rows=%d | symbols=%d",
        asset_class,
        len(filtered),
        filtered["symbol"].nunique() if not filtered.empty else 0,
    )
    print(
        f"[V4B] Loaded asset_class={asset_class} rows={len(filtered):,} "
        f"symbols={filtered['symbol'].nunique() if not filtered.empty else 0}",
        flush=True,
    )
    return filtered


def count_training_rows_for_asset_class(db_path: str, asset_class: str) -> int:
    conn = VanguardDB(db_path).connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM vanguard_training_data WHERE asset_class = ?",
            (asset_class,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def prepare_features(df: pd.DataFrame, feature_names: list[str] | None = None) -> np.ndarray:
    """Extract feature matrix, filling NaN with 0 for LightGBM."""
    feature_names = feature_names or FEATURES
    available = [feature for feature in feature_names if feature in df.columns]
    missing = [feature for feature in feature_names if feature not in df.columns]
    if missing:
        logger.warning("[V4B] Missing %d feature columns in training frame: %s", len(missing), missing)
    X = df.reindex(columns=available).copy()
    # LightGBM handles NaN natively — no imputation needed
    return X.values.astype(np.float32)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def compute_sample_weights(df: pd.DataFrame) -> np.ndarray:
    """
    Label uniqueness weighting.
    Rows at timestamps with more overlapping samples get lower weight.
    Prevents the model from memorizing busy periods.
    """
    if "asof_ts_utc" not in df.columns:
        return np.ones(len(df))
    concurrency = df.groupby("asof_ts_utc").size()
    weights = 1.0 / df["asof_ts_utc"].map(concurrency).values
    # Normalize so mean weight = 1
    weights = weights / weights.mean()
    return weights


def _scale_pos_weight(y: np.ndarray) -> float:
    pos = y.sum()
    neg = len(y) - pos
    return float(neg / pos) if pos > 0 else 1.0


def train_lgbm(
    label_col: str,
    df_train: pd.DataFrame,
    df_test: pd.DataFrame,
    feature_names: list[str] | None = None,
    params_override: dict | None = None,
) -> tuple[lgb.LGBMClassifier, dict]:
    """
    Train one LightGBM classifier.

    Returns (fitted_model, metrics_dict).
    """
    feature_names = feature_names or FEATURES
    X_train = prepare_features(df_train, feature_names=feature_names)
    y_train = df_train[label_col].values.astype(int)
    X_test  = prepare_features(df_test, feature_names=feature_names)
    y_test  = df_test[label_col].values.astype(int)

    spw = _scale_pos_weight(y_train)
    params = _make_lgbm_params(spw)
    if params_override:
        params.update(params_override)

    early_stopping = params.pop("n_estimators", 500)
    es_rounds      = 50

    model = lgb.LGBMClassifier(
        n_estimators=early_stopping,
        **params,
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        callbacks=[
            lgb.early_stopping(es_rounds, verbose=False),
            lgb.log_evaluation(period=-1),
        ],
    )

    metrics = evaluate_model(
        model, X_test, y_test,
        df_test["forward_return"].values,
        label_col,
    )
    metrics["train_rows"]  = len(df_train)
    metrics["test_rows"]   = len(df_test)
    metrics["label_rate_train"] = float(y_train.mean())
    metrics["label_rate_test"]  = float(y_test.mean())
    metrics["best_iteration"]   = int(model.best_iteration_ or early_stopping)
    metrics["scale_pos_weight"] = spw
    metrics["feature_count"]    = len(feature_names)

    return model, metrics


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_model(
    model: lgb.LGBMClassifier,
    X: np.ndarray,
    y: np.ndarray,
    forward_return: np.ndarray,
    label_col: str,
) -> dict:
    """Compute AUC, accuracy, precision, recall, IC, WR@0.55."""
    proba = model.predict_proba(X)[:, 1]

    auc = float(roc_auc_score(y, proba)) if len(np.unique(y)) > 1 else 0.5
    acc = float(accuracy_score(y, (proba >= 0.5).astype(int)))
    prc = float(precision_score(y, (proba >= 0.5).astype(int), zero_division=0))
    rec = float(recall_score(y, (proba >= 0.5).astype(int), zero_division=0))

    # IC = Spearman corr between predicted prob and forward return
    ic_fwd = float(spearmanr(proba, forward_return).statistic) if len(proba) > 2 else 0.0
    # IC vs label (simpler, always defined)
    ic_lbl = float(spearmanr(proba, y).statistic) if len(proba) > 2 else 0.0

    # Win rate at P > WR_THRESHOLD
    mask_55 = proba >= WR_THRESHOLD
    wr_55 = float(y[mask_55].mean()) if mask_55.sum() > 0 else float("nan")
    n_55  = int(mask_55.sum())

    return {
        "auc":          round(auc, 4),
        "accuracy":     round(acc, 4),
        "precision":    round(prc, 4),
        "recall":       round(rec, 4),
        "ic_vs_return": round(ic_fwd, 4),
        "ic_vs_label":  round(ic_lbl, 4),
        "wr_at_55":     round(wr_55, 4) if not (isinstance(wr_55, float) and np.isnan(wr_55)) else None,
        "n_at_55":      n_55,
    }


def feature_importance(
    model,
    top_n: int = 10,
    feature_names: list[str] | None = None,
) -> list[tuple[str, float]]:
    """Return top_n (feature_name, importance) pairs, descending."""
    feature_names = feature_names or FEATURES
    if hasattr(model, "booster_"):
        importances = model.booster_.feature_importance(importance_type="gain")
    elif hasattr(model, "feature_importances_"):
        importances = np.asarray(model.feature_importances_, dtype=float)
    elif hasattr(model, "coef_"):
        importances = np.abs(np.asarray(model.coef_, dtype=float)).reshape(-1)
    else:
        importances = np.zeros(len(feature_names), dtype=float)
    pairs = sorted(zip(feature_names, importances), key=lambda x: x[1], reverse=True)
    return pairs[:top_n]


# ---------------------------------------------------------------------------
# Artifact saving
# ---------------------------------------------------------------------------

def save_artifacts(
    model,
    metrics: dict,
    label_col: str,
    model_dir: Path,
    model_name: str | None = None,
    feature_names: list[str] | None = None,
    extra_config: dict | None = None,
) -> Path:
    """Save model .pkl and config .json to model_dir. Returns model path."""
    model_dir.mkdir(parents=True, exist_ok=True)

    if "long" in label_col:
        side = "long"
    elif "short" in label_col:
        side = "short"
    else:
        side = "regressor"
    feature_names = feature_names or FEATURES
    model_name = model_name or f"lgbm_{side}"
    pkl_path    = model_dir / f"{model_name}.pkl"
    config_path = model_dir / f"{model_name}_config.json"

    with open(pkl_path, "wb") as f:
        pickle.dump(model, f, protocol=pickle.HIGHEST_PROTOCOL)

    config = {
        "model_family":  model_name,
        "track":         "A",
        "target_label":  label_col,
        "features":      feature_names,
        "feature_count": len(feature_names),
        "metrics":       metrics,
        "feature_importance_top10": feature_importance(model, feature_names=feature_names),
    }
    if model_name.startswith("lgbm_"):
        if label_col == "forward_return":
            config["lgbm_params"] = _make_regressor_lgbm_params()
        else:
            config["lgbm_params"] = _make_lgbm_params(metrics.get("scale_pos_weight", 1.0))
    if extra_config:
        config.update(extra_config)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    logger.info("Saved %s → %s", model_name, pkl_path)
    return pkl_path


# ---------------------------------------------------------------------------
# Walk-forward split (70/30 temporal)
# ---------------------------------------------------------------------------

def temporal_split(df: pd.DataFrame, train_frac: float = TRAIN_FRAC) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split DataFrame temporally: first train_frac → train, rest → test."""
    n_train = int(len(df) * train_frac)
    return df.iloc[:n_train].copy(), df.iloc[n_train:].copy()


def cross_sectional_normalize(df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """
    Z-score features across all assets at the SAME timestamp.
    Critical for multi-asset models where feature scales differ.
    """
    df = df.copy()
    for col in feature_cols:
        if col in df.columns:
            df[col] = df.groupby("asof_ts_utc")[col].transform(
                lambda x: (x - x.mean()) / (x.std() + 1e-8)
            )
    return df


def purged_walk_forward_split(df: pd.DataFrame, n_splits: int = 5, embargo_bars: int = 6):
    """
    Purged walk-forward by date blocks.
    Expanding window: each fold trains on ALL prior data.
    Embargo: skip `embargo_bars` rows between train and test to prevent leakage.
    """
    df = df.sort_values("asof_ts_utc").reset_index(drop=True)

    # Group by date (approximate sessions)
    dates = pd.to_datetime(df["asof_ts_utc"]).dt.date.unique()
    dates = sorted(dates)

    # Minimum 60% of dates for first training window
    min_train = int(len(dates) * 0.6)
    test_size = max(1, (len(dates) - min_train) // n_splits)

    for i in range(n_splits):
        train_end_idx = min_train + i * test_size
        test_start_idx = train_end_idx
        test_end_idx = min(test_start_idx + test_size, len(dates))

        if test_end_idx > len(dates):
            break

        train_dates = set(dates[:train_end_idx])
        test_dates = set(dates[test_start_idx:test_end_idx])

        train_mask = pd.to_datetime(df["asof_ts_utc"]).dt.date.isin(train_dates)
        test_mask = pd.to_datetime(df["asof_ts_utc"]).dt.date.isin(test_dates)

        # Purge: remove last `embargo_bars` rows from training set
        if embargo_bars > 0:
            train_indices = df[train_mask].index
            if len(train_indices) > embargo_bars:
                purge_indices = train_indices[-embargo_bars:]
                train_mask.iloc[purge_indices] = False

        if train_mask.sum() < 100 or test_mask.sum() < 50:
            continue

        yield train_mask, test_mask


def _model_name(asset_class: str, side: str) -> str:
    if side == "regressor":
        return str(_scorer_spec(asset_class)["model_id"])
    return f"lgbm_{asset_class}_{side}_v1"


def _fallback_family(asset_class: str, side: str) -> str | None:
    if side == "regressor":
        return None
    return None if asset_class == "equity" else _model_name("equity", side)


def _readiness_for_window_count(window_count: int) -> str:
    if window_count < MIN_VALIDATION_WINDOWS:
        return READINESS_TRAINED_INSUFFICIENT
    return READINESS_VALIDATED_SHADOW


def _ic_column(direction: str) -> str:
    if direction == "regressor":
        return "ic_long"
    return "ic_long" if direction == "long" else "ic_short"


def _registry_ic_column(direction: str) -> str:
    if direction == "regressor":
        return "mean_ic_long"
    return "mean_ic_long" if direction == "long" else "mean_ic_short"


def _registry_wr_column(direction: str) -> str:
    if direction == "regressor":
        return "long_wr_at_55"
    return "long_wr_at_55" if direction == "long" else "short_wr_at_55"


def promotion_check(db_path: str = DB_PATH) -> list[dict]:
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM vanguard_model_registry ORDER BY asset_class, direction, model_id"
        ).fetchall()

        results: list[dict] = []
        for row in rows:
            model = dict(row)
            direction = str(model.get("direction") or ("long" if "long" in str(model.get("model_id")) else "short"))
            ic_col = _ic_column(direction)
            registry_ic = model.get(_registry_ic_column(direction))
            wr_at_55 = model.get(_registry_wr_column(direction))
            wf_rows = con.execute(
                f"SELECT {ic_col} AS ic_value FROM vanguard_walkforward_results WHERE model_id = ? ORDER BY window_id",
                (model["model_id"],),
            ).fetchall()
            ic_values = [float(r["ic_value"]) for r in wf_rows if r["ic_value"] is not None]
            window_count = len(ic_values)
            ic_std = float(np.std(ic_values)) if ic_values else None

            reasons: list[str] = []
            if int(model.get("training_rows") or 0) < MIN_TRAINING_ROWS:
                reasons.append(f"training_rows<{MIN_TRAINING_ROWS}")
            if window_count < MIN_VALIDATION_WINDOWS:
                reasons.append(f"walk_forward_windows<{MIN_VALIDATION_WINDOWS}")
            if registry_ic is None or float(registry_ic) <= PROMOTION_MIN_IC:
                reasons.append(f"mean_ic<={PROMOTION_MIN_IC:.2f}")
            if wr_at_55 is None or float(wr_at_55) <= PROMOTION_MIN_WR:
                reasons.append(f"wr_at_55<={PROMOTION_MIN_WR:.3f}")
            if ic_std is None or float(ic_std) >= PROMOTION_MAX_IC_STD:
                reasons.append(f"ic_std>={PROMOTION_MAX_IC_STD:.2f}")

            results.append({
                "model_id": model["model_id"],
                "asset_class": model.get("asset_class"),
                "direction": direction,
                "readiness": model.get("readiness"),
                "training_rows": int(model.get("training_rows") or 0),
                "walk_forward_windows": window_count,
                "mean_ic": None if registry_ic is None else float(registry_ic),
                "wr_at_55": None if wr_at_55 is None else float(wr_at_55),
                "ic_std": ic_std,
                "eligible": len(reasons) == 0,
                "reasons": reasons,
            })
    return results


def _set_model_readiness(model_id: str, readiness: str, db_path: str = DB_PATH) -> bool:
    db = VanguardDB(db_path)
    with db.connect() as con:
        row = con.execute(
            "SELECT model_id FROM vanguard_model_registry WHERE model_id = ?",
            (model_id,),
        ).fetchone()
        if not row:
            return False
        con.execute(
            "UPDATE vanguard_model_registry SET readiness = ? WHERE model_id = ?",
            (readiness, model_id),
        )
        con.commit()
    return True


def _build_regressor(model_filename: str):
    """Instantiate the estimator implied by the scorer artifact filename."""
    stem = Path(model_filename).stem.lower()
    if stem.startswith("lgbm_"):
        params = _make_regressor_lgbm_params()
        n_estimators = params.pop("n_estimators", 500)
        return lgb.LGBMRegressor(n_estimators=n_estimators, **params)
    if stem.startswith("ridge_"):
        return Ridge(alpha=1.0, random_state=42)
    if stem.startswith("et_"):
        return ExtraTreesRegressor(
            n_estimators=500,
            max_depth=None,
            min_samples_leaf=5,
            n_jobs=-1,
            random_state=42,
        )
    if stem.startswith("rf_"):
        return RandomForestRegressor(
            n_estimators=500,
            max_depth=None,
            min_samples_leaf=5,
            n_jobs=-1,
            random_state=42,
        )
    raise ValueError(f"Unsupported regressor artifact name: {model_filename}")


def _regressor_model_names(asset_class: str) -> list[str]:
    spec = _scorer_spec(asset_class)
    return [str(Path(filename).stem) for filename, _weight in spec.get("models", [])]


def _regressor_target(df: pd.DataFrame) -> np.ndarray:
    return (
        pd.to_numeric(df["forward_return"], errors="coerce")
        .clip(-REGRESSOR_TARGET_CLIP, REGRESSOR_TARGET_CLIP)
        .fillna(0.0)
        .to_numpy(dtype=np.float32)
    )


def evaluate_regressor_predictions(pred: np.ndarray, y_true: np.ndarray) -> dict:
    pred = np.asarray(pred, dtype=float)
    y_true = np.asarray(y_true, dtype=float)
    if len(pred) == 0:
        return {
            "ic_vs_return": 0.0,
            "rmse": 0.0,
            "mae": 0.0,
            "directional_accuracy": 0.0,
            "prediction_mean": 0.0,
            "prediction_std": 0.0,
        }
    ic = float(spearmanr(pred, y_true).statistic) if len(pred) > 2 else 0.0
    if not np.isfinite(ic):
        ic = 0.0
    return {
        "ic_vs_return": round(ic, 4),
        "rmse": round(float(np.sqrt(mean_squared_error(y_true, pred))), 8),
        "mae": round(float(mean_absolute_error(y_true, pred)), 8),
        "directional_accuracy": round(float((np.sign(pred) == np.sign(y_true)).mean()), 4),
        "prediction_mean": round(float(pred.mean()), 8),
        "prediction_std": round(float(pred.std()), 8),
    }


def _write_regressor_meta(
    asset_class: str,
    feature_names: list[str],
    metrics: dict,
    component_metrics: dict[str, dict],
    model_dir: Path,
) -> Path:
    spec = _scorer_spec(asset_class)
    meta = {
        "model_id": spec["model_id"],
        "asset_class": asset_class,
        "target_label": "forward_return",
        "target_clip": [-REGRESSOR_TARGET_CLIP, REGRESSOR_TARGET_CLIP],
        "features": feature_names,
        "feature_count": len(feature_names),
        "models": [
            {"file": filename, "weight": weight}
            for filename, weight in spec.get("models", [])
        ],
        "metrics": metrics,
        "component_metrics": component_metrics,
        "normalization": "cross_sectional_zscore_by_asof_ts_utc",
        "created_at_utc": iso_utc(now_utc()),
    }
    model_dir.mkdir(parents=True, exist_ok=True)
    meta_path = model_dir / spec["meta_file"]
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    return meta_path


def _upsert_regressor_registry(
    *,
    db_path: str,
    asset_class: str,
    model_id: str,
    meta_path: Path,
    feature_names: list[str],
    training_rows: int,
    metrics: dict,
    window_count: int,
    wf_rows: list[dict],
    feature_profile: dict,
    tbm_profile: dict,
) -> None:
    db = VanguardDB(db_path)
    now_str = iso_utc(now_utc())
    readiness = _readiness_for_window_count(window_count)
    db.upsert_model_registry({
        "model_id": model_id,
        "model_family": model_id,
        "track": "A",
        "target_label": "forward_return",
        "feature_count": len(feature_names),
        "train_rows": int(metrics.get("train_rows") or 0),
        "test_rows": int(metrics.get("test_rows") or 0),
        "training_rows": int(training_rows),
        "walk_forward_windows": int(window_count),
        "mean_ic_long": metrics.get("mean_ic"),
        "mean_ic_short": None,
        "auc_long": None,
        "auc_short": None,
        "long_wr_at_55": metrics.get("directional_accuracy"),
        "short_wr_at_55": None,
        "artifact_path": str(meta_path),
        "created_at_utc": now_str,
        "status": "trained",
        "notes": "regressor ensemble aligned to vanguard_scorer.py",
        "asset_class": asset_class,
        "direction": "regressor",
        "feature_profile": feature_profile.get("profile"),
        "tbm_profile": tbm_profile.get("version"),
        "readiness": readiness,
        "fallback_family": None,
    })
    db.upsert_walkforward_results([
        {
            "model_id": model_id,
            "window_id": idx + 1,
            "train_start": row["train_start"],
            "train_end": row["train_end"],
            "test_start": row["test_start"],
            "test_end": row["test_end"],
            "test_rows": row["test_rows"],
            "ic_long": row["ic"],
            "ic_short": None,
            "auc": None,
            "accuracy": row.get("directional_accuracy"),
            "wr_at_55": row.get("directional_accuracy"),
            "asset_class": asset_class,
        }
        for idx, row in enumerate(wf_rows)
    ])


def train_regressor_ensemble(
    asset_class: str,
    db_path: str = DB_PATH,
    model_dir: Path = MODEL_DIR,
    max_rows: int | None = None,
    tbm_profiles: dict[str, dict] | None = None,
    feature_profiles: dict[str, dict] | None = None,
) -> dict:
    """Train the exact regressor ensemble consumed by vanguard_scorer.py."""
    spec = _scorer_spec(asset_class)
    model_id = str(spec["model_id"])
    logger.info("Training %s regressor ensemble for asset_class=%s", model_id, asset_class)
    t0 = time.perf_counter()

    tbm_profiles = tbm_profiles or load_tbm_profiles()
    feature_profiles = feature_profiles or load_feature_profiles()

    df = load_training_data_for_asset_class(db_path, asset_class)
    if df.empty:
        raise RuntimeError(f"No training data found in vanguard_training_data for asset_class={asset_class}")
    if max_rows and len(df) > max_rows:
        print(f"[V4B] Sampling asset_class={asset_class} from {len(df):,} to {max_rows:,} rows", flush=True)
        df = df.sample(n=max_rows, random_state=42).sort_values("asof_ts_utc").reset_index(drop=True)
        print(f"[V4B] Sampled asset_class={asset_class} rows={len(df):,}", flush=True)

    requested_feature_names = _requested_feature_names_from_scorer(asset_class, feature_profiles)
    feature_names = resolve_available_feature_names(df, requested_feature_names, asset_class)
    feature_profile = feature_profiles.get(asset_class, feature_profiles.get("equity", {}))
    tbm_profile = tbm_profiles.get(asset_class, tbm_profiles.get("equity", {}))

    df = df.dropna(subset=["forward_return"]).copy()
    if len(df) < 100:
        raise RuntimeError(f"Not enough non-null forward_return rows for asset_class={asset_class}")
    df["forward_return"] = pd.to_numeric(df["forward_return"], errors="coerce").clip(
        -REGRESSOR_TARGET_CLIP,
        REGRESSOR_TARGET_CLIP,
    )

    print(f"[V4B] Cross-sectional normalizing asset_class={asset_class} regressor features...", flush=True)
    df = cross_sectional_normalize(df, feature_names)

    component_names = _regressor_model_names(asset_class)
    weights_by_model = {str(Path(filename).stem): float(weight) for filename, weight in spec.get("models", [])}
    wf_rows: list[dict] = []
    fold_ics: list[float] = []
    df_train_last: pd.DataFrame | None = None
    df_test_last: pd.DataFrame | None = None

    for fold_i, (train_mask, test_mask) in enumerate(purged_walk_forward_split(df, n_splits=5, embargo_bars=6)):
        print(
            f"[V4B] Regressor WF window {fold_i + 1}: train={train_mask.sum():,} test={test_mask.sum():,}",
            flush=True,
        )
        df_train_fold = df.loc[train_mask].copy()
        df_test_fold = df.loc[test_mask].copy()
        X_train = prepare_features(df_train_fold, feature_names=feature_names).astype(np.float64)
        y_train = _regressor_target(df_train_fold)
        X_test = prepare_features(df_test_fold, feature_names=feature_names).astype(np.float64)
        y_test = _regressor_target(df_test_fold)
        sample_weight = compute_sample_weights(df_train_fold)

        ensemble_pred = np.zeros(len(df_test_fold), dtype=float)
        for model_name in component_names:
            estimator = _build_regressor(f"{model_name}.pkl")

            # FIX: Ridge cannot handle NaNs
            if "ridge" in model_name.lower():
                X_train_clean = np.nan_to_num(X_train, nan=0.0)
                X_test_clean = np.nan_to_num(X_test, nan=0.0)
                estimator.fit(X_train_clean, y_train, sample_weight=sample_weight)
                pred = estimator.predict(X_test_clean)
            else:
                estimator.fit(X_train, y_train, sample_weight=sample_weight)
                pred = estimator.predict(X_test)

            ensemble_pred += weights_by_model[model_name] * np.asarray(pred, dtype=float)

        fold_metrics = evaluate_regressor_predictions(ensemble_pred, y_test)
        fold_ics.append(float(fold_metrics["ic_vs_return"]))
        print(f"[V4B]   Regressor window {fold_i + 1} IC: {fold_metrics['ic_vs_return']:.4f}", flush=True)
        wf_rows.append({
            "train_start": str(df_train_fold["asof_ts_utc"].iloc[0]),
            "train_end": str(df_train_fold["asof_ts_utc"].iloc[-1]),
            "test_start": str(df_test_fold["asof_ts_utc"].iloc[0]),
            "test_end": str(df_test_fold["asof_ts_utc"].iloc[-1]),
            "test_rows": int(test_mask.sum()),
            "ic": float(fold_metrics["ic_vs_return"]),
            "directional_accuracy": float(fold_metrics["directional_accuracy"]),
        })
        df_train_last = df_train_fold
        df_test_last = df_test_fold

    if not fold_ics:
        print("[V4B] Regressor walk-forward produced no windows — falling back to 70/30 split", flush=True)
        df_train_last, df_test_last = temporal_split(df)
        X_train_last = prepare_features(df_train_last, feature_names=feature_names).astype(np.float64)
        y_train_last = _regressor_target(df_train_last)
        X_test_last = prepare_features(df_test_last, feature_names=feature_names).astype(np.float64)
        y_test_last = _regressor_target(df_test_last)
        sample_weight = compute_sample_weights(df_train_last)
        ensemble_pred = np.zeros(len(df_test_last), dtype=float)
        for model_name in component_names:
            estimator = _build_regressor(f"{model_name}.pkl")
            estimator.fit(X_train_last, y_train_last, sample_weight=sample_weight)
            ensemble_pred += weights_by_model[model_name] * np.asarray(estimator.predict(X_test_last), dtype=float)
        metrics = evaluate_regressor_predictions(ensemble_pred, y_test_last)
        fold_ics = [float(metrics["ic_vs_return"])]
        wf_rows = [{
            "train_start": str(df_train_last["asof_ts_utc"].iloc[0]),
            "train_end": str(df_train_last["asof_ts_utc"].iloc[-1]),
            "test_start": str(df_test_last["asof_ts_utc"].iloc[0]),
            "test_end": str(df_test_last["asof_ts_utc"].iloc[-1]),
            "test_rows": len(df_test_last),
            "ic": float(metrics["ic_vs_return"]),
            "directional_accuracy": float(metrics["directional_accuracy"]),
        }]
    else:
        X_test_last = prepare_features(df_test_last, feature_names=feature_names).astype(np.float64)
        y_test_last = _regressor_target(df_test_last)
        ensemble_pred = np.zeros(len(df_test_last), dtype=float)
        for model_name in component_names:
            estimator = _build_regressor(f"{model_name}.pkl")
            estimator.fit(
                prepare_features(df_train_last, feature_names=feature_names).astype(np.float64),
                _regressor_target(df_train_last),
                sample_weight=compute_sample_weights(df_train_last),
            )
            ensemble_pred += weights_by_model[model_name] * np.asarray(estimator.predict(X_test_last), dtype=float)
        metrics = evaluate_regressor_predictions(ensemble_pred, y_test_last)

    final_X = prepare_features(df, feature_names=feature_names).astype(np.float64)
    final_y = _regressor_target(df)
    final_weights = compute_sample_weights(df)
    final_ensemble_pred = np.zeros(len(df), dtype=float)
    component_metrics: dict[str, dict] = {}
    model_paths: dict[str, str] = {}

    for filename, weight in spec.get("models", []):
        model_name = str(Path(filename).stem)
        estimator = _build_regressor(filename)
        estimator.fit(final_X, final_y, sample_weight=final_weights)
        component_pred = np.asarray(estimator.predict(final_X), dtype=float)
        final_ensemble_pred += float(weight) * component_pred
        component_metrics[model_name] = evaluate_regressor_predictions(component_pred, final_y)
        pkl_path = save_artifacts(
            estimator,
            component_metrics[model_name],
            "forward_return",
            model_dir,
            model_name=model_name,
            feature_names=feature_names,
            extra_config={
                "model_kind": "regressor",
                "asset_class": asset_class,
                "ensemble_model_id": model_id,
                "ensemble_weight": float(weight),
                "normalization": "cross_sectional_zscore_by_asof_ts_utc",
            },
        )
        model_paths[model_name] = str(pkl_path)

    mean_ic = float(np.mean(fold_ics)) if fold_ics else 0.0
    metrics.update({
        "mean_ic": round(mean_ic, 4),
        "ic_std": round(float(np.std(fold_ics)), 4) if fold_ics else 0.0,
        "ic_hit_rate": round(sum(1 for ic in fold_ics if ic > 0) / len(fold_ics), 4) if fold_ics else 0.0,
        "train_rows": len(df_train_last) if df_train_last is not None else len(df),
        "test_rows": len(df_test_last) if df_test_last is not None else 0,
        "training_rows": len(df),
        "walk_forward_windows": len(fold_ics),
        "feature_count": len(feature_names),
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "asset_class": asset_class,
        "model_id": model_id,
        "target_clip": REGRESSOR_TARGET_CLIP,
        "ensemble_weights": weights_by_model,
        "artifact_paths": model_paths,
    })
    metrics["ensemble_in_sample"] = evaluate_regressor_predictions(final_ensemble_pred, final_y)

    meta_path = _write_regressor_meta(asset_class, feature_names, metrics, component_metrics, model_dir)
    _upsert_regressor_registry(
        db_path=db_path,
        asset_class=asset_class,
        model_id=model_id,
        meta_path=meta_path,
        feature_names=feature_names,
        training_rows=len(df),
        metrics=metrics,
        window_count=len(fold_ics),
        wf_rows=wf_rows,
        feature_profile=feature_profile,
        tbm_profile=tbm_profile,
    )

    print(f"\n{'='*60}", flush=True)
    print(f"  {model_id} — Regressor Walk-Forward Summary", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Rows            : {len(df):,}", flush=True)
    print(f"  Windows         : {len(fold_ics)}", flush=True)
    print(f"  Mean IC         : {metrics['mean_ic']:.4f}", flush=True)
    print(f"  IC Std Dev      : {metrics['ic_std']:.4f}", flush=True)
    print(f"  Directional Acc : {metrics['directional_accuracy']:.4f}", flush=True)
    print(f"  RMSE / MAE      : {metrics['rmse']:.6f} / {metrics['mae']:.6f}", flush=True)
    print(f"  Meta            : {meta_path}", flush=True)
    for model_name, comp_metrics in component_metrics.items():
        print(
            f"  {model_name:<28} w={weights_by_model.get(model_name, 0.0):.2f} "
            f"IC={comp_metrics['ic_vs_return']:.4f} RMSE={comp_metrics['rmse']:.6f}",
            flush=True,
        )
    print(f"{'='*60}\n", flush=True)
    return metrics


# ---------------------------------------------------------------------------
# Full training pipeline
# ---------------------------------------------------------------------------

def train_model(
    side: str,
    db_path: str = DB_PATH,
    model_dir: Path = MODEL_DIR,
    asset_class: str = "equity",
    max_rows: int | None = None,
    tbm_profiles: dict[str, dict] | None = None,
    feature_profiles: dict[str, dict] | None = None,
) -> dict:
    """
    Train lgbm_long or lgbm_short.

    Parameters
    ----------
    side      : 'long' or 'short'
    db_path   : path to vanguard_universe.db
    model_dir : directory to save artifacts

    Returns metrics dict.
    """
    label_col = _LABEL_MAP[side]
    logger.info("Training %s on %s", _model_name(asset_class, side), label_col)
    t0 = time.perf_counter()

    tbm_profiles = tbm_profiles or load_tbm_profiles()
    feature_profiles = feature_profiles or load_feature_profiles()

    df = load_training_data_for_asset_class(db_path, asset_class)
    if max_rows and len(df) > max_rows:
        print(
            f"[V4B] Sampling asset_class={asset_class} from {len(df):,} to {max_rows:,} rows",
            flush=True,
        )
        df = (
            df.sample(n=max_rows, random_state=42)
            .sort_values("asof_ts_utc")
            .reset_index(drop=True)
        )
        print(
            f"[V4B] Sampled asset_class={asset_class} rows={len(df):,}",
            flush=True,
        )
    if df.empty:
        raise RuntimeError(f"No training data found in vanguard_training_data for asset_class={asset_class}")

    requested_feature_names = _requested_feature_names_from_scorer(asset_class, feature_profiles)
    feature_names = resolve_available_feature_names(df, requested_feature_names, asset_class)
    feature_profile = feature_profiles.get(asset_class, feature_profiles.get("equity", {}))
    tbm_profile = tbm_profiles.get(asset_class, tbm_profiles.get("equity", {}))

    # --- Cross-sectional normalization ---
    print(f"[V4B] Cross-sectional normalizing asset_class={asset_class} features...", flush=True)
    df = cross_sectional_normalize(df, feature_names)

    # --- Purged walk-forward CV ---
    print(f"[V4B] Starting purged walk-forward CV for asset_class={asset_class}...", flush=True)
    ics_per_window: list[float] = []
    wf_rows: list[dict] = []
    model = None
    metrics: dict = {}
    df_train_last: pd.DataFrame | None = None
    df_test_last: pd.DataFrame | None = None

    for fold_i, (train_mask, test_mask) in enumerate(
        purged_walk_forward_split(df, n_splits=5, embargo_bars=6)
    ):
        print(
            f"[V4B] Walk-forward window {fold_i + 1}: "
            f"train={train_mask.sum():,} test={test_mask.sum():,}",
            flush=True,
        )
        df_train_fold = df.loc[train_mask].copy()
        df_test_fold  = df.loc[test_mask].copy()

        X_train = prepare_features(df_train_fold, feature_names=feature_names)
        y_train = df_train_fold[label_col].values.astype(int)
        X_test  = prepare_features(df_test_fold, feature_names=feature_names)
        y_test  = df_test_fold[label_col].values.astype(int)

        weights = compute_sample_weights(df_train_fold)
        spw = _scale_pos_weight(y_train)
        fold_params = _make_lgbm_params(spw)
        n_est = fold_params.pop("n_estimators", 500)

        fold_model = lgb.LGBMClassifier(n_estimators=n_est, **fold_params)
        fold_model.fit(
            X_train, y_train,
            sample_weight=weights,
            eval_set=[(X_test, y_test)],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(period=-1)],
        )

        proba = fold_model.predict_proba(X_test)[:, 1]
        if "forward_return" in df.columns:
            fwd = df_test_fold["forward_return"].values
            ic = float(spearmanr(proba, fwd).statistic)
        else:
            ic = float(spearmanr(proba, y_test).statistic)

        ics_per_window.append(ic)
        print(f"[V4B]   Window {fold_i + 1} IC: {ic:.4f}", flush=True)

        wf_rows.append({
            "train_start": str(df_train_fold["asof_ts_utc"].iloc[0]),
            "train_end":   str(df_train_fold["asof_ts_utc"].iloc[-1]),
            "test_start":  str(df_test_fold["asof_ts_utc"].iloc[0]),
            "test_end":    str(df_test_fold["asof_ts_utc"].iloc[-1]),
            "test_rows":   int(test_mask.sum()),
            "ic_long":     ic if side == "long" else None,
            "ic_short":    ic if side == "short" else None,
            "auc":         None,
            "accuracy":    None,
            "wr_at_55":    None,
        })
        model = fold_model
        df_train_last = df_train_fold
        df_test_last  = df_test_fold

    if not ics_per_window:
        # Fallback to single temporal split if walk-forward produced no windows
        print(f"[V4B] Walk-forward produced no windows — falling back to 70/30 split", flush=True)
        df_train_last, df_test_last = temporal_split(df)
        model, metrics = train_lgbm(label_col, df_train_last, df_test_last, feature_names=feature_names)
        ics_per_window = [metrics["ic_vs_return"]]
        wf_rows = [{
            "train_start": str(df_train_last["asof_ts_utc"].iloc[0]),
            "train_end":   str(df_train_last["asof_ts_utc"].iloc[-1]),
            "test_start":  str(df_test_last["asof_ts_utc"].iloc[0]),
            "test_end":    str(df_test_last["asof_ts_utc"].iloc[-1]),
            "test_rows":   len(df_test_last),
            "ic_long":     ics_per_window[0] if side == "long" else None,
            "ic_short":    ics_per_window[0] if side == "short" else None,
            "auc":         metrics["auc"],
            "accuracy":    metrics["accuracy"],
            "wr_at_55":    metrics.get("wr_at_55"),
        }]
    else:
        # Full metrics on the last fold's test set
        X_test_last = prepare_features(df_test_last, feature_names=feature_names)
        y_test_last = df_test_last[label_col].values.astype(int)
        metrics = evaluate_model(
            model, X_test_last, y_test_last,
            df_test_last["forward_return"].values, label_col,
        )
        metrics["train_rows"]       = len(df_train_last)
        metrics["test_rows"]        = len(df_test_last)
        metrics["label_rate_train"] = float(df_train_last[label_col].mean())
        metrics["label_rate_test"]  = float(df_test_last[label_col].mean())
        metrics["best_iteration"]   = int(model.best_iteration_ or 500)
        metrics["scale_pos_weight"] = _scale_pos_weight(df_train_last[label_col].values.astype(int))
        metrics["feature_count"]    = len(feature_names)
        # Backfill last window with full metrics
        wf_rows[-1]["auc"]      = metrics["auc"]
        wf_rows[-1]["accuracy"] = metrics["accuracy"]
        wf_rows[-1]["wr_at_55"] = metrics.get("wr_at_55")

    mean_ic = float(np.mean(ics_per_window))

    # --- Walk-forward summary ---
    print(f"\n{'='*60}", flush=True)
    print(f"  {_model_name(asset_class, side)} — Walk-Forward Summary", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Windows         : {len(ics_per_window)}", flush=True)
    print(f"  Mean IC         : {mean_ic:.4f}", flush=True)
    print(f"  IC Std Dev      : {np.std(ics_per_window):.4f}", flush=True)
    print(f"  IC Hit Rate     : {sum(1 for ic in ics_per_window if ic > 0) / len(ics_per_window):.1%}", flush=True)
    print(f"  Best Window IC  : {max(ics_per_window):.4f}", flush=True)
    print(f"  Worst Window IC : {min(ics_per_window):.4f}", flush=True)
    print(f"{'='*60}\n", flush=True)

    elapsed = time.perf_counter() - t0
    metrics["elapsed_s"] = round(elapsed, 2)
    metrics["asset_class"] = asset_class

    _print_metrics(side, metrics, model, feature_names=feature_names, asset_class=asset_class)
    model_name = _model_name(asset_class, side)
    pkl_path = save_artifacts(
        model,
        metrics,
        label_col,
        model_dir,
        model_name=model_name,
        feature_names=feature_names,
        extra_config={
            "asset_class": asset_class,
            "direction": side,
            "feature_profile": feature_profile.get("profile"),
            "tbm_profile": tbm_profile.get("version"),
            "readiness": _readiness_for_window_count(1),
            "fallback_family": _fallback_family(asset_class, side),
        },
    )
    print(
        f"[V4B] Saved model asset_class={asset_class} side={side} -> {pkl_path}",
        flush=True,
    )

    # Write to model registry
    db = VanguardDB(db_path)
    now_str = iso_utc(now_utc())
    readiness = _readiness_for_window_count(len(ics_per_window))
    model_id = model_name
    registry_row = {
        "model_id":              model_id,
        "model_family":          model_name,
        "track":                 "A",
        "target_label":          label_col,
        "feature_count":         len(feature_names),
        "train_rows":            metrics["train_rows"],
        "test_rows":             metrics["test_rows"],
        "training_rows":         len(df),
        "walk_forward_windows":  len(ics_per_window),
        "mean_ic_long":          mean_ic if side == "long" else None,
        "mean_ic_short":         mean_ic if side == "short" else None,
        "auc_long":              metrics["auc"] if side == "long" else None,
        "auc_short":             metrics["auc"] if side == "short" else None,
        "long_wr_at_55":         metrics.get("wr_at_55") if side == "long" else None,
        "short_wr_at_55":        metrics.get("wr_at_55") if side == "short" else None,
        "artifact_path":         str(pkl_path),
        "created_at_utc":        now_str,
        "status":                "trained",
        "notes":                 None,
        "asset_class":           asset_class,
        "direction":             side,
        "feature_profile":       feature_profile.get("profile"),
        "tbm_profile":           tbm_profile.get("version"),
        "readiness":             readiness,
        "fallback_family":       _fallback_family(asset_class, side),
    }
    db.upsert_model_registry(registry_row)
    db.upsert_walkforward_results([
        {
            "model_id":    model_id,
            "window_id":   window_i + 1,
            "train_start": row["train_start"],
            "train_end":   row["train_end"],
            "test_start":  row["test_start"],
            "test_end":    row["test_end"],
            "test_rows":   row["test_rows"],
            "ic_long":     row["ic_long"],
            "ic_short":    row["ic_short"],
            "auc":         row["auc"],
            "accuracy":    row["accuracy"],
            "wr_at_55":    row["wr_at_55"],
            "asset_class": asset_class,
        }
        for window_i, row in enumerate(wf_rows)
    ])

    return {**metrics, "model_id": model_id, "pkl_path": str(pkl_path)}


def _print_metrics(
    side: str,
    metrics: dict,
    model: lgb.LGBMClassifier,
    feature_names: list[str] | None = None,
    asset_class: str | None = None,
) -> None:
    fi = feature_importance(model, feature_names=feature_names)
    print(f"\n{'='*55}", flush=True)
    title = _model_name(asset_class or "equity", side) if asset_class else f"lgbm_{side}"
    print(f"  {title.upper()} — Validation Results", flush=True)
    print(f"{'='*55}", flush=True)
    print(f"  Train rows     : {metrics['train_rows']:,}  "
          f"(label rate {metrics['label_rate_train']:.1%})", flush=True)
    print(f"  Test rows      : {metrics['test_rows']:,}  "
          f"(label rate {metrics['label_rate_test']:.1%})", flush=True)
    print(f"  Best iteration : {metrics['best_iteration']}", flush=True)
    print(f"  AUC-ROC        : {metrics['auc']:.4f}", flush=True)
    print(f"  Accuracy @0.50 : {metrics['accuracy']:.4f}", flush=True)
    print(f"  Precision @0.50: {metrics['precision']:.4f}", flush=True)
    print(f"  Recall @0.50   : {metrics['recall']:.4f}", flush=True)
    print(f"  IC (vs return) : {metrics['ic_vs_return']:.4f}", flush=True)
    print(f"  IC (vs label)  : {metrics['ic_vs_label']:.4f}", flush=True)
    wr = metrics.get('wr_at_55')
    wr_str = f"{wr:.1%}" if wr is not None else "n/a"
    print(f"  WR @ P>0.55    : {wr_str}  ({metrics['n_at_55']:,} predictions)", flush=True)
    print(f"  Elapsed        : {metrics['elapsed_s']:.1f}s", flush=True)
    print(f"\n  Top 10 features ({side} model):", flush=True)
    for rank, (fname, imp) in enumerate(fi, 1):
        print(f"    {rank:>2}. {fname:<38} {imp:>10.1f}", flush=True)
    print(flush=True)


# ---------------------------------------------------------------------------
# Validate (load existing models)
# ---------------------------------------------------------------------------

def validate(
    db_path: str = DB_PATH,
    model_dir: Path = MODEL_DIR,
) -> bool:
    """Load saved models and re-run evaluation on held-out test set."""
    from vanguard.models.model_loader import load_lgbm_long, load_lgbm_short

    pkl_long  = model_dir / "lgbm_long.pkl"
    pkl_short = model_dir / "lgbm_short.pkl"

    if not pkl_long.exists() or not pkl_short.exists():
        missing = [p for p in [pkl_long, pkl_short] if not p.exists()]
        print(f"\n[VALIDATE] FAIL — model files not found: {missing}", flush=True)
        print("Run trainer first: python3 stages/vanguard_model_trainer.py", flush=True)
        return False

    df = load_training_data(db_path)
    if df.empty:
        print("\n[VALIDATE] FAIL — no training data", flush=True)
        return False

    _, df_test = temporal_split(df)
    X_test = prepare_features(df_test)

    model_long  = load_lgbm_long(model_dir)
    model_short = load_lgbm_short(model_dir)

    metrics_long  = evaluate_model(
        model_long,  X_test, df_test["label_long"].values.astype(int),
        df_test["forward_return"].values, "label_long",
    )
    metrics_short = evaluate_model(
        model_short, X_test, df_test["label_short"].values.astype(int),
        df_test["forward_return"].values, "label_short",
    )

    print(f"\n[VALIDATE] Test set: {len(df_test):,} rows "
          f"({df_test['asof_ts_utc'].iloc[0][:10]} → {df_test['asof_ts_utc'].iloc[-1][:10]})", flush=True)

    for side, m, model in [("long", metrics_long, model_long), ("short", metrics_short, model_short)]:
        fi = feature_importance(model)
        print(f"\n  lgbm_{side}:", flush=True)
        print(f"    AUC-ROC        : {m['auc']:.4f}", flush=True)
        print(f"    Accuracy @0.50 : {m['accuracy']:.4f}", flush=True)
        print(f"    Precision @0.50: {m['precision']:.4f}", flush=True)
        print(f"    Recall @0.50   : {m['recall']:.4f}", flush=True)
        print(f"    IC (vs return) : {m['ic_vs_return']:.4f}", flush=True)
        wr = m.get('wr_at_55')
        print(f"    WR @ P>0.55    : {f'{wr:.1%}' if wr else 'n/a'} ({m['n_at_55']:,} calls)", flush=True)
        print(f"    Top 10 features:", flush=True)
        for rank, (fname, imp) in enumerate(fi, 1):
            print(f"      {rank:>2}. {fname:<38} {imp:>10.1f}", flush=True)

    both_ok = metrics_long["auc"] > 0.50 and metrics_short["auc"] > 0.50
    print(f"\n[VALIDATE] {'PASS' if both_ok else 'WARN'} — "
          f"AUC long={metrics_long['auc']:.4f} | short={metrics_short['auc']:.4f}", flush=True)
    return both_ok


def show_feature_importance(model_dir: Path = MODEL_DIR) -> None:
    """Print top 10 features for both saved models."""
    from vanguard.models.model_loader import load_lgbm_long, load_lgbm_short

    for side, loader in [("long", load_lgbm_long), ("short", load_lgbm_short)]:
        pkl = model_dir / f"lgbm_{side}.pkl"
        if not pkl.exists():
            print(f"  lgbm_{side}: not found at {pkl}", flush=True)
            continue
        model = loader(model_dir)
        fi = feature_importance(model, top_n=10)
        print(f"\n  Top 10 features — lgbm_{side}:", flush=True)
        for rank, (fname, imp) in enumerate(fi, 1):
            print(f"    {rank:>2}. {fname:<38} {imp:>10.1f}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="V4B Vanguard Model Trainer — classifiers + regressor ensembles"
    )
    p.add_argument(
        "--model",
        choices=["long", "short", "both", "regressor"],
        default=None,
        help="Which model family to train. Defaults to regressor for forex/crypto, otherwise both classifiers.",
    )
    p.add_argument("--validate", action="store_true",
                   help="Validate existing saved models (no retraining)")
    p.add_argument("--feature-importance", action="store_true",
                   help="Show top-10 features for saved models and exit")
    p.add_argument("--asset-class", default=None,
                   help="Restrict training to one asset class")
    p.add_argument("--max-rows", type=int, default=2000000,
                   help="Maximum rows to load per asset class before random sampling (default: 2000000)")
    p.add_argument("--promotion-check", action="store_true",
                   help="Show promotion readiness for trained models and exit")
    p.add_argument("--promote", default=None,
                   help="Promote one model to live_native if it passes QA thresholds")
    p.add_argument("--demote", default=None,
                   help="Demote one model back to validated_shadow")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if args.feature_importance:
        show_feature_importance()
        return

    if args.validate:
        ok = validate()
        sys.exit(0 if ok else 1)

    if args.promotion_check:
        checks = promotion_check()
        print(f"\n{'='*90}", flush=True)
        print("PROMOTION CHECK", flush=True)
        print(f"{'='*90}", flush=True)
        for row in checks:
            status = "READY" if row["eligible"] else "BLOCKED"
            mean_ic = "n/a" if row["mean_ic"] is None else f"{row['mean_ic']:.4f}"
            wr = "n/a" if row["wr_at_55"] is None else f"{row['wr_at_55']:.1%}"
            ic_std = "n/a" if row["ic_std"] is None else f"{row['ic_std']:.4f}"
            reasons = "manual sign-off required" if row["eligible"] else ", ".join(row["reasons"])
            print(
                f"{row['model_id']:30s} {status:7s} "
                f"rows={row['training_rows']:6d} wf={row['walk_forward_windows']:2d} "
                f"ic={mean_ic:>7s} wr@55={wr:>7s} ic_std={ic_std:>7s} | {reasons}"
            , flush=True)
        return

    if args.promote:
        checks = {row["model_id"]: row for row in promotion_check()}
        result = checks.get(args.promote)
        if not result:
            print(f"Model not found: {args.promote}", flush=True)
            sys.exit(1)
        if not result["eligible"]:
            print(f"Promotion blocked for {args.promote}: {', '.join(result['reasons'])}", flush=True)
            sys.exit(1)
        if not _set_model_readiness(args.promote, READINESS_LIVE_NATIVE):
            print(f"Model not found: {args.promote}", flush=True)
            sys.exit(1)
        print(f"Promoted {args.promote} -> {READINESS_LIVE_NATIVE}", flush=True)
        return

    if args.demote:
        if not _set_model_readiness(args.demote, READINESS_VALIDATED_SHADOW):
            print(f"Model not found: {args.demote}", flush=True)
            sys.exit(1)
        print(f"Demoted {args.demote} -> {READINESS_VALIDATED_SHADOW}", flush=True)
        return

    tbm_profiles = load_tbm_profiles()
    feature_profiles = load_feature_profiles()
    selected_model = args.model or (
        "regressor" if args.asset_class in {"equity", "forex", "crypto"} else "both"
    )
    target_asset_classes = (
        [args.asset_class]
        if args.asset_class
        else [
            asset_class for asset_class in tbm_profiles.keys()
            if selected_model != "regressor" or asset_class in SCORER_MODEL_SPECS
        ]
    )
    sides = ["long", "short"] if selected_model == "both" else [selected_model]
    db = VanguardDB(DB_PATH)

    for asset_class in target_asset_classes:
        print(f"[V4B] Starting asset_class={asset_class}", flush=True)
        row_count = count_training_rows_for_asset_class(DB_PATH, asset_class)
        print(f"[V4B] DB row count asset_class={asset_class}: {row_count:,}", flush=True)
        feature_profile = feature_profiles.get(asset_class, feature_profiles.get("equity", {}))
        tbm_profile = tbm_profiles.get(asset_class, tbm_profiles.get("equity", {}))
        if selected_model == "regressor":
            if asset_class not in SCORER_MODEL_SPECS:
                logger.warning("Skipping %s — no regressor scorer spec", asset_class)
                continue
            if row_count < MIN_TRAINING_ROWS:
                logger.warning(
                    "Skipping %s | asset_class=%s has only %d rows (< %d)",
                    _model_name(asset_class, "regressor"),
                    asset_class,
                    row_count,
                    MIN_TRAINING_ROWS,
                )
                db.upsert_model_registry({
                    "model_id": _model_name(asset_class, "regressor"),
                    "model_family": _model_name(asset_class, "regressor"),
                    "track": "A",
                    "target_label": "forward_return",
                    "feature_count": len(_scorer_spec(asset_class).get("features") or feature_names_for_asset_class(asset_class, feature_profiles)),
                    "train_rows": 0,
                    "test_rows": 0,
                    "training_rows": row_count,
                    "walk_forward_windows": 0,
                    "mean_ic_long": None,
                    "mean_ic_short": None,
                    "auc_long": None,
                    "auc_short": None,
                    "long_wr_at_55": None,
                    "short_wr_at_55": None,
                    "artifact_path": None,
                    "created_at_utc": iso_utc(now_utc()),
                    "status": "skipped",
                    "notes": f"insufficient data for {asset_class}",
                    "asset_class": asset_class,
                    "direction": "regressor",
                    "feature_profile": feature_profile.get("profile"),
                    "tbm_profile": tbm_profile.get("version"),
                    "readiness": READINESS_NOT_TRAINED,
                    "fallback_family": None,
                })
                continue
            train_regressor_ensemble(
                asset_class,
                db_path=DB_PATH,
                model_dir=MODEL_DIR,
                max_rows=args.max_rows,
                tbm_profiles=tbm_profiles,
                feature_profiles=feature_profiles,
            )
            continue

        for side in sides:
            model_name = _model_name(asset_class, side)
            label_col = _LABEL_MAP[side]
            if row_count < MIN_TRAINING_ROWS:
                logger.warning(
                    "Skipping %s | asset_class=%s has only %d rows (< %d)",
                    model_name,
                    asset_class,
                    row_count,
                    MIN_TRAINING_ROWS,
                )
                db.upsert_model_registry({
                    "model_id": model_name,
                    "model_family": model_name,
                    "track": "A",
                    "target_label": label_col,
                    "feature_count": len(feature_names_for_asset_class(asset_class, feature_profiles)),
                    "train_rows": 0,
                    "test_rows": 0,
                    "training_rows": row_count,
                    "walk_forward_windows": 0,
                    "mean_ic_long": None,
                    "mean_ic_short": None,
                    "auc_long": None,
                    "auc_short": None,
                    "long_wr_at_55": None,
                    "short_wr_at_55": None,
                    "artifact_path": None,
                    "created_at_utc": iso_utc(now_utc()),
                    "status": "skipped",
                    "notes": f"insufficient data for {asset_class}",
                    "asset_class": asset_class,
                    "direction": side,
                    "feature_profile": feature_profile.get("profile"),
                    "tbm_profile": tbm_profile.get("version"),
                    "readiness": READINESS_NOT_TRAINED,
                    "fallback_family": _fallback_family(asset_class, side),
                })
                continue
            train_model(
                side,
                db_path=DB_PATH,
                model_dir=MODEL_DIR,
                asset_class=asset_class,
                max_rows=args.max_rows,
                tbm_profiles=tbm_profiles,
                feature_profiles=feature_profiles,
            )


if __name__ == "__main__":
    main()
