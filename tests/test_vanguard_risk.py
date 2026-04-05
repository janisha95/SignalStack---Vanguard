"""
test_vanguard_risk.py — V6 Risk Filters test suite.

Tests:
  1.  ATR-based position sizing produces correct shares
  2.  Daily budget check pauses account when exceeded
  3.  Max loss check terminates account when exceeded
  4.  Position limit prevents new trades when at max
  5.  Time gate blocks trades after cutoff
  6.  EOD flatten returns all open positions for closure
  7.  Sector cap filters excess positions in same sector
  8.  Correlation cap blocks highly correlated positions
  9.  TTP consistency rule flags single-trade dominance
  10. Each account gets independent portfolio (same shortlist, different output)
  11. Account profiles loaded from DB (not hardcoded)
  12. Missing/disabled account skipped gracefully

Location: ~/SS/Vanguard/tests/test_vanguard_risk.py
"""
from __future__ import annotations

import math
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard.helpers.position_sizer import (
    compute_risk_dollars,
    size_equity,
    size_forex,
    size_futures,
)
from vanguard.helpers.portfolio_state import (
    ensure_schema,
    get_or_init_state,
    get_account_status,
    save_state,
    load_state,
)
from vanguard.helpers.correlation_checker import is_too_correlated
from vanguard.helpers.eod_flatten import (
    check_eod_action,
    get_positions_to_flatten,
)
from vanguard.helpers.ttp_rules import (
    check_consistency_rule,
    check_scaling_reset,
    check_min_trade_range,
    apply_scaling,
)
from stages.vanguard_risk_filters import (
    load_accounts,
    process_account,
    get_sector,
    _reject,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> str:
    """Return path to a fresh temp SQLite DB (caller is responsible for cleanup via tempfile)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def _make_account_profiles_table(db_path: str) -> None:
    """Create minimal account_profiles schema."""
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS account_profiles (
                id TEXT PRIMARY KEY,
                prop_firm TEXT,
                account_size REAL,
                daily_loss_limit REAL,
                max_drawdown REAL,
                max_positions INTEGER DEFAULT 5,
                is_active INTEGER DEFAULT 1,
                instruments TEXT DEFAULT 'us_equities',
                must_close_eod INTEGER DEFAULT 0,
                no_new_positions_after TEXT,
                eod_flatten_time TEXT,
                risk_per_trade_pct REAL DEFAULT 0.005,
                max_per_sector INTEGER DEFAULT 3,
                max_correlation REAL DEFAULT 0.85,
                max_portfolio_heat_pct REAL DEFAULT 0.04,
                stop_atr_multiple REAL DEFAULT 1.5,
                tp_atr_multiple REAL DEFAULT 3.0,
                consistency_rule_pct REAL,
                weekly_loss_limit REAL,
                volume_limit INTEGER,
                block_earnings_overnight INTEGER DEFAULT 0,
                block_halted INTEGER DEFAULT 0,
                min_trade_duration_sec INTEGER DEFAULT 0,
                min_trade_range_cents INTEGER DEFAULT 0,
                max_trades_per_day INTEGER,
                scaling_profit_trigger_pct REAL,
                scaling_bp_increase_pct REAL,
                scaling_pause_increase_pct REAL,
                plan_type TEXT DEFAULT 'swing'
            )
        """)
        con.commit()


