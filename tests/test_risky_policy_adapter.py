from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages import vanguard_risk_filters as risk_filters
from vanguard.risk.risky_policy_adapter import compile_effective_risk_policy
import vanguard.risk.risky_sizing as risky_sizing
from vanguard.risk.risky_sizing import evaluate_risky_sizing
from vanguard.risk.sizing import size_crypto_spread_aware


def _runtime_config(*profiles: dict) -> dict:
    return {
        "config_version": "test-runtime",
        "profiles": list(profiles),
        "v6_health": {
            "enabled": True,
            "default_context_source_id": "gft_mt5_local",
            "mode_requirements": {
                "auto": {
                    "required_context": ["quote", "account", "positions", "orders", "symbol_specs", "daemon"],
                    "ready_state_allows_execution": True,
                }
            },
            "thresholds": {},
            "crypto_validation": {"enabled": False, "block_execution": False},
        },
    }


def _risk_rules() -> dict:
    common_sizing = {
        "forex": {
            "method": "risk_per_stop_pips",
            "risk_per_trade_pct": 0.005,
            "min_sl_pips": 20,
            "min_tp_pips": 40,
            "use_model_horizon_exits": True,
            "model_horizon_bars": 12,
            "target_rr_multiple": 2.0,
            "min_automation_rr_multiple": 1.0,
            "mid_opportunity_rr_multiple": 1.5,
            "floor_opportunity_rr_multiple": 1.0,
            "tp_capture_fraction": 0.9,
            "min_stop_floor_pips": 3.0,
            "min_spread_multiple": 3.0,
            "min_atr_stop_multiple": 0.75,
            "contract_size": 100000.0,
            "pip_value_usd_per_standard_lot": {"default": 10.0},
        },
        "crypto": {
            "method": "risk_per_stop_spread_aware",
            "risk_per_trade_pct": 0.005,
            "max_notional_pct": 0.05,
            "min_qty": 0.000001,
            "min_sl_pct": 0.008,
            "min_tp_pct": 0.024,
            "use_model_horizon_exits": True,
            "model_horizon_bars": 6,
            "target_rr_multiple": 2.0,
            "min_automation_rr_multiple": 1.0,
            "mid_opportunity_rr_multiple": 1.5,
            "floor_opportunity_rr_multiple": 1.0,
            "tp_capture_fraction": 0.9,
            "min_stop_floor_bps": 8.0,
            "min_spread_multiple": 1.5,
            "min_atr_stop_multiple": 0.2,
            "sl_atr_multiple": 1.5,
            "tp_atr_multiple": 3.0,
            "contract_sizes": {"default": 1},
        },
    }
    return {
        "config_version": "test-risky",
        "global": {"default_mode": "check_only"},
        "per_account_profiles": {
            "gft_10k": {
                "side_controls": {"allow_long": True, "allow_short": True},
                "position_limits": {
                    "max_open_positions": 2,
                    "block_duplicate_symbols": True,
                    "block_same_direction_reentry": True,
                    "reentry_block_statuses": ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"],
                    "reentry_cooldown_minutes": 120,
                    "reentry_key": "profile_symbol_side",
                    "max_holding_minutes": 240,
                    "auto_close_after_max_holding": True,
                },
                "drawdown_rules": {
                    "daily_loss_pause_pct": 0.03,
                    "trailing_drawdown_pause_pct": 0.05,
                    "pause_until_next_day": True,
                },
                "reject_rules": {
                    "enforce_universe": True,
                    "enforce_session": True,
                    "enforce_position_limit": True,
                    "enforce_heat_limit": False,
                    "enforce_duplicate_block": True,
                    "enforce_drawdown_pause": True,
                    "enforce_blackout": True,
                    "enforce_streak_minimum": True,
                    "min_streak_required": 4,
                },
                "sizing_by_asset_class": common_sizing,
            },
            "gft_5k": {
                "side_controls": {"allow_long": True, "allow_short": True},
                "position_limits": {
                    "max_open_positions": 3,
                    "block_duplicate_symbols": True,
                    "block_same_direction_reentry": True,
                    "reentry_block_statuses": ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"],
                    "reentry_cooldown_minutes": 120,
                    "reentry_key": "profile_symbol_side",
                    "max_holding_minutes": 240,
                    "auto_close_after_max_holding": True,
                },
                "drawdown_rules": {
                    "daily_loss_pause_pct": 0.03,
                    "trailing_drawdown_pause_pct": 0.05,
                    "pause_until_next_day": True,
                },
                "reject_rules": {
                    "enforce_universe": True,
                    "enforce_session": True,
                    "enforce_position_limit": True,
                    "enforce_heat_limit": False,
                    "enforce_duplicate_block": True,
                    "enforce_drawdown_pause": True,
                    "enforce_blackout": True,
                    "enforce_streak_minimum": False,
                    "min_streak_required": 4,
                },
                "sizing_by_asset_class": common_sizing,
            },
        },
        "universal_rules": {
            "spread_sanity": {"warn_ratio": 2.0, "block_ratio": 3.0},
        },
        "per_symbol_class_session_rules": {},
        "closing_buffer": {},
    }


def test_gft_10k_policy_resolves_from_risk_rules_not_runtime_templates() -> None:
    runtime = _runtime_config({"id": "gft_10k", "risk_profile_id": "gft_10k", "policy_id": "gft_10k_v1"})
    runtime["policy_templates"] = {"gft_10k_v1": {"side_controls": {"allow_long": True, "allow_short": True}}}
    rules = _risk_rules()
    rules["per_account_profiles"]["gft_10k"]["side_controls"] = {"allow_long": False, "allow_short": True}
    policy = compile_effective_risk_policy(runtime["profiles"][0], runtime, rules)
    assert policy["side_controls"] == {"allow_long": False, "allow_short": True}


