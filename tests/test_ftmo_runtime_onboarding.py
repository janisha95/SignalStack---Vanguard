from __future__ import annotations

import json
from pathlib import Path


RUNTIME_PATH = Path("/Users/sjani008/SS/Vanguard_QAenv/config/vanguard_runtime.json")
RISK_RULES_PATH = Path("/Users/sjani008/SS/Risky/config/risk_rules.json")


def test_runtime_contains_ftmo_profile_source_and_universe() -> None:
    runtime = json.loads(RUNTIME_PATH.read_text())

    profiles = {profile["id"]: profile for profile in runtime["profiles"]}
    ftmo_profile = profiles["ftmo_demo_100k"]
    assert ftmo_profile["risk_profile_id"] == "ftmo_demo_100k"
    assert ftmo_profile["instrument_scope"] == "ftmo_universe"
    assert ftmo_profile["context_source_id"] == "ftmo_mt5_local"
    assert ftmo_profile["execution_bridge"] == "mt5_local_ftmo_demo_100k"

    ftmo_source = runtime["context_sources"]["ftmo_mt5_local"]
    assert ftmo_source["broker"] == "ftmo"
    assert "ftmo_demo_100k" in ftmo_source["profiles"]

    ftmo_universe = runtime["universes"]["ftmo_universe"]["symbols"]
    assert "EURUSD" in ftmo_universe["forex"]
    assert "BTCUSD" in ftmo_universe["crypto"]
    assert "US500.cash" in ftmo_universe["index"]
    assert "XAUUSD" in ftmo_universe["metal"]
    assert "USOIL.cash" in ftmo_universe["energy"]
    assert "CORN.c" in ftmo_universe["agriculture"]
    assert "AAPL" in ftmo_universe["equity"]


def test_risk_rules_contains_ftmo_profile() -> None:
    rules = json.loads(RISK_RULES_PATH.read_text())
    ftmo = rules["per_account_profiles"]["ftmo_demo_100k"]

    assert ftmo["account_size_usd"] == 100000
    assert ftmo["position_limits"]["max_open_positions"] == 4
    assert ftmo["drawdown_rules"]["daily_loss_pause_pct"] == 0.05
    assert ftmo["drawdown_rules"]["trailing_drawdown_pause_pct"] == 0.10
    assert ftmo["checks"]["daily_drawdown"]["pct_of_session_start_equity"] == 0.05
    assert ftmo["checks"]["daily_drawdown"]["includes_floating_pnl"] is True
    assert ftmo["checks"]["max_overall_loss_static"]["pct_of_initial_balance"] == 0.10
    assert ftmo["checks"]["max_overall_loss_static"]["absolute_floor_usd"] == 90000
    assert ftmo["checks"]["max_overall_loss_static"]["is_trailing"] is False
    assert ftmo["cost_model"]["forex_commission_per_lot_per_side_usd"] == 5.0
    assert ftmo["cost_model"]["crypto_commission_per_lot_per_side_usd"] == 0.0
    assert ftmo["cost_model"]["equity_cfd_commission_per_lot_per_side_usd"] == 0.0
    assert ftmo["sizing_by_asset_class"]["forex"]["method"] == "risk_per_stop_pips"
    assert ftmo["sizing_by_asset_class"]["crypto"]["method"] == "risk_per_stop_spread_aware"


def test_runtime_ftmo_account_number_is_bound_and_only_path_is_pending() -> None:
    runtime = json.loads(RUNTIME_PATH.read_text())
    profile = {profile["id"]: profile for profile in runtime["profiles"]}["ftmo_demo_100k"]
    source = runtime["context_sources"]["ftmo_mt5_local"]

    assert profile["onboarding_status"] == "pending_dwx_path_only"
    assert source["symbol_suffix"] == ""
    assert source["profiles"]["ftmo_demo_100k"]["account_number"] == "1513073754"