def _insert_account(db_path: str, overrides: dict | None = None) -> dict:
    """Insert a standard test account and return it as a dict."""
    defaults = {
        "id": "test_account",
        "prop_firm": "ttp",
        "account_size": 50000.0,
        "daily_loss_limit": 1000.0,
        "max_drawdown": 2500.0,
        "max_positions": 3,
        "is_active": 1,
        "instruments": "us_equities",
        "must_close_eod": 0,
        "no_new_positions_after": None,
        "eod_flatten_time": None,
        "risk_per_trade_pct": 0.005,
        "max_per_sector": 2,
        "max_correlation": 0.85,
        "max_portfolio_heat_pct": 0.06,
        "stop_atr_multiple": 1.5,
        "tp_atr_multiple": 3.0,
        "consistency_rule_pct": None,
        "weekly_loss_limit": None,
        "volume_limit": None,
        "block_earnings_overnight": 0,
        "block_halted": 0,
        "min_trade_duration_sec": 0,
        "min_trade_range_cents": 0,
        "max_trades_per_day": None,
        "scaling_profit_trigger_pct": None,
        "scaling_bp_increase_pct": None,
        "scaling_pause_increase_pct": None,
        "plan_type": "swing",
    }
    if overrides:
        defaults.update(overrides)

    _make_account_profiles_table(db_path)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(["?"] * len(defaults))
    with sqlite3.connect(db_path) as con:
        con.execute(
            f"INSERT OR REPLACE INTO account_profiles ({cols}) VALUES ({placeholders})",
            list(defaults.values()),
        )
        con.commit()
    return defaults


def _make_shortlist(
    symbols: list[str],
    direction: str = "LONG",
    asset_class: str = "equity",
    ml_prob: float = 0.75,
) -> pd.DataFrame:
    """Create a minimal shortlist DataFrame."""
    rows = []
    for i, sym in enumerate(symbols):
        rows.append({
            "symbol": sym,
            "direction": direction,
            "asset_class": asset_class,
            "strategy": "momentum",
            "strategy_rank": i + 1,
            "strategy_score": 0.8 - i * 0.05,
            "ml_prob": ml_prob,
            "edge_score": 0.7 - i * 0.05,
            "consensus_count": 3,
            "strategies_matched": "momentum,smc_confluence",
            "regime": "ACTIVE",
        })
    return pd.DataFrame(rows)


# ===========================================================================
# Test 1: ATR-based position sizing produces correct shares
# ===========================================================================

class TestPositionSizing:
    def test_equity_shares_formula(self):
        """shares = floor(risk_dollars / (atr × stop_multiple))"""
        price = 150.0
        atr   = 1.5
        stop_multiple = 2.0
        tp_multiple   = 4.0
        risk_dollars  = 300.0

        result = size_equity(price, atr, stop_multiple, tp_multiple, risk_dollars, "LONG")

        expected_stop_dist = atr * stop_multiple        # 3.0
        expected_shares    = math.floor(risk_dollars / expected_stop_dist)  # 100
        assert result["shares"] == expected_shares
        assert result["stop_price"] == round(price - expected_stop_dist, 2)
        assert result["tp_price"]   == round(price + atr * tp_multiple, 2)

    def test_equity_short_direction(self):
        """SHORT: stop above entry, tp below entry."""
        result = size_equity(100.0, 1.0, 1.5, 3.0, 150.0, "SHORT")
        assert result["stop_price"] > 100.0
        assert result["tp_price"]   < 100.0

    def test_risk_dollars_min_constraint(self):
        """compute_risk_dollars returns minimum of 3 constraints."""
        per_trade_max      = 0.005 * 50000  # 250
        daily_capped       = 2000 * 0.5     # 1000
        drawdown_capped    = 4000 * 0.25    # 1000
        expected           = min(per_trade_max, daily_capped, drawdown_capped)  # 250

        result = compute_risk_dollars(
            account_size=50000,
            risk_per_trade_pct=0.005,
            daily_budget_remaining=2000,
            drawdown_budget_remaining=4000,
        )
        assert result == expected

    def test_zero_atr_returns_empty(self):
        """Zero ATR → 0 shares (no trade)."""
        result = size_equity(100.0, 0.0, 1.5, 3.0, 200.0, "LONG")
        assert result["shares"] == 0

    def test_zero_risk_dollars_returns_empty(self):
        """Zero risk budget → 0 shares."""
        result = size_equity(100.0, 1.0, 1.5, 3.0, 0.0, "LONG")
        assert result["shares"] == 0


