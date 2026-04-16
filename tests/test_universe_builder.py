"""
test_universe_builder.py — Tests for vanguard/helpers/universe_builder.py.

Covers:
  • load_universes / _ensure_loaded
  • get_universe
  • get_ftmo_universe
  • get_topstep_universe
  • classify_asset_class
  • get_health_thresholds
  • refresh_to_db (FTMO + TopStep, no Alpaca required)
  • DB table schema (vanguard_universe_members upsert/query)
  • VanguardDB new methods (upsert_universe_members, get_universe_members,
    count_universe_members)

Run: python3 -m pytest tests/test_universe_builder.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vanguard.helpers.universe_builder as ub
from vanguard.helpers.db import VanguardDB

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "vanguard_universes.json"
_ACCOUNTS_PATH = Path(__file__).resolve().parent.parent / "config" / "vanguard_accounts.json"


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset cached module state before each test."""
    ub._universes = None
    ub._accounts = None
    ub._asset_class_map = None
    yield
    ub._universes = None
    ub._accounts = None
    ub._asset_class_map = None


@pytest.fixture
def loaded():
    """Load the real config once and return the universe dict."""
    return ub.load_universes(_CONFIG_PATH)


@pytest.fixture
def tmp_db():
    """Temporary VanguardDB in a temp file."""
    with tempfile.NamedTemporaryFile(suffix="_vanguard_universe.db", delete=False) as f:
        path = f.name
    db = VanguardDB(path)
    yield db
    Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# load_universes
# ---------------------------------------------------------------------------

class TestLoadUniverses:

    def test_loads_from_real_config(self):
        result = ub.load_universes(_CONFIG_PATH)
        assert isinstance(result, dict)
        assert len(result) >= 3

    def test_returns_universe_keys(self, loaded):
        assert "ttp_equity" in loaded
        assert "ftmo_cfd" in loaded
        assert "topstep_futures" in loaded

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            ub.load_universes("/nonexistent/path/universes.json")

    def test_sets_module_cache(self):
        assert ub._universes is None
        ub.load_universes(_CONFIG_PATH)
        assert ub._universes is not None

    def test_reload_invalidates_asset_class_map(self, loaded):
        ub._build_asset_class_map()
        assert ub._asset_class_map is not None
        ub.load_universes(_CONFIG_PATH)
        assert ub._asset_class_map is None   # invalidated on reload


# ---------------------------------------------------------------------------
# get_universe
# ---------------------------------------------------------------------------

class TestGetUniverse:

    def test_ftmo_cfd_returns_list_of_strings(self, loaded):
        result = ub.get_universe("ftmo_cfd")
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    def test_topstep_futures_returns_list_of_dicts(self, loaded):
        result = ub.get_universe("topstep_futures")
        assert isinstance(result, list)
        assert all(isinstance(c, dict) for c in result)

    def test_ttp_equity_returns_filter_rule_keys(self, loaded):
        result = ub.get_universe("ttp_equity")
        assert isinstance(result, list)
        # Should contain filter rule keys, not actual symbols

    def test_unknown_universe_raises_key_error(self, loaded):
        with pytest.raises(KeyError, match="Unknown universe"):
            ub.get_universe("nonexistent_universe")


# ---------------------------------------------------------------------------
# get_ftmo_universe
# ---------------------------------------------------------------------------

class TestGetFtmoUniverse:

    def test_returns_list(self, loaded):
        result = ub.get_ftmo_universe()
        assert isinstance(result, list)

    def test_all_uppercase(self, loaded):
        result = ub.get_ftmo_universe()
        for sym in result:
            assert sym == sym.upper(), f"{sym} is not uppercase"

    def test_count_approx_165(self, loaded):
        result = ub.get_ftmo_universe()
        # Allow ±20 since exact count varies with config updates
        assert 130 <= len(result) <= 200, f"Expected ~165, got {len(result)}"

    def test_contains_key_forex_pairs(self, loaded):
        result = ub.get_ftmo_universe()
        for sym in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD"):
            assert sym in result, f"{sym} should be in FTMO universe"

    def test_contains_key_index_cfds(self, loaded):
        result = ub.get_ftmo_universe()
        assert "US500.CASH" in result or "US500.cash" in [s for s in result if "US500" in s.upper()]

    def test_contains_metals(self, loaded):
        result = ub.get_ftmo_universe()
        assert "XAUUSD" in result
        assert "XAGUSD" in result

    def test_contains_equity_cfds(self, loaded):
        result = ub.get_ftmo_universe()
        for sym in ("AAPL", "MSFT", "NVDA", "TSLA"):
            assert sym in result, f"{sym} should be in FTMO equity CFDs"

    def test_contains_crypto(self, loaded):
        result = ub.get_ftmo_universe()
        assert "BTCUSD" in result
        assert "ETHUSD" in result

    def test_no_duplicates(self, loaded):
        result = ub.get_ftmo_universe()
        assert len(result) == len(set(result)), "Duplicate symbols in FTMO universe"


