from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.selection.shortlisting import (
    mirror_selected_shortlist,
    persist_selection,
    select_shortlist,
)


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "vanguard_v5_selection.json"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _row(
    symbol: str,
    after_cost: float,
    *,
    pred_pips: float = 5.0,
    economics_state: str = "PASS",
    direction: str = "LONG",
) -> dict:
    return {
        "cycle_ts_utc": "2026-04-11T10:00:00Z",
        "profile_id": "gft_10k",
        "symbol": symbol,
        "asset_class": "forex",
        "direction": direction,
        "economics_state": economics_state,
        "v5_action_prelim": economics_state,
        "model_id": "lgbm_vp45_raw_return_6bar_v1",
        "predicted_return": 0.0005 if direction == "LONG" else -0.0005,
        "metrics": {
            "predicted_move_pips": pred_pips,
            "after_cost_pips": after_cost,
            "live_mid": 1.1,
            "pip_size": 0.0001,
            "model_rank": 1,
        },
        "reasons": [],
    }


def test_v52_computes_tiers_but_ranks_model_first() -> None:
    cfg = _config()
    cfg["defaults"]["memory"]["min_gap_seconds_by_asset_class"]["forex"] = 0
    rows = [
        _row("EURUSD", 3.0, pred_pips=5.0),
        _row("GBPUSD", 5.0, pred_pips=5.5),
        _row("USDJPY", 1.0, pred_pips=3.0),
    ]
    rows[0]["metrics"]["model_rank"] = 1
    rows[1]["metrics"]["model_rank"] = 2
    rows[2]["metrics"]["model_rank"] = 3
    history = [
        {
            **_row("EURUSD", 2.8, pred_pips=5.0),
            "cycle_ts_utc": "2026-04-11T09:58:00Z",
        },
        {
            **_row("EURUSD", 2.7, pred_pips=5.0),
            "cycle_ts_utc": "2026-04-11T09:56:00Z",
        },
        {
            **_row("GBPUSD", 4.7, pred_pips=5.5),
            "cycle_ts_utc": "2026-04-11T09:58:00Z",
        },
    ]

    selected = select_shortlist(rows, cfg, history_rows=history, selected_at="2026-04-11T10:00:01Z")
    chosen = [row for row in selected if row["selected"] == 1]

    assert [row["symbol"] for row in chosen] == ["EURUSD", "GBPUSD", "USDJPY"]
    assert [row["selection_rank"] for row in chosen] == [1, 2, 3]
    assert chosen[0]["metrics"]["economics_rank"] == 2
    assert chosen[0]["display_label"] == "TIER_1"
    assert "STREAK_3" in chosen[0]["selection_flags"]
    assert chosen[2]["display_label"] == "TIER_2"


def test_model_rank_beats_after_cost_after_economics_gate() -> None:
    rows = [
        _row("EURUSD", 2.0, pred_pips=4.0),
        _row("NZDUSD", 8.0, pred_pips=9.0),
    ]
    rows[0]["metrics"]["model_rank"] = 1
    rows[1]["metrics"]["model_rank"] = 10

    selected = select_shortlist(rows, _config(), selected_at="2026-04-11T10:00:01Z")
    chosen = [row for row in selected if row["selected"] == 1]

    assert [row["symbol"] for row in chosen] == ["EURUSD", "NZDUSD"]
    assert chosen[0]["metrics"]["economics_rank"] == 2
    assert chosen[1]["metrics"]["economics_rank"] == 1


def test_crypto_selection_uses_bps_metrics_but_model_rank_first() -> None:
    rows = [
        {
            **_row("BTCUSD", 0.0),
            "asset_class": "crypto",
            "predicted_return": 0.003,
            "metrics": {
                "score_unit": "bps",
                "predicted_move_bps": 30.0,
                "predicted_move_score": 30.0,
                "after_cost_bps": 12.0,
                "after_cost_score": 12.0,
                "live_mid": 70000.0,
                "model_rank": 1,
            },
        },
        {
            **_row("ETHUSD", 0.0),
            "asset_class": "crypto",
            "predicted_return": 0.002,
            "metrics": {
                "score_unit": "bps",
                "predicted_move_bps": 20.0,
                "predicted_move_score": 20.0,
                "after_cost_bps": 18.0,
                "after_cost_score": 18.0,
                "live_mid": 2200.0,
                "model_rank": 2,
            },
        },
    ]

    selected = select_shortlist(rows, _config(), selected_at="2026-04-11T10:00:01Z")
    chosen = [row for row in selected if row["selected"] == 1]

    assert [row["symbol"] for row in chosen] == ["BTCUSD", "ETHUSD"]
    assert chosen[0]["metrics"]["economics_rank"] == 2
    assert chosen[1]["metrics"]["economics_rank"] == 1


