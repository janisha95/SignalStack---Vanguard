from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.selection.tradeability import (
    evaluate_cycle_tradeability,
    evaluate_tradeability,
    persist_tradeability,
)


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "vanguard_v5_tradeability.json"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _candidate(**overrides) -> dict:
    base = {
        "cycle_ts_utc": "2026-04-11T10:00:00Z",
        "profile_id": "gft_10k",
        "symbol": "EURUSD",
        "asset_class": "forex",
        "direction": "LONG",
        "predicted_return": 0.0005,
        "rank": 3,
        "model_id": "lgbm_vp45_raw_return_6bar_v1",
    }
    base.update(overrides)
    return base


def _pair_state(**overrides) -> dict:
    base = {
        "cycle_ts_utc": "2026-04-11T09:59:58Z",
        "profile_id": "gft_10k",
        "symbol": "EURUSD",
        "asset_class": "forex",
        "session_bucket": "ny",
        "liquidity_bucket": "high",
        "spread_pips": 0.8,
        "spread_ratio_vs_baseline": 1.1,
        "pair_correlation_bucket": "eur",
        "same_currency_open_count": 0,
        "correlated_open_count": 0,
        "live_mid": 1.10000,
        "quote_ts_utc": "2026-04-11T09:59:58Z",
        "context_ts_utc": "2026-04-11T09:59:59Z",
        "source": "mt5_dwx",
        "source_status": "OK",
        "config_version": "forex_pair_state_v1",
    }
    base.update(overrides)
    return base


