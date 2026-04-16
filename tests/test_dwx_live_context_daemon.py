from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import dwx_live_context_daemon as daemon


def test_load_symbols_for_context_source_is_profile_and_asset_config_driven() -> None:
    cfg = {
        "profiles": [
            {
                "id": "gft_10k",
                "is_active": True,
                "context_source_id": "gft_mt5_local",
                "instrument_scope": "gft_universe",
            },
            {
                "id": "ftmo_demo",
                "is_active": True,
                "context_source_id": "ftmo_mt5_local",
                "instrument_scope": "ftmo_universe",
            },
        ],
        "universes": {
            "gft_universe": {
                "symbols": {
                    "forex": ["EUR/USD"],
                    "crypto": ["BTCUSD"],
                    "equity": ["AAPL"],
                }
            },
            "ftmo_universe": {
                "symbols": {
                    "forex": ["GBPUSD"],
                    "crypto": ["ETHUSD", "SOLUSD"],
                }
            },
        },
    }
    source_cfg = {
        "_source_id": "gft_mt5_local",
        "primary_for": ["forex", "crypto"],
        "profiles": {"gft_10k": {"enabled": True}},
    }

    symbols, asset_by_symbol, by_asset = daemon._load_symbols_for_context_source(cfg, source_cfg, "gft_10k")

    assert symbols == ["BTCUSD", "EURUSD"]
    assert asset_by_symbol == {"BTCUSD": "crypto", "EURUSD": "forex"}
    assert by_asset == {"crypto": ["BTCUSD"], "forex": ["EURUSD"]}


def test_resolve_symbol_suffix_map_supports_asset_class_overrides() -> None:
    suffix_map = daemon._resolve_symbol_suffix_map(
        {
            "symbol_suffix": ".x",
            "symbol_suffix_by_asset_class": {"crypto": ".c"},
        },
        ["EURUSD", "BTCUSD"],
        {"EURUSD": "forex", "BTCUSD": "crypto"},
        ".x",
    )

    assert suffix_map == {"EURUSD": ".x", "BTCUSD": ".c"}


def test_active_profiles_fallback_uses_runtime_scope_not_gft_default() -> None:
    cfg = {
        "profiles": [],
        "universes": {
            "ftmo_universe": {
                "symbols": {
                    "forex": ["EURUSD"],
                }
            }
        },
    }
    source_cfg = {
        "_source_id": "ftmo_mt5_local",
        "primary_for": ["forex"],
        "profiles": {"ftmo_demo_100k": {"enabled": True}},
    }

    profiles = daemon._active_profiles(cfg, source_cfg, None)

    assert profiles == [
        {
            "id": "ftmo_demo_100k",
            "instrument_scope": "ftmo_universe",
            "context_source_id": "ftmo_mt5_local",
        }
    ]
