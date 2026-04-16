#!/usr/bin/env python3
"""
vanguard_scorer.py — V4B inference-only regressor scorer.

Loads trained regressor ensembles from models/vanguard/latest, scores the latest
V3 factor cycle, and writes predicted returns to vanguard_predictions.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

try:
    import torch
except Exception:  # pragma: no cover
    torch = None

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.config.runtime_config import get_model_config, get_shadow_db_path
from vanguard.helpers.clock import now_utc, iso_utc
from vanguard.features.feature_computer import CRYPTO_FEATURES
from vanguard.models.alfa_sequence import build_model_from_meta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_scorer")

DB_PATH = get_shadow_db_path()
DEFAULT_MODEL_DIR = _REPO_ROOT / "models" / "vanguard" / "latest"

EQUITY_FEATURES = [
    "session_vwap_distance", "premium_discount_zone", "gap_pct",
    "session_opening_range_position", "daily_drawdown_from_high",
    "momentum_3bar", "momentum_12bar", "momentum_acceleration",
    "atr_expansion", "daily_adx", "relative_volume", "volume_burst_z",
    "down_volume_ratio", "effort_vs_result", "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark", "benchmark_momentum_12bar", "cross_asset_correlation",
    "session_phase", "ob_proximity_5m", "fvg_bullish_nearest", "fvg_bearish_nearest",
    "structure_break", "liquidity_sweep", "smc_premium_discount",
    "htf_trend_direction", "htf_structure_break", "htf_fvg_nearest", "htf_ob_proximity",
]

FOREX_FEATURES = [
    "session_vwap_distance", "premium_discount_zone", "gap_pct",
    "session_opening_range_position", "daily_drawdown_from_high",
    "momentum_3bar", "momentum_12bar", "momentum_acceleration",
    "atr_expansion", "daily_adx", "volume_burst_z", "down_volume_ratio",
    "rs_vs_benchmark_intraday", "daily_rs_vs_benchmark", "benchmark_momentum_12bar",
    "cross_asset_correlation", "session_phase", "ob_proximity_5m",
    "fvg_bullish_nearest", "fvg_bearish_nearest", "structure_break",
    "liquidity_sweep", "smc_premium_discount", "htf_trend_direction",
    "htf_structure_break", "htf_fvg_nearest", "htf_ob_proximity",
]

FOREX_SHAP14_FEATURES = [
    "gap_pct",
    "premium_discount_zone",
    "daily_drawdown_from_high",
    "rs_vs_benchmark_intraday",
    "daily_rs_vs_benchmark",
    "session_opening_range_position",
    "atr_expansion",
    "momentum_12bar",
    "session_phase",
    "session_vwap_distance",
    "momentum_3bar",
    "smc_premium_discount",
    "down_volume_ratio",
    "spread_proxy",
]

CRYPTO_PRUNED_FEATURES = [
    "atr_expansion",
    "premium_discount_zone",
    "down_volume_ratio",
    "fvg_bullish_nearest",
    "momentum_12bar",
    "ob_proximity_5m",
    "fvg_bearish_nearest",
    "smc_premium_discount",
    "momentum_acceleration",
    "momentum_3bar",
]

MODEL_SPECS = {
    "equity": {
        "model_dir": _REPO_ROOT / "models" / "equity_regressors",
        "meta_file": "meta.json",
        "features": EQUITY_FEATURES,
        "target": "forward_return",
        # Equity ensemble wired to LGBM/LGBM_SW/ET/Ridge regressors with user-specified weights.
        "models": [
            ("lgbm_equity_regressor_v1.pkl", 0.4),
            ("lgbm_sw_equity_regressor_v1.pkl", 0.2),
            ("et_equity_regressor_v1.pkl", 0.2),
            ("ridge_equity_regressor_v1.pkl", 0.2),
        ],
        "model_id": "equity_regressor_ensemble_v1",
    },
    "forex": {
        "model_dir": DEFAULT_MODEL_DIR,
        "meta_file": "vanguard_forex_model_meta.json",
        "features": FOREX_SHAP14_FEATURES,
        "target": "forward_return",
        "models": [("lgbm_ridge_shap14_forward_return_v1.joblib", 1.0)],
        "model_id": "lgbm_ridge_shap14_forward_return_v1",
    },
    "crypto": {
        "model_dir": DEFAULT_MODEL_DIR,
        "meta_file": "vanguard_crypto_model_meta.json",
        "features": CRYPTO_PRUNED_FEATURES,
        "fallback_features": CRYPTO_FEATURES,
        "target": "forward_return",
        # 2026-04-04 Calibrated stacked ensemble with volatility scaling
        "models": [("stacked_crypto_calibrated_v1.pkl", 1.0)],
        "model_id": "stacked_crypto_calibrated_v1",
    },
    "metal": {
        "model_dir": _REPO_ROOT / "models" / "lgbm_metals_energy_v1",
        "meta_file": "meta.json",
        "features": FOREX_FEATURES,
        "models": [("lgbm_metals_energy_v1.pkl", 1.0)],
        "model_id": "lgbm_metals_energy_v1",
    },
    "energy": {
        "model_dir": _REPO_ROOT / "models" / "lgbm_metals_energy_v1",
        "meta_file": "meta.json",
        "features": FOREX_FEATURES,
        "models": [("lgbm_metals_energy_v1.pkl", 1.0)],
        "model_id": "lgbm_metals_energy_v1",
    },
    "index": {
        "model_dir": _REPO_ROOT / "models" / "lgbm_index_v1",
        "meta_file": "meta.json",
        "features": FOREX_FEATURES,
        "models": [("lgbm_index_v1.pkl", 1.0)],
        "model_id": "lgbm_index_v1",
    },
}

CREATE_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS vanguard_predictions (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    predicted_return REAL,
    direction TEXT,
    model_id TEXT,
    scored_at TEXT,
    PRIMARY KEY (cycle_ts_utc, symbol)
);
CREATE INDEX IF NOT EXISTS idx_vg_predictions_cycle
    ON vanguard_predictions(cycle_ts_utc, asset_class);
"""