def test_fail_and_weak_are_not_selected() -> None:
    rows = [
        _row("EURUSD", 0.1, economics_state="WEAK", pred_pips=1.1),
        _row("GBPUSD", -1.0, economics_state="FAIL", pred_pips=1.0),
    ]

    selected = select_shortlist(rows, _config(), selected_at="2026-04-11T10:00:01Z")

    observed = sorted(
        (row["symbol"], row["selected"], row["selection_state"], row["display_label"])
        for row in selected
    )
    assert observed == [
        ("EURUSD", 0, "WATCHLIST", "WATCHLIST"),
        ("GBPUSD", 0, "REJECTED", "NO_TRADE"),
    ]


def test_memory_counts_direction_streak_flip_and_followthrough() -> None:
    cfg = _config()
    cfg["defaults"]["memory"]["min_gap_seconds_by_asset_class"]["forex"] = 0
    history = [
        {
            **_row("EURUSD", 2.5, pred_pips=4.0, direction="LONG"),
            "cycle_ts_utc": "2026-04-11T09:58:00Z",
            "metrics": {
                "predicted_move_pips": 4.0,
                "after_cost_pips": 2.5,
                "live_mid": 1.0995,
                "pip_size": 0.0001,
                "economics_rank": 1,
            },
        },
        {
            **_row("EURUSD", 2.5, pred_pips=4.0, direction="SHORT"),
            "cycle_ts_utc": "2026-04-11T09:56:00Z",
        },
    ]

    selected = select_shortlist(
        [_row("EURUSD", 3.0, pred_pips=5.0, direction="LONG")],
        cfg,
        history_rows=history,
        selected_at="2026-04-11T10:00:01Z",
    )
    row = selected[0]

    assert row["direction_streak"] == 2
    assert row["metrics"]["raw_direction_streak"] == 2
    assert row["flip_count_window"] == 1
    assert round(row["live_followthrough_pips"], 6) == 5.0
    assert row["live_followthrough_state"] == "ALIGNED"
    assert "FOLLOWTHROUGH_ALIGNED" not in row["selection_flags"]


def test_memory_resets_direction_streak_when_cycle_gap_is_too_large() -> None:
    cfg = _config()
    cfg["defaults"]["memory"]["min_gap_seconds_by_asset_class"]["crypto"] = 0
    history = [
        {
            **_row("BTCUSD", 12.0, direction="SHORT"),
            "asset_class": "crypto",
            "cycle_ts_utc": "2026-04-11T09:56:00Z",
            "metrics": {
                "score_unit": "bps",
                "predicted_move_bps": 30.0,
                "after_cost_bps": 12.0,
                "live_mid": 70000.0,
                "model_rank": 1,
            },
        }
    ]

    selected = select_shortlist(
        [
            {
                **_row("BTCUSD", 12.0, direction="SHORT"),
                "asset_class": "crypto",
                "cycle_ts_utc": "2026-04-11T10:00:00Z",
                "metrics": {
                    "score_unit": "bps",
                    "predicted_move_bps": 30.0,
                    "after_cost_bps": 12.0,
                    "live_mid": 70010.0,
                    "model_rank": 1,
                },
            }
        ],
        cfg,
        history_rows=history,
        selected_at="2026-04-11T10:00:01Z",
    )
    row = selected[0]

    assert row["selection_state"] == "WATCHLIST"
    assert row["direction_streak"] == 0
    assert row["metrics"]["raw_direction_streak"] == 2


def test_forex_selected_cadence_counts_consecutive_two_minute_cycles() -> None:
    cfg = _config()
    cfg["defaults"]["memory"]["min_gap_seconds_by_asset_class"]["forex"] = 60
    history = [
        {
            **_row("EURJPY", 3.0, pred_pips=5.0, direction="SHORT"),
            "cycle_ts_utc": "2026-04-11T09:57:40Z",
            "selection_state": "SELECTED",
        },
        {
            **_row("EURJPY", 3.0, pred_pips=5.0, direction="SHORT"),
            "cycle_ts_utc": "2026-04-11T09:55:20Z",
            "selection_state": "SELECTED",
        },
    ]

    selected = select_shortlist(
        [
            {
                **_row("EURJPY", 3.2, pred_pips=5.2, direction="SHORT"),
                "cycle_ts_utc": "2026-04-11T10:00:00Z",
            }
        ],
        cfg,
        history_rows=history,
        selected_at="2026-04-11T10:00:01Z",
    )
    row = selected[0]

    assert row["selection_state"] == "SELECTED"
    assert row["direction_streak"] == 3