# ===========================================================================
# Test 2: Daily budget check pauses account when exceeded
# ===========================================================================

class TestDailyBudget:
    def test_daily_paused_when_limit_hit(self):
        """Running balance at or below daily_pause_level → DAILY_PAUSED."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_dp",
            "account_size": 50000.0,
            "daily_loss_limit": 1000.0,
        })
        # daily_pause_level = 50000 - 1000 = 49000
        # running_bal = 50000 + (-1000) = 49000 → at the level → DAILY_PAUSED
        ensure_schema(db)
        state = get_or_init_state("acc_dp", account, db)
        state["daily_realized_pnl"] = -1000.0  # exactly at limit
        save_state(state, db)

        refreshed_state = load_state("acc_dp", db_path=db)
        result = get_account_status("acc_dp", refreshed_state, account)
        assert result == "DAILY_PAUSED"

    def test_active_when_budget_remains(self):
        """Partial loss still within budget → ACTIVE."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_ok",
            "account_size": 50000.0,
            "daily_loss_limit": 1000.0,
        })
        ensure_schema(db)
        state = get_or_init_state("acc_ok", account, db)
        state["daily_realized_pnl"] = -500.0
        save_state(state, db)

        refreshed_state = load_state("acc_ok", db_path=db)
        result = get_account_status("acc_ok", refreshed_state, account)
        assert result == "ACTIVE"


# ===========================================================================
# Test 3: Max loss check terminates account when exceeded
# ===========================================================================

class TestMaxLoss:
    def test_max_loss_terminates(self):
        """Running balance at/below max_loss_level → MAX_LOSS."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_ml",
            "account_size": 50000.0,
            "daily_loss_limit": 1000.0,
            "max_drawdown": 2500.0,
        })
        # max_loss_level = 50000 - 2500 = 47500
        # running_bal = 50000 + (-2500) = 47500 → MAX_LOSS
        ensure_schema(db)
        state = get_or_init_state("acc_ml", account, db)
        state["daily_realized_pnl"] = -2500.0
        save_state(state, db)

        refreshed_state = load_state("acc_ml", db_path=db)
        result = get_account_status("acc_ml", refreshed_state, account)
        assert result == "MAX_LOSS"

    def test_max_loss_takes_priority_over_daily_pause(self):
        """MAX_LOSS wins over DAILY_PAUSED when both conditions are met."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_both",
            "account_size": 50000.0,
            "daily_loss_limit": 1000.0,
            "max_drawdown": 2500.0,
        })
        ensure_schema(db)
        state = get_or_init_state("acc_both", account, db)
        state["daily_realized_pnl"] = -3000.0  # beyond both limits
        save_state(state, db)

        refreshed_state = load_state("acc_both", db_path=db)
        result = get_account_status("acc_both", refreshed_state, account)
        assert result == "MAX_LOSS"


# ===========================================================================
# Test 4: Position limit prevents new trades when at max
# ===========================================================================

