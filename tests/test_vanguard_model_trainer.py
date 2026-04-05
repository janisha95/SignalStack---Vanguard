"""
tests/test_vanguard_model_trainer.py — Tests for V4B Model Trainer.

Covers:
  - Feature list completeness (35 features)
  - Temporal split
  - scale_pos_weight computation
  - evaluate_model metrics
  - feature_importance output
  - save_artifacts / load round-trip
  - model_loader.predict / predict_single
  - DB registry writes
"""
from __future__ import annotations

import json
import math
import pickle
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stages.vanguard_model_trainer import (
    FEATURES,
    TRAIN_FRAC,
    _make_lgbm_params,
    _scale_pos_weight,
    evaluate_model,
    feature_importance,
    prepare_features,
    save_artifacts,
    temporal_split,
    train_lgbm,
    train_model,
    validate,
)
from vanguard.helpers.db import VanguardDB
from vanguard.models.model_loader import (
    FEATURES as LOADER_FEATURES,
    clear_cache,
    load_lgbm_long,
    load_lgbm_short,
    predict,
    predict_single,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> tuple[VanguardDB, str]:
    db_path = str(tmp_path / "vanguard_universe.db")
    return VanguardDB(db_path), db_path


def _synthetic_training_df(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """Synthetic training DataFrame with all required columns."""
    rng = np.random.default_rng(seed)
    rows = []
    base_ts = "2026-01-13T14:30:00+00:00"
    for i in range(n):
        # Advance timestamp by 5 minutes per bar
        mins = i * 5
        h = 14 + (30 + mins) // 60
        m = (30 + mins) % 60
        ts = f"2026-01-{13 + i // 78:02d}T{h:02d}:{m:02d}:00+00:00"
        row: dict = {
            "asof_ts_utc":  ts,
            "symbol":       "TEST",
            "asset_class":  "equity",
            "path":         "ttp_equity",
            "entry_price":  100.0 + rng.normal(0, 1),
            "label_long":   int(rng.random() < 0.25),
            "label_short":  int(rng.random() < 0.28),
            "forward_return": rng.normal(0, 0.003),
            "max_favorable_excursion": abs(rng.normal(0, 0.003)),
            "max_adverse_excursion":   abs(rng.normal(0, 0.002)),
            "exit_bar":      int(rng.integers(1, 7)),
            "exit_type_long":  "TP",
            "exit_type_short": "TIMEOUT",
            "truncated": 0,
            "warm_up":   0,
            "horizon_bars": 6,
            "tp_pct": 0.003,
            "sl_pct": 0.0015,
        }
        for fname in FEATURES:
            row[fname] = rng.normal(0, 1)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("asof_ts_utc").reset_index(drop=True)


def _insert_training_data(db: VanguardDB, df: pd.DataFrame) -> None:
    """Insert synthetic training data into vanguard_training_data."""
    rows = df.to_dict("records")
    db.upsert_training_data(rows)


# ===========================================================================
# TestFeatureContract
# ===========================================================================

class TestFeatureContract:
    def test_35_features(self):
        assert len(FEATURES) == 35

    def test_no_duplicates(self):
        assert len(FEATURES) == len(set(FEATURES))

    def test_loader_features_match_trainer(self):
        assert FEATURES == LOADER_FEATURES

    def test_known_features_present(self):
        for f in ["session_phase", "momentum_3bar", "relative_volume",
                  "htf_trend_direction", "nan_ratio", "bars_available"]:
            assert f in FEATURES


# ===========================================================================
# TestTemporalSplit
# ===========================================================================

class TestTemporalSplit:
    def test_split_ratio(self):
        df = _synthetic_training_df(200)
        train, test = temporal_split(df)
        assert len(train) + len(test) == 200
        assert abs(len(train) / 200 - TRAIN_FRAC) < 0.02

    def test_train_before_test(self):
        df = _synthetic_training_df(200)
        train, test = temporal_split(df)
        assert train["asof_ts_utc"].max() <= test["asof_ts_utc"].min()

    def test_no_row_loss(self):
        df = _synthetic_training_df(100)
        train, test = temporal_split(df)
        assert len(train) + len(test) == 100


# ===========================================================================
# TestScalePosWeight
# ===========================================================================

class TestScalePosWeight:
    def test_imbalanced(self):
        y = np.array([0] * 80 + [1] * 20)
        spw = _scale_pos_weight(y)
        assert abs(spw - 4.0) < 0.01

    def test_balanced(self):
        y = np.array([0, 1] * 50)
        spw = _scale_pos_weight(y)
        assert abs(spw - 1.0) < 0.01

    def test_all_negative_returns_one(self):
        y = np.zeros(50, dtype=int)
        spw = _scale_pos_weight(y)
        assert spw == 1.0


# ===========================================================================
# TestPrepareFeatures
# ===========================================================================

class TestPrepareFeatures:
    def test_shape(self):
        df = _synthetic_training_df(50)
        X = prepare_features(df)
        assert X.shape == (50, 35)

    def test_dtype_float32(self):
        df = _synthetic_training_df(50)
        X = prepare_features(df)
        assert X.dtype == np.float32

    def test_nan_passthrough(self):
        df = _synthetic_training_df(10)
        df.loc[0, "momentum_3bar"] = float("nan")
        X = prepare_features(df)
        assert np.isnan(X[0, FEATURES.index("momentum_3bar")])


# ===========================================================================
# TestLgbmParams
# ===========================================================================

class TestLgbmParams:
    def test_contains_required_keys(self):
        p = _make_lgbm_params(4.0)
        for k in ["objective", "metric", "num_leaves", "learning_rate",
                  "scale_pos_weight", "seed"]:
            assert k in p

    def test_scale_pos_weight_set(self):
        p = _make_lgbm_params(3.5)
        assert p["scale_pos_weight"] == 3.5

    def test_objective_binary(self):
        p = _make_lgbm_params(1.0)
        assert p["objective"] == "binary"


# ===========================================================================
# TestTrainAndEvaluate
# ===========================================================================

class TestTrainAndEvaluate:
    """Integration: train a small model on synthetic data."""

    def _get_split(self, n=300):
        df = _synthetic_training_df(n)
        train, test = temporal_split(df)
        return train, test

    def test_train_lgbm_long_returns_model_and_metrics(self):
        train, test = self._get_split()
        model, metrics = train_lgbm("label_long", train, test)
        assert model is not None
        assert "auc" in metrics
        assert "accuracy" in metrics
        assert "ic_vs_return" in metrics

    def test_train_lgbm_short_runs(self):
        train, test = self._get_split()
        model, metrics = train_lgbm("label_short", train, test)
        assert metrics["train_rows"] == len(train)
        assert metrics["test_rows"] == len(test)

    def test_auc_between_0_and_1(self):
        train, test = self._get_split()
        model, metrics = train_lgbm("label_long", train, test)
        assert 0.0 <= metrics["auc"] <= 1.0

    def test_accuracy_between_0_and_1(self):
        train, test = self._get_split()
        model, metrics = train_lgbm("label_long", train, test)
        assert 0.0 <= metrics["accuracy"] <= 1.0

    def test_label_drift_reported(self):
        train, test = self._get_split()
        model, metrics = train_lgbm("label_long", train, test)
        assert "label_rate_train" in metrics
        assert "label_rate_test" in metrics
        assert 0.0 <= metrics["label_rate_train"] <= 1.0


# ===========================================================================
# TestFeatureImportance
# ===========================================================================

class TestFeatureImportance:
    def _train_small(self):
        df = _synthetic_training_df(300)
        train, test = temporal_split(df)
        model, _ = train_lgbm("label_long", train, test)
        return model

    def test_returns_10_features(self):
        model = self._train_small()
        fi = feature_importance(model, top_n=10)
        assert len(fi) == 10

    def test_each_entry_is_name_float_tuple(self):
        model = self._train_small()
        fi = feature_importance(model)
        for name, imp in fi:
            assert isinstance(name, str)
            assert isinstance(imp, (int, float))

    def test_sorted_descending(self):
        model = self._train_small()
        fi = feature_importance(model)
        imps = [imp for _, imp in fi]
        assert imps == sorted(imps, reverse=True)

    def test_all_names_in_feature_list(self):
        model = self._train_small()
        fi = feature_importance(model, top_n=35)
        for name, _ in fi:
            assert name in FEATURES


# ===========================================================================
# TestSaveArtifacts
# ===========================================================================

class TestSaveArtifacts:
    def _train_small(self, label="label_long"):
        df = _synthetic_training_df(300)
        train, test = temporal_split(df)
        return train_lgbm(label, train, test)

    def test_pkl_created(self, tmp_path):
        model, metrics = self._train_small()
        save_artifacts(model, metrics, "label_long", tmp_path)
        assert (tmp_path / "lgbm_long.pkl").exists()

    def test_config_json_created(self, tmp_path):
        model, metrics = self._train_small()
        save_artifacts(model, metrics, "label_long", tmp_path)
        assert (tmp_path / "lgbm_long_config.json").exists()

    def test_config_contains_features(self, tmp_path):
        model, metrics = self._train_small()
        save_artifacts(model, metrics, "label_long", tmp_path)
        with open(tmp_path / "lgbm_long_config.json") as f:
            cfg = json.load(f)
        assert cfg["features"] == FEATURES
        assert cfg["feature_count"] == 35

    def test_pkl_round_trip(self, tmp_path):
        model, metrics = self._train_small()
        save_artifacts(model, metrics, "label_long", tmp_path)
        with open(tmp_path / "lgbm_long.pkl", "rb") as f:
            loaded = pickle.load(f)
        df = _synthetic_training_df(50)
        X = prepare_features(df)
        orig_proba  = model.predict_proba(X)[:, 1]
        load_proba = loaded.predict_proba(X)[:, 1]
        np.testing.assert_array_almost_equal(orig_proba, load_proba)

    def test_short_model_saved_as_lgbm_short(self, tmp_path):
        model, metrics = self._train_small("label_short")
        save_artifacts(model, metrics, "label_short", tmp_path)
        assert (tmp_path / "lgbm_short.pkl").exists()
        assert (tmp_path / "lgbm_short_config.json").exists()


# ===========================================================================
# TestModelLoader
# ===========================================================================

class TestModelLoader:
    def _train_and_save(self, tmp_path: Path) -> Path:
        clear_cache()
        df = _synthetic_training_df(300)
        train, test = temporal_split(df)
        for label in ["label_long", "label_short"]:
            model, metrics = train_lgbm(label, train, test)
            save_artifacts(model, metrics, label, tmp_path)
        return tmp_path

    def test_load_lgbm_long(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        model = load_lgbm_long(tmp_path)
        assert model is not None

    def test_load_lgbm_short(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        model = load_lgbm_short(tmp_path)
        assert model is not None

    def test_load_missing_model_raises(self, tmp_path):
        clear_cache()
        with pytest.raises(FileNotFoundError):
            load_lgbm_long(tmp_path)

    def test_predict_adds_prob_columns(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        df = _synthetic_training_df(20)
        result = predict(df, model_dir=tmp_path)
        assert "lgbm_long_prob" in result.columns
        assert "lgbm_short_prob" in result.columns

    def test_predict_probs_in_0_1(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        df = _synthetic_training_df(20)
        result = predict(df, model_dir=tmp_path)
        assert result["lgbm_long_prob"].between(0, 1).all()
        assert result["lgbm_short_prob"].between(0, 1).all()

    def test_predict_empty_df(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        df = _synthetic_training_df(5).iloc[:0]  # empty
        result = predict(df, model_dir=tmp_path)
        assert len(result) == 0
        assert "lgbm_long_prob" in result.columns

    def test_predict_missing_feature_raises(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        df = _synthetic_training_df(5).drop(columns=["momentum_3bar"])
        with pytest.raises(ValueError, match="missing"):
            predict(df, model_dir=tmp_path)

    def test_predict_single_returns_both_probs(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        feats = {f: 0.5 for f in FEATURES}
        result = predict_single(feats, model_dir=tmp_path)
        assert "lgbm_long_prob" in result
        assert "lgbm_short_prob" in result
        assert 0.0 <= result["lgbm_long_prob"] <= 1.0
        assert 0.0 <= result["lgbm_short_prob"] <= 1.0

    def test_cache_returns_same_object(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        m1 = load_lgbm_long(tmp_path)
        m2 = load_lgbm_long(tmp_path)
        assert m1 is m2  # same cached instance

    def test_clear_cache_forces_reload(self, tmp_path):
        self._train_and_save(tmp_path)
        clear_cache()
        m1 = load_lgbm_long(tmp_path)
        clear_cache()
        m2 = load_lgbm_long(tmp_path)
        assert m1 is not m2  # reloaded after cache clear


# ===========================================================================
# TestDbRegistry
# ===========================================================================

class TestDbRegistry:
    def test_upsert_model_registry(self, tmp_path):
        db, _ = _make_db(tmp_path)
        row = {
            "model_id": "lgbm_long_20260329",
            "model_family": "lgbm_long", "track": "A",
            "target_label": "label_long",
            "feature_count": 35, "train_rows": 26692, "test_rows": 11440,
            "walk_forward_windows": 1,
            "mean_ic_long": 0.041, "mean_ic_short": None,
            "auc_long": 0.62, "auc_short": None,
            "long_wr_at_55": 0.38, "short_wr_at_55": None,
            "artifact_path": "/models/lgbm_long.pkl",
            "created_at_utc": "2026-03-29T21:00:00+00:00",
            "status": "trained", "notes": None,
        }
        db.upsert_model_registry(row)
        # Verify row exists
        conn = db.connect()
        try:
            r = conn.execute(
                "SELECT model_id, auc_long FROM vanguard_model_registry WHERE model_id = ?",
                ("lgbm_long_20260329",)
            ).fetchone()
        finally:
            conn.close()
        assert r is not None
        assert r[0] == "lgbm_long_20260329"
        assert abs(r[1] - 0.62) < 0.001

    def test_upsert_walkforward_results(self, tmp_path):
        db, _ = _make_db(tmp_path)
        rows = [{"model_id": "m1", "window_id": 1,
                 "train_start": "2026-01-01", "train_end": "2026-02-28",
                 "test_start": "2026-03-01", "test_end": "2026-03-29",
                 "test_rows": 5000, "ic_long": 0.04, "ic_short": None,
                 "auc": 0.61, "accuracy": 0.78, "wr_at_55": 0.38}]
        n = db.upsert_walkforward_results(rows)
        assert n == 1

    def test_train_model_writes_registry(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        df = _synthetic_training_df(300)
        _insert_training_data(db, df)
        model_dir = tmp_path / "models"
        train_model("long", db_path=db_path, model_dir=model_dir)
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT model_id FROM vanguard_model_registry"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) >= 1
