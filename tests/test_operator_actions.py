from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.position_manager_cli import _print_snapshot
from vanguard.execution import operator_actions as operator_actions_module
from vanguard.execution.forward_checkpoints import ensure_table as ensure_forward_checkpoints
from vanguard.execution.operator_actions import get_service
from vanguard.execution.trade_journal import ensure_table as ensure_trade_journal
from vanguard.execution.trade_journal import insert_approval_row, mark_open_from_operator


class FakeExecutor:
    def __init__(self, positions: dict[str, dict] | None = None, *, snapshot_meta: dict | None = None) -> None:
        self.positions = positions or {}
        self.snapshot_meta = snapshot_meta or {
            "source": "fake",
            "path": "/tmp/fake",
            "mtime_utc": "2026-04-15T04:05:00Z",
            "age_seconds": 0.0,
            "is_fresh": True,
            "reason": "",
        }
        self.account_info = {
            "balance": 10000.0,
            "equity": 10012.0,
            "free_margin": 9800.0,
            "leverage": 100,
        }

    def connect(self) -> bool:
        return True

    def get_open_positions(self) -> list[dict]:
        return list(self.positions.values())

    def get_positions_snapshot_meta(self, max_age_seconds: float = 90.0) -> dict:
        meta = dict(self.snapshot_meta)
        if "is_fresh" not in meta:
            meta["is_fresh"] = True
        return meta

    def get_account_info(self) -> dict:
        return dict(self.account_info)

    def execute_trade(self, symbol: str, direction: str, lot_size: float, stop_loss: float | None = None, take_profit: float | None = None) -> dict:
        ticket = f"ticket-{len(self.positions) + 1}"
        position = {
            "ticket": ticket,
            "broker_position_id": ticket,
            "symbol": symbol,
            "side": direction,
            "qty": float(lot_size),
            "entry_price": 1.1002 if "JPY" not in symbol else 155.123,
            "current_sl": float(stop_loss or 0.0),
            "current_tp": float(take_profit or 0.0),
            "pnl": 0.0,
        }
        self.positions[ticket] = position
        return {
            "status": "filled",
            "position_id": ticket,
            "orderId": ticket,
            "id": ticket,
            "price": position["entry_price"],
            "symbol": symbol,
        }

    def close_position(self, position_id: str, symbol: str = "", reason: str = "") -> dict:
        if position_id not in self.positions:
            return {"status": "failed", "error": "not_found", "position_id": position_id}
        self.positions.pop(position_id, None)
        return {"status": "closed", "position_id": position_id, "symbol": symbol}

    def modify_position(self, ticket: str, sl: float = 0, tp: float = 0) -> dict:
        pos = self.positions.get(ticket)
        if not pos:
            return {"status": "failed", "error": "not_found", "ticket": ticket}
        pos["current_sl"] = float(sl)
        pos["current_tp"] = float(tp)
        return {"status": "modified", "ticket": ticket, "sl": sl, "tp": tp}


def _runtime_config() -> dict:
    return {
        "profiles": [
            {
                "id": "gft_10k",
                "is_active": True,
                "execution_bridge": "mt5_local_gft_10k",
                "context_source_id": "gft_mt5_local",
            },
            {
                "id": "ftmo_demo_100k",
                "is_active": True,
                "execution_bridge": "mt5_local_ftmo_demo_100k",
                "context_source_id": "ftmo_mt5_local",
            },
        ],
        "context_sources": {
            "gft_mt5_local": {
                "dwx_files_path": "/tmp/dwx",
                "symbol_suffix": ".x",
            },
            "ftmo_mt5_local": {
                "dwx_files_path": "/tmp/dwx-ftmo",
                "symbol_suffix": ".x",
            },
        },
        "position_manager": {
            "execute_signal_max_age_seconds": 0,
            "timeout_auto_close": {
                "enabled": True,
                "eligible_profiles": ["ftmo_demo_100k"],
            },
        },
    }