def _ensure_schema(con: sqlite3.Connection) -> None:
    for stmt in CREATE_PREDICTIONS.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.commit()


def _latest_cycle(con: sqlite3.Connection, asset_class: str | None = None) -> str | None:
    if asset_class:
        row = con.execute(
            "SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix WHERE asset_class = ?",
            (asset_class,),
        ).fetchone()
    else:
        row = con.execute("SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix").fetchone()
    return row[0] if row and row[0] else None


def _load_factor_frame(con: sqlite3.Connection, cycle_ts: str, asset_class: str | None = None) -> pd.DataFrame:
    if asset_class:
        return pd.read_sql_query(
            "SELECT * FROM vanguard_factor_matrix WHERE cycle_ts_utc = ? AND asset_class = ? ORDER BY symbol",
            con,
            params=(cycle_ts, asset_class),
        )
    return pd.read_sql_query(
        "SELECT * FROM vanguard_factor_matrix WHERE cycle_ts_utc = ? ORDER BY symbol",
        con,
        params=(cycle_ts,),
    )


def _model_dir(asset_class: str) -> Path:
    runtime_model_cfg = get_model_config(asset_class)
    return Path(runtime_model_cfg.get("model_dir") or MODEL_SPECS[asset_class].get("model_dir") or DEFAULT_MODEL_DIR)


def _load_meta(asset_class: str) -> dict[str, Any]:
    spec = MODEL_SPECS[asset_class]
    runtime_model_cfg = get_model_config(asset_class)
    meta_file = runtime_model_cfg.get("meta_file") or spec["meta_file"]
    meta_path = _model_dir(asset_class) / meta_file
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text())


def _meta_features(asset_class: str, meta: dict[str, Any]) -> list[str]:
    spec_features = list(MODEL_SPECS[asset_class]["features"])
    if not meta:
        return spec_features
    features = meta.get("features")
    if isinstance(features, list) and features:
        return [str(col) for col in features]
    training_data = meta.get("training_data") or {}
    features = training_data.get("features")
    if isinstance(features, list) and features:
        return [str(col) for col in features]
    return spec_features


def _meta_model_id(asset_class: str, meta: dict[str, Any]) -> str:
    runtime_model_cfg = get_model_config(asset_class)
    return str(
        runtime_model_cfg.get("model_id")
        or meta.get("model_id")
        or MODEL_SPECS[asset_class]["model_id"]
    )


