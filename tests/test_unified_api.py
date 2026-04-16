"""
test_unified_api.py — pytest tests for the SignalStack Unified API.

Tests: adapters, field registry, userviews CRUD, trade desk, reports CRUD,
       filter/sort logic, and FastAPI endpoints via TestClient.

Run:
    cd ~/SS/Vanguard
    python3 -m pytest tests/test_unified_api.py -v

Location: ~/SS/Vanguard/tests/test_unified_api.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path: Path) -> str:
    """Return a Vanguard-safe temp SQLite path that passes DB isolation checks."""
    return str(tmp_path / "test_vanguard_universe.db")


@pytest.fixture()
def meridian_db(tmp_path: Path) -> Path:
    """Minimal Meridian DB with shortlist_daily rows."""
    db_path = tmp_path / "v2_universe.db"
    con = sqlite3.connect(str(db_path))
    con.execute("""
        CREATE TABLE shortlist_daily (
            date TEXT, ticker TEXT, direction TEXT,
            predicted_return REAL, beta REAL, market_component REAL,
            residual_alpha REAL, rank INTEGER, regime TEXT, sector TEXT,
            price REAL, top_shap_factors TEXT, factor_rank REAL,
            tcn_score REAL, final_score REAL, expected_return REAL,
            conviction REAL, alpha REAL,
            PRIMARY KEY (date, ticker)
        )
    """)
    # 3 LONG rows (2 pass tier thresholds, 1 doesn't)
    rows = [
        ("2026-03-29", "AAPL", "LONG", 0.05, 1.1, 0.02, 0.03, 1, "TRENDING",
         "TECH", 175.0, None, 0.90, 0.85, 0.875, None, None, None),
        ("2026-03-29", "MSFT", "LONG", 0.04, 0.9, 0.01, 0.03, 2, "TRENDING",
         "TECH", 410.0, None, 0.88, 0.80, 0.84, None, None, None),
        ("2026-03-29", "LOW_TCN", "LONG", 0.01, 0.5, 0.0, 0.01, 3, "CHOPPY",
         None, 50.0, None, 0.60, 0.40, 0.50, None, None, None),
        # 1 SHORT row passing threshold
        ("2026-03-29", "NVDA", "SHORT", -0.06, 1.5, 0.04, -0.10, 4, "TRENDING",
         "TECH", 900.0, None, 0.95, 0.15, 0.90, None, None, None),
    ]
    con.executemany(
        "INSERT INTO shortlist_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return db_path


@pytest.fixture()
def s1_db_and_conv(tmp_path: Path) -> tuple[Path, Path]:
    """Minimal S1 DB + convergence JSON."""
    db_path = tmp_path / "signalstack_results.db"
    con = sqlite3.connect(str(db_path))
    con.execute("""
        CREATE TABLE scorer_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            direction TEXT,
            strategy TEXT,
            p_tp REAL, nn_p_tp REAL,
            gate_source TEXT,
            scorer_prob REAL, scorer_rank INTEGER,
            regime TEXT, price REAL,
            created_at TEXT, sector TEXT
        )
    """)
    rows = [
        ("2026-03-29", "SHO",  "LONG",  "rct",  0.614, 0.894, "BOTH", 0.73, 1, "TRENDING",    12.50, "2026-03-29T17:00:00Z", None),
        ("2026-03-29", "UAL",  "LONG",  "edge", 0.45,  0.95,  "NN",   0.60, 2, "VOLATILE",    93.54, "2026-03-29T17:00:00Z", None),
        ("2026-03-29", "PAYO", "SHORT", "edge", 0.30,  0.20,  "RF",   0.62, 3, "CHOPPY",      8.10,  "2026-03-29T17:00:00Z", None),
        ("2026-03-29", "LOW_SCORER", "LONG", "mr", 0.30, 0.30, "RF",  0.40, 4, "CHOPPY",     20.00, "2026-03-29T17:00:00Z", None),
    ]
    con.executemany(
        "INSERT INTO scorer_predictions "
        "(run_date,ticker,direction,strategy,p_tp,nn_p_tp,gate_source,"
        "scorer_prob,scorer_rank,regime,price,created_at,sector) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()

    # Convergence JSON
    conv_dir = tmp_path / "evening_results"
    conv_dir.mkdir()
    conv_data = {
        "generated_at": "2026-03-29T17:16:00Z",
        "run_date": "2026-03-29",
        "shortlist": [
            {"ticker": "SHO",  "direction": "LONG",  "n_agree": 2, "avg_score": 0.73,
             "convergence_score": 0.85, "strategies": ["rct", "edge"],
             "rel_volume": 1.4, "p_tp": 0.614, "nn_p_tp": 0.894},
            {"ticker": "UAL",  "direction": "LONG",  "n_agree": 1, "avg_score": 0.60,
             "convergence_score": 0.50, "strategies": ["edge"],
             "rel_volume": 1.1, "p_tp": 0.45, "nn_p_tp": 0.95},
            {"ticker": "PAYO", "direction": "SHORT", "n_agree": 1, "avg_score": 0.62,
             "convergence_score": 0.30, "strategies": ["edge"],
             "rel_volume": 0.9, "p_tp": 0.30, "nn_p_tp": 0.20},
        ],
    }
    conv_file = conv_dir / "convergence_20260329_1716.json"
    conv_file.write_text(json.dumps(conv_data))
    return db_path, conv_dir


# ── Field Registry ─────────────────────────────────────────────────────────────

class TestFieldRegistry:
    def test_registry_is_list(self):
        from vanguard.api.field_registry import FIELD_REGISTRY
        assert isinstance(FIELD_REGISTRY, list)
        assert len(FIELD_REGISTRY) > 20

    def test_all_entries_have_required_keys(self):
        from vanguard.api.field_registry import FIELD_REGISTRY
        required = {"key", "label", "type", "sources", "filterable", "sortable", "groupable", "format"}
        for f in FIELD_REGISTRY:
            missing = required - set(f.keys())
            assert not missing, f"Field {f['key']} missing: {missing}"

    def test_common_fields_in_all_sources(self):
        from vanguard.api.field_registry import FIELD_REGISTRY
        common_keys = {"symbol", "source", "side", "price", "as_of", "tier"}
        all_sources = {"meridian", "s1", "vanguard"}
        for f in FIELD_REGISTRY:
            if f["key"] in common_keys:
                assert set(f["sources"]) == all_sources, f"{f['key']} should be in all sources"

    def test_meridian_native_fields(self):
        from vanguard.api.field_registry import FIELD_REGISTRY
        mer_keys = {f["key"] for f in FIELD_REGISTRY if "meridian" in f["sources"] and f["sources"] == ["meridian"]}
        assert "tcn_score" in mer_keys
        assert "factor_rank" in mer_keys
        assert "final_score" in mer_keys

    def test_s1_native_fields(self):
        from vanguard.api.field_registry import FIELD_REGISTRY
        s1_keys = {f["key"] for f in FIELD_REGISTRY if f["sources"] == ["s1"]}
        assert "p_tp" in s1_keys
        assert "nn_p_tp" in s1_keys
        assert "convergence_score" in s1_keys
        assert "n_strategies_agree" in s1_keys

    def test_by_key_lookup(self):
        from vanguard.api.field_registry import FIELD_REGISTRY_BY_KEY
        assert "symbol" in FIELD_REGISTRY_BY_KEY
        assert FIELD_REGISTRY_BY_KEY["tcn_score"]["label"] == "TCN Score"


# ── Meridian Adapter ──────────────────────────────────────────────────────────

class TestMeridianAdapter:
    def test_returns_empty_if_db_missing(self, tmp_path):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", tmp_path / "nonexistent.db"):
            rows = meridian_adapter.get_candidates()
        assert rows == []

    def test_returns_rows(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            rows = meridian_adapter.get_candidates(trade_date="2026-03-29")
        assert len(rows) == 4

    def test_normalized_row_schema(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            rows = meridian_adapter.get_candidates(trade_date="2026-03-29")
        row = next(r for r in rows if r["symbol"] == "AAPL")
        assert row["source"] == "meridian"
        assert row["side"] == "LONG"
        assert row["row_id"].startswith("meridian:AAPL:LONG:")
        assert row["price"] == 175.0
        assert "tcn_score" in row
        assert "factor_rank" in row
        assert "native" in row

    def test_tier_assignment_long_pass(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            rows = meridian_adapter.get_candidates(trade_date="2026-03-29")
        aapl = next(r for r in rows if r["symbol"] == "AAPL")
        assert aapl["tier"] == "tier_meridian_long"

    def test_tier_assignment_low_tcn_untiered(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            rows = meridian_adapter.get_candidates(trade_date="2026-03-29")
        low = next(r for r in rows if r["symbol"] == "LOW_TCN")
        assert low["tier"] == "meridian_untiered"

    def test_tier_assignment_short_pass(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            rows = meridian_adapter.get_candidates(trade_date="2026-03-29")
        nvda = next(r for r in rows if r["symbol"] == "NVDA")
        assert nvda["tier"] == "tier_meridian_short"

    def test_direction_filter(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            longs  = meridian_adapter.get_candidates(trade_date="2026-03-29", direction="LONG")
            shorts = meridian_adapter.get_candidates(trade_date="2026-03-29", direction="SHORT")
        assert all(r["side"] == "LONG"  for r in longs)
        assert all(r["side"] == "SHORT" for r in shorts)
        assert len(longs) + len(shorts) == 4

    def test_latest_date(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            d = meridian_adapter.get_latest_date()
        assert d == "2026-03-29"

    def test_sector_defaults_to_unknown(self, meridian_db):
        from vanguard.api.adapters import meridian_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db):
            rows = meridian_adapter.get_candidates(trade_date="2026-03-29")
        # LOW_TCN has no sector
        low = next(r for r in rows if r["symbol"] == "LOW_TCN")
        assert low["sector"] == "UNKNOWN"


# ── S1 Adapter ────────────────────────────────────────────────────────────────

class TestS1Adapter:
    def test_returns_empty_if_db_missing(self, tmp_path):
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", tmp_path / "nonexistent.db"):
            rows = s1_adapter.get_candidates()
        assert rows == []

    def test_returns_rows(self, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            rows = s1_adapter.get_candidates(run_date="2026-03-29")
        assert len(rows) >= 3

    def test_normalized_row_schema(self, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            rows = s1_adapter.get_candidates(run_date="2026-03-29")
        row = next(r for r in rows if r["symbol"] == "SHO")
        assert row["source"] == "s1"
        assert row["side"] == "LONG"
        assert "p_tp" in row
        assert "nn_p_tp" in row
        assert "convergence_score" in row
        assert "n_strategies_agree" in row
        assert "volume_ratio" in row
        assert "native" in row

    def test_tier_dual_assigned(self, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            rows = s1_adapter.get_candidates(run_date="2026-03-29")
        sho = next(r for r in rows if r["symbol"] == "SHO")
        # SHO: p_tp=0.614 >= 0.60 AND nn_p_tp=0.894 >= 0.75 → tier_dual
        assert sho["tier"] == "tier_dual"

    def test_tier_nn_assigned(self, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            rows = s1_adapter.get_candidates(run_date="2026-03-29")
        ual = next(r for r in rows if r["symbol"] == "UAL")
        # UAL: nn_p_tp=0.95 >= 0.90 AND not in dual → tier_nn
        assert ual["tier"] == "tier_nn"

    def test_short_tier_assigned(self, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            rows = s1_adapter.get_candidates(run_date="2026-03-29")
        payo = next(r for r in rows if r["symbol"] == "PAYO")
        assert payo["side"] == "SHORT"
        assert payo["tier"] == "tier_s1_short"

    def test_convergence_merged(self, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            rows = s1_adapter.get_candidates(run_date="2026-03-29")
        sho = next(r for r in rows if r["symbol"] == "SHO")
        assert sho["convergence_score"] == pytest.approx(0.85)
        assert sho["n_strategies_agree"] == 2
        assert sho["volume_ratio"] == pytest.approx(1.4)

    def test_direction_filter(self, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api.adapters import s1_adapter
        with patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            longs  = s1_adapter.get_candidates(run_date="2026-03-29", direction="LONG")
            shorts = s1_adapter.get_candidates(run_date="2026-03-29", direction="SHORT")
        assert all(r["side"] == "LONG"  for r in longs)
        assert all(r["side"] == "SHORT" for r in shorts)


# ── Vanguard Adapter ──────────────────────────────────────────────────────────

class TestVanguardAdapter:
    def test_returns_empty_list(self):
        from vanguard.api.adapters import vanguard_adapter
        assert vanguard_adapter.get_candidates() == []

    def test_get_latest_date_returns_none(self):
        from vanguard.api.adapters import vanguard_adapter
        assert vanguard_adapter.get_latest_date() is None

    def test_count_returns_zero(self):
        from vanguard.api.adapters import vanguard_adapter
        assert vanguard_adapter.count_candidates() == 0


# ── Userviews ─────────────────────────────────────────────────────────────────

class TestUserviews:
    @pytest.fixture(autouse=True)
    def patch_db(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            userviews.init_table()
            yield

    def test_seed_system_views(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            userviews.seed_system_views()
            views = userviews.list_views()
        system_views = [v for v in views if v["is_system"] == 1]
        assert len(system_views) == 11

    def test_create_view(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            created = userviews.create_view({
                "name": "Test View",
                "source": "meridian",
                "direction": "LONG",
                "display_mode": "table",
            })
        assert created["name"] == "Test View"
        assert created["is_system"] == 0

    def test_list_views(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            userviews.create_view({"name": "V1", "source": "s1"})
            userviews.create_view({"name": "V2", "source": "meridian"})
            views = userviews.list_views()
        assert len(views) >= 2

    def test_update_view(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            created = userviews.create_view({"name": "Original", "source": "s1"})
            updated = userviews.update_view(created["id"], {"name": "Updated"})
        assert updated["name"] == "Updated"

    def test_delete_user_view(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            created = userviews.create_view({"name": "ToDelete", "source": "s1"})
            success, err = userviews.delete_view(created["id"])
        assert success
        assert err == ""

    def test_delete_system_view_rejected(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            userviews.seed_system_views()
            views = userviews.list_views()
            sys_view = next(v for v in views if v["is_system"] == 1)
            success, err = userviews.delete_view(sys_view["id"])
        assert not success
        assert "system" in err.lower()

    def test_update_nonexistent_returns_none(self, tmp_db):
        from vanguard.api import userviews
        with patch.object(userviews, "VANGUARD_DB", tmp_db):
            result = userviews.update_view("nonexistent-id", {"name": "X"})
        assert result is None


# ── Trade Desk ────────────────────────────────────────────────────────────────

class TestTradeDeskPicks:
    def test_get_picks_today_structure(self, meridian_db, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api import trade_desk
        from vanguard.api.adapters import meridian_adapter, s1_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db), \
             patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            result = trade_desk.get_picks_today("2026-03-29")
        assert "date" in result
        assert "tiers" in result
        assert "total_picks" in result
        assert len(result["tiers"]) == 7

    def test_canonical_tier_order(self, meridian_db, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api import trade_desk
        from vanguard.api.adapters import meridian_adapter, s1_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db), \
             patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            result = trade_desk.get_picks_today("2026-03-29")
        tier_names = [t["tier"] for t in result["tiers"]]
        assert tier_names[0] == "tier_dual"
        assert tier_names[-1] == "tier_meridian_short"

    def test_picks_have_required_fields(self, meridian_db, s1_db_and_conv):
        db_path, conv_dir = s1_db_and_conv
        from vanguard.api import trade_desk
        from vanguard.api.adapters import meridian_adapter, s1_adapter
        with patch.object(meridian_adapter, "MERIDIAN_DB", meridian_db), \
             patch.object(s1_adapter, "S1_DB", db_path), \
             patch.object(s1_adapter, "S1_CONV_DIR", conv_dir):
            result = trade_desk.get_picks_today("2026-03-29")
        for tier in result["tiers"]:
            for pick in tier["picks"]:
                assert "symbol" in pick
                assert "direction" in pick
                assert "shares" in pick
                assert "scores" in pick


class TestTradeDeskExecution:
    def test_execute_no_webhook(self, tmp_db):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value=""):
            trade_desk.init_table()
            results = trade_desk.execute_trades([
                {"symbol": "AAPL", "direction": "LONG", "shares": 100, "tier": "tier_dual"}
            ])
        assert len(results) == 1
        assert results[0]["status"] == "FORWARD_TRACKED"
        assert results[0]["success"] is True

    def test_execute_with_webhook_success(self, tmp_db):
        from vanguard.api import trade_desk
        mock_adapter = MagicMock()
        mock_adapter.send_order.return_value = {
            "success": True, "status_code": 200,
            "response_body": "ok", "latency_ms": 50, "error": None, "payload": {}
        }
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value="https://app.signalstack.com/hook/test"), \
             patch("vanguard.execution.signalstack_adapter.SignalStackAdapter", return_value=mock_adapter):
            trade_desk.init_table()
            results = trade_desk.execute_trades([
                {"symbol": "NVDA", "direction": "SHORT", "shares": 50, "tier": "tier_s1_short"}
            ])
        assert results[0]["success"] is True
        assert results[0]["status"] == "SUBMITTED"
        mock_adapter.send_order.assert_called_once_with(
            symbol="NVDA", direction="SHORT", quantity=50, operation="open"
        )

    def test_execute_with_webhook_failure(self, tmp_db):
        from vanguard.api import trade_desk
        mock_adapter = MagicMock()
        mock_adapter.send_order.return_value = {
            "success": False, "status_code": 500,
            "response_body": "error", "latency_ms": 100,
            "error": "HTTP 500", "payload": {}
        }
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value="https://app.signalstack.com/hook/test"), \
             patch("vanguard.execution.signalstack_adapter.SignalStackAdapter", return_value=mock_adapter):
            trade_desk.init_table()
            results = trade_desk.execute_trades([
                {"symbol": "AAPL", "direction": "LONG", "shares": 100, "tier": "tier_dual"}
            ])
        assert results[0]["success"] is False
        assert results[0]["status"] == "FAILED"

    def test_execution_log_written(self, tmp_db):
        from vanguard.api import trade_desk
        import sqlite3
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value=""):
            trade_desk.init_table()
            trade_desk.execute_trades([
                {"symbol": "TSLA", "direction": "LONG", "shares": 75, "tier": "tier_nn"}
            ])
            # Query directly — get_execution_log filters by local date which may differ
            # from SQLite's UTC datetime('now'); check the table directly instead.
            with sqlite3.connect(str(tmp_db)) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute("SELECT * FROM execution_log").fetchall()
        assert len(rows) >= 1
        assert rows[0]["symbol"] == "TSLA"


# ── Reports ───────────────────────────────────────────────────────────────────

class TestReportsCRUD:
    @pytest.fixture(autouse=True)
    def patch_db(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            reports.init_table()
            yield

    def test_create_report(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            created = reports.create_report({
                "name": "Morning Report",
                "schedule": "0 6 * * *",
                "blocks": [{"type": "text_block", "content": "Good morning!"}],
            })
        assert created["name"] == "Morning Report"
        assert "id" in created

    def test_list_reports(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            reports.create_report({"name": "R1", "schedule": "0 6 * * *", "blocks": []})
            reports.create_report({"name": "R2", "schedule": "0 8 * * *", "blocks": []})
            rpts = reports.list_reports()
        assert len(rpts) >= 2

    def test_update_report(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            created = reports.create_report({"name": "Old", "schedule": "0 6 * * *", "blocks": []})
            updated = reports.update_report(created["id"], {"name": "New"})
        assert updated["name"] == "New"

    def test_delete_report(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            created = reports.create_report({"name": "ToDelete", "schedule": "0 6 * * *", "blocks": []})
            success, err = reports.delete_report(created["id"])
        assert success

    def test_delete_nonexistent(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            success, err = reports.delete_report("does-not-exist")
        assert not success


class TestReportGeneration:
    def test_text_block(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            rpt = {
                "blocks": json.dumps([{"type": "text_block", "content": "Hello from test!"}])
            }
            text = reports.generate_report(rpt, trade_date="2026-03-29")
        assert "Hello from test!" in text

    def test_overlap_block_no_data(self, tmp_db):
        from vanguard.api import reports
        from vanguard.api.adapters import meridian_adapter, s1_adapter
        with patch.object(reports, "VANGUARD_DB", tmp_db), \
             patch.object(meridian_adapter, "MERIDIAN_DB", tmp_db), \
             patch.object(s1_adapter, "S1_DB", tmp_db):
            rpt = {"blocks": json.dumps([{"type": "overlap_block", "label": "Overlap"}])}
            text = reports.generate_report(rpt, trade_date="2026-03-29")
        assert "Overlap" in text

    def test_ai_brief_no_anthropic(self, tmp_db):
        from vanguard.api import reports
        with patch.object(reports, "VANGUARD_DB", tmp_db):
            rpt = {"blocks": json.dumps([{"type": "ai_brief_block", "model": "haiku"}])}
            with patch.dict("sys.modules", {"anthropic": None}):
                text = reports.generate_report(rpt, trade_date="2026-03-29")
        assert "AI Brief" in text


# ── FastAPI Endpoints ─────────────────────────────────────────────────────────

@pytest.fixture()
def api_client(tmp_db, meridian_db, s1_db_and_conv):
    """TestClient with all DB paths patched to temp files."""
    db_path, conv_dir = s1_db_and_conv
    from vanguard.api import unified_api, userviews, trade_desk, reports
    from vanguard.api.adapters import meridian_adapter, s1_adapter

    with patch.object(userviews,         "VANGUARD_DB", tmp_db), \
         patch.object(trade_desk,        "VANGUARD_DB", tmp_db), \
         patch.object(reports,           "VANGUARD_DB", tmp_db), \
         patch.object(meridian_adapter,  "MERIDIAN_DB", meridian_db), \
         patch.object(s1_adapter,        "S1_DB",       db_path), \
         patch.object(s1_adapter,        "S1_CONV_DIR", conv_dir), \
         patch.object(unified_api, "_fetch_risky_health", new=AsyncMock(return_value={
             "reachable": True,
             "config_version": "test-risky-config",
             "checks_enabled": 6,
             "mode": "check_only",
             "raw": {"status": "ok"},
         })), \
         patch.object(unified_api, "_check_trades_with_risky", new=AsyncMock(return_value=[{
             "approved": True,
             "severity": "pass",
             "reasons": [],
             "computed": {},
             "config_version": "test-risky-config",
             "checked_at": "2026-04-08T00:00:00Z",
         }])):
        # Startup event fires once per app instance; explicitly init tables
        # so every test gets a clean, fully-initialized tmp_db regardless of
        # whether the startup event has already fired in a prior test.
        userviews.init_table()
        userviews.seed_system_views()
        trade_desk.init_table()
        reports.init_table()
        with TestClient(unified_api.app) as client:
            yield client


class TestHealthEndpoint:
    def test_health_ok(self, api_client):
        resp = api_client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "sources" in data
        assert "meridian" in data["sources"]
        assert "s1" in data["sources"]
        assert "vanguard" in data["sources"]

    def test_health_sources_structure(self, api_client):
        data = api_client.get("/api/v1/health").json()
        assert "available" in data["sources"]["meridian"]
        assert "available" in data["sources"]["s1"]


class TestRiskyIntegration:
    def test_system_status_includes_risky_block(self, api_client):
        from vanguard.api import unified_api

        risky_status = {
            "reachable": True,
            "config_version": "cfg-123",
            "checks_enabled": 6,
            "mode": "check_only",
        }
        with patch.object(
            unified_api,
            "_get_risky_status_snapshot",
            new=AsyncMock(return_value=risky_status),
        ):
            resp = api_client.get("/api/v1/system/status")

        assert resp.status_code == 200
        assert resp.json()["risky"] == risky_status

    def test_risk_health_proxy_returns_risky_payload(self, api_client):
        from vanguard.api import unified_api
        import httpx

        risky_payload = {"status": "ok", "config_version": "cfg-123"}
        response = httpx.Response(
            200,
            json=risky_payload,
            request=httpx.Request("GET", "http://127.0.0.1:8092/risk/health"),
        )
        with patch.object(
            unified_api,
            "_request_risky",
            new=AsyncMock(return_value=response),
        ):
            resp = api_client.get("/api/v1/risk/health")

        assert resp.status_code == 200
        assert resp.json() == risky_payload

    def test_execute_attaches_risky_verdict(self, api_client):
        from vanguard.api import unified_api, trade_desk

        risky_verdict = {
            "approved": True,
            "severity": "warn",
            "reasons": [{"check": "spread_sanity", "status": "warn", "message": "wide", "override_allowed": False}],
            "computed": {},
            "config_version": "cfg-123",
            "checked_at": "2026-04-08T02:00:00Z",
        }
        execution_result = {
            "approved": [],
            "rejected": [],
            "summary": {"submitted": 1},
            "submitted": 1,
            "failed": 0,
            "results": [{"id": 7, "symbol": "EURUSD", "status": "FORWARD_TRACKED"}],
        }
        with patch.object(
            unified_api,
            "_check_trades_with_risky",
            new=AsyncMock(return_value=[risky_verdict]),
        ), patch.object(
            trade_desk,
            "execute_trades",
            return_value=execution_result,
        ) as execute_mock:
            resp = api_client.post("/api/v1/execute", json={
                "profile_id": "ftmo_100k",
                "trades": [
                    {"symbol": "EURUSD", "direction": "LONG", "shares": 1, "tier": "manual"}
                ],
            })

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["risky_verdict"]["config_version"] == "cfg-123"
        assert payload["risky_verdict"]["severity"] == "warn"
        execute_mock.assert_called_once()

    def test_execute_rejects_when_risky_blocks(self, api_client):
        from vanguard.api import unified_api, trade_desk
        from fastapi.responses import JSONResponse

        block_verdict = {
            "approved": False,
            "severity": "block",
            "reasons": [{"check": "spread_sanity", "status": "block", "message": "blocked", "override_allowed": False}],
            "computed": {},
            "config_version": "cfg-block",
            "checked_at": "2026-04-08T02:00:00Z",
        }
        rejection = JSONResponse(
            status_code=422,
            content={"status": "rejected_by_risky", "verdict": block_verdict},
        )
        with patch.object(
            unified_api,
            "_check_trades_with_risky",
            new=AsyncMock(return_value=rejection),
        ), patch.object(trade_desk, "execute_trades") as execute_mock:
            resp = api_client.post("/api/v1/execute", json={
                "profile_id": "ftmo_100k",
                "trades": [
                    {"symbol": "EURUSD", "direction": "LONG", "shares": 1, "tier": "manual"}
                ],
            })

        assert resp.status_code == 422
        assert resp.json()["status"] == "rejected_by_risky"
        execute_mock.assert_not_called()

    def test_execute_returns_503_when_risky_required_and_unreachable(self, api_client):
        from vanguard.api import unified_api, trade_desk
        from fastapi.responses import JSONResponse

        unreachable = JSONResponse(
            status_code=503,
            content={"error": "risky_unreachable", "message": "connect failed"},
        )
        with patch.object(
            unified_api,
            "_check_trades_with_risky",
            new=AsyncMock(return_value=unreachable),
        ), patch.object(trade_desk, "execute_trades") as execute_mock:
            resp = api_client.post("/api/v1/execute", json={
                "profile_id": "ftmo_100k",
                "trades": [
                    {"symbol": "EURUSD", "direction": "LONG", "shares": 1, "tier": "manual"}
                ],
            })

        assert resp.status_code == 503
        assert resp.json()["error"] == "risky_unreachable"
        execute_mock.assert_not_called()

    def test_execute_proceeds_when_risky_optional(self, api_client):
        from vanguard.api import unified_api, trade_desk

        execution_result = {
            "approved": [],
            "rejected": [],
            "summary": {"submitted": 1},
            "submitted": 1,
            "failed": 0,
            "results": [{"id": 9, "symbol": "EURUSD", "status": "FORWARD_TRACKED"}],
        }
        with patch.dict(os.environ, {"RISKY_REQUIRED": "false"}), patch.object(
            unified_api,
            "_check_trades_with_risky",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            trade_desk,
            "execute_trades",
            return_value=execution_result,
        ):
            resp = api_client.post("/api/v1/execute", json={
                "profile_id": "ftmo_100k",
                "trades": [
                    {"symbol": "EURUSD", "direction": "LONG", "shares": 1, "tier": "manual"}
                ],
            })

        assert resp.status_code == 200
        assert resp.json()["risky_verdict"] is None


class TestFieldRegistryEndpoint:
    def test_returns_fields(self, api_client):
        resp = api_client.get("/api/v1/field-registry")
        assert resp.status_code == 200
        data = resp.json()
        assert "fields" in data
        assert data["count"] == len(data["fields"])
        assert data["count"] > 20


class TestCandidatesEndpoint:
    def test_meridian_source(self, api_client):
        resp = api_client.get("/api/v1/candidates?source=meridian")
        assert resp.status_code == 200
        data = resp.json()
        assert "rows" in data
        assert "total" in data
        assert "field_registry" in data
        assert all(r["source"] == "meridian" for r in data["rows"])

    def test_s1_source(self, api_client):
        resp = api_client.get("/api/v1/candidates?source=s1")
        assert resp.status_code == 200
        data = resp.json()
        assert all(r["source"] == "s1" for r in data["rows"])

    def test_combined_source(self, api_client):
        resp = api_client.get("/api/v1/candidates?source=combined")
        assert resp.status_code == 200
        data = resp.json()
        sources = {r["source"] for r in data["rows"]}
        assert len(sources) > 1  # both meridian and s1

    def test_direction_filter_long(self, api_client):
        resp = api_client.get("/api/v1/candidates?source=combined&direction=LONG")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert all(r["side"] == "LONG" for r in rows)

    def test_direction_filter_short(self, api_client):
        resp = api_client.get("/api/v1/candidates?source=combined&direction=SHORT")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert all(r["side"] == "SHORT" for r in rows)

    def test_pagination(self, api_client):
        resp = api_client.get("/api/v1/candidates?source=combined&page=1&page_size=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["rows"]) <= 2
        assert data["page"] == 1

    def test_sort_by_price_desc(self, api_client):
        resp = api_client.get("/api/v1/candidates?source=meridian&sort=price:desc")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        prices = [r["price"] for r in rows if r["price"] is not None]
        assert prices == sorted(prices, reverse=True)

    def test_filter_by_tcn_score(self, api_client):
        filters = json.dumps({"tcn_score": {"gte": 0.70}})
        resp = api_client.get(f"/api/v1/candidates?source=meridian&filters={filters}")
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert all(r.get("tcn_score", 0) >= 0.70 for r in rows)

    def test_candidate_detail(self, api_client):
        # Get a valid row_id first
        resp = api_client.get("/api/v1/candidates?source=meridian")
        rows = resp.json()["rows"]
        assert rows
        row_id = rows[0]["row_id"]
        detail_resp = api_client.get(f"/api/v1/candidates/{row_id}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["row_id"] == row_id

    def test_candidate_detail_not_found(self, api_client):
        resp = api_client.get("/api/v1/candidates/meridian:FAKE:LONG:2099-01-01")
        assert resp.status_code == 404


class TestUserviewsEndpoint:
    def test_list_userviews(self, api_client):
        resp = api_client.get("/api/v1/userviews")
        assert resp.status_code == 200
        data = resp.json()
        assert "views" in data
        assert data["count"] == len(data["views"])
        # System views should be seeded
        assert data["count"] >= 11

    def test_create_userview(self, api_client):
        resp = api_client.post("/api/v1/userviews", json={
            "name": "My Test View",
            "source": "s1",
            "direction": "LONG",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Test View"

    def test_update_userview(self, api_client):
        create_resp = api_client.post("/api/v1/userviews", json={
            "name": "View to Update", "source": "s1"
        })
        view_id = create_resp.json()["id"]
        update_resp = api_client.put(f"/api/v1/userviews/{view_id}", json={
            "name": "Updated View"
        })
        assert update_resp.status_code == 200
        assert update_resp.json()["name"] == "Updated View"

    def test_delete_user_view(self, api_client):
        create_resp = api_client.post("/api/v1/userviews", json={
            "name": "To Delete", "source": "s1"
        })
        view_id = create_resp.json()["id"]
        del_resp = api_client.delete(f"/api/v1/userviews/{view_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True

    def test_delete_system_view_forbidden(self, api_client):
        views = api_client.get("/api/v1/userviews").json()["views"]
        sys_view = next(v for v in views if v["is_system"] == 1)
        del_resp = api_client.delete(f"/api/v1/userviews/{sys_view['id']}")
        assert del_resp.status_code == 403


class TestPicksEndpoint:
    def test_picks_today_structure(self, api_client):
        resp = api_client.get("/api/v1/picks/today?date=2026-03-29")
        assert resp.status_code == 200
        data = resp.json()
        assert "date" in data
        assert "tiers" in data
        assert "total_picks" in data
        assert len(data["tiers"]) == 7

    def test_picks_tier_names(self, api_client):
        resp = api_client.get("/api/v1/picks/today?date=2026-03-29")
        data = resp.json()
        tier_names = [t["tier"] for t in data["tiers"]]
        expected = [
            "tier_dual", "tier_nn", "tier_rf", "tier_scorer_long",
            "tier_s1_short", "tier_meridian_long", "tier_meridian_short",
        ]
        assert tier_names == expected


class TestExecuteEndpoint:
    def test_execute_no_webhook(self, api_client):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "_get_webhook_url", return_value=""):
            resp = api_client.post("/api/v1/execute", json={
                "profile_id": "ttp_20k_swing",
                "trades": [
                    {"symbol": "AAPL", "direction": "LONG", "shares": 100, "tier": "tier_dual"}
                ]
            })
        assert resp.status_code == 200
        data = resp.json()
        assert "submitted" in data
        assert "results" in data
        assert len(data["results"]) == 1

    def test_execution_log_endpoint(self, api_client):
        resp = api_client.get("/api/v1/execution/log")
        assert resp.status_code == 200
        data = resp.json()
        assert "rows" in data
        assert "count" in data


class TestReportsEndpoint:
    def test_list_reports(self, api_client):
        resp = api_client.get("/api/v1/reports")
        assert resp.status_code == 200
        data = resp.json()
        assert "reports" in data

    def test_create_report(self, api_client):
        resp = api_client.post("/api/v1/reports", json={
            "name": "Test Report",
            "schedule": "0 7 * * *",
            "blocks": [{"type": "text_block", "content": "Hi"}],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Report"

    def test_update_report(self, api_client):
        create_resp = api_client.post("/api/v1/reports", json={
            "name": "Old Name", "schedule": "0 7 * * *", "blocks": []
        })
        rpt_id = create_resp.json()["id"]
        upd_resp = api_client.put(f"/api/v1/reports/{rpt_id}", json={"name": "New Name"})
        assert upd_resp.status_code == 200
        assert upd_resp.json()["name"] == "New Name"

    def test_delete_report(self, api_client):
        create_resp = api_client.post("/api/v1/reports", json={
            "name": "Del Report", "schedule": "0 7 * * *", "blocks": []
        })
        rpt_id = create_resp.json()["id"]
        del_resp = api_client.delete(f"/api/v1/reports/{rpt_id}")
        assert del_resp.status_code == 200

    def test_delete_nonexistent_report(self, api_client):
        resp = api_client.delete("/api/v1/reports/does-not-exist")
        assert resp.status_code == 404


# ── Execution forward tracking ─────────────────────────────────────────────────

class TestExecutionForwardTracking:
    """Test tags/notes on execute + PUT /execution/:id + GET /execution/analytics."""

    def test_execute_with_tags_and_notes(self, tmp_db):
        from vanguard.api import trade_desk
        import sqlite3
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value=""):
            trade_desk.init_table()
            results = trade_desk.execute_trades([{
                "symbol": "TRNO", "direction": "LONG", "shares": 100,
                "tier": "tier_meridian_long",
                "tags": ["tcn", "alpha"],
                "notes": "Strong TCN + FR alignment",
                "entry_price": 60.71,
                "stop_loss": 59.49,
                "take_profit": 63.15,
                "order_type": "limit",
                "limit_buffer": 0.01,
            }])
        assert results[0]["id"] is not None
        with sqlite3.connect(str(tmp_db)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM execution_log WHERE id = ?", (results[0]["id"],)).fetchone()
        assert row["tags"] == '["tcn", "alpha"]'
        assert row["notes"] == "Strong TCN + FR alignment"
        assert row["entry_price"] == 60.71
        assert row["stop_loss"] == 59.49
        assert row["take_profit"] == 63.15
        assert row["order_type"] == "limit"
        assert row["limit_buffer"] == 0.01

    def test_execute_returns_row_id(self, tmp_db):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value=""):
            trade_desk.init_table()
            r1 = trade_desk.execute_trades([{"symbol": "A", "direction": "LONG", "shares": 100, "tier": "t"}])
            r2 = trade_desk.execute_trades([{"symbol": "B", "direction": "LONG", "shares": 100, "tier": "t"}])
        assert r1[0]["id"] == 1
        assert r2[0]["id"] == 2

    def test_update_execution_outcome(self, tmp_db):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value=""):
            trade_desk.init_table()
            result = trade_desk.execute_trades([{
                "symbol": "TSLA", "direction": "LONG", "shares": 100, "tier": "t",
                "tags": ["tcn"], "notes": "test",
            }])
            row_id = result[0]["id"]
            updated = trade_desk.update_execution(row_id, {
                "exit_price": 250.0, "exit_date": "2026-04-03",
                "pnl_dollars": 200.0, "pnl_pct": 2.5,
                "outcome": "WIN", "days_held": 3,
            })
        assert updated["outcome"] == "WIN"
        assert updated["exit_price"] == 250.0
        assert updated["pnl_pct"] == 2.5
        assert updated["days_held"] == 3
        # Original tags preserved
        assert updated["tags"] == '["tcn"]'

    def test_update_execution_not_found(self, tmp_db):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db):
            trade_desk.init_table()
            result = trade_desk.update_execution(9999, {"outcome": "WIN"})
        assert result is None

    def test_update_execution_tags(self, tmp_db):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value=""):
            trade_desk.init_table()
            result = trade_desk.execute_trades([{"symbol": "X", "direction": "LONG", "shares": 100, "tier": "t"}])
            row_id = result[0]["id"]
            updated = trade_desk.update_execution(row_id, {"tags": ["alpha", "convergence"]})
        import json
        assert json.loads(updated["tags"]) == ["alpha", "convergence"]

    def test_get_analytics_empty(self, tmp_db):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db):
            trade_desk.init_table()
            analytics = trade_desk.get_analytics(period="all")
        assert "by_tag" in analytics
        assert "by_tier" in analytics
        assert "by_source" in analytics
        assert analytics["total_rows"] == 0

    def test_get_analytics_with_data(self, tmp_db):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", tmp_db), \
             patch.object(trade_desk, "_get_webhook_url", return_value=""):
            trade_desk.init_table()
            r = trade_desk.execute_trades([{
                "symbol": "TRNO", "direction": "LONG", "shares": 100,
                "tier": "tier_meridian_long", "tags": ["tcn", "alpha"],
            }])
            trade_desk.update_execution(r[0]["id"], {"outcome": "WIN", "pnl_pct": 2.5})
            r2 = trade_desk.execute_trades([{
                "symbol": "SHO", "direction": "LONG", "shares": 100,
                "tier": "tier_dual", "tags": ["dual"],
            }])
            trade_desk.update_execution(r2[0]["id"], {"outcome": "LOSE", "pnl_pct": -1.2})
            analytics = trade_desk.get_analytics(period="all")
        assert analytics["total_rows"] == 2
        assert "tier_meridian_long" in analytics["by_tier"]
        assert analytics["by_tier"]["tier_meridian_long"]["wins"] == 1
        assert analytics["by_tier"]["tier_dual"]["losses"] == 1
        assert "tcn" in analytics["by_tag"]
        assert analytics["by_tag"]["tcn"]["win_rate"] == 1.0
        assert "meridian" in analytics["by_source"]

    def test_migration_adds_columns_to_existing_table(self, tmp_path):
        """init_table() must add new columns to a pre-existing table that lacks them."""
        import sqlite3
        old_db = tmp_path / "old.db"
        with sqlite3.connect(str(old_db)) as con:
            con.execute("""
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    executed_at TEXT, symbol TEXT, direction TEXT,
                    tier TEXT, shares INTEGER, status TEXT
                )
            """)
            con.commit()
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "VANGUARD_DB", old_db):
            trade_desk.init_table()
            # All new columns must now exist
            with sqlite3.connect(str(old_db)) as con:
                cols = {r[1] for r in con.execute("PRAGMA table_info(execution_log)").fetchall()}
        for col in ("tags", "notes", "stop_loss", "take_profit", "order_type", "limit_buffer", "exit_price", "outcome", "days_held"):
            assert col in cols, f"Missing column: {col}"


class TestExecutionForwardTrackingEndpoints:
    """Integration tests for PUT /execution/:id and GET /execution/analytics."""

    def test_put_execution_updates_outcome(self, api_client):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "_get_webhook_url", return_value=""):
            exec_resp = api_client.post("/api/v1/execute", json={
                "profile_id": "ttp_20k_swing",
                "trades": [{"symbol": "AAPL", "direction": "LONG", "shares": 100,
                            "tier": "tier_meridian_long", "tags": ["tcn"]}]
            })
        row_id = exec_resp.json()["results"][0]["id"]
        assert row_id is not None

        upd_resp = api_client.put(f"/api/v1/execution/{row_id}", json={
            "exit_price": 225.0, "pnl_pct": 3.5, "outcome": "WIN", "days_held": 4
        })
        assert upd_resp.status_code == 200
        data = upd_resp.json()
        assert data["outcome"] == "WIN"
        assert data["pnl_pct"] == 3.5

    def test_put_execution_not_found(self, api_client):
        resp = api_client.put("/api/v1/execution/9999", json={"outcome": "WIN"})
        assert resp.status_code == 404

    def test_analytics_endpoint_structure(self, api_client):
        resp = api_client.get("/api/v1/execution/analytics?period=all")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_tag" in data
        assert "by_tier" in data
        assert "by_source" in data
        assert "period" in data
        assert "total_rows" in data

    def test_analytics_period_params(self, api_client):
        for period in ("last_7_days", "last_30_days", "last_90_days", "all"):
            resp = api_client.get(f"/api/v1/execution/analytics?period={period}")
            assert resp.status_code == 200
            assert resp.json()["period"] == period

    def test_execution_log_outcome_filter(self, api_client):
        from vanguard.api import trade_desk
        with patch.object(trade_desk, "_get_webhook_url", return_value=""):
            r = api_client.post("/api/v1/execute", json={
                "profile_id": "ttp_20k_swing",
                "trades": [{"symbol": "MSFT", "direction": "LONG", "shares": 100, "tier": "t"}]
            })
        row_id = r.json()["results"][0]["id"]
        api_client.put(f"/api/v1/execution/{row_id}", json={"outcome": "WIN"})

        # Filter by outcome=WIN — note: uses UTC date for executed_at
        import sqlite3
        from vanguard.api import trade_desk as td
        with sqlite3.connect(str(td.VANGUARD_DB)) as con:
            utc_date = con.execute(
                "SELECT DATE(executed_at) FROM execution_log WHERE id = ?", (row_id,)
            ).fetchone()[0]

        resp = api_client.get(f"/api/v1/execution/log?date={utc_date}&outcome=WIN")
        assert resp.status_code == 200
        data = resp.json()
        assert any(r["outcome"] == "WIN" for r in data["rows"])