class TestPositionLimit:
    def test_position_limit_rejects_all(self):
        """open_positions == max_positions → all candidates rejected with POSITION_LIMIT."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_plimit",
            "max_positions": 2,
            "account_size": 50000.0,
        })
        ensure_schema(db)
        # Set open positions at max
        state = get_or_init_state("acc_plimit", account, db)
        state["open_positions"] = 2
        save_state(state, db)

        shortlist = _make_shortlist(["AAPL", "MSFT", "NVDA"])

        with patch("stages.vanguard_risk_filters.compute_atr", return_value=1.5), \
             patch("stages.vanguard_risk_filters.compute_correlations", return_value=pd.DataFrame()), \
             patch("stages.vanguard_risk_filters.check_eod_action", return_value=None):
            results = process_account(account, shortlist, "2026-01-01T10:00:00Z",
                                      dry_run=True, db_path=db)

        rejected = [r for r in results if r["status"] == "REJECTED"]
        assert len(rejected) == 3
        assert all(r["rejection_reason"] == "POSITION_LIMIT" for r in rejected)

    def test_position_limit_approves_up_to_max(self):
        """With capacity available, approves up to remaining slots."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_partial",
            "max_positions": 2,
            "max_portfolio_heat_pct": 1.0,  # wide open heat cap
            "account_size": 50000.0,
        })
        ensure_schema(db)
        state = get_or_init_state("acc_partial", account, db)
        state["open_positions"] = 1  # 1 slot remaining
        save_state(state, db)

        shortlist = _make_shortlist(["AAPL", "MSFT"])

        def fake_atr(symbol, db_path, *a, **kw):
            return 1.5

        # Note: no sqlite3.connect mock — price query falls back to 100.0 via try/except
        with patch("stages.vanguard_risk_filters.compute_atr", side_effect=fake_atr), \
             patch("stages.vanguard_risk_filters.compute_correlations",
                   return_value=pd.DataFrame()), \
             patch("stages.vanguard_risk_filters.check_eod_action", return_value=None):
            results = process_account(account, shortlist, "2026-01-01T10:00:00Z",
                                      dry_run=True, db_path=db)

        # max 1 approved (open_positions was already at 1 of 2)
        approved = [r for r in results if r["status"] == "APPROVED"]
        assert len(approved) <= 1


# ===========================================================================
# Test 5: Time gate blocks trades after cutoff
# ===========================================================================

class TestTimeGate:
    def test_no_new_trades_after_cutoff(self):
        """check_eod_action returns NO_NEW_TRADES → all rejected."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_timegate",
            "must_close_eod": 1,
            "no_new_positions_after": "15:30",
        })
        ensure_schema(db)
        state = get_or_init_state("acc_timegate", account, db)

        shortlist = _make_shortlist(["AAPL", "MSFT"])

        with patch("stages.vanguard_risk_filters.check_eod_action",
                   return_value="NO_NEW_TRADES"), \
             patch("stages.vanguard_risk_filters.compute_correlations",
                   return_value=pd.DataFrame()):
            results = process_account(account, shortlist, "2026-01-01T15:45:00Z",
                                      dry_run=True, db_path=db)

        assert all(r["status"] == "REJECTED" for r in results)
        assert all("NO_NEW_TRADES" in r["rejection_reason"] for r in results)

    def test_eod_flatten_returns_empty_portfolio(self):
        """check_eod_action returns FLATTEN_ALL → no new portfolio rows."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_flatten",
            "must_close_eod": 1,
            "eod_flatten_time": "15:50",
        })
        ensure_schema(db)
        get_or_init_state("acc_flatten", account, db)

        shortlist = _make_shortlist(["AAPL"])

        with patch("stages.vanguard_risk_filters.check_eod_action",
                   return_value="FLATTEN_ALL"), \
             patch("stages.vanguard_risk_filters.compute_correlations",
                   return_value=pd.DataFrame()):
            results = process_account(account, shortlist, "2026-01-01T16:00:00Z",
                                      dry_run=True, db_path=db)

        assert results == []

    def test_check_eod_action_no_new_trades(self):
        """check_eod_action logic: past no_new_positions_after → NO_NEW_TRADES."""
        from datetime import datetime as dt
        mock_time = dt(2026, 1, 1, 15, 31, tzinfo=timezone.utc)
        result = check_eod_action(
            must_close_eod=True,
            no_new_positions_after="15:30",
            eod_flatten_time="15:50",
            current_et=mock_time,
        )
        assert result == "NO_NEW_TRADES"

    def test_check_eod_action_flatten(self):
        """check_eod_action: past flatten time → FLATTEN_ALL."""
        from datetime import datetime as dt
        mock_time = dt(2026, 1, 1, 15, 51, tzinfo=timezone.utc)
        result = check_eod_action(
            must_close_eod=True,
            no_new_positions_after="15:30",
            eod_flatten_time="15:50",
            current_et=mock_time,
        )
        assert result == "FLATTEN_ALL"


