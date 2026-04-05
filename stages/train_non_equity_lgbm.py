#!/usr/bin/env python3
"""
train_non_equity_lgbm.py — Train index / metals-energy LGBM regressors from V3 replay.

This script deliberately uses the production V3 compute_features() path so the
training features match live inference as closely as possible.
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
from scipy.stats import spearmanr
from sklearn.model_selection import TimeSeriesSplit

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from stages import vanguard_factor_engine as v3
from vanguard.helpers.bars import parse_utc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("train_non_equity_lgbm")


DB_PATH = str(_REPO_ROOT / "data" / "vanguard_universe.db")
MODELS_DIR = _REPO_ROOT / "models"
MIN_REPLAY_ROWS = 1000
MIN_FOLD_ROWS = 200
DEFAULT_FORWARD_BARS = 6
DEFAULT_STRIDE = 3

MODEL_TARGETS = {
    "lgbm_metals_energy_v1": ["metal", "energy"],
    "lgbm_index_v1": ["index"],
}

META_COLUMNS = {
    "asof_ts_utc",
    "symbol",
    "asset_class",
    "forward_return",
}


def _load_symbol_rows(con: sqlite3.Connection, asset_classes: list[str]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in asset_classes)
    con.row_factory = sqlite3.Row
    return con.execute(
        f"""
        SELECT asset_class, symbol, COUNT(*) AS bars,
               MIN(bar_ts_utc) AS earliest, MAX(bar_ts_utc) AS latest
        FROM vanguard_bars_5m
        WHERE asset_class IN ({placeholders})
        GROUP BY asset_class, symbol
        ORDER BY asset_class, symbol
        """,
        tuple(asset_classes),
    ).fetchall()


def _load_bars(con: sqlite3.Connection, symbol: str, table: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        f"""
        SELECT symbol, bar_ts_utc, open, high, low, close, volume
        FROM {table}
        WHERE symbol = ?
        ORDER BY bar_ts_utc
        """,
        con,
        params=(symbol,),
    )
    if frame.empty:
        return frame
    frame["bar_ts_utc"] = frame["bar_ts_utc"].astype(str)
    return frame.sort_values("bar_ts_utc").reset_index(drop=True)


def _benchmark_frames(
    con: sqlite3.Connection,
    asset_class: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    symbol = v3._get_benchmark_symbol(asset_class)
    if symbol:
        bench_5m = _load_bars(con, symbol, "vanguard_bars_5m")
        bench_1h = _load_bars(con, symbol, "vanguard_bars_1h")
        if not bench_5m.empty:
            return bench_5m, bench_1h

    fallback = v3.BENCHMARK_FALLBACKS.get(asset_class)
    if fallback:
        return _load_bars(con, fallback, "vanguard_bars_5m"), _load_bars(con, fallback, "vanguard_bars_1h")

    return pd.DataFrame(), pd.DataFrame()


def _window(frame: pd.DataFrame, asof_ts: str, limit: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    subset = frame[frame["bar_ts_utc"] <= asof_ts].tail(limit)
    return subset.reset_index(drop=True)


def _replay_symbol_features(
    symbol: str,
    asset_class: str,
    bars_5m: pd.DataFrame,
    bars_1h: pd.DataFrame,
    benchmark_5m: pd.DataFrame,
    forward_bars: int,
    stride: int,
) -> list[dict[str, Any]]:
    if len(bars_5m) < max(v3.BARS_5M_LOOKBACK + forward_bars + 1, 60):
        logger.info("%s/%s: only %d 5m bars, skipping", asset_class, symbol, len(bars_5m))
        return []

    rows: list[dict[str, Any]] = []
    start_idx = max(v3.BARS_5M_LOOKBACK - 1, 0)
    stop_idx = len(bars_5m) - forward_bars

    for idx in range(start_idx, stop_idx, max(int(stride), 1)):
        asof_ts = str(bars_5m.iloc[idx]["bar_ts_utc"])
        entry = float(bars_5m.iloc[idx]["close"])
        future = float(bars_5m.iloc[idx + forward_bars]["close"])
        if entry <= 0:
            continue

        try:
            features = v3.compute_features(
                symbol,
                bars_5m.iloc[max(0, idx + 1 - v3.BARS_5M_LOOKBACK):idx + 1].reset_index(drop=True),
                _window(bars_1h, asof_ts, v3.BARS_1H_LOOKBACK),
                _window(benchmark_5m, asof_ts, v3.BARS_5M_LOOKBACK),
                asset_class=asset_class,
            )
        except Exception as exc:
            logger.debug("Replay failed for %s @ %s: %s", symbol, asof_ts, exc)
            continue

        rows.append(
            {
                "asof_ts_utc": asof_ts,
                "symbol": symbol,
                "asset_class": asset_class,
                "forward_return": (future / entry) - 1.0,
                **features,
            }
        )

    logger.info("%s/%s: generated %d replay rows", asset_class, symbol, len(rows))
    return rows


def build_training_frame(
    asset_classes: list[str],
    *,
    forward_bars: int = DEFAULT_FORWARD_BARS,
    stride: int = DEFAULT_STRIDE,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    with sqlite3.connect(db_path) as con:
        symbols = _load_symbol_rows(con, asset_classes)
        if not symbols:
            return pd.DataFrame()

        all_rows: list[dict[str, Any]] = []
        for row in symbols:
            asset_class = str(row["asset_class"])
            symbol = str(row["symbol"])
            benchmark_5m, _ = _benchmark_frames(con, asset_class)
            bars_5m = _load_bars(con, symbol, "vanguard_bars_5m")
            bars_1h = _load_bars(con, symbol, "vanguard_bars_1h")
            all_rows.extend(
                _replay_symbol_features(
                    symbol,
                    asset_class,
                    bars_5m,
                    bars_1h,
                    benchmark_5m,
                    forward_bars=forward_bars,
                    stride=stride,
                )
            )

    if not all_rows:
        return pd.DataFrame()
    frame = pd.DataFrame(all_rows).sort_values(["asof_ts_utc", "asset_class", "symbol"]).reset_index(drop=True)
    return frame


def _feature_columns(frame: pd.DataFrame) -> list[str]:
    return [
        col
        for col in frame.columns
        if col not in META_COLUMNS
        and pd.api.types.is_numeric_dtype(frame[col])
        and frame[col].notna().sum() >= len(frame) * 0.3
    ]


def _standardize_by_timestamp(frame: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    out = frame.copy()
    grouped = out.groupby("asof_ts_utc", sort=False)
    for col in feature_cols:
        values = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
        means = grouped[col].transform("mean")
        stds = grouped[col].transform("std").fillna(0.0)
        out[col] = np.where(stds > 1e-8, (values - means) / stds, 0.0)
    return out


def train_target(
    frame: pd.DataFrame,
    model_name: str,
    asset_classes: list[str],
) -> dict[str, Any]:
    import lightgbm as lgb

    result: dict[str, Any] = {
        "model": model_name,
        "asset_classes": asset_classes,
        "rows": int(len(frame)),
        "symbols": int(frame["symbol"].nunique()) if not frame.empty else 0,
        "avg_ic": None,
        "fold_ics": [],
        "trained": False,
    }

    if frame.empty or len(frame) < MIN_REPLAY_ROWS:
        logger.warning("[%s] insufficient replay rows (%d < %d), skipping", model_name, len(frame), MIN_REPLAY_ROWS)
        return result

    feature_cols = _feature_columns(frame)
    if not feature_cols:
        logger.warning("[%s] no usable numeric features, skipping", model_name)
        return result

    train_frame = _standardize_by_timestamp(frame, feature_cols).dropna(subset=["forward_return"]).copy()
    if len(train_frame) < MIN_REPLAY_ROWS:
        logger.warning("[%s] insufficient labeled rows after standardization (%d), skipping", model_name, len(train_frame))
        return result

    dates = np.array(sorted(train_frame["asof_ts_utc"].unique()))
    n_splits = min(5, max(2, len(dates) - 1))
    if len(dates) < 3:
        logger.warning("[%s] insufficient timestamps (%d), skipping", model_name, len(dates))
        return result

    logger.info(
        "[%s] training_rows=%d symbols=%d timestamps=%d features=%d",
        model_name,
        len(train_frame),
        train_frame["symbol"].nunique(),
        len(dates),
        len(feature_cols),
    )

    fold_ics: list[float] = []
    tscv = TimeSeriesSplit(n_splits=n_splits)
    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(dates), start=1):
        train_dates = set(dates[train_idx])
        test_dates = set(dates[test_idx])
        train = train_frame[train_frame["asof_ts_utc"].isin(train_dates)]
        test = train_frame[train_frame["asof_ts_utc"].isin(test_dates)]
        if len(train) < MIN_FOLD_ROWS or len(test) < MIN_FOLD_ROWS:
            logger.info(
                "[%s] fold %d skipped train=%d test=%d",
                model_name,
                fold_idx,
                len(train),
                len(test),
            )
            continue

        model = lgb.LGBMRegressor(
            n_estimators=500,
            learning_rate=0.05,
            num_leaves=31,
            feature_fraction=0.8,
            bagging_fraction=0.8,
            bagging_freq=5,
            min_child_samples=20,
            verbose=-1,
            random_state=42,
        )
        model.fit(train[feature_cols].to_numpy(dtype=np.float64), train["forward_return"].to_numpy(dtype=np.float64))
        pred = model.predict(test[feature_cols].to_numpy(dtype=np.float64))
        ic = float(spearmanr(pred, test["forward_return"].to_numpy(dtype=np.float64), nan_policy="omit").statistic)
        if np.isfinite(ic):
            fold_ics.append(ic)
        logger.info("[%s] fold %d IC=%+.4f train=%d test=%d", model_name, fold_idx, ic, len(train), len(test))

    if not fold_ics:
        logger.warning("[%s] no valid walk-forward folds, skipping", model_name)
        result["features"] = feature_cols
        return result

    final_model = lgb.LGBMRegressor(
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=31,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=5,
        min_child_samples=20,
        verbose=-1,
        random_state=42,
    )
    final_model.fit(
        train_frame[feature_cols].to_numpy(dtype=np.float64),
        train_frame["forward_return"].to_numpy(dtype=np.float64),
    )

    out_dir = MODELS_DIR / model_name
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, out_dir / f"{model_name}.pkl")

    importances = sorted(
        zip(feature_cols, final_model.feature_importances_),
        key=lambda item: item[1],
        reverse=True,
    )
    avg_ic = float(np.mean(fold_ics))
    meta = {
        "model": model_name,
        "features": feature_cols,
        "asset_classes": asset_classes,
        "rows": int(len(train_frame)),
        "symbols": int(train_frame["symbol"].nunique()),
        "avg_ic": round(avg_ic, 6),
        "fold_ics": [round(v, 6) for v in fold_ics],
        "top_features": [{"name": name, "importance": float(importance)} for name, importance in importances[:20]],
        "standardization": "per_timestamp_cross_sectional_zscore",
        "forward_bars": DEFAULT_FORWARD_BARS,
        "trained_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    logger.info("[%s] saved %s | avg_ic=%+.4f", model_name, out_dir, avg_ic)
    for name, importance in importances[:10]:
        logger.info("[%s] feature %s importance=%.0f", model_name, name, importance)

    result.update(
        {
            "rows": int(len(train_frame)),
            "symbols": int(train_frame["symbol"].nunique()),
            "avg_ic": avg_ic,
            "fold_ics": fold_ics,
            "features": feature_cols,
            "trained": True,
            "model_dir": str(out_dir),
        }
    )
    return result


def run(
    *,
    db_path: str = DB_PATH,
    forward_bars: int = DEFAULT_FORWARD_BARS,
    stride: int = DEFAULT_STRIDE,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for model_name, asset_classes in MODEL_TARGETS.items():
        logger.info("=== %s (%s) ===", model_name, ",".join(asset_classes))
        frame = build_training_frame(
            asset_classes,
            forward_bars=forward_bars,
            stride=stride,
            db_path=db_path,
        )
        results[model_name] = train_target(frame, model_name, asset_classes)
    print(json.dumps(results, indent=2, default=str))
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train non-equity LGBM regressors from V3 replay")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--forward-bars", type=int, default=DEFAULT_FORWARD_BARS)
    parser.add_argument("--stride", type=int, default=DEFAULT_STRIDE)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    run(db_path=args.db_path, forward_bars=args.forward_bars, stride=args.stride)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