def test_gft_5k_policy_resolves_from_risk_rules_not_runtime_templates() -> None:
    runtime = _runtime_config({"id": "gft_5k", "risk_profile_id": "gft_5k", "policy_id": "gft_standard_v1"})
    runtime["policy_templates"] = {"gft_standard_v1": {"side_controls": {"allow_long": False, "allow_short": True}}}
    rules = _risk_rules()
    rules["per_account_profiles"]["gft_5k"]["side_controls"] = {"allow_long": True, "allow_short": False}
    policy = compile_effective_risk_policy(runtime["profiles"][0], runtime, rules)
    assert policy["side_controls"] == {"allow_long": True, "allow_short": False}


def test_missing_risk_profile_id_falls_back_to_profile_id() -> None:
    runtime = _runtime_config({"id": "gft_10k"})
    policy = compile_effective_risk_policy(runtime["profiles"][0], runtime, _risk_rules())
    assert policy["_risk_profile_id"] == "gft_10k"


def test_ftmo_profile_resolves_from_risk_rules() -> None:
    runtime = _runtime_config(
        {
            "id": "ftmo_demo_100k",
            "risk_profile_id": "ftmo_demo_100k",
            "context_source_id": "ftmo_mt5_local",
        }
    )
    rules = _risk_rules()
    rules["per_account_profiles"]["ftmo_demo_100k"] = {
        "side_controls": {"allow_long": True, "allow_short": False},
        "position_limits": {
            "max_open_positions": 6,
            "block_duplicate_symbols": True,
            "block_same_direction_reentry": True,
            "reentry_block_statuses": ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"],
            "reentry_cooldown_minutes": 120,
            "reentry_key": "profile_symbol_side",
            "max_holding_minutes": 240,
            "auto_close_after_max_holding": True,
        },
        "drawdown_rules": {
            "daily_loss_pause_pct": 0.045,
            "trailing_drawdown_pause_pct": 0.09,
            "pause_until_next_day": True,
        },
        "reject_rules": {
            "enforce_universe": True,
            "enforce_session": True,
            "enforce_position_limit": True,
            "enforce_heat_limit": False,
            "enforce_duplicate_block": True,
            "enforce_drawdown_pause": True,
            "enforce_blackout": True,
            "enforce_streak_minimum": False,
            "min_streak_required": 4,
        },
        "sizing_by_asset_class": {
            "forex": {
                "method": "risk_per_stop_pips",
                "risk_per_trade_pct": 0.005,
                "min_sl_pips": 20,
                "min_tp_pips": 40,
                "use_model_horizon_exits": True,
                "model_horizon_bars": 12,
                "target_rr_multiple": 2.0,
                "min_automation_rr_multiple": 1.0,
                "mid_opportunity_rr_multiple": 1.5,
                "floor_opportunity_rr_multiple": 1.0,
                "tp_capture_fraction": 0.9,
                "min_stop_floor_pips": 3.0,
                "min_spread_multiple": 3.0,
                "min_atr_stop_multiple": 0.75,
                "contract_size": 100000.0,
                "pip_value_usd_per_standard_lot": {"default": 10.0},
            },
            "crypto": {
                "method": "risk_per_stop_spread_aware",
                "risk_per_trade_pct": 0.005,
                "max_notional_pct": 0.05,
                "min_qty": 0.000001,
                "min_sl_pct": 0.008,
                "min_tp_pct": 0.024,
                "sl_atr_multiple": 1.5,
                "tp_atr_multiple": 3.0,
                "contract_sizes": {"default": 1},
            },
        },
    }

    policy = compile_effective_risk_policy(runtime["profiles"][0], runtime, rules)

    assert policy["_risk_profile_id"] == "ftmo_demo_100k"
    assert policy["side_controls"] == {"allow_long": True, "allow_short": False}


def test_compiled_policy_keeps_reentry_blocking_fields() -> None:
    runtime = _runtime_config({"id": "gft_10k", "risk_profile_id": "gft_10k"})
    policy = compile_effective_risk_policy(runtime["profiles"][0], runtime, _risk_rules())

    position_limits = policy["position_limits"]
    assert position_limits["block_same_direction_reentry"] is True
    assert position_limits["reentry_cooldown_minutes"] == 120
    assert position_limits["reentry_key"] == "profile_symbol_side"
    assert position_limits["reentry_block_statuses"] == ["PENDING_FILL", "OPEN", "FORWARD_TRACKED"]


def test_missing_live_spread_does_not_fallback_to_baseline() -> None:
    result = size_crypto_spread_aware(
        mid_price=100.0,
        bid=None,
        ask=None,
        atr=2.0,
        side="LONG",
        account_equity=10_000.0,
        policy_crypto=_risk_rules()["per_account_profiles"]["gft_10k"]["sizing_by_asset_class"]["crypto"],
        crypto_spread_config={
            "fallback_spreads_absolute": {"BTCUSD": 0.5},
            "emergency_default_spread_pct": 0.5,
            "max_allowed_spread_pct": 0.5,
            "min_tp_spread_multiple": 3.0,
        },
        symbol="BTCUSD",
    )
    assert result["approved"] is False
    assert result["reject_reason"] == "BLOCKED_SPREAD_MISSING"


def test_live_spread_threshold_blocks_correctly() -> None:
    result = size_crypto_spread_aware(
        mid_price=100.0,
        bid=100.0,
        ask=101.0,
        atr=2.0,
        side="LONG",
        account_equity=10_000.0,
        policy_crypto=_risk_rules()["per_account_profiles"]["gft_10k"]["sizing_by_asset_class"]["crypto"],
        crypto_spread_config={"max_allowed_spread_pct": 0.5, "min_tp_spread_multiple": 3.0},
        symbol="BTCUSD",
    )
    assert result["approved"] is False
    assert result["reject_reason"] == "BLOCKED_SPREAD_TOO_WIDE"


def _make_temp_db() -> str:
    handle = tempfile.NamedTemporaryFile(prefix="vanguard_universe_", suffix=".db", delete=False)
    handle.close()
    return handle.name