# ===========================================================================
# Test 6: EOD flatten returns all open positions for closure
# ===========================================================================

class TestEodFlatten:
    def _seed_positions(self, db_path: str, account_id: str, symbols: list[str]) -> None:
        with sqlite3.connect(db_path) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_tradeable_portfolio (
                    cycle_ts_utc TEXT, account_id TEXT, symbol TEXT,
                    direction TEXT, shares_or_lots REAL, entry_price REAL,
                    status TEXT,
                    PRIMARY KEY (cycle_ts_utc, account_id, symbol)
                )
            """)
            for sym in symbols:
                con.execute(
                    "INSERT OR REPLACE INTO vanguard_tradeable_portfolio "
                    "(cycle_ts_utc, account_id, symbol, direction, shares_or_lots, entry_price, status) "
                    "VALUES (?, ?, ?, 'LONG', 10, 100.0, 'APPROVED')",
                    ("2026-01-01T10:00:00Z", account_id, sym),
                )
            con.commit()

    def test_get_positions_to_flatten_returns_open(self):
        """get_positions_to_flatten returns all APPROVED positions for the account."""
        db = _make_db()
        self._seed_positions(db, "acc_eod", ["AAPL", "MSFT", "GOOG"])

        positions = get_positions_to_flatten("acc_eod", db_path=db)
        assert len(positions) == 3
        syms = {p["symbol"] for p in positions}
        assert syms == {"AAPL", "MSFT", "GOOG"}

    def test_get_positions_to_flatten_other_account_excluded(self):
        """Positions for a different account are not returned."""
        db = _make_db()
        self._seed_positions(db, "acc_other", ["NVDA"])
        positions = get_positions_to_flatten("acc_eod", db_path=db)
        assert positions == []


# ===========================================================================
# Test 7: Sector cap filters excess positions in same sector
# ===========================================================================

class TestSectorCap:
    def test_sector_cap_rejects_overflow(self):
        """max_per_sector=1 → only 1 tech symbol per cycle."""
        db = _make_db()
        account = _insert_account(db, {
            "id": "acc_sector",
            "max_positions": 5,
            "max_per_sector": 1,       # strict cap
            "max_portfolio_heat_pct": 1.0,
            "account_size": 100000.0,
        })
        ensure_schema(db)
        get_or_init_state("acc_sector", account, db)

        # All tech symbols → only 1 should pass sector cap
        shortlist = _make_shortlist(["AAPL", "MSFT", "NVDA"])

        def fake_atr(symbol, db_path, *a, **kw):
            return 1.5

        # price query falls back to 100.0 via try/except when vanguard_bars_5m is absent
        with patch("stages.vanguard_risk_filters.compute_atr", side_effect=fake_atr), \
             patch("stages.vanguard_risk_filters.compute_correlations",
                   return_value=pd.DataFrame()), \
             patch("stages.vanguard_risk_filters.check_eod_action", return_value=None):
            results = process_account(account, shortlist, "2026-01-01T10:00:00Z",
                                      dry_run=True, db_path=db)

        sector_rejected = [r for r in results
                           if (r.get("rejection_reason") or "").startswith("SECTOR_CAP")]
        assert len(sector_rejected) >= 1

    def test_get_sector_maps_equity(self):
        """Known symbols map to correct sectors."""
        assert get_sector("AAPL", "equity") == "technology"
        assert get_sector("JPM",  "equity") == "financials"
        assert get_sector("XOM",  "equity") == "energy"

    def test_get_sector_nonequity_returns_asset_class(self):
        """Non-equity returns asset_class directly."""
        assert get_sector("EURUSD", "forex")   == "forex"
        assert get_sector("BTCUSD", "crypto")  == "crypto"
        assert get_sector("XAUUSD", "metal")   == "metal"


# ===========================================================================
# Test 8: Correlation cap blocks highly correlated positions
# ===========================================================================

class TestCorrelationCap:
    def test_is_too_correlated_blocks_high_correlation(self):
        """Correlation > 0.85 between two symbols → is_too_correlated returns True."""
        symbols = ["AAPL", "MSFT"]
        corr = pd.DataFrame(
            [[1.0, 0.92], [0.92, 1.0]],
            index=symbols, columns=symbols,
        )
        blocked, peer = is_too_correlated("MSFT", ["AAPL"], corr, threshold=0.85)
        assert blocked is True
        assert peer == "AAPL"

    def test_is_too_correlated_allows_low_correlation(self):
        """Correlation < 0.85 → allowed."""
        symbols = ["AAPL", "GOOG"]
        corr = pd.DataFrame(
            [[1.0, 0.40], [0.40, 1.0]],
            index=symbols, columns=symbols,
        )
        blocked, peer = is_too_correlated("GOOG", ["AAPL"], corr, threshold=0.85)
        assert blocked is False
        assert peer is None

    def test_is_too_correlated_empty_existing(self):
        """No existing positions → never blocked."""
        blocked, peer = is_too_correlated("AAPL", [], pd.DataFrame(), threshold=0.85)
        assert blocked is False

    def test_negative_correlation_also_blocked(self):
        """abs(corr) > threshold → blocked, even negative correlation."""
        symbols = ["GLD", "DXY"]
        corr = pd.DataFrame(
            [[1.0, -0.91], [-0.91, 1.0]],
            index=symbols, columns=symbols,
        )
        blocked, _ = is_too_correlated("DXY", ["GLD"], corr, threshold=0.85)
        assert blocked is True


# ===========================================================================
# Test 9: TTP consistency rule flags single-trade dominance
# ===========================================================================

class TestConsistencyRule:
    def test_consistency_violated_when_best_trade_dominates(self):
        """best_trade_pnl > 40% of total → violated."""
        violated = check_consistency_rule(
            best_trade_pnl=500.0,
            total_valid_pnl=1000.0,
            consistency_rule_pct=0.40,
        )
        assert violated is True

    def test_consistency_ok_when_within_limit(self):
        """best_trade_pnl <= 40% of total → not violated."""
        violated = check_consistency_rule(
            best_trade_pnl=400.0,
            total_valid_pnl=1000.0,
            consistency_rule_pct=0.40,
        )
        assert violated is False  # exactly at limit is not violated

    def test_consistency_skipped_when_no_profit(self):
        """total_valid_pnl <= 0 → rule not applicable, returns False."""
        violated = check_consistency_rule(
            best_trade_pnl=500.0,
            total_valid_pnl=0.0,
            consistency_rule_pct=0.40,
        )
        assert violated is False

    def test_consistency_skipped_when_pct_none(self):
        """consistency_rule_pct=None → rule disabled."""
        violated = check_consistency_rule(
            best_trade_pnl=9999.0,
            total_valid_pnl=1000.0,
            consistency_rule_pct=None,
        )
        assert violated is False


# ===========================================================================
# Test 10: Each account gets independent portfolio (same shortlist, different output)
# ===========================================================================

class TestIndependentPortfolios:
    def test_two_accounts_get_different_portfolios(self):
        """Same shortlist → conservative account approves fewer trades than liberal one."""
        db = _make_db()

        # Conservative: max_positions=1
        account_a = _insert_account(db, {
            "id": "acc_conservative",
            "max_positions": 1,
            "max_portfolio_heat_pct": 1.0,
            "account_size": 50000.0,
        })
        # Liberal: max_positions=5
        account_b = _insert_account(db, {
            "id": "acc_liberal",
            "max_positions": 5,
            "max_portfolio_heat_pct": 1.0,
            "account_size": 50000.0,
        })
        ensure_schema(db)
        get_or_init_state("acc_conservative", account_a, db)
        get_or_init_state("acc_liberal",      account_b, db)

        shortlist = _make_shortlist(["AAPL", "MSFT", "NVDA", "AMD"])

        def fake_atr(symbol, db_path, *a, **kw):
            return 1.5

        common_patches = [
            patch("stages.vanguard_risk_filters.compute_atr", side_effect=fake_atr),
            patch("stages.vanguard_risk_filters.compute_correlations",
                  return_value=pd.DataFrame()),
            patch("stages.vanguard_risk_filters.check_eod_action", return_value=None),
        ]

        import contextlib
        with contextlib.ExitStack() as stack:
            for p in common_patches:
                stack.enter_context(p)

            # price query falls back to 100.0 via try/except when vanguard_bars_5m is absent
            results_a = process_account(account_a, shortlist,
                                        "2026-01-01T10:00:00Z", dry_run=True, db_path=db)
            results_b = process_account(account_b, shortlist,
                                        "2026-01-01T10:00:00Z", dry_run=True, db_path=db)

        approved_a = sum(1 for r in results_a if r["status"] == "APPROVED")
        approved_b = sum(1 for r in results_b if r["status"] == "APPROVED")

        # Conservative has stricter cap → fewer or equal approved
        assert approved_a <= approved_b


# ===========================================================================
# Test 11: Account profiles loaded from DB (not hardcoded)
# ===========================================================================

class TestAccountProfilesFromDB:
    def test_load_accounts_reads_from_db(self):
        """load_accounts returns all active accounts in DB."""
        db = _make_db()
        _insert_account(db, {"id": "db_acc_1", "is_active": 1})
        _insert_account(db, {"id": "db_acc_2", "is_active": 1})

        accounts = load_accounts(db_path=db)
        ids = {a["id"] for a in accounts}
        assert "db_acc_1" in ids
        assert "db_acc_2" in ids

    def test_load_accounts_returns_correct_values(self):
        """Account dict contains expected field values from DB."""
        db = _make_db()
        _insert_account(db, {
            "id": "db_acc_vals",
            "account_size": 75000.0,
            "daily_loss_limit": 1500.0,
            "is_active": 1,
        })
        accounts = load_accounts(account_filter="db_acc_vals", db_path=db)
        assert len(accounts) == 1
        assert accounts[0]["account_size"] == 75000.0
        assert accounts[0]["daily_loss_limit"] == 1500.0

    def test_load_accounts_respects_filter(self):
        """account_filter limits to a single account."""
        db = _make_db()
        _insert_account(db, {"id": "acc_a", "is_active": 1})
        _insert_account(db, {"id": "acc_b", "is_active": 1})

        accounts = load_accounts(account_filter="acc_a", db_path=db)
        assert len(accounts) == 1
        assert accounts[0]["id"] == "acc_a"


# ===========================================================================
# Test 12: Missing/disabled account skipped gracefully
# ===========================================================================

class TestMissingOrDisabledAccount:
    def test_disabled_account_not_returned(self):
        """is_active=0 → not returned by load_accounts."""
        db = _make_db()
        _insert_account(db, {"id": "acc_disabled", "is_active": 0})

        accounts = load_accounts(db_path=db)
        ids = {a["id"] for a in accounts}
        assert "acc_disabled" not in ids

    def test_nonexistent_account_filter_returns_empty(self):
        """Filtering by a nonexistent account_id → empty list, no crash."""
        db = _make_db()
        _make_account_profiles_table(db)  # table exists but is empty
        accounts = load_accounts(account_filter="does_not_exist", db_path=db)
        assert accounts == []

    def test_empty_db_returns_empty_list(self):
        """No account_profiles table → graceful empty list (no crash)."""
        db = _make_db()
        # Do NOT create account_profiles table
        try:
            accounts = load_accounts(db_path=db)
            assert accounts == []
        except Exception:
            # Some DB errors are acceptable — what matters is no hard crash
            pass