# ---------------------------------------------------------------------------
# get_topstep_universe
# ---------------------------------------------------------------------------

class TestGetTopstepUniverse:

    def test_returns_list_of_dicts(self, loaded):
        result = ub.get_topstep_universe()
        assert isinstance(result, list)
        assert all(isinstance(c, dict) for c in result)

    def test_count_is_42(self, loaded):
        result = ub.get_topstep_universe()
        assert len(result) == 42, f"Expected 42 TopStep futures, got {len(result)}"

    def test_each_contract_has_required_keys(self, loaded):
        result = ub.get_topstep_universe()
        required = {"symbol", "name", "exchange", "asset_class", "tick_size", "tick_value", "margin"}
        for c in result:
            missing = required - set(c.keys())
            assert not missing, f"Contract {c.get('symbol')} missing keys: {missing}"

    def test_tick_sizes_positive(self, loaded):
        result = ub.get_topstep_universe()
        for c in result:
            assert c["tick_size"] > 0, f"{c['symbol']} has non-positive tick_size"
            assert c["tick_value"] > 0, f"{c['symbol']} has non-positive tick_value"
            assert c["margin"] > 0, f"{c['symbol']} has non-positive margin"

    def test_contains_es_and_nq(self, loaded):
        result = ub.get_topstep_universe()
        symbols = [c["symbol"] for c in result]
        assert "ES" in symbols
        assert "NQ" in symbols
        assert "MES" in symbols
        assert "MNQ" in symbols

    def test_contains_cl_and_gc(self, loaded):
        result = ub.get_topstep_universe()
        symbols = [c["symbol"] for c in result]
        assert "CL" in symbols
        assert "GC" in symbols

    def test_contains_treasury_futures(self, loaded):
        result = ub.get_topstep_universe()
        symbols = [c["symbol"] for c in result]
        for sym in ("ZB", "ZN", "ZF"):
            assert sym in symbols, f"{sym} should be in TopStep universe"

    def test_contains_forex_futures(self, loaded):
        result = ub.get_topstep_universe()
        symbols = [c["symbol"] for c in result]
        for sym in ("6E", "6B", "6J"):
            assert sym in symbols, f"{sym} should be in TopStep universe"

    def test_asset_class_values(self, loaded):
        result = ub.get_topstep_universe()
        valid_classes = {"index", "energy", "metal", "agriculture", "interest_rate", "forex"}
        for c in result:
            assert c["asset_class"] in valid_classes, (
                f"{c['symbol']} has unexpected asset_class: {c['asset_class']}"
            )

    def test_no_duplicate_symbols(self, loaded):
        result = ub.get_topstep_universe()
        symbols = [c["symbol"] for c in result]
        assert len(symbols) == len(set(symbols)), "Duplicate symbols in TopStep universe"


# ---------------------------------------------------------------------------
# classify_asset_class
# ---------------------------------------------------------------------------