def _create_shortlist_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_shortlist (
                cycle_ts_utc TEXT,
                symbol TEXT,
                asset_class TEXT,
                direction TEXT,
                strategy TEXT,
                strategy_rank INTEGER,
                strategy_score REAL,
                ml_prob REAL,
                edge_score REAL,
                consensus_count INTEGER,
                strategies_matched TEXT,
                regime TEXT,
                model_family TEXT,
                model_source TEXT,
                model_readiness TEXT,
                feature_profile TEXT,
                tbm_profile TEXT,
                entry_price REAL,
                intraday_atr REAL,
                rank INTEGER
            )
            """
        )
        con.commit()


def _insert_shortlist_row(db_path: str, *, cycle_ts: str, symbol: str, asset_class: str, direction: str, entry_price: float, atr: float) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO vanguard_shortlist (
                cycle_ts_utc, symbol, asset_class, direction, strategy, strategy_rank,
                strategy_score, ml_prob, edge_score, consensus_count, strategies_matched,
                regime, model_family, model_source, model_readiness, feature_profile,
                tbm_profile, entry_price, intraday_atr, rank
            ) VALUES (?, ?, ?, ?, 'v5_2_selection', 1, 0.9, 0.9, 0.9, 4, 'TIER_1',
                      'ACTIVE', 'regressor', 'test_model', 'v5_2_selected', '', '',
                      ?, ?, 1)
            """,
            (cycle_ts, symbol, asset_class, direction, entry_price, atr),
        )
        con.commit()


def _create_crypto_state(db_path: str, *, cycle_ts: str, profile_id: str, symbol: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_crypto_symbol_state (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                asset_class TEXT,
                quote_ts_utc TEXT,
                source TEXT,
                live_bid REAL,
                live_ask REAL,
                live_mid REAL,
                spread_price REAL,
                spread_bps REAL,
                spread_ratio_vs_baseline REAL,
                session_bucket TEXT,
                liquidity_bucket TEXT,
                trade_allowed INTEGER,
                min_qty REAL,
                max_qty REAL,
                qty_step REAL,
                tick_size REAL,
                open_position_count INTEGER,
                same_symbol_open_count INTEGER,
                correlated_open_count INTEGER,
                asset_state_json TEXT,
                source_status TEXT,
                source_error TEXT,
                config_version TEXT,
                created_at TEXT
            )
            """
        )
        con.execute(
            """
            INSERT INTO vanguard_crypto_symbol_state
            VALUES (?, ?, ?, 'crypto', ?, 'mt5_dwx', 100.0, 100.1, 100.05, 0.1, 9.995,
                    1.1, '24x7', 'normal', 1, 0.001, 100.0, 0.001, 0.01, 0, 0, 0,
                    '{}', 'OK', NULL, 'test-config', ?)
            """,
            (cycle_ts, profile_id, symbol, cycle_ts, cycle_ts),
        )
        con.commit()


def _create_forex_state(db_path: str, *, cycle_ts: str, profile_id: str, symbol: str) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_forex_pair_state (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                base_currency TEXT,
                quote_currency TEXT,
                usd_side TEXT,
                asset_class TEXT,
                session_bucket TEXT,
                liquidity_bucket TEXT,
                spread_pips REAL,
                spread_ratio_vs_baseline REAL,
                pair_correlation_bucket TEXT,
                currency_exposure_json TEXT,
                open_exposure_overlap_json TEXT,
                usd_concentration REAL,
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
            INSERT INTO vanguard_forex_pair_state
            VALUES (?, ?, ?, 'EUR', 'USD', 'quote', 'forex', 'london', 'high',
                    0.8, 1.1, 'LOW', '{}', '{}', 0.5, 0, 0, 1.1001,
                    ?, ?, 'mt5_dwx', 'OK', NULL, 'test-config')
            """,
            (cycle_ts, profile_id, symbol, cycle_ts, cycle_ts),
        )
        con.commit()


def _create_context_quote_latest(
    db_path: str,
    *,
    profile_id: str,
    symbol: str,
    bid: float,
    ask: float,
    cycle_ts: str,
    spread_pips: float | None,
    spread_rt: float | None,
) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_context_quote_latest (
                profile_id TEXT,
                broker TEXT,
                account_number TEXT,
                symbol TEXT,
                broker_symbol TEXT,
                bid REAL,
                ask REAL,
                mid REAL,
                spread_price REAL,
                spread_pips REAL,
                spread_rt REAL,
                quote_ts_utc TEXT,
                received_ts_utc TEXT,
                source TEXT,
                source_status TEXT,
                source_error TEXT,
                raw_payload_json TEXT
            )
            """
        )
        mid = (bid + ask) / 2.0
        con.execute(
            """
            INSERT INTO vanguard_context_quote_latest
            VALUES (?, 'mt5', '123', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'mt5_dwx', 'OK', NULL, '{}')
            """,
            (profile_id, symbol, symbol, bid, ask, mid, ask - bid, spread_pips, spread_rt, cycle_ts, cycle_ts),
        )
        con.commit()


def _create_context_account_latest(db_path: str, *, profile_id: str, equity: float, free_margin: float) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_context_account_latest (
                profile_id TEXT,
                broker TEXT,
                account_number TEXT,
                account_name TEXT,
                currency TEXT,
                balance REAL,
                equity REAL,
                free_margin REAL,
                margin_used REAL,
                margin_level REAL,
                leverage INTEGER,
                received_ts_utc TEXT,
                source TEXT,
                source_status TEXT,
                source_error TEXT,
                raw_payload_json TEXT
            )
            """
        )
        con.execute(
            """
            INSERT INTO vanguard_context_account_latest
            VALUES (?, 'mt5', '123', 'test', 'USD', ?, ?, ?, 0.0, 0.0, 100,
                    '2026-04-12T12:00:00Z', 'mt5_dwx', 'OK', NULL, '{}')
            """,
            (profile_id, equity, equity, free_margin),
        )
        con.commit()