def _model_kind(asset_class: str, meta: dict[str, Any]) -> str:
    runtime_model_cfg = get_model_config(asset_class)
    return str(
        runtime_model_cfg.get("model_kind")
        or meta.get("inference_adapter")
        or meta.get("model_family")
        or "joblib_regressor"
    )


def _artifact_specs(asset_class: str) -> list[tuple[str, float]]:
    runtime_model_cfg = get_model_config(asset_class)
    configured = runtime_model_cfg.get("artifact_files") or runtime_model_cfg.get("models")
    if configured:
        specs: list[tuple[str, float]] = []
        if isinstance(configured, str):
            specs.append((configured, 1.0))
        else:
            for entry in configured:
                if isinstance(entry, str):
                    specs.append((entry, 1.0))
                elif isinstance(entry, dict):
                    specs.append((str(entry.get("file") or entry.get("filename")), float(entry.get("weight", 1.0))))
                elif isinstance(entry, (list, tuple)) and len(entry) >= 1:
                    specs.append((str(entry[0]), float(entry[1] if len(entry) > 1 else 1.0)))
        if specs:
            return specs
    return [(str(filename), float(weight)) for filename, weight in MODEL_SPECS[asset_class]["models"]]


def _load_models(asset_class: str, meta: dict[str, Any]) -> list[tuple[Any, float]]:
    loaded = []
    model_kind = _model_kind(asset_class, meta)
    for filename, weight in _artifact_specs(asset_class):
        path = _model_dir(asset_class) / filename
        if not path.exists():
            logger.warning("Missing %s model artifact, skipping component: %s", asset_class, path)
            continue
        if model_kind == "alfa_sequence":
            if torch is None:
                raise RuntimeError("torch is required for alfa_sequence inference")
            payload = torch.load(path, map_location="cpu")
            if not isinstance(payload, dict) or "state_dict" not in payload:
                raise RuntimeError(f"Invalid alfa_sequence checkpoint payload: {path}")
            model_meta = payload.get("model_meta") or meta
            model = build_model_from_meta(model_meta)
            model.load_state_dict(payload["state_dict"])
            model.eval()
            loaded.append(({"model": model, "meta": model_meta}, weight))
        else:
            loaded.append((joblib.load(path), weight))
    if not loaded:
        raise FileNotFoundError(f"No model artifacts available for asset_class={asset_class}")
    return loaded


def _predict_stacked_crypto_model(artifact: dict[str, Any], x: pd.DataFrame) -> np.ndarray:
    """Predict with the calibrated stacked crypto artifact and apply volatility scaling."""
    base_models = artifact.get("base_models") or {}
    meta_model = artifact.get("meta_model")
    scale_factor = float(artifact.get("scale_factor") or 1.0)
    if not base_models or meta_model is None:
        raise RuntimeError("Invalid stacked crypto artifact: missing base_models/meta_model")

    ordered_base_predictions = [
        np.asarray(model.predict(x), dtype=float)
        for _name, model in base_models.items()
    ]
    stacked_x = np.column_stack(ordered_base_predictions)
    raw_meta_pred = np.asarray(meta_model.predict(stacked_x), dtype=float)
    return raw_meta_pred * scale_factor


def _model_input(model: Any, x: pd.DataFrame, x_values: np.ndarray) -> pd.DataFrame | np.ndarray:
    model_feature_names = getattr(model, "feature_names_in_", None)
    if model_feature_names is not None:
        names = list(model_feature_names)
        if names == list(x.columns):
            return x
        if len(names) == x_values.shape[1]:
            return pd.DataFrame(x_values, columns=names, index=x.index)
    return x_values


def _predict_blended_dict_model(artifact: dict[str, Any], x: pd.DataFrame, x_values: np.ndarray) -> np.ndarray:
    lgbm = artifact.get("lgbm")
    ridge = artifact.get("ridge")
    if lgbm is None or ridge is None:
        raise RuntimeError("Invalid forex blend artifact: missing lgbm/ridge components")
    lgbm_weight = float(artifact.get("lgbm_weight", 0.7))
    ridge_weight = float(artifact.get("ridge_weight", 0.3))
    lgbm_pred = np.asarray(lgbm.predict(_model_input(lgbm, x, x_values)), dtype=float)
    ridge_pred = np.asarray(ridge.predict(_model_input(ridge, x, x_values)), dtype=float)
    return lgbm_weight * lgbm_pred + ridge_weight * ridge_pred