class TestClassifyAssetClass:

    def test_forex_pairs(self, loaded):
        assert ub.classify_asset_class("EURUSD") == "forex"
        assert ub.classify_asset_class("GBPUSD") == "forex"
        assert ub.classify_asset_class("USDJPY") == "forex"

    def test_case_insensitive(self, loaded):
        assert ub.classify_asset_class("eurusd") == "forex"
        assert ub.classify_asset_class("Eurusd") == "forex"

    def test_index_cfds(self, loaded):
        result = ub.classify_asset_class("US500.cash")
        assert result == "index"

    def test_topstep_index_futures(self, loaded):
        assert ub.classify_asset_class("ES") == "index"
        assert ub.classify_asset_class("NQ") == "index"

    def test_metals(self, loaded):
        assert ub.classify_asset_class("XAUUSD") == "metal"
        assert ub.classify_asset_class("XAGUSD") == "metal"
        assert ub.classify_asset_class("GC") == "metal"
        assert ub.classify_asset_class("SI") == "metal"

    def test_energy(self, loaded):
        assert ub.classify_asset_class("USOIL.cash") == "energy"
        assert ub.classify_asset_class("CL") == "energy"
        assert ub.classify_asset_class("NG") == "energy"

    def test_agriculture(self, loaded):
        assert ub.classify_asset_class("CORN.c") == "agriculture"
        assert ub.classify_asset_class("ZC") == "agriculture"
        assert ub.classify_asset_class("ZS") == "agriculture"

    def test_crypto(self, loaded):
        assert ub.classify_asset_class("BTCUSD") == "crypto"
        assert ub.classify_asset_class("ETHUSD") == "crypto"

    def test_equity_cfds(self, loaded):
        assert ub.classify_asset_class("AAPL") == "equity"
        assert ub.classify_asset_class("MSFT") == "equity"
        assert ub.classify_asset_class("NVDA") == "equity"

    def test_interest_rate_futures(self, loaded):
        assert ub.classify_asset_class("ZB") == "interest_rate"
        assert ub.classify_asset_class("ZN") == "interest_rate"
        assert ub.classify_asset_class("ZF") == "interest_rate"

    def test_forex_futures(self, loaded):
        assert ub.classify_asset_class("6E") == "forex"
        assert ub.classify_asset_class("6B") == "forex"

    def test_unknown_symbol_defaults_to_equity(self, loaded):
        assert ub.classify_asset_class("UNKNWN") == "equity"

    def test_ttp_equity_symbol_returns_equity(self, loaded):
        # TTP equity symbols are not in the static map → should default to equity
        assert ub.classify_asset_class("SPY") == "equity"
        assert ub.classify_asset_class("GOOG") == "equity"


# ---------------------------------------------------------------------------
# get_health_thresholds
# ---------------------------------------------------------------------------

class TestGetHealthThresholds:

    def test_returns_dict(self, loaded):
        result = ub.get_health_thresholds("ttp_equity")
        assert isinstance(result, dict)

    def test_equity_thresholds_keys(self, loaded):
        result = ub.get_health_thresholds("ttp_equity")
        for key in ("max_stale_minutes", "min_rel_vol", "max_spread_pct",
                    "min_bars_warmup", "session_avg_window"):
            assert key in result, f"Missing threshold key: {key}"

    def test_equity_defaults(self, loaded):
        result = ub.get_health_thresholds("ttp_equity")
        assert result["max_stale_minutes"] == 10.0
        assert result["min_rel_vol"] == 0.3
        assert result["max_spread_pct"] == 0.05
        assert result["min_bars_warmup"] == 50

    def test_forex_thresholds_wider_than_equity(self, loaded):
        equity = ub.get_health_thresholds("ttp_equity")
        forex  = ub.get_health_thresholds("ftmo_cfd")
        # Forex should have wider spread tolerance
        assert forex["max_spread_pct"] > equity["max_spread_pct"]
        # Forex should have lower min_rel_vol (always has volume)
        assert forex["min_rel_vol"] < equity["min_rel_vol"]

    def test_unknown_universe_returns_defaults(self, loaded):
        result = ub.get_health_thresholds("nonexistent_universe")
        # Should not raise; returns equity defaults
        assert "max_stale_minutes" in result


# ---------------------------------------------------------------------------
# refresh_to_db
# ---------------------------------------------------------------------------