def _make_service(tmp_path: Path, executor: FakeExecutor):
    db_path = str(tmp_path / "vanguard_universe_test.db")
    return get_service(
        db_path=db_path,
        runtime_config=_runtime_config(),
        executor_factory=lambda profile_id, profile, db_path: executor,
    )


def _insert_context_position(db_path: str, *, ticket: str, symbol: str, side: str, lots: float, entry: float, sl: float, tp: float, current_price: float, pnl: float) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO vanguard_context_positions_latest (
                profile_id, source, ticket, broker, account_number, symbol, direction,
                lots, entry_price, current_price, sl, tp, floating_pnl, swap,
                open_time_utc, holding_minutes, received_ts_utc, source_status,
                source_error, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gft_10k", "gft_mt5_local", ticket, "dwx", None, symbol, side, lots,
                entry, current_price, sl, tp, pnl, None,
                "2026-04-15T04:00:00Z", 10.0, "2026-04-15T04:05:00Z", "OK", None, "{}",
            ),
        )
        con.commit()


def _insert_forward_tracked_trade(db_path: str, *, symbol: str = "EURUSD", side: str = "LONG") -> str:
    ensure_trade_journal(db_path)
    return insert_approval_row(
        db_path=db_path,
        profile_id="gft_10k",
        candidate={
            "symbol": symbol,
            "asset_class": "forex",
            "side": side,
            "entry_price": 1.1000 if symbol != "AUDJPY" else 155.100,
            "stop_price": 1.0990 if side == "LONG" else 1.1010,
            "tp_price": 1.1020 if side == "LONG" else 1.0980,
            "approved_qty": 0.5,
            "original_risk_dollars": 25.0,
            "original_rr_multiple": 2.0,
            "original_r_multiple_target": 2.0,
        },
        policy_decision={"policy_id": "gft_10k_v1"},
        cycle_ts_utc="2026-04-15T04:00:00Z",
        status="FORWARD_TRACKED",
    )


def _insert_open_trade(db_path: str, *, ticket: str = "ticket-open", symbol: str = "EURUSD", side: str = "LONG") -> str:
    trade_id = _insert_forward_tracked_trade(db_path, symbol=symbol, side=side)
    mark_open_from_operator(
        db_path,
        trade_id,
        broker_order_id=ticket,
        broker_position_id=ticket,
        fill_price=1.1002 if symbol != "AUDJPY" else 155.123,
        fill_qty=0.5,
        filled_at_utc="2026-04-15T04:01:00Z",
        expected_entry=1.1000 if symbol != "AUDJPY" else 155.100,
        side=side,
    )
    return trade_id


def _insert_timeout_policy_checkpoint(
    db_path: str,
    *,
    trade_id: str,
    profile_id: str = "gft_10k",
    symbol: str = "EURUSD",
    side: str = "LONG",
    approved_cycle_ts_utc: str = "2026-04-15T04:00:00Z",
    timeout_minutes: int = 30,
    dedupe_minutes: int = 120,
    session_bucket: str = "london",
) -> None:
    ensure_forward_checkpoints(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO vanguard_forward_checkpoints (
                trade_id, profile_id, symbol, side, approved_cycle_ts_utc,
                checkpoint_minutes, checkpoint_ts_utc, bars_used,
                price_at_checkpoint, close_move_pips, close_move_bps, is_positive_direction,
                mfe_pips, mae_pips, status, source_bar_ts_utc, created_at_utc,
                session_bucket, entry_price, direction,
                dedupe_chain_60, dedupe_chain_90, dedupe_chain_120,
                dedupe_is_primary_60, dedupe_is_primary_90, dedupe_is_primary_120,
                policy_timeout_minutes, policy_dedupe_minutes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                profile_id,
                symbol,
                side,
                approved_cycle_ts_utc,
                timeout_minutes,
                "2026-04-15T04:30:00Z",
                timeout_minutes,
                1.1010,
                8.0,
                7.2,
                1,
                10.0,
                -2.0,
                "READY",
                "2026-04-15T04:30:00Z",
                "2026-04-15T04:30:00Z",
                session_bucket,
                1.1000,
                side,
                "chain60",
                "chain90",
                "chain120",
                1,
                1,
                1,
                timeout_minutes,
                dedupe_minutes,
            ),
        )
        con.commit()