def _normalize_symbols(values: pd.Series) -> pd.Series:
    return values.astype(str).str.replace("/", "", regex=False).str.upper()


def _load_ranked_history(
    con: sqlite3.Connection,
    *,
    table: str,
    time_col: str,
    symbol_col: str,
    features: list[str],
    symbols: list[str],
    cutoff_ts: str,
    width: int,
    extra_where: str = "",
    extra_params: list[Any] | None = None,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["ts", "symbol", *features])
    placeholders = ",".join("?" for _ in symbols)
    cols = ", ".join(features)
    where_clauses = [f"{time_col} <= ?", f"{symbol_col} IN ({placeholders})"]
    params: list[Any] = [cutoff_ts, *symbols]
    if extra_where:
        where_clauses.insert(0, extra_where)
        params = list(extra_params or []) + params
    sql = f"""
        SELECT ts, symbol, {cols}
        FROM (
            SELECT
                {time_col} AS ts,
                {symbol_col} AS symbol,
                {cols},
                ROW_NUMBER() OVER (PARTITION BY {symbol_col} ORDER BY {time_col} DESC) AS rn
            FROM {table}
            WHERE {' AND '.join(where_clauses)}
        )
        WHERE rn <= ?
        ORDER BY symbol, ts
    """
    params.append(width)
    return pd.read_sql_query(sql, con, params=params)


def _history_windows(history: pd.DataFrame, features: list[str]) -> dict[str, np.ndarray]:
    if history.empty:
        return {}
    history = history.copy()
    history["symbol"] = _normalize_symbols(history["symbol"])
    history["ts"] = pd.to_datetime(history["ts"], utc=True)
    windows: dict[str, np.ndarray] = {}
    for symbol, grp in history.groupby("symbol", sort=False):
        windows[str(symbol)] = grp.sort_values("ts")[features].fillna(0.0).to_numpy(dtype=np.float32)
    return windows


def _pad_window(window: np.ndarray | None, width: int, n_feats: int) -> np.ndarray:
    if window is None or len(window) == 0:
        return np.zeros((width, n_feats), dtype=np.float32)
    arr = np.asarray(window, dtype=np.float32)
    if arr.shape[0] >= width:
        return arr[-width:]
    pad = np.zeros((width - arr.shape[0], n_feats), dtype=np.float32)
    return np.vstack([pad, arr])