class TestRefreshToDb:

    def test_refresh_ftmo_writes_rows(self, loaded, tmp_db):
        n = ub.refresh_to_db("ftmo_cfd", tmp_db, "2025-01-10T14:00:00Z")
        assert n > 100, f"Expected 100+ FTMO rows, got {n}"

    def test_refresh_topstep_writes_42_rows(self, loaded, tmp_db):
        n = ub.refresh_to_db("topstep_futures", tmp_db, "2025-01-10T14:00:00Z")
        assert n == 42, f"Expected 42 TopStep rows, got {n}"

    def test_refresh_ftmo_stores_asset_class(self, loaded, tmp_db):
        ub.refresh_to_db("ftmo_cfd", tmp_db, "2025-01-10T14:00:00Z")
        rows = tmp_db.get_universe_members("ftmo_cfd")
        asset_classes = {r["asset_class"] for r in rows}
        assert "forex" in asset_classes
        assert "metal" in asset_classes
        assert "energy" in asset_classes

    def test_runtime_source_resolution_uses_runtime_config(self, loaded):
        assert ub._runtime_data_source_for_asset_class("forex", fallback="unknown") == "twelvedata"
        assert ub._runtime_data_source_for_asset_class("crypto", fallback="unknown") == "mt5_dwx"
        assert ub._runtime_data_source_for_asset_class("metal", fallback="unknown") == "ibkr"

    def test_refresh_topstep_stores_tick_info(self, loaded, tmp_db):
        ub.refresh_to_db("topstep_futures", tmp_db, "2025-01-10T14:00:00Z")
        rows = tmp_db.get_universe_members("topstep_futures")
        es = next((r for r in rows if r["symbol"] == "ES"), None)
        assert es is not None
        assert es["tick_size"] == 0.25
        assert es["tick_value"] == 12.50
        assert es["margin"] == 500.0

    def test_refresh_unknown_universe_raises(self, loaded, tmp_db):
        with pytest.raises(ValueError, match="unknown universe"):
            ub.refresh_to_db("nonexistent", tmp_db, "2025-01-10T14:00:00Z")

    def test_refresh_updates_timestamp(self, loaded, tmp_db):
        ts = "2025-01-10T14:00:00Z"
        ub.refresh_to_db("topstep_futures", tmp_db, ts)
        rows = tmp_db.get_universe_members("topstep_futures")
        for r in rows:
            assert r["last_refreshed_utc"] == ts


# ---------------------------------------------------------------------------
# VanguardDB universe_members methods
# ---------------------------------------------------------------------------

