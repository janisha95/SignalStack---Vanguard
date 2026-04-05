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

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.helpers.clock import now_utc, iso_utc
from vanguard.features.feature_computer import CRYPTO_FEATURES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_scorer")

DB_PATH = str(_REPO_ROOT / "data" / "vanguard_universe.db")
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
        "features": FOREX_FEATURES,
        "target": "forward_return",
        # NEW regressor ensemble trained 2026-04-03 — IC 0.0437
        "models": [
            ("rf_forex_regressor_v1.pkl", 0.5),
            ("lgbm_forex_regressor_v1.pkl", 0.5),
        ],
        "model_id": "forex_regressor_ensemble_v1",
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
    return Path(MODEL_SPECS[asset_class].get("model_dir") or DEFAULT_MODEL_DIR)


def _load_meta(asset_class: str) -> dict[str, Any]:
    spec = MODEL_SPECS[asset_class]
    meta_path = _model_dir(asset_class) / spec["meta_file"]
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text())


def _load_models(asset_class: str) -> list[tuple[Any, float]]:
    spec = MODEL_SPECS[asset_class]
    loaded = []
    for filename, weight in spec["models"]:
        path = _model_dir(asset_class) / filename
        if not path.exists():
            logger.warning("Missing %s model artifact, skipping component: %s", asset_class, path)
            continue
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


def _predict_asset_class(frame: pd.DataFrame, asset_class: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["cycle_ts_utc", "symbol", "asset_class", "predicted_return", "direction", "model_id", "scored_at"])

    meta = _load_meta(asset_class)
    models = _load_models(asset_class)
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
    else:
        features = list(meta.get("features") or MODEL_SPECS[asset_class]["features"])
    available = [col for col in features if col in frame.columns]
    if not available:
        raise RuntimeError(f"No usable features for asset_class={asset_class}")

    x = frame.reindex(columns=features).copy()
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

    pred = np.zeros(len(frame), dtype=float)
    for model, weight in models:
        if asset_class == "crypto" and isinstance(model, dict) and model.get("meta_model") is not None:
            # 2026-04-04 Calibrated stacked ensemble with volatility scaling
            pred += weight * _predict_stacked_crypto_model(model, x)
        else:
            pred += weight * np.asarray(model.predict(x), dtype=float)

    out = frame[["cycle_ts_utc", "symbol", "asset_class"]].copy()
    out["predicted_return"] = pred
    out["direction"] = np.where(out["predicted_return"] > 0, "LONG", np.where(out["predicted_return"] < 0, "SHORT", "FLAT"))
    out["model_id"] = MODEL_SPECS[asset_class]["model_id"]
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
            parts.append(_predict_asset_class(asset_df, asset))
        except FileNotFoundError as exc:
            logger.warning("Skipping %s: %s", asset, exc)
            skipped.append(asset)

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

    with connect_wal(db_path) as con:
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