def _create_context_symbol_specs(
    db_path: str,
    *,
    profile_id: str,
    symbol: str,
    min_lot: float,
    max_lot: float,
    lot_step: float,
    contract_size: float,
    trade_allowed: int = 1,
) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_context_symbol_specs (
                broker TEXT,
                profile_id TEXT,
                symbol TEXT,
                broker_symbol TEXT,
                min_lot REAL,
                max_lot REAL,
                lot_step REAL,
                pip_size REAL,
                tick_size REAL,
                tick_value REAL,
                contract_size REAL,
                trade_allowed INTEGER,
                source TEXT,
                updated_ts_utc TEXT,
                source_status TEXT,
                source_error TEXT,
                raw_payload_json TEXT
            )
            """
        )
        con.execute(
            """
            INSERT INTO vanguard_context_symbol_specs
            VALUES ('mt5', ?, ?, ?, ?, ?, ?, 0.0001, 0.0001, 10.0, ?, ?, 'mt5_dwx',
                    '2026-04-12T12:00:00Z', 'OK', NULL, '{}')
            """,
            (profile_id, symbol, symbol, min_lot, max_lot, lot_step, contract_size, trade_allowed),
        )
        con.commit()


def _create_v5_tradeability(
    db_path: str,
    *,
    cycle_ts: str,
    profile_id: str,
    symbol: str,
    asset_class: str,
    direction: str,
    economics_state: str = "PASS",
    predicted_move_pips: float = 25.0,
    after_cost_pips: float = 23.8,
    predicted_move_bps: float = 25.0,
    after_cost_bps: float = 13.0,
) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_v5_tradeability (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                asset_class TEXT,
                direction TEXT,
                evaluated_ts_utc TEXT,
                model_id TEXT,
                predicted_return REAL,
                predicted_move_pips REAL,
                spread_pips REAL,
                cost_pips REAL,
                after_cost_pips REAL,
                rank_value REAL,
                direction_streak INTEGER,
                strong_prediction_streak INTEGER,
                top_rank_streak INTEGER,
                same_bucket_streak INTEGER,
                flip_count_window INTEGER,
                live_followthrough_pips REAL,
                live_followthrough_state TEXT,
                v5_action TEXT,
                route_tier TEXT,
                display_label TEXT,
                reasons_json TEXT,
                metrics_json TEXT,
                context_snapshot_json TEXT,
                config_version TEXT,
                economics_state TEXT,
                v5_action_prelim TEXT,
                pair_state_ref_json TEXT,
                context_snapshot_ref_json TEXT,
                predicted_move_bps REAL,
                spread_bps REAL,
                cost_bps REAL,
                after_cost_bps REAL
            )
            """
        )
        con.execute(
            """
            INSERT INTO vanguard_v5_tradeability (
                cycle_ts_utc, profile_id, symbol, asset_class, direction, evaluated_ts_utc,
                model_id, predicted_return, predicted_move_pips, spread_pips, cost_pips,
                after_cost_pips, rank_value, direction_streak, strong_prediction_streak,
                top_rank_streak, same_bucket_streak, flip_count_window, live_followthrough_pips,
                live_followthrough_state, v5_action, route_tier, display_label, reasons_json,
                metrics_json, context_snapshot_json, config_version, economics_state,
                v5_action_prelim, pair_state_ref_json, context_snapshot_ref_json,
                predicted_move_bps, spread_bps, cost_bps, after_cost_bps
            ) VALUES (
                ?, ?, ?, ?, ?, ?, 'test_model', 0.001, ?, 0.8, 1.2, ?, 1.0,
                1, 1, 1, 1, 0, 0.0, 'UNKNOWN', 'ROUTE', 'TIER_2', 'TIER_2',
                '[]', '{}', '{}', 'test-v5', ?, 'PASS',
                '{"state":"pair"}', '{"context":"quote"}', ?, 10.0, 12.0, ?
            )
            """,
            (
                cycle_ts,
                profile_id,
                symbol,
                asset_class,
                direction,
                cycle_ts,
                predicted_move_pips,
                after_cost_pips,
                economics_state,
                predicted_move_bps,
                after_cost_bps,
            ),
        )
        con.commit()