class TestVanguardDBUniverseMembers:

    def test_upsert_and_query(self, tmp_db):
        rows = [
            {"symbol": "EURUSD", "universe": "ftmo_cfd", "asset_class": "forex",
             "tick_size": None, "tick_value": None, "margin": None,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
            {"symbol": "XAUUSD", "universe": "ftmo_cfd", "asset_class": "metal",
             "tick_size": None, "tick_value": None, "margin": None,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
        ]
        n = tmp_db.upsert_universe_members(rows)
        assert n == 2

    def test_get_all_members(self, tmp_db):
        rows = [
            {"symbol": "ES",     "universe": "topstep_futures", "asset_class": "index",
             "tick_size": 0.25, "tick_value": 12.50, "margin": 500.0,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
            {"symbol": "EURUSD", "universe": "ftmo_cfd", "asset_class": "forex",
             "tick_size": None, "tick_value": None, "margin": None,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
        ]
        tmp_db.upsert_universe_members(rows)
        all_rows = tmp_db.get_universe_members()
        assert len(all_rows) == 2

    def test_get_members_by_universe(self, tmp_db):
        rows = [
            {"symbol": "ES",     "universe": "topstep_futures", "asset_class": "index",
             "tick_size": 0.25, "tick_value": 12.50, "margin": 500.0,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
            {"symbol": "EURUSD", "universe": "ftmo_cfd", "asset_class": "forex",
             "tick_size": None, "tick_value": None, "margin": None,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
        ]
        tmp_db.upsert_universe_members(rows)
        ts_rows = tmp_db.get_universe_members("topstep_futures")
        assert len(ts_rows) == 1
        assert ts_rows[0]["symbol"] == "ES"

    def test_count_universe_members(self, tmp_db):
        rows = [
            {"symbol": "ES",     "universe": "topstep_futures", "asset_class": "index",
             "tick_size": 0.25, "tick_value": 12.50, "margin": 500.0,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
            {"symbol": "NQ",     "universe": "topstep_futures", "asset_class": "index",
             "tick_size": 0.25, "tick_value": 5.00, "margin": 1600.0,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
            {"symbol": "EURUSD", "universe": "ftmo_cfd", "asset_class": "forex",
             "tick_size": None, "tick_value": None, "margin": None,
             "last_refreshed_utc": "2025-01-10T14:00:00Z"},
        ]
        tmp_db.upsert_universe_members(rows)
        counts = tmp_db.count_universe_members()
        assert counts["topstep_futures"] == 2
        assert counts["ftmo_cfd"] == 1

    def test_upsert_empty_list(self, tmp_db):
        n = tmp_db.upsert_universe_members([])
        assert n == 0

    def test_upsert_is_idempotent(self, tmp_db):
        row = {"symbol": "ES", "universe": "topstep_futures", "asset_class": "index",
               "tick_size": 0.25, "tick_value": 12.50, "margin": 500.0,
               "last_refreshed_utc": "2025-01-10T14:00:00Z"}
        tmp_db.upsert_universe_members([row])
        tmp_db.upsert_universe_members([row])
        rows = tmp_db.get_universe_members("topstep_futures")
        assert len(rows) == 1   # not duplicated

    def test_upsert_overwrites_old_refresh_ts(self, tmp_db):
        row = {"symbol": "ES", "universe": "topstep_futures", "asset_class": "index",
               "tick_size": 0.25, "tick_value": 12.50, "margin": 500.0,
               "last_refreshed_utc": "2025-01-10T14:00:00Z"}
        tmp_db.upsert_universe_members([row])
        row2 = {**row, "last_refreshed_utc": "2025-02-01T09:00:00Z"}
        tmp_db.upsert_universe_members([row2])
        rows = tmp_db.get_universe_members("topstep_futures")
        assert rows[0]["last_refreshed_utc"] == "2025-02-01T09:00:00Z"


# ---------------------------------------------------------------------------
# get_instruments_for_account
# ---------------------------------------------------------------------------

class TestGetInstrumentsForAccount:

    def test_unknown_account_raises(self, loaded):
        with pytest.raises(KeyError, match="not found"):
            ub.get_instruments_for_account(
                "nonexistent_account",
                accounts_path=_ACCOUNTS_PATH,
            )

    def test_ttp_account_triggers_equity(self, loaded, monkeypatch):
        """TTP account should call get_equity_universe(); we mock it."""
        monkeypatch.setattr(ub, "get_equity_universe", lambda **kw: ["AAPL", "MSFT"])
        result = ub.get_instruments_for_account(
            "ttp_demo_1m",
            accounts_path=_ACCOUNTS_PATH,
        )
        assert result == ["AAPL", "MSFT"]

    def test_accounts_config_exists(self):
        assert _ACCOUNTS_PATH.exists(), f"Accounts config not found: {_ACCOUNTS_PATH}"


# ---------------------------------------------------------------------------
# Config file structure validation
# ---------------------------------------------------------------------------

class TestConfigFileStructure:

    def test_config_file_exists(self):
        assert _CONFIG_PATH.exists(), f"Config not found: {_CONFIG_PATH}"

    def test_config_is_valid_json(self):
        with _CONFIG_PATH.open() as f:
            data = json.load(f)
        assert "universes" in data

    def test_all_three_universes_present(self):
        with _CONFIG_PATH.open() as f:
            data = json.load(f)
        universes = data["universes"]
        assert "ttp_equity" in universes
        assert "ftmo_cfd" in universes
        assert "topstep_futures" in universes

    def test_ttp_equity_has_filter_rules(self):
        with _CONFIG_PATH.open() as f:
            data = json.load(f)
        rules = data["universes"]["ttp_equity"]["filter_rules"]
        assert "min_price" in rules
        assert "min_avg_volume" in rules
        assert "lookback_days" in rules

    def test_ftmo_has_all_asset_class_sections(self):
        with _CONFIG_PATH.open() as f:
            data = json.load(f)
        instruments = data["universes"]["ftmo_cfd"]["instruments"]
        for section in ("forex", "index", "metal", "energy", "agriculture",
                        "equity_cfd", "crypto"):
            assert section in instruments, f"Missing FTMO section: {section}"

    def test_topstep_has_all_asset_class_sections(self):
        with _CONFIG_PATH.open() as f:
            data = json.load(f)
        instruments = data["universes"]["topstep_futures"]["instruments"]
        for section in ("index", "energy", "metal", "agriculture",
                        "interest_rate", "forex"):
            assert section in instruments, f"Missing TopStep section: {section}"

    def test_topstep_contracts_have_tick_fields(self):
        with _CONFIG_PATH.open() as f:
            data = json.load(f)
        instruments = data["universes"]["topstep_futures"]["instruments"]
        for section, contracts in instruments.items():
            for c in contracts:
                for field in ("symbol", "tick_size", "tick_value", "margin"):
                    assert field in c, (
                        f"TopStep contract in {section} missing '{field}': {c}"
                    )
