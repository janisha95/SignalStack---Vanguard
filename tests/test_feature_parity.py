"""
Test that every MODEL_SPECS entry matches the loaded model feature count.
"""
from __future__ import annotations

import sys
from pathlib import Path

import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages.vanguard_scorer import MODEL_SPECS


def _model_feature_count(model) -> int:
    if isinstance(model, dict):
        if model.get("features"):
            return len(model["features"])
        base_models = model.get("base_models") or {}
        if base_models:
            first = next(iter(base_models.values()))
            return int(getattr(first, "n_features_in_", 0))
        raise AssertionError("Unsupported dict model artifact without features/base_models")
    count = getattr(model, "n_features_in_", None)
    if count is None:
        raise AssertionError(f"Model artifact {type(model).__name__} has no n_features_in_")
    return int(count)


def test_feature_count_matches_model():
    failures: list[str] = []
    for asset_class, spec in MODEL_SPECS.items():
        model_dir = Path(spec["model_dir"])
        expected = len(spec["features"])
        for filename, _weight in spec["models"]:
            model_path = model_dir / filename
            if not model_path.exists():
                failures.append(f"{asset_class}: model file does not exist at {model_path}")
                continue
            model = joblib.load(model_path)
            actual = _model_feature_count(model)
            if expected != actual:
                failures.append(
                    f"{asset_class}: spec has {expected} features, model {filename} expects {actual}"
                )
    if failures:
        raise AssertionError("Feature parity failures:\n" + "\n".join(failures))


def test_no_duplicate_features():
    for asset_class, spec in MODEL_SPECS.items():
        features = spec["features"]
        assert len(features) == len(set(features)), (
            f"{asset_class} has duplicate features in spec"
        )


if __name__ == "__main__":
    test_feature_count_matches_model()
    test_no_duplicate_features()
    print("All feature parity tests passed.")