def _prepare_alfa_sequence_inputs(
    con: sqlite3.Connection,
    frame: pd.DataFrame,
    meta: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    sequence_spec = meta.get("sequence_spec") or {}
    feat_5m = [str(col) for col in sequence_spec.get("feat_5m") or []]
    feat_mtf = [str(col) for col in sequence_spec.get("feat_mtf") or []]
    w_5m = int(sequence_spec.get("w_5m", 30))
    w_15m = int(sequence_spec.get("w_15m", 10))
    w_30m = int(sequence_spec.get("w_30m", 5))
    trained_symbols = [_normalize_symbols(pd.Series([sym])).iloc[0] for sym in sequence_spec.get("symbols") or []]
    if not feat_5m or not feat_mtf or not trained_symbols:
        raise RuntimeError("alfa_sequence metadata missing sequence_spec")

    cycle_ts = str(frame["cycle_ts_utc"].iloc[0])
    symbols = _normalize_symbols(frame["symbol"]).tolist()
    unknown_symbols = sorted(set(symbols) - set(trained_symbols))
    if unknown_symbols:
        raise RuntimeError(f"alfa_sequence missing symbol embeddings for {unknown_symbols}")

    hist_5m = _load_ranked_history(
        con,
        table="vanguard_factor_matrix",
        time_col="cycle_ts_utc",
        symbol_col="symbol",
        features=feat_5m,
        symbols=symbols,
        cutoff_ts=cycle_ts,
        width=w_5m,
        extra_where="asset_class = ?",
        extra_params=["forex"],
    )
    hist_15m = _load_ranked_history(
        con,
        table="vanguard_training_data_mtf",
        time_col="asof_ts",
        symbol_col="symbol",
        features=feat_mtf,
        symbols=symbols,
        cutoff_ts=cycle_ts,
        width=w_15m,
        extra_where="asset_class = ? AND timeframe = ?",
        extra_params=["forex", "15m"],
    )
    hist_30m = _load_ranked_history(
        con,
        table="vanguard_training_data_mtf",
        time_col="asof_ts",
        symbol_col="symbol",
        features=feat_mtf,
        symbols=symbols,
        cutoff_ts=cycle_ts,
        width=w_30m,
        extra_where="asset_class = ? AND timeframe = ?",
        extra_params=["forex", "30m"],
    )

    win_5m = _history_windows(hist_5m, feat_5m)
    win_15m = _history_windows(hist_15m, feat_mtf)
    win_30m = _history_windows(hist_30m, feat_mtf)
    sym_to_idx = {sym: idx for idx, sym in enumerate(trained_symbols)}

    x5 = np.stack([_pad_window(win_5m.get(sym), w_5m, len(feat_5m)) for sym in symbols], axis=0)
    x15 = np.stack([_pad_window(win_15m.get(sym), w_15m, len(feat_mtf)) for sym in symbols], axis=0)
    x30 = np.stack([_pad_window(win_30m.get(sym), w_30m, len(feat_mtf)) for sym in symbols], axis=0)
    sym_idx = np.asarray([sym_to_idx[sym] for sym in symbols], dtype=np.int64)
    return x5, x15, x30, sym_idx


def _predict_alfa_sequence_model(
    artifact: dict[str, Any],
    frame: pd.DataFrame,
    con: sqlite3.Connection,
) -> np.ndarray:
    if torch is None:
        raise RuntimeError("torch is required for alfa_sequence inference")
    model = artifact.get("model")
    meta = artifact.get("meta") or {}
    if model is None:
        raise RuntimeError("Invalid alfa_sequence artifact: missing model")
    x5, x15, x30, sym_idx = _prepare_alfa_sequence_inputs(con, frame, meta)
    with torch.no_grad():
        pred = model(
            torch.from_numpy(x5),
            torch.from_numpy(x15),
            torch.from_numpy(x30),
            torch.from_numpy(sym_idx),
        )
    return pred.detach().cpu().numpy().astype(float)


def _predict_asset_class(frame: pd.DataFrame, asset_class: str, con: sqlite3.Connection) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["cycle_ts_utc", "symbol", "asset_class", "predicted_return", "direction", "model_id", "scored_at"])

    meta = _load_meta(asset_class)
    models = _load_models(asset_class, meta)
    model_kind = _model_kind(asset_class, meta)
    if asset_class == "crypto":
        model_artifact = models[0][0] if models else None
        features = list(
            (model_artifact.get("features") if isinstance(model_artifact, dict) else None)
            or MODEL_SPECS[asset_class]["features"]
        )
        if not any(col in frame.columns for col in features):
            features = list(
                meta.get("features")
                or MODEL_SPECS[asset_class].get("fallback_features")
                or MODEL_SPECS[asset_class]["features"]
            )
    elif model_kind == "alfa_sequence":
        features = []
    else:
        features = _meta_features(asset_class, meta)
    available = [col for col in features if col in frame.columns]
    if features and not available:
        raise RuntimeError(f"No usable features for asset_class={asset_class}")

    x = frame.reindex(columns=features).copy() if features else pd.DataFrame(index=frame.index)
    x_values = np.empty((len(frame), 0), dtype=np.float64)
    if features:
        for col in x.columns:
            x[col] = pd.to_numeric(x[col], errors="coerce").fillna(0.0)
        x = x.astype(np.float64)

        # Cross-sectional standardization to reduce train-serving skew.
        for col in x.columns:
            mean = float(x[col].mean())
            std = float(x[col].std(ddof=0))
            if std > 1e-8:
                x[col] = (x[col] - mean) / std
            else:
                x[col] = 0.0

        x_values = x.to_numpy(dtype=np.float64)

    pred = np.zeros(len(frame), dtype=float)
    for model, weight in models:
        if asset_class == "crypto" and isinstance(model, dict) and model.get("meta_model") is not None:
            # 2026-04-04 Calibrated stacked ensemble with volatility scaling
            pred += weight * _predict_stacked_crypto_model(model, x)
        elif asset_class == "forex" and model_kind == "alfa_sequence" and isinstance(model, dict) and model.get("model") is not None:
            pred += weight * _predict_alfa_sequence_model(model, frame, con)
        elif asset_class == "forex" and isinstance(model, dict) and model.get("lgbm") is not None and model.get("ridge") is not None:
            pred += weight * _predict_blended_dict_model(model, x, x_values)
        else:
            pred += weight * np.asarray(model.predict(_model_input(model, x, x_values)), dtype=float)

    out = frame[["cycle_ts_utc", "symbol", "asset_class"]].copy()
    out["predicted_return"] = pred
    out["direction"] = np.where(out["predicted_return"] > 0, "LONG", np.where(out["predicted_return"] < 0, "SHORT", "FLAT"))
    out["model_id"] = _meta_model_id(asset_class, meta)
    out["scored_at"] = iso_utc(now_utc())
    return out


