from __future__ import annotations

from vanguard.selection.shortlisting import _sort_key, _tier_matches


def _cfg(use_streak_tier: bool, use_streak_sort: bool) -> dict:
    return {
        "selection_behavior": {
            "use_direction_streak_in_tier_matching": use_streak_tier,
            "use_direction_streak_in_sorting": use_streak_sort,
        },
        "route_tier_priority": {"TIER_1": 1},
    }


def test_tier_matching_ignores_direction_streak_when_flag_disabled() -> None:
    row = {"metrics": {"predicted_move_pips": 20, "after_cost_pips": 10, "model_rank": 1}}
    memory = {
        "direction_streak": 1,
        "strong_prediction_streak": 0,
        "top_rank_streak": 0,
        "flip_count_window": 0,
        "live_followthrough_state": "ALIGNED",
    }
    spec = {
        "min_predicted_move": 1,
        "min_after_cost": 1,
        "max_rank": 5,
        "min_direction_streak": 3,
        "allowed_followthrough_states": ["ALIGNED"],
    }

    assert _tier_matches(row, memory, spec, _cfg(False, False)) is True
    assert _tier_matches(row, memory, spec, _cfg(True, False)) is False


def test_sort_key_drops_direction_streak_when_flag_disabled() -> None:
    row = {
        "profile_id": "gft_5k",
        "route_tier": "TIER_1",
        "direction_streak": 4,
        "symbol": "EURUSD",
        "metrics": {"model_rank": 1, "after_cost_pips": 12},
    }

    sort_without_streak = _sort_key(row, _cfg(False, False))
    sort_with_streak = _sort_key(row, _cfg(False, True))

    assert sort_without_streak[3] == 0
    assert sort_with_streak[3] == -4