def _create_v5_selection(
    db_path: str,
    *,
    cycle_ts: str,
    profile_id: str,
    symbol: str,
    asset_class: str,
    direction: str,
) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE vanguard_v5_selection (
                cycle_ts_utc TEXT,
                profile_id TEXT,
                symbol TEXT,
                asset_class TEXT,
                direction TEXT,
                selected INTEGER,
                selection_state TEXT,
                selection_rank INTEGER,
                selection_reason TEXT,
                route_tier TEXT,
                display_label TEXT,
                v5_action TEXT,
                selected_ts_utc TEXT,
                tradeability_ref_json TEXT,
                config_version TEXT,
                direction_streak INTEGER,
                top_rank_streak INTEGER,
                strong_prediction_streak INTEGER,
                same_bucket_streak INTEGER,
                flip_count_window INTEGER,
                live_followthrough_pips REAL,
                live_followthrough_state TEXT,
                metrics_json TEXT,
                selection_flags_json TEXT,
                live_followthrough_bps REAL
            )
            """
        )
        con.execute(
            """
            INSERT INTO vanguard_v5_selection
            VALUES (?, ?, ?, ?, ?, 1, 'SELECTED', 1, 'ok', 'TIER_2', 'TIER_2',
                    'ROUTE', ?, '{"tradeability":"ref"}', 'test-v5sel',
                    1, 1, 1, 1, 0, 0.0, 'UNKNOWN', '{}', '[]', 0.0)
            """,
            (cycle_ts, profile_id, symbol, asset_class, direction, cycle_ts),
        )
        con.commit()


def _runtime_profile(profile_id: str) -> dict:
    return {
        "id": profile_id,
        "risk_profile_id": profile_id,
        "account_size": 10000 if profile_id == "gft_10k" else 5000,
        "is_active": True,
        "context_source_id": "gft_mt5_local",
        "context_health_mode": "auto",
        "execution_mode": "auto",
    }


def test_v6_health_runs_independently_after_policy_approval() -> None:
    db_path = _make_temp_db()
    cycle_ts = "2026-04-12T12:00:00Z"
    _create_shortlist_table(db_path)
    _insert_shortlist_row(
        db_path,
        cycle_ts=cycle_ts,
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
        entry_price=100.05,
        atr=0.2,
    )
    _create_crypto_state(db_path, cycle_ts=cycle_ts, profile_id="gft_10k", symbol="BTCUSD")
    _create_context_quote_latest(
        db_path,
        profile_id="gft_10k",
        symbol="BTCUSD",
        bid=100.0,
        ask=100.1,
        cycle_ts=cycle_ts,
        spread_pips=None,
        spread_rt=0.001,
    )
    _create_context_account_latest(db_path, profile_id="gft_10k", equity=10000.0, free_margin=5000.0)
    _create_context_symbol_specs(
        db_path,
        profile_id="gft_10k",
        symbol="BTCUSD",
        min_lot=0.001,
        max_lot=10.0,
        lot_step=0.001,
        contract_size=1.0,
    )
    _create_v5_tradeability(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
        predicted_move_bps=45.0,
        after_cost_bps=30.0,
    )
    _create_v5_selection(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
    )
    runtime = _runtime_config(_runtime_profile("gft_10k"))
    with (
        patch.object(risk_filters, "load_runtime_config", return_value=runtime),
        patch.object(risk_filters, "load_risk_rules", return_value=_risk_rules()),
        patch.object(
            risk_filters,
            "evaluate_context_health",
            return_value={
                "cycle_ts_utc": cycle_ts,
                "profile_id": "gft_10k",
                "symbol": "BTCUSD",
                "direction": "LONG",
                "health_ts_utc": cycle_ts,
                "context_source_id": "gft_mt5_local",
                "source": "mt5_dwx",
                "broker": "mt5",
                "mode": "auto",
                "v6_state": "HOLD",
                "ready_to_route": 0,
                "reasons": [{"severity": "HOLD", "code": "QUOTE_STALE"}],
                "source_snapshot": {},
            },
        ),
    ):
        result = risk_filters.run(cycle_ts=cycle_ts, db_path=db_path)

    assert result["status"] == "ok"
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT status, rejection_reason, v6_state FROM vanguard_tradeable_portfolio"
        ).fetchone()
    assert row == ("HOLD", "QUOTE_STALE", "HOLD")


def test_risky_sizing_uses_account_state_fallback_when_live_account_truth_missing() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_5k"]
    decision = evaluate_risky_sizing(
        candidate={
            "symbol": "BNBUSD",
            "asset_class": "crypto",
            "direction": "LONG",
            "entry_price": 597.5,
            "bid": 597.2,
            "ask": 597.8,
            "atr": 8.0,
            "economics_state": "PASS",
            "spread_bps": 10.0,
            "predicted_move_bps": 60.0,
            "after_cost_bps": 45.0,
            "trade_allowed": 1,
            "min_qty": 0.001,
            "max_qty": 10.0,
            "qty_step": 0.001,
        },
        profile={"id": "gft_5k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 5000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["crypto"],
    )

    assert decision.decision == "APPROVED"
    assert decision.checks_json["account_truth_source"] == "vanguard_account_state"
    assert decision.checks_json["free_margin_truth_source"] == "vanguard_account_state"
    assert decision.sizing_inputs_json["effective_account_equity"] == 5000.0
    assert decision.sizing_inputs_json["effective_free_margin"] == 5000.0


def test_crypto_model_aware_exit_allows_sub_2r_when_model_cap_is_tight() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_5k"]
    decision = evaluate_risky_sizing(
        candidate={
            "symbol": "BNBUSD",
            "asset_class": "crypto",
            "direction": "LONG",
            "entry_price": 100.05,
            "bid": 100.0,
            "ask": 100.1,
            "atr": 0.2,
            "economics_state": "PASS",
            "spread_bps": 10.0,
            "predicted_move_bps": 20.0,
            "after_cost_bps": 15.0,
            "trade_allowed": 1,
            "min_qty": 0.001,
            "max_qty": 10.0,
            "qty_step": 0.001,
        },
        profile={"id": "gft_5k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 5000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["crypto"],
    )

    assert decision.decision == "APPROVED"
    assert decision.checks_json["used_legacy_exit_geometry"] is False
    assert decision.checks_json["sub_2r_model_cap"] is True
    assert "SUB_2R_MODEL_CAP" in decision.checks_json["reason_codes"]
    assert decision.sizing_outputs_json["rr_multiple"] < 2.0
    assert decision.sizing_outputs_json["take_profit"] > decision.sizing_outputs_json["entry_ref_price"]
    assert decision.checks_json["opportunity_multiplier"] == 0.5


def test_crypto_model_aware_exit_blocks_sub_1r_after_cost() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_5k"]
    decision = evaluate_risky_sizing(
        candidate={
            "symbol": "BNBUSD",
            "asset_class": "crypto",
            "direction": "LONG",
            "entry_price": 100.05,
            "bid": 100.0,
            "ask": 100.1,
            "atr": 0.2,
            "economics_state": "PASS",
            "spread_bps": 10.0,
            "predicted_move_bps": 20.0,
            "after_cost_bps": 5.0,
            "trade_allowed": 1,
            "min_qty": 0.001,
            "max_qty": 10.0,
            "qty_step": 0.001,
        },
        profile={"id": "gft_5k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 5000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["crypto"],
    )

    assert decision.decision == "BLOCKED_SUB_1R_AFTER_COST"


def test_forex_model_aware_exit_computes_monetary_risk_from_lots() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_10k"]
    decision = evaluate_risky_sizing(
        candidate={
            "symbol": "EURUSD",
            "asset_class": "forex",
            "direction": "LONG",
            "entry_price": 1.1001,
            "bid": 1.1000,
            "ask": 1.1002,
            "atr": 0.0002,
            "economics_state": "PASS",
            "spread_pips": 0.8,
            "predicted_move_pips": 12.0,
            "after_cost_pips": 5.0,
            "spec_trade_allowed": 1,
            "spec_min_lot": 0.01,
            "spec_max_lot": 10.0,
            "spec_lot_step": 0.01,
            "spec_contract_size": 100000.0,
        },
        profile={"id": "gft_10k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 10000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["forex"],
    )

    assert decision.decision == "APPROVED"
    assert decision.checks_json["used_legacy_exit_geometry"] is False
    assert decision.sizing_outputs_json["risk_dollars"] > 20.0
    assert decision.sizing_outputs_json["risk_dollars"] < 40.0
    assert decision.sizing_outputs_json["position_notional"] > 50000.0


def test_forex_opportunity_multiplier_reduces_lot_size() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_10k"]
    common = {
        "symbol": "EURUSD",
        "asset_class": "forex",
        "direction": "LONG",
        "entry_price": 1.1001,
        "bid": 1.1000,
        "ask": 1.1002,
        "atr": 0.0002,
        "economics_state": "PASS",
        "spread_pips": 0.8,
        "predicted_move_pips": 18.0,
        "spec_trade_allowed": 1,
        "spec_min_lot": 0.01,
        "spec_max_lot": 10.0,
        "spec_lot_step": 0.01,
        "spec_contract_size": 100000.0,
    }
    strong = evaluate_risky_sizing(
        candidate={**common, "after_cost_pips": 5.1},
        profile={"id": "gft_10k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 10000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["forex"],
    )
    weak = evaluate_risky_sizing(
        candidate={**common, "after_cost_pips": 3.4},
        profile={"id": "gft_10k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 10000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["forex"],
    )

    assert strong.decision == "APPROVED"
    assert weak.decision == "APPROVED"
    assert strong.approved_qty > weak.approved_qty
    assert strong.checks_json["opportunity_multiplier"] == 0.75
    assert weak.checks_json["opportunity_multiplier"] == 0.5


def test_forex_generic_exit_keeps_floor_geometry_on_low_atr_major() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_10k"]
    decision = evaluate_risky_sizing(
        candidate={
            "symbol": "GBPUSD",
            "asset_class": "forex",
            "direction": "LONG",
            "entry_price": 1.2501,
            "bid": 1.2500,
            "ask": 1.2502,
            "atr": 0.0002,
            "economics_state": "PASS",
            "spread_pips": 0.7,
            "predicted_move_pips": 120.0,
            "after_cost_pips": 8.0,
            "spec_trade_allowed": 1,
            "spec_min_lot": 0.01,
            "spec_max_lot": 10.0,
            "spec_lot_step": 0.01,
            "spec_contract_size": 100000.0,
        },
        profile={"id": "gft_10k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 10000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["forex"],
    )

    assert decision.decision == "APPROVED"
    assert round(decision.sizing_outputs_json["stop_distance"], 6) == 0.0003
    assert round(decision.sizing_outputs_json["tp_distance"], 6) == 0.0006
    assert decision.sizing_outputs_json["rr_multiple"] == 2.0


def test_forex_generic_exit_uses_atr_on_high_atr_pair() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_10k"]
    decision = evaluate_risky_sizing(
        candidate={
            "symbol": "GBPJPY",
            "asset_class": "forex",
            "direction": "SHORT",
            "entry_price": 201.10,
            "bid": 201.08,
            "ask": 201.10,
            "atr": 0.08,
            "economics_state": "PASS",
            "spread_pips": 1.2,
            "predicted_move_pips": 180.0,
            "after_cost_pips": 70.0,
            "spec_trade_allowed": 1,
            "spec_min_lot": 0.01,
            "spec_max_lot": 10.0,
            "spec_lot_step": 0.01,
            "spec_contract_size": 100000.0,
        },
        profile={"id": "gft_10k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 10000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["forex"],
    )

    assert decision.decision == "APPROVED"
    assert decision.sizing_outputs_json["stop_distance"] > 0.03
    assert decision.sizing_outputs_json["tp_distance"] == decision.sizing_outputs_json["stop_distance"] * 2.0


def test_forex_generic_exit_uses_spread_floor_when_spread_is_wide() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_10k"]
    decision = evaluate_risky_sizing(
        candidate={
            "symbol": "EURUSD",
            "asset_class": "forex",
            "direction": "LONG",
            "entry_price": 1.1001,
            "bid": 1.1000,
            "ask": 1.1002,
            "atr": 0.0002,
            "economics_state": "PASS",
            "spread_pips": 1.5,
            "predicted_move_pips": 25.0,
            "after_cost_pips": 10.0,
            "spec_trade_allowed": 1,
            "spec_min_lot": 0.01,
            "spec_max_lot": 10.0,
            "spec_lot_step": 0.01,
            "spec_contract_size": 100000.0,
        },
        profile={"id": "gft_10k", "execution_mode": "manual", "context_health_mode": "manual"},
        policy=policy,
        account_state={"equity": 10000.0},
        sizing_cfg=policy["sizing_by_asset_class"]["forex"],
    )

    assert decision.decision == "APPROVED"
    assert round(decision.sizing_outputs_json["stop_distance"], 6) == 0.00045
    assert round(decision.sizing_outputs_json["tp_distance"], 6) == 0.0009


def test_forex_generic_exit_resolves_horizon_from_model_meta() -> None:
    policy = _risk_rules()["per_account_profiles"]["gft_10k"]
    with patch.object(risky_sizing, "_active_model_meta", return_value={"model_horizon_bars": 16}):
        decision = evaluate_risky_sizing(
            candidate={
                "symbol": "EURUSD",
                "asset_class": "forex",
                "direction": "LONG",
                "entry_price": 1.1001,
                "bid": 1.1000,
                "ask": 1.1002,
                "atr": 0.0002,
                "economics_state": "PASS",
                "spread_pips": 0.8,
                "predicted_move_pips": 30.0,
                "after_cost_pips": 8.0,
                "spec_trade_allowed": 1,
                "spec_min_lot": 0.01,
                "spec_max_lot": 10.0,
                "spec_lot_step": 0.01,
                "spec_contract_size": 100000.0,
            },
            profile={"id": "gft_10k", "execution_mode": "manual", "context_health_mode": "manual"},
            policy=policy,
            account_state={"equity": 10000.0},
            sizing_cfg=policy["sizing_by_asset_class"]["forex"],
        )

    assert decision.decision == "APPROVED"
    assert decision.checks_json["model_horizon_bars"] == 16
    assert "model_horizon_bars=16" in decision.notes


def test_crypto_smoke_path_still_writes_tradeable_portfolio() -> None:
    db_path = _make_temp_db()
    cycle_ts = "2026-04-12T12:05:00Z"
    _create_shortlist_table(db_path)
    _insert_shortlist_row(
        db_path,
        cycle_ts=cycle_ts,
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
        entry_price=100.05,
        atr=0.2,
    )
    _create_crypto_state(db_path, cycle_ts=cycle_ts, profile_id="gft_10k", symbol="BTCUSD")
    _create_context_quote_latest(
        db_path,
        profile_id="gft_10k",
        symbol="BTCUSD",
        bid=100.0,
        ask=100.1,
        cycle_ts=cycle_ts,
        spread_pips=None,
        spread_rt=0.001,
    )
    _create_context_account_latest(db_path, profile_id="gft_10k", equity=10000.0, free_margin=5000.0)
    _create_context_symbol_specs(
        db_path,
        profile_id="gft_10k",
        symbol="BTCUSD",
        min_lot=0.001,
        max_lot=10.0,
        lot_step=0.001,
        contract_size=1.0,
    )
    _create_v5_tradeability(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
        predicted_move_bps=45.0,
        after_cost_bps=30.0,
    )
    _create_v5_selection(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
    )
    runtime = _runtime_config(_runtime_profile("gft_10k"))
    with (
        patch.object(risk_filters, "load_runtime_config", return_value=runtime),
        patch.object(risk_filters, "load_risk_rules", return_value=_risk_rules()),
        patch.object(
            risk_filters,
            "evaluate_context_health",
            return_value={
                "cycle_ts_utc": cycle_ts,
                "profile_id": "gft_10k",
                "symbol": "BTCUSD",
                "direction": "LONG",
                "health_ts_utc": cycle_ts,
                "context_source_id": "gft_mt5_local",
                "source": "mt5_dwx",
                "broker": "mt5",
                "mode": "auto",
                "v6_state": "READY",
                "ready_to_route": 1,
                "reasons": [],
                "source_snapshot": {},
            },
        ),
    ):
        result = risk_filters.run(cycle_ts=cycle_ts, db_path=db_path)

    assert result["status"] == "ok"
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT status, symbol, account_id FROM vanguard_tradeable_portfolio"
        ).fetchone()
        audit = con.execute(
            "SELECT final_verdict, risk_profile_id FROM vanguard_risky_decisions"
        ).fetchone()
    assert row == ("APPROVED", "BTCUSD", "gft_10k")
    assert audit == ("APPROVED", "gft_10k")


def test_forex_smoke_path_still_writes_tradeable_portfolio() -> None:
    db_path = _make_temp_db()
    cycle_ts = "2026-04-12T12:10:00Z"
    _create_shortlist_table(db_path)
    _insert_shortlist_row(
        db_path,
        cycle_ts=cycle_ts,
        symbol="EURUSD",
        asset_class="forex",
        direction="LONG",
        entry_price=1.1001,
        atr=0.0015,
    )
    _create_forex_state(db_path, cycle_ts=cycle_ts, profile_id="gft_10k", symbol="EURUSD")
    _create_context_quote_latest(
        db_path,
        profile_id="gft_10k",
        symbol="EURUSD",
        bid=1.1000,
        ask=1.1002,
        cycle_ts=cycle_ts,
        spread_pips=0.8,
        spread_rt=0.0001818,
    )
    _create_context_account_latest(db_path, profile_id="gft_10k", equity=10000.0, free_margin=5000.0)
    _create_context_symbol_specs(
        db_path,
        profile_id="gft_10k",
        symbol="EURUSD",
        min_lot=0.01,
        max_lot=10.0,
        lot_step=0.01,
        contract_size=100000.0,
    )
    _create_v5_tradeability(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="EURUSD",
        asset_class="forex",
        direction="LONG",
    )
    _create_v5_selection(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="EURUSD",
        asset_class="forex",
        direction="LONG",
    )
    runtime = _runtime_config(_runtime_profile("gft_10k"))
    with (
        patch.object(risk_filters, "load_runtime_config", return_value=runtime),
        patch.object(risk_filters, "load_risk_rules", return_value=_risk_rules()),
        patch.object(
            risk_filters,
            "evaluate_context_health",
            return_value={
                "cycle_ts_utc": cycle_ts,
                "profile_id": "gft_10k",
                "symbol": "EURUSD",
                "direction": "LONG",
                "health_ts_utc": cycle_ts,
                "context_source_id": "gft_mt5_local",
                "source": "mt5_dwx",
                "broker": "mt5",
                "mode": "auto",
                "v6_state": "READY",
                "ready_to_route": 1,
                "reasons": [],
                "source_snapshot": {},
            },
        ),
    ):
        result = risk_filters.run(cycle_ts=cycle_ts, db_path=db_path)

    assert result["status"] == "ok"
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT status, symbol, account_id FROM vanguard_tradeable_portfolio"
        ).fetchone()
    assert row == ("APPROVED", "EURUSD", "gft_10k")


def test_forex_enrichment_uses_context_quote_latest_for_bid_ask() -> None:
    db_path = _make_temp_db()
    cycle_ts = "2026-04-12T12:15:00Z"
    _create_forex_state(db_path, cycle_ts=cycle_ts, profile_id="gft_10k", symbol="EURUSD")
    _create_context_quote_latest(
        db_path,
        profile_id="gft_10k",
        symbol="EURUSD",
        bid=1.2345,
        ask=1.2347,
        cycle_ts=cycle_ts,
        spread_pips=1.2,
        spread_rt=0.000162,
    )
    out = risk_filters._enrich_with_live_measurements(
        {"symbol": "EURUSD", "asset_class": "forex", "direction": "LONG"},
        "gft_10k",
        cycle_ts,
        db_path,
    )
    assert out["bid"] == 1.2345
    assert out["ask"] == 1.2347
    assert out["spread_pips"] == 0.8


def test_forex_missing_live_spread_blocks_without_fallback() -> None:
    db_path = _make_temp_db()
    cycle_ts = "2026-04-12T12:17:00Z"
    _create_shortlist_table(db_path)
    _insert_shortlist_row(
        db_path,
        cycle_ts=cycle_ts,
        symbol="EURUSD",
        asset_class="forex",
        direction="LONG",
        entry_price=1.1001,
        atr=0.0015,
    )
    _create_context_quote_latest(
        db_path,
        profile_id="gft_10k",
        symbol="EURUSD",
        bid=1.1000,
        ask=1.1002,
        cycle_ts=cycle_ts,
        spread_pips=None,
        spread_rt=0.0001818,
    )
    _create_forex_state(db_path, cycle_ts=cycle_ts, profile_id="gft_10k", symbol="EURUSD")
    with sqlite3.connect(db_path) as con:
        con.execute("UPDATE vanguard_forex_pair_state SET spread_pips = NULL")
        con.commit()
    _create_context_account_latest(db_path, profile_id="gft_10k", equity=10000.0, free_margin=5000.0)
    _create_context_symbol_specs(
        db_path,
        profile_id="gft_10k",
        symbol="EURUSD",
        min_lot=0.01,
        max_lot=10.0,
        lot_step=0.01,
        contract_size=100000.0,
    )
    _create_v5_tradeability(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="EURUSD",
        asset_class="forex",
        direction="LONG",
    )
    _create_v5_selection(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="EURUSD",
        asset_class="forex",
        direction="LONG",
    )
    runtime = _runtime_config(_runtime_profile("gft_10k"))
    with (
        patch.object(risk_filters, "load_runtime_config", return_value=runtime),
        patch.object(risk_filters, "load_risk_rules", return_value=_risk_rules()),
        patch.object(
            risk_filters,
            "evaluate_context_health",
            return_value={
                "cycle_ts_utc": cycle_ts,
                "profile_id": "gft_10k",
                "symbol": "EURUSD",
                "direction": "LONG",
                "health_ts_utc": cycle_ts,
                "context_source_id": "gft_mt5_local",
                "source": "mt5_dwx",
                "broker": "mt5",
                "mode": "auto",
                "v6_state": "READY",
                "ready_to_route": 1,
                "reasons": [],
                "source_snapshot": {},
            },
        ),
    ):
        result = risk_filters.run(cycle_ts=cycle_ts, db_path=db_path)
    assert result["status"] == "ok"
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT status, rejection_reason FROM vanguard_tradeable_portfolio"
        ).fetchone()
    assert row == ("REJECTED", "BLOCKED_SPREAD_MISSING")


def test_weak_tradeability_is_rejected_before_sizing() -> None:
    db_path = _make_temp_db()
    cycle_ts = "2026-04-12T12:20:00Z"
    _create_shortlist_table(db_path)
    _insert_shortlist_row(
        db_path,
        cycle_ts=cycle_ts,
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
        entry_price=100.05,
        atr=2.0,
    )
    _create_crypto_state(db_path, cycle_ts=cycle_ts, profile_id="gft_10k", symbol="BTCUSD")
    _create_context_quote_latest(
        db_path,
        profile_id="gft_10k",
        symbol="BTCUSD",
        bid=100.0,
        ask=100.1,
        cycle_ts=cycle_ts,
        spread_pips=None,
        spread_rt=0.001,
    )
    _create_context_account_latest(db_path, profile_id="gft_10k", equity=10000.0, free_margin=5000.0)
    _create_context_symbol_specs(
        db_path,
        profile_id="gft_10k",
        symbol="BTCUSD",
        min_lot=0.001,
        max_lot=10.0,
        lot_step=0.001,
        contract_size=1.0,
    )
    _create_v5_tradeability(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
        economics_state="WEAK",
    )
    _create_v5_selection(
        db_path,
        cycle_ts=cycle_ts,
        profile_id="gft_10k",
        symbol="BTCUSD",
        asset_class="crypto",
        direction="LONG",
    )
    runtime = _runtime_config(_runtime_profile("gft_10k"))
    with (
        patch.object(risk_filters, "load_runtime_config", return_value=runtime),
        patch.object(risk_filters, "load_risk_rules", return_value=_risk_rules()),
        patch.object(
            risk_filters,
            "evaluate_context_health",
            return_value={
                "cycle_ts_utc": cycle_ts,
                "profile_id": "gft_10k",
                "symbol": "BTCUSD",
                "direction": "LONG",
                "health_ts_utc": cycle_ts,
                "context_source_id": "gft_mt5_local",
                "source": "mt5_dwx",
                "broker": "mt5",
                "mode": "auto",
                "v6_state": "READY",
                "ready_to_route": 1,
                "reasons": [],
                "source_snapshot": {},
            },
        ),
    ):
        result = risk_filters.run(cycle_ts=cycle_ts, db_path=db_path)
    assert result["status"] == "ok"
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            "SELECT status, rejection_reason FROM vanguard_tradeable_portfolio"
        ).fetchone()
    assert row == ("REJECTED", "BLOCKED_ECONOMICS_WEAK")