def test_profile_caps_hold_overflow_rows() -> None:
    cfg = _config()
    cfg["defaults"]["max_selected_per_profile"] = 1
    rows = [
        _row("EURUSD", 4.0),
        _row("GBPUSD", 3.0),
    ]

    selected = select_shortlist(rows, cfg, selected_at="2026-04-11T10:00:01Z")

    assert selected[0]["selected"] == 1
    assert selected[1]["selected"] == 0
    assert selected[1]["selection_reason"] == "PROFILE_SELECTION_CAP"


def test_persist_selection_creates_audit_rows() -> None:
    rows = select_shortlist(
        [_row("EURUSD", 4.0)],
        _config(),
        selected_at="2026-04-11T10:00:01Z",
    )
    con = sqlite3.connect(":memory:")
    persist_selection(con, rows)

    row = con.execute(
        """
        SELECT selected, selection_state, route_tier, display_label,
               direction_streak, live_followthrough_state, selection_flags_json
        FROM vanguard_v5_selection
        WHERE symbol = 'EURUSD'
        """
    ).fetchone()

    assert row[:6] == (1, "SELECTED", "TIER_2", "TIER_2", 1, "UNKNOWN")
    flags = json.loads(row[6])
    assert "NEW_ENTRY" in flags
    assert "FIRST_SIGNAL" not in flags
    assert "MODEL_TOP_3" not in flags
    assert "STRONG_PREDICTION" not in flags


def test_mirror_selected_shortlist_writes_v6_compatible_tables() -> None:
    rows = select_shortlist(
        [_row("EURUSD", 4.0)],
        _config(),
        selected_at="2026-04-11T10:00:01Z",
    )
    con = sqlite3.connect(":memory:")
    written = mirror_selected_shortlist(con, rows)

    assert written == 1
    v2 = con.execute(
        "SELECT symbol, direction, rank FROM vanguard_shortlist_v2 WHERE symbol='EURUSD'"
    ).fetchone()
    legacy = con.execute(
        "SELECT symbol, strategy, consensus_count FROM vanguard_shortlist WHERE symbol='EURUSD'"
    ).fetchone()
    assert v2 == ("EURUSD", "LONG", 1)
    assert legacy == ("EURUSD", "v5_2_selection", 1)


def test_exact_streak_number_can_roll_high() -> None:
    cfg = _config()
    cfg["defaults"]["memory"]["min_gap_seconds_by_asset_class"]["forex"] = 0
    current_ts = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)
    history = []
    for idx in range(98):
        ts = current_ts - timedelta(seconds=(idx + 1) * 10)
        history.append(
            {
                **_row("EURUSD", 3.0, pred_pips=5.0, direction="LONG"),
                "cycle_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    selected = select_shortlist(
        [_row("EURUSD", 3.0, pred_pips=5.0, direction="LONG")],
        cfg,
        history_rows=history,
        selected_at="2026-04-11T10:00:01Z",
    )
    row = selected[0]

    assert row["direction_streak"] == 99
    assert "STREAK_99" in row["selection_flags"]
    assert "STREAK_4_PLUS" not in row["selection_flags"]


def test_fail_and_flat_rows_do_not_surface_direction_streak() -> None:
    cfg = _config()
    history = [
        {
            **_row("EURUSD", 3.0, pred_pips=5.0, direction="SHORT"),
            "cycle_ts_utc": "2026-04-11T09:55:00Z",
            "selection_state": "SELECTED",
            "selected": 1,
        },
        {
            **_row("NZDJPY", 0.0, pred_pips=0.0, direction="FLAT", economics_state="FAIL"),
            "cycle_ts_utc": "2026-04-11T09:55:00Z",
            "selection_state": "REJECTED",
            "selected": 0,
        },
    ]

    selected = select_shortlist(
        [
            _row("EURUSD", 3.0, pred_pips=5.0, direction="SHORT", economics_state="FAIL"),
            _row("NZDJPY", 0.0, pred_pips=0.0, direction="FLAT", economics_state="FAIL"),
        ],
        cfg,
        history_rows=history,
        selected_at="2026-04-11T10:00:01Z",
    )

    eurusd = next(row for row in selected if row["symbol"] == "EURUSD")
    nzdjpy = next(row for row in selected if row["symbol"] == "NZDJPY")
    assert eurusd["direction_streak"] == 0
    assert nzdjpy["direction_streak"] == 0
    assert not any(flag.startswith("STREAK_") for flag in eurusd["selection_flags"])
    assert not any(flag.startswith("STREAK_") for flag in nzdjpy["selection_flags"])