def _write_predictions(con: sqlite3.Connection, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    _ensure_schema(con)
    cycle_ts = str(frame["cycle_ts_utc"].iloc[0])
    con.execute("DELETE FROM vanguard_predictions WHERE cycle_ts_utc = ?", (cycle_ts,))
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_predictions
        (cycle_ts_utc, symbol, asset_class, predicted_return, direction, model_id, scored_at)
        VALUES (:cycle_ts_utc, :symbol, :asset_class, :predicted_return, :direction, :model_id, :scored_at)
        """,
        frame.to_dict("records"),
    )
    con.commit()
    return len(frame)


def run(*, dry_run: bool = False, asset_class: str | None = None, db_path: str = DB_PATH) -> dict[str, Any]:
    with connect_wal(db_path) as con:
        cycle_ts = _latest_cycle(con, asset_class=asset_class)
        if not cycle_ts:
            logger.warning("No factor matrix cycle found")
            return {"status": "no_features", "rows": 0, "cycle_ts_utc": None}

        factor_df = _load_factor_frame(con, cycle_ts, asset_class=asset_class)
        if factor_df.empty:
            logger.warning("Factor matrix empty for cycle=%s asset_class=%s", cycle_ts, asset_class)
            return {"status": "no_features", "rows": 0, "cycle_ts_utc": cycle_ts}

        parts = []
        skipped = []
        for asset in sorted(factor_df["asset_class"].dropna().astype(str).unique()):
            if asset not in MODEL_SPECS:
                skipped.append(asset)
                continue
            asset_df = factor_df[factor_df["asset_class"] == asset].copy()
            try:
                parts.append(_predict_asset_class(asset_df, asset, con))
            except FileNotFoundError as exc:
                logger.warning("No model for %s (%s) — using neutral edge=0.50", asset, exc)
                neutral_df = asset_df[["cycle_ts_utc", "symbol", "asset_class"]].copy()
                neutral_df["predicted_return"] = 0.0
                neutral_df["direction"] = "LONG"
                neutral_df["model_id"] = "neutral_fallback_v0"
                neutral_df["scored_at"] = cycle_ts
                parts.append(neutral_df)
                skipped.append(f"{asset}(neutral)")

        if not parts:
            logger.warning("No supported asset classes in factor matrix cycle=%s", cycle_ts)
            return {"status": "no_models", "rows": 0, "cycle_ts_utc": cycle_ts, "skipped": skipped}

        scored = pd.concat(parts, ignore_index=True).sort_values(["asset_class", "symbol"]).reset_index(drop=True)

        if dry_run:
            print(f"[V4B] cycle={cycle_ts} scored_rows={len(scored)} skipped={skipped}")
            for asset, grp in scored.groupby("asset_class"):
                print(f"  {asset}: {len(grp)} rows")
                print(grp[["symbol", "predicted_return", "direction"]].head(5).to_string(index=False))
            return {"status": "dry_run", "rows": len(scored), "cycle_ts_utc": cycle_ts, "skipped": skipped}

        written = _write_predictions(con, scored)

    logger.info("Wrote %d predictions for cycle=%s (skipped=%s)", written, cycle_ts, skipped)
    return {"status": "ok", "rows": written, "cycle_ts_utc": cycle_ts, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description="Vanguard V4B regressor scorer")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--asset-class", default="", help="Optional asset class filter")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, asset_class=(args.asset_class or None))
    return 0 if result.get("status") not in {"no_models"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