def test_execute_signal_promotes_forward_tracked_journal_and_logs_events(tmp_path: Path) -> None:
    executor = FakeExecutor()
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    trade_id = _insert_forward_tracked_trade(service.db_path)

    request_id = service.queue_execute_signal(
        profile_id="gft_10k",
        trade_id=trade_id,
        requested_by="tester",
    )
    result = service.process_request(request_id)

    assert result.status == "RECONCILED"
    with sqlite3.connect(service.db_path) as con:
        con.row_factory = sqlite3.Row
        trade = con.execute(
            "SELECT status, broker_position_id, fill_price FROM vanguard_trade_journal WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        events = con.execute(
            "SELECT event_type FROM vanguard_execution_events WHERE request_id = ? ORDER BY event_id",
            (request_id,),
        ).fetchall()
        ctx = con.execute(
            "SELECT ticket, symbol, direction, sl, tp FROM vanguard_context_positions_latest WHERE profile_id = ?",
            ("gft_10k",),
        ).fetchone()
    assert trade["status"] == "OPEN"
    assert trade["broker_position_id"]
    assert [row["event_type"] for row in events] == ["VALIDATED_OK", "SUBMIT_OK", "BROKER_FILLED"]
    assert ctx["symbol"] == "EURUSD"
    assert ctx["direction"] == "LONG"


