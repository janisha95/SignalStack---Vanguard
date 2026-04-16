from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.selection.model_v2_outcomes import (
    ensure_model_v2_results_schema,
    persist_model_v2_results,
)


def test_persist_model_v2_results_writes_new_table_only() -> None:
    con = sqlite3.connect(":memory:")
    con.execute(
        """
        CREATE TABLE execution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            status TEXT
        )
        """
    )
    row = {
        "cycle_ts_utc": "2026-04-11T10:00:00Z",
        "profile_id": "gft_10k",
        "symbol": "EURUSD",
        "asset_class": "forex",
        "direction": "LONG",
        "selected": 1,
        "selection_rank": 1,
        "selection_state": "SELECTED",
        "route_tier": "TIER_2",
        "display_label": "TIER_2",
        "direction_streak": 1,
        "top_rank_streak": 1,
        "strong_prediction_streak": 1,
        "same_bucket_streak": 1,
        "flip_count_window": 0,
        "live_followthrough_pips": None,
        "live_followthrough_state": "UNKNOWN",
        "selection_flags": ["ECONOMICS_PASS", "TIER_2", "NEW_ENTRY"],
        "config_version": "v5_selection_v1",
        "tradeability_ref": {
            "model_id": "lgbm_vp45_raw_return_6bar_v1",
            "predicted_return": 0.0004,
            "economics_state": "PASS",
            "reasons": [],
            "pair_state_ref": {
                "quote_ts_utc": "2026-04-11T10:00:00Z",
                "context_ts_utc": "2026-04-11T10:00:00Z",
                "source": "mt5_dwx",
            },
        },
        "metrics": {
            "model_rank": 1,
            "feature_count": 45,
            "live_mid": 1.1,
            "spread_pips": 0.5,
            "spread_ratio_vs_baseline": 1.2,
            "predicted_move_pips": 4.4,
            "cost_pips": 0.7,
            "after_cost_pips": 3.7,
        },
    }

    ensure_model_v2_results_schema(con)
    written = persist_model_v2_results(con, [row], horizon_minutes=120)

    assert written == 1
    result = con.execute(
        """
        SELECT symbol, model_id, selected, selection_rank, route_tier,
               predicted_move_pips, after_cost_pips, outcome_state, target_ts_utc
        FROM vanguard_model_v2_lgbm_results
        """
    ).fetchone()
    assert result == (
        "EURUSD",
        "lgbm_vp45_raw_return_6bar_v1",
        1,
        1,
        "TIER_2",
        4.4,
        3.7,
        "UNRESOLVED",
        "2026-04-11T12:00:00Z",
    )
    assert con.execute("SELECT COUNT(*) FROM execution_log").fetchone()[0] == 0