def test_pass_when_prediction_survives_pair_economics() -> None:
    verdict = evaluate_tradeability(
        _candidate(),
        _pair_state(),
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "PASS"
    assert verdict["v5_action_prelim"] == "PASS"
    assert verdict["metrics"]["predicted_move_pips"] == 5.5
    assert round(verdict["metrics"]["after_cost_pips"], 6) == 4.5
    assert verdict["reasons"] == []


def test_weak_when_after_cost_is_positive_but_below_threshold() -> None:
    verdict = evaluate_tradeability(
        _candidate(predicted_return=0.0001),
        _pair_state(spread_pips=0.8),
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "WEAK"
    assert round(verdict["metrics"]["after_cost_pips"], 6) == 0.1
    assert "AFTER_COST_NEGATIVE" not in verdict["reasons"]


def test_fail_on_wide_spread_ratio() -> None:
    verdict = evaluate_tradeability(
        _candidate(),
        _pair_state(spread_pips=4.0, spread_ratio_vs_baseline=3.5),
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "FAIL"
    assert "SPREAD_RATIO_TOO_WIDE" in verdict["reasons"]


def test_pass_on_wide_absolute_spread_when_ratio_is_normal() -> None:
    verdict = evaluate_tradeability(
        _candidate(),
        _pair_state(spread_pips=4.0, spread_ratio_vs_baseline=1.2),
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "PASS"
    assert "SPREAD_TOO_WIDE" not in verdict["reasons"]
    assert "SPREAD_RATIO_TOO_WIDE" not in verdict["reasons"]


def test_fail_on_absolute_spread_cap_from_risky_spread_sanity() -> None:
    verdict = evaluate_tradeability(
        _candidate(profile_id="ftmo_demo_100k", symbol="CHFJPY"),
        _pair_state(
            profile_id="ftmo_demo_100k",
            symbol="CHFJPY",
            spread_pips=5.1,
            spread_ratio_vs_baseline=1.2,
            live_mid=203.2500,
        ),
        _config(),
        evaluated_at=datetime(2026, 4, 11, 22, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "FAIL"
    assert "SPREAD_TOO_WIDE" in verdict["reasons"]


def test_fail_on_direction_mismatch() -> None:
    verdict = evaluate_tradeability(
        _candidate(direction="LONG", predicted_return=-0.0005),
        _pair_state(),
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "FAIL"
    assert "PREDICTION_DIRECTION_MISMATCH" in verdict["reasons"]


def test_fail_when_pair_state_missing() -> None:
    verdict = evaluate_tradeability(
        _candidate(),
        None,
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "FAIL"
    assert "PAIR_STATE_MISSING" in verdict["reasons"]
    assert "MID_MISSING" in verdict["reasons"]


def test_crypto_pass_uses_bps_economics() -> None:
    cfg = _config()
    verdict = evaluate_tradeability(
        _candidate(
            symbol="BTCUSD",
            asset_class="crypto",
            direction="LONG",
            predicted_return=0.003,
            model_id="lgbm_crypto_regressor_v1",
        ),
        {
            "cycle_ts_utc": "2026-04-11T09:59:58Z",
            "profile_id": "gft_10k",
            "symbol": "BTCUSD",
            "asset_class": "crypto",
            "session_bucket": "24x7",
            "liquidity_bucket": "high",
            "spread_bps": 10.0,
            "spread_ratio_vs_baseline": 1.0,
            "same_symbol_open_count": 0,
            "correlated_open_count": 0,
            "live_mid": 70000.0,
            "quote_ts_utc": "2026-04-11T09:59:58Z",
            "context_ts_utc": "2026-04-11T09:59:59Z",
            "source": "twelvedata",
            "source_status": "OK",
            "trade_allowed": 1,
            "config_version": "crypto_context_v1",
        },
        cfg,
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "PASS"
    assert verdict["metrics"]["score_unit"] == "bps"
    assert verdict["metrics"]["predicted_move_bps"] == 30.0
    assert verdict["metrics"]["cost_bps"] == 15.0
    assert verdict["metrics"]["after_cost_bps"] == 15.0


def test_crypto_fail_when_after_cost_negative() -> None:
    verdict = evaluate_tradeability(
        _candidate(symbol="BTCUSD", asset_class="crypto", direction="LONG", predicted_return=0.001),
        {
            "cycle_ts_utc": "2026-04-11T09:59:58Z",
            "profile_id": "gft_10k",
            "symbol": "BTCUSD",
            "asset_class": "crypto",
            "spread_bps": 20.0,
            "live_mid": 70000.0,
            "source": "twelvedata",
            "source_status": "OK",
            "trade_allowed": 1,
        },
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert verdict["economics_state"] == "FAIL"
    assert "AFTER_COST_NEGATIVE" in verdict["reasons"]


def test_persist_tradeability_creates_economics_row_with_compat_columns() -> None:
    verdict = evaluate_tradeability(
        _candidate(),
        _pair_state(),
        _config(),
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )
    con = sqlite3.connect(":memory:")
    persist_tradeability(con, verdict)

    row = con.execute(
        """
        SELECT economics_state, v5_action_prelim, v5_action, route_tier,
               display_label, predicted_move_pips, after_cost_pips
        FROM vanguard_v5_tradeability
        WHERE profile_id = 'gft_10k' AND symbol = 'EURUSD'
        """
    ).fetchone()

    assert row == ("PASS", "PASS", "PASS", "UNASSIGNED", "PASS", 5.5, 4.5)


def test_evaluate_cycle_tradeability_reads_predictions_and_pair_state() -> None:
    con = sqlite3.connect(":memory:")
    con.execute(
        """
        CREATE TABLE vanguard_predictions (
            cycle_ts_utc TEXT,
            symbol TEXT,
            asset_class TEXT,
            predicted_return REAL,
            direction TEXT,
            model_id TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE vanguard_forex_pair_state (
            cycle_ts_utc TEXT,
            profile_id TEXT,
            symbol TEXT,
            asset_class TEXT,
            session_bucket TEXT,
            liquidity_bucket TEXT,
            spread_pips REAL,
            spread_ratio_vs_baseline REAL,
            pair_correlation_bucket TEXT,
            same_currency_open_count INTEGER,
            correlated_open_count INTEGER,
            live_mid REAL,
            quote_ts_utc TEXT,
            context_ts_utc TEXT,
            source TEXT,
            source_status TEXT,
            source_error TEXT,
            config_version TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_predictions
        VALUES ('2026-04-11T10:00:00Z', 'EURUSD', 'forex', 0.0005, 'LONG',
                'lgbm_vp45_raw_return_6bar_v1')
        """
    )
    con.execute(
        """
        INSERT INTO vanguard_forex_pair_state
        VALUES ('2026-04-11T09:59:58Z', 'gft_10k', 'EURUSD', 'forex',
                'ny', 'high', 0.8, 1.1, 'eur', 0, 0, 1.1,
                '2026-04-11T09:59:58Z', '2026-04-11T09:59:59Z',
                'mt5_dwx', 'OK', NULL, 'forex_pair_state_v1')
        """
    )
    con.commit()

    rows = evaluate_cycle_tradeability(
        con,
        _config(),
        cycle_ts_utc="2026-04-11T10:00:00Z",
        profile_ids=["gft_10k"],
        persist=True,
        evaluated_at=datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert rows[0]["economics_state"] == "PASS"
    persisted = con.execute(
        "SELECT economics_state FROM vanguard_v5_tradeability WHERE symbol='EURUSD'"
    ).fetchone()
    assert tuple(persisted) == ("PASS",)