def test_set_exact_sl_tp_updates_context_and_logs_modify(tmp_path: Path) -> None:
    ticket = "ticket-open"
    executor = FakeExecutor(
        positions={
            ticket: {
                "ticket": ticket,
                "broker_position_id": ticket,
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 8.0,
            }
        }
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    _insert_open_trade(service.db_path, ticket=ticket)
    _insert_context_position(
        service.db_path,
        ticket=ticket,
        symbol="EURUSD",
        side="LONG",
        lots=0.5,
        entry=1.1002,
        sl=1.0990,
        tp=1.1020,
        current_price=1.1010,
        pnl=8.0,
    )

    request_id = service.queue_set_exact_sl_tp(
        profile_id="gft_10k",
        broker_position_id=ticket,
        sl=1.1000,
        tp=1.1030,
        requested_by="tester",
    )
    result = service.process_request(request_id)
    assert result.status == "RECONCILED"

    with sqlite3.connect(service.db_path) as con:
        con.row_factory = sqlite3.Row
        ctx = con.execute(
            "SELECT sl, tp FROM vanguard_context_positions_latest WHERE profile_id = ? AND ticket = ?",
            ("gft_10k", ticket),
        ).fetchone()
    assert abs(float(ctx["sl"]) - 1.1000) < 1e-9
    assert abs(float(ctx["tp"]) - 1.1030) < 1e-9


def test_move_breakeven_uses_fill_price_and_keeps_tp(tmp_path: Path) -> None:
    ticket = "ticket-open"
    executor = FakeExecutor(
        positions={
            ticket: {
                "ticket": ticket,
                "broker_position_id": ticket,
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 8.0,
            }
        }
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    _insert_open_trade(service.db_path, ticket=ticket)
    _insert_context_position(
        service.db_path,
        ticket=ticket,
        symbol="EURUSD",
        side="LONG",
        lots=0.5,
        entry=1.1002,
        sl=1.0990,
        tp=1.1020,
        current_price=1.1010,
        pnl=8.0,
    )

    request_id = service.queue_move_breakeven(
        profile_id="gft_10k",
        broker_position_id=ticket,
        requested_by="tester",
    )
    result = service.process_request(request_id)
    assert result.status == "RECONCILED"

    with sqlite3.connect(service.db_path) as con:
        con.row_factory = sqlite3.Row
        ctx = con.execute(
            "SELECT sl, tp FROM vanguard_context_positions_latest WHERE profile_id = ? AND ticket = ?",
            ("gft_10k", ticket),
        ).fetchone()
    assert abs(float(ctx["sl"]) - 1.1002) < 1e-9
    assert abs(float(ctx["tp"]) - 1.1020) < 1e-9


def test_close_position_reconciles_journal_and_removes_context_position(tmp_path: Path) -> None:
    ticket = "ticket-open"
    executor = FakeExecutor(
        positions={
            ticket: {
                "ticket": ticket,
                "broker_position_id": ticket,
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 12.0,
            }
        }
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    trade_id = _insert_open_trade(service.db_path, ticket=ticket)
    _insert_context_position(
        service.db_path,
        ticket=ticket,
        symbol="EURUSD",
        side="LONG",
        lots=0.5,
        entry=1.1002,
        sl=1.0990,
        tp=1.1020,
        current_price=1.1012,
        pnl=12.0,
    )

    request_id = service.queue_close_position(
        profile_id="gft_10k",
        broker_position_id=ticket,
        requested_by="tester",
    )
    result = service.process_request(request_id)
    assert result.status == "RECONCILED"

    with sqlite3.connect(service.db_path) as con:
        con.row_factory = sqlite3.Row
        trade = con.execute(
            "SELECT status, close_reason FROM vanguard_trade_journal WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        ctx = con.execute(
            "SELECT * FROM vanguard_context_positions_latest WHERE profile_id = ? AND ticket = ?",
            ("gft_10k", ticket),
        ).fetchone()
    assert trade["status"] == "CLOSED"
    assert trade["close_reason"] == "MANUAL_CLOSE_POSITION"
    assert ctx is None


def test_position_manager_view_surfaces_pending_request_state(tmp_path: Path) -> None:
    ticket = "ticket-open"
    executor = FakeExecutor(
        positions={
            ticket: {
                "ticket": ticket,
                "broker_position_id": ticket,
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 8.0,
            }
        }
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    _insert_open_trade(service.db_path, ticket=ticket)
    _insert_context_position(
        service.db_path,
        ticket=ticket,
        symbol="EURUSD",
        side="LONG",
        lots=0.5,
        entry=1.1002,
        sl=1.0990,
        tp=1.1020,
        current_price=1.1010,
        pnl=8.0,
    )
    service.queue_close_position(
        profile_id="gft_10k",
        broker_position_id=ticket,
        requested_by="tester",
    )

    rows = service.list_positions("gft_10k")
    assert len(rows) == 1
    assert rows[0]["pending_action_type"] == "CLOSE_POSITION"
    assert rows[0]["pending_action_status"] == "PENDING"


def test_position_manager_view_dedupes_same_ticket_across_sources(tmp_path: Path) -> None:
    ticket = "ticket-open"
    executor = FakeExecutor(
        positions={
            ticket: {
                "ticket": ticket,
                "broker_position_id": ticket,
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 8.0,
            }
        }
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    _insert_context_position(
        service.db_path,
        ticket=ticket,
        symbol="EURUSD",
        side="LONG",
        lots=0.5,
        entry=1.1002,
        sl=1.0990,
        tp=1.1020,
        current_price=1.1010,
        pnl=8.0,
    )
    with sqlite3.connect(service.db_path) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO vanguard_context_positions_latest (
                profile_id, source, ticket, broker, account_number, symbol, direction,
                lots, entry_price, current_price, sl, tp, floating_pnl, swap,
                open_time_utc, holding_minutes, received_ts_utc, source_status,
                source_error, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gft_10k", "other_source", ticket, "dwx", None, "EURUSD", "LONG", 0.5,
                1.1002, 1.1012, 1.0990, 1.1020, 9.5, None,
                "2026-04-15T04:00:00Z", 10.0, "2026-04-15T04:06:00Z", "OK", None, "{}",
            ),
        )
        con.commit()

    rows = service.list_positions("gft_10k")
    assert len(rows) == 1
    assert rows[0]["broker_position_id"] == ticket
    assert abs(float(rows[0]["floating_pnl"]) - 8.0) < 1e-9


def test_get_position_snapshot_derives_account_metrics_and_cli_header(tmp_path: Path, capsys) -> None:
    executor = FakeExecutor(
        positions={
            "ticket-1": {
                "ticket": "ticket-1",
                "broker_position_id": "ticket-1",
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 5.0,
            },
            "ticket-2": {
                "ticket": "ticket-2",
                "broker_position_id": "ticket-2",
                "symbol": "GBPUSD",
                "side": "SHORT",
                "qty": 0.4,
                "entry_price": 1.2500,
                "current_sl": 1.2510,
                "current_tp": 1.2480,
                "pnl": -2.0,
            },
        }
    )
    executor.account_info = {
        "balance": 9982.99,
        "equity": 9978.74,
        "free_margin": 9800.29,
        "leverage": 100,
    }
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    trade_id = _insert_open_trade(service.db_path, ticket="ticket-1")
    _insert_timeout_policy_checkpoint(service.db_path, trade_id=trade_id)

    snapshot = service.get_position_snapshot("gft_10k")

    assert snapshot["profile_id"] == "gft_10k"
    assert snapshot["account"]["open_positions"] == 2
    assert abs(float(snapshot["account"]["total_upl"]) - 3.0) < 1e-9
    assert abs(float(snapshot["account"]["used_margin"]) - 178.45) < 1e-9
    assert abs(float(snapshot["account"]["margin_level_pct"]) - ((9978.74 / 178.45) * 100.0)) < 1e-9
    assert len(snapshot["positions"]) == 2

    _print_snapshot(snapshot)
    out = capsys.readouterr().out
    assert "Profile gft_10k" in out
    assert "Balance 9982.99" in out
    assert "Equity 9978.74" in out
    assert "Used Margin 178.45" in out
    assert "Free Margin 9800.29" in out
    assert "Open Positions 2" in out
    assert "UPL 3.00" in out
    assert "timeout=30m" in out
    assert "dedupe=120" in out


def test_get_position_snapshot_handles_zero_positions(tmp_path: Path, capsys) -> None:
    executor = FakeExecutor(positions={})
    executor.account_info = {
        "balance": 10000.0,
        "equity": 10000.0,
        "free_margin": 10000.0,
        "leverage": 100,
    }
    service = _make_service(tmp_path, executor)
    service.ensure_schema()

    snapshot = service.get_position_snapshot("gft_10k")

    assert snapshot["account"]["open_positions"] == 0
    assert snapshot["account"]["margin_level_pct"] is None
    assert snapshot["positions"] == []

    _print_snapshot(snapshot)
    out = capsys.readouterr().out
    assert "Profile gft_10k" in out
    assert "Open Positions 0" in out
    assert "No open positions." in out


def test_flatten_profile_creates_child_close_requests(tmp_path: Path) -> None:
    executor = FakeExecutor(
        positions={
            "ticket-1": {
                "ticket": "ticket-1",
                "broker_position_id": "ticket-1",
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 5.0,
            },
            "ticket-2": {
                "ticket": "ticket-2",
                "broker_position_id": "ticket-2",
                "symbol": "GBPUSD",
                "side": "SHORT",
                "qty": 0.4,
                "entry_price": 1.2500,
                "current_sl": 1.2510,
                "current_tp": 1.2480,
                "pnl": 4.0,
            },
        }
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    _insert_open_trade(service.db_path, ticket="ticket-1", symbol="EURUSD", side="LONG")
    _insert_open_trade(service.db_path, ticket="ticket-2", symbol="GBPUSD", side="SHORT")
    _insert_context_position(service.db_path, ticket="ticket-1", symbol="EURUSD", side="LONG", lots=0.5, entry=1.1002, sl=1.0990, tp=1.1020, current_price=1.1008, pnl=5.0)
    _insert_context_position(service.db_path, ticket="ticket-2", symbol="GBPUSD", side="SHORT", lots=0.4, entry=1.2500, sl=1.2510, tp=1.2480, current_price=1.2495, pnl=4.0)

    request_id = service.queue_flatten_profile(
        profile_id="gft_10k",
        requested_by="tester",
    )
    result = service.process_request(request_id)
    assert result.status == "RECONCILED"

    with sqlite3.connect(service.db_path) as con:
        close_requests = con.execute(
            "SELECT COUNT(*) FROM vanguard_execution_requests WHERE action_type = 'CLOSE_POSITION'"
        ).fetchone()[0]
    assert close_requests == 2


def test_build_executor_reuses_cached_factory_result(tmp_path: Path) -> None:
    executor = FakeExecutor()
    calls: list[str] = []

    def _factory(profile_id: str, profile: dict, db_path: str):
        calls.append(profile_id)
        return executor

    service = get_service(
        db_path=str(tmp_path / "vanguard_universe_test.db"),
        runtime_config=_runtime_config(),
        executor_factory=_factory,
    )
    service.ensure_schema()

    first = service._build_executor("ftmo_demo_100k")
    second = service._build_executor("ftmo_demo_100k")

    assert first is executor
    assert second is executor
    assert calls == ["ftmo_demo_100k"]


def test_list_positions_rejects_stale_live_snapshot(tmp_path: Path) -> None:
    executor = FakeExecutor(
        positions={
            "ticket-1": {
                "ticket": "ticket-1",
                "broker_position_id": "ticket-1",
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 5.0,
            }
        },
        snapshot_meta={
            "source": "stored",
            "path": "/tmp/fake-stale",
            "mtime_utc": "2026-04-15T01:00:00Z",
            "age_seconds": 7200.0,
            "is_fresh": False,
            "reason": "orders_snapshot_stale>90s",
        },
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()

    try:
        service.list_positions("gft_10k")
        assert False, "expected stale position source to raise"
    except Exception as exc:
        assert "live position source stale" in str(exc)


def test_close_position_rejects_stale_live_snapshot(tmp_path: Path) -> None:
    ticket = "ticket-open"
    executor = FakeExecutor(
        positions={
            ticket: {
                "ticket": ticket,
                "broker_position_id": ticket,
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 12.0,
            }
        },
        snapshot_meta={
            "source": "stored",
            "path": "/tmp/fake-stale",
            "mtime_utc": "2026-04-15T01:00:00Z",
            "age_seconds": 7200.0,
            "is_fresh": False,
            "reason": "orders_snapshot_stale>90s",
        },
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    _insert_open_trade(service.db_path, ticket=ticket)
    _insert_context_position(
        service.db_path,
        ticket=ticket,
        symbol="EURUSD",
        side="LONG",
        lots=0.5,
        entry=1.1002,
        sl=1.0990,
        tp=1.1020,
        current_price=1.1012,
        pnl=12.0,
    )
    request_id = service.queue_close_position(
        profile_id="gft_10k",
        broker_position_id=ticket,
        requested_by="tester",
    )
    result = service.process_request(request_id)
    assert result.status == "REJECTED"
    assert result.error_code == "POSITION_SOURCE_STALE"


def test_close_at_timeout_closes_ftmo_demo_only(tmp_path: Path) -> None:
    ticket = "ticket-ftmo"
    executor = FakeExecutor(
        positions={
            ticket: {
                "ticket": ticket,
                "broker_position_id": ticket,
                "symbol": "EURUSD",
                "side": "LONG",
                "qty": 0.5,
                "entry_price": 1.1002,
                "current_sl": 1.0990,
                "current_tp": 1.1020,
                "pnl": 6.0,
            }
        }
    )
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    trade_id = _insert_forward_tracked_trade(service.db_path, symbol="EURUSD", side="LONG")
    mark_open_from_operator(
        service.db_path,
        trade_id,
        broker_order_id=ticket,
        broker_position_id=ticket,
        fill_price=1.1002,
        fill_qty=0.5,
        filled_at_utc="2026-04-15T04:01:00Z",
        expected_entry=1.1000,
        side="LONG",
    )
    _insert_context_position(
        service.db_path,
        ticket=ticket,
        symbol="EURUSD",
        side="LONG",
        lots=0.5,
        entry=1.1002,
        sl=1.0990,
        tp=1.1020,
        current_price=1.1011,
        pnl=6.0,
    )
    _insert_timeout_policy_checkpoint(
        service.db_path,
        trade_id=trade_id,
        profile_id="ftmo_demo_100k",
        approved_cycle_ts_utc="2026-04-15T03:00:00Z",
        timeout_minutes=30,
        dedupe_minutes=120,
        session_bucket="london",
    )
    with sqlite3.connect(service.db_path) as con:
        con.execute("UPDATE vanguard_trade_journal SET profile_id = 'ftmo_demo_100k' WHERE trade_id = ?", (trade_id,))
        con.execute("UPDATE vanguard_context_positions_latest SET profile_id = 'ftmo_demo_100k' WHERE ticket = ?", (ticket,))
        con.commit()

    request_id = service.queue_close_at_timeout(
        profile_id="ftmo_demo_100k",
        broker_position_id=ticket,
        requested_by="tester",
    )
    result = service.process_request(request_id)
    assert result.status == "RECONCILED"


def test_close_at_timeout_rejects_non_ftmo_profile(tmp_path: Path) -> None:
    executor = FakeExecutor()
    service = _make_service(tmp_path, executor)
    service.ensure_schema()
    request_id = service.queue_close_at_timeout(
        profile_id="gft_10k",
        broker_position_id="ticket-open",
        requested_by="tester",
    )
    result = service.process_request(request_id)
    assert result.status == "REJECTED"
    assert result.error_code == "TIMEOUT_CLOSE_NOT_ENABLED"


def test_build_executor_preserves_empty_symbol_suffix(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class DummyAdapter:
        def __init__(self, *, dwx_files_path: str, db_path: str, symbol_suffix: str) -> None:
            captured["dwx_files_path"] = dwx_files_path
            captured["db_path"] = db_path
            captured["symbol_suffix"] = symbol_suffix

        def connect(self) -> bool:
            return True

    class DummyExecutor:
        def __init__(self, mt5_adapter, profile_id: str, db_path: str) -> None:
            self._mt5 = mt5_adapter
            self.profile_id = profile_id
            self.db_path = db_path

    monkeypatch.setattr(operator_actions_module, "MT5DWXAdapter", DummyAdapter)
    monkeypatch.setattr(operator_actions_module, "DWXExecutor", DummyExecutor)

    runtime = _runtime_config()
    runtime["context_sources"]["ftmo_mt5_local"]["symbol_suffix"] = ""
    db_path = str(tmp_path / "vanguard_universe_test.db")
    service = get_service(db_path=db_path, runtime_config=runtime)

    executor = service._build_executor("ftmo_demo_100k")

    assert isinstance(executor, DummyExecutor)
    assert captured["symbol_suffix"] == ""


def test_queue_close_at_timeout_persists_trade_id(tmp_path: Path) -> None:
    service = _make_service(tmp_path, FakeExecutor())
    trade_id = insert_approval_row(
        service.db_path,
        "ftmo_demo_100k",
        {
            "symbol": "EURUSD",
            "asset_class": "forex",
            "side": "LONG",
            "entry_price": 1.1000,
            "stop_price": 1.0990,
            "tp_price": 1.1020,
            "shares_or_lots": 0.01,
        },
        {"decision": "APPROVED"},
        "2026-04-15T15:00:00Z",
        status="FORWARD_TRACKED",
    )
    mark_open_from_operator(
        service.db_path,
        trade_id,
        broker_order_id="ticket-timeout",
        broker_position_id="ticket-timeout",
        fill_price=1.1000,
        fill_qty=0.01,
        filled_at_utc="2026-04-15T15:00:10Z",
        expected_entry=1.1000,
        side="LONG",
    )

    request_id = service.queue_close_at_timeout(
        profile_id="ftmo_demo_100k",
        broker_position_id="ticket-timeout",
        requested_by="tester",
    )

    with sqlite3.connect(service.db_path) as con:
        row = con.execute(
            "SELECT trade_id FROM vanguard_execution_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == trade_id
