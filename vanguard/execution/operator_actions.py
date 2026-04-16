from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from vanguard.config.runtime_config import get_runtime_config, get_shadow_db_path
from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter
from vanguard.execution.position_manager import (
    DDL as POSITION_LIFECYCLE_DDL,
    LATEST_VIEW_SQL as POSITION_LIFECYCLE_LATEST_VIEW_SQL,
)
from vanguard.execution.trade_journal import (
    ensure_table as ensure_trade_journal_table,
    get_trade_row,
    mark_open_from_operator,
    update_journal_closed_from_auto,
)
from vanguard.execution.timeout_policy import (
    ensure_schema as ensure_timeout_policy_schema,
    get_policy_for_session,
    normalize_session_bucket,
)
from vanguard.executors.dwx_executor import DWXExecutor
from vanguard.helpers.db import sqlite_conn

logger = logging.getLogger(__name__)

ACTIVE_REQUEST_STATUSES = {"PENDING", "VALIDATED", "SUBMITTED", "ACKED"}
TERMINAL_REQUEST_STATUSES = {"REJECTED", "FAILED", "RECONCILED"}
VALID_ACTION_TYPES = {
    "EXECUTE_SIGNAL",
    "CLOSE_POSITION",
    "CLOSE_AT_TIMEOUT",
    "SET_EXACT_SL_TP",
    "MOVE_SL_TO_BREAKEVEN",
    "FLATTEN_PROFILE",
}

REQUEST_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_execution_requests (
    request_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at_utc   TEXT NOT NULL,
    requested_by       TEXT NOT NULL,
    source             TEXT NOT NULL,
    action_type        TEXT NOT NULL,
    profile_id         TEXT NOT NULL,
    broker_position_id TEXT,
    trade_id           TEXT,
    signal_ref_json    TEXT NOT NULL DEFAULT '{}',
    position_ref_json  TEXT NOT NULL DEFAULT '{}',
    payload_json       TEXT NOT NULL DEFAULT '{}',
    idempotency_key    TEXT NOT NULL,
    status             TEXT NOT NULL,
    error_code         TEXT,
    error_message      TEXT,
    submitted_at_utc   TEXT,
    finished_at_utc    TEXT
);
CREATE INDEX IF NOT EXISTS idx_execution_requests_profile_status
    ON vanguard_execution_requests(profile_id, status, action_type);
CREATE INDEX IF NOT EXISTS idx_execution_requests_position
    ON vanguard_execution_requests(profile_id, broker_position_id, action_type, status);
CREATE INDEX IF NOT EXISTS idx_execution_requests_trade
    ON vanguard_execution_requests(profile_id, trade_id, action_type, status);

CREATE TABLE IF NOT EXISTS vanguard_execution_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          INTEGER NOT NULL,
    event_ts_utc        TEXT NOT NULL,
    profile_id          TEXT NOT NULL,
    broker_position_id  TEXT,
    trade_id            TEXT,
    event_type          TEXT NOT NULL,
    event_payload_json  TEXT NOT NULL DEFAULT '{}',
    broker_result_json  TEXT NOT NULL DEFAULT '{}',
    note                TEXT
);
CREATE INDEX IF NOT EXISTS idx_execution_events_request
    ON vanguard_execution_events(request_id, event_ts_utc);
"""

CONTEXT_POSITION_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_context_positions_latest (
    profile_id        TEXT NOT NULL,
    source            TEXT NOT NULL,
    ticket            TEXT NOT NULL,
    broker            TEXT,
    account_number    TEXT,
    symbol            TEXT,
    direction         TEXT,
    lots              REAL,
    entry_price       REAL,
    current_price     REAL,
    sl                REAL,
    tp                REAL,
    floating_pnl      REAL,
    swap              REAL,
    open_time_utc     TEXT,
    holding_minutes   REAL,
    received_ts_utc   TEXT NOT NULL,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT,
    PRIMARY KEY (profile_id, source, ticket)
);
"""

CONTEXT_SYMBOL_SPECS_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_context_symbol_specs (
    broker            TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    broker_symbol     TEXT,
    min_lot           REAL,
    max_lot           REAL,
    lot_step          REAL,
    pip_size          REAL,
    tick_size         REAL,
    tick_value        REAL,
    contract_size     REAL,
    trade_allowed     INTEGER,
    source            TEXT,
    updated_ts_utc    TEXT,
    source_status     TEXT NOT NULL,
    source_error      TEXT,
    raw_payload_json  TEXT,
    PRIMARY KEY (broker, profile_id, symbol)
);
"""

REQUEST_PENDING_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS vanguard_execution_requests_latest_pending AS
SELECT req.*
FROM vanguard_execution_requests req
JOIN (
    SELECT profile_id,
           COALESCE(broker_position_id, '') AS broker_position_id_key,
           MAX(request_id) AS max_request_id
    FROM vanguard_execution_requests
    WHERE status IN ('PENDING', 'VALIDATED', 'SUBMITTED', 'ACKED')
      AND COALESCE(broker_position_id, '') <> ''
    GROUP BY profile_id, COALESCE(broker_position_id, '')
) latest
  ON latest.profile_id = req.profile_id
 AND latest.broker_position_id_key = COALESCE(req.broker_position_id, '')
 AND latest.max_request_id = req.request_id;
"""

POSITION_MANAGER_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS vanguard_position_manager_view AS
WITH latest_context AS (
    SELECT cp.*
    FROM vanguard_context_positions_latest cp
    JOIN (
        SELECT profile_id,
               ticket,
               MAX(COALESCE(received_ts_utc, '')) AS max_received_ts_utc
        FROM vanguard_context_positions_latest
        WHERE source_status = 'OK'
        GROUP BY profile_id, ticket
    ) latest
      ON latest.profile_id = cp.profile_id
     AND latest.ticket = cp.ticket
     AND COALESCE(latest.max_received_ts_utc, '') = COALESCE(cp.received_ts_utc, '')
    WHERE cp.source_status = 'OK'
)
SELECT
    cp.profile_id AS profile_id,
    cp.ticket AS broker_position_id,
    UPPER(COALESCE(cp.symbol, tj.symbol, '')) AS symbol,
    UPPER(COALESCE(tj.side, cp.direction, '')) AS side,
    COALESCE(cp.lots, 0.0) AS lots,
    COALESCE(cp.entry_price, tj.fill_price, tj.expected_entry) AS broker_fill_entry,
    cp.sl AS current_sl,
    cp.tp AS current_tp,
    cp.floating_pnl AS floating_pnl,
    COALESCE(cp.open_time_utc, tj.filled_at_utc, tj.approved_cycle_ts_utc) AS opened_at_utc,
    ROUND(
        (julianday('now') - julianday(COALESCE(cp.open_time_utc, tj.filled_at_utc, tj.approved_cycle_ts_utc))) * 1440.0,
        2
    ) AS holding_minutes,
    tj.trade_id AS trade_id,
    tj.original_entry_ref_price AS original_entry_ref_price,
    tj.original_stop_price AS original_stop_price,
    tj.original_tp_price AS original_tp_price,
    tj.original_risk_dollars AS original_risk_dollars,
    tj.original_rr_multiple AS original_rr_multiple,
    ll.lifecycle_phase AS latest_lifecycle_phase,
    ll.review_action AS latest_review_action,
    ll.review_reason_codes_json AS latest_review_reasons,
    ll.same_side_signal_snapshot_json AS latest_same_side_signal_state,
    ll.opposite_side_signal_snapshot_json AS latest_opposite_side_signal_state,
    req.action_type AS pending_action_type,
    req.status AS pending_action_status
FROM latest_context cp
LEFT JOIN vanguard_trade_journal tj
    ON tj.profile_id = cp.profile_id
   AND tj.broker_position_id = cp.ticket
   AND tj.status = 'OPEN'
LEFT JOIN vanguard_position_lifecycle_latest ll
    ON ll.trade_id = tj.trade_id
LEFT JOIN vanguard_execution_requests_latest_pending req
    ON req.profile_id = cp.profile_id
   AND req.broker_position_id = cp.ticket
;
"""


@dataclass
class ActionResult:
    request_id: int
    status: str
    action_type: str
    profile_id: str
    broker_position_id: str | None = None
    trade_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    dry_run: bool = False
    broker_result: dict[str, Any] | None = None


class PositionSourceStaleError(RuntimeError):
    """Raised when the live position source is too stale to trust."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _json_load(value: Any) -> Any:
    if value in (None, "", b""):
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _rowdict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PositionActionService:
    """Operator-facing execution and position-management service."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        runtime_config: dict[str, Any] | None = None,
        executor_factory: Optional[Callable[[str, dict[str, Any], str], Any]] = None,
    ) -> None:
        self.db_path = str(db_path or get_shadow_db_path())
        self.runtime_config = runtime_config or get_runtime_config()
        self.executor_factory = executor_factory
        self._executor_cache: dict[str, Any] = {}

    def ensure_schema(self) -> None:
        ensure_trade_journal_table(self.db_path)
        ensure_timeout_policy_schema(self.db_path)
        with sqlite_conn(self.db_path) as con:
            for stmt in POSITION_LIFECYCLE_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    con.execute(stmt)
            for stmt in (REQUEST_DDL + CONTEXT_POSITION_DDL + CONTEXT_SYMBOL_SPECS_DDL).strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    con.execute(stmt)
            con.execute("DROP VIEW IF EXISTS vanguard_position_lifecycle_latest")
            con.execute(POSITION_LIFECYCLE_LATEST_VIEW_SQL)
            con.execute("DROP VIEW IF EXISTS vanguard_execution_requests_latest_pending")
            con.execute(REQUEST_PENDING_VIEW_SQL)
            con.execute("DROP VIEW IF EXISTS vanguard_position_manager_view")
            con.execute(POSITION_MANAGER_VIEW_SQL)

    def list_positions(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        self.ensure_schema()
        if profile_id:
            self._refresh_profile_positions(profile_id)
        return self._query_position_rows(profile_id)

    def _query_position_rows(self, profile_id: str | None = None) -> list[dict[str, Any]]:
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            sql = "SELECT * FROM vanguard_position_manager_view"
            params: list[Any] = []
            if profile_id:
                sql += " WHERE profile_id = ?"
                params.append(str(profile_id))
            sql += " ORDER BY profile_id, symbol, broker_position_id"
            rows = con.execute(sql, params).fetchall()
        return [_rowdict(row) for row in rows]

    def _load_timeout_policy_rows(self, trade_ids: list[str]) -> dict[str, dict[str, Any]]:
        clean_ids = [str(trade_id) for trade_id in trade_ids if str(trade_id).strip()]
        if not clean_ids:
            return {}
        placeholders = ",".join("?" for _ in clean_ids)
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"""
                SELECT trade_id,
                       session_bucket,
                       policy_timeout_minutes,
                       policy_dedupe_minutes,
                       approved_cycle_ts_utc
                FROM vanguard_forward_checkpoints
                WHERE trade_id IN ({placeholders})
                  AND checkpoint_minutes = policy_timeout_minutes
                """,
                clean_ids,
            ).fetchall()
        return {str(row["trade_id"]): _rowdict(row) for row in rows}

    def _augment_position_timeout_fields(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        policy_rows = self._load_timeout_policy_rows([str(row.get("trade_id") or "") for row in rows])
        now_dt = datetime.now(timezone.utc)
        enriched: list[dict[str, Any]] = []
        for row in rows:
            trade_id = str(row.get("trade_id") or "")
            policy_row = policy_rows.get(trade_id) or {}
            session_bucket = normalize_session_bucket(policy_row.get("session_bucket"))
            timeout_minutes = policy_row.get("policy_timeout_minutes")
            dedupe_minutes = policy_row.get("policy_dedupe_minutes")
            timeout_at_utc = None
            minutes_to_timeout = None
            approved_ts = policy_row.get("approved_cycle_ts_utc")
            approved_dt = datetime.fromisoformat(str(approved_ts).replace("Z", "+00:00")).astimezone(timezone.utc) if approved_ts else None
            if approved_dt is not None and timeout_minutes not in (None, ""):
                timeout_dt = approved_dt + timedelta(minutes=int(timeout_minutes))
                timeout_at_utc = timeout_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                minutes_to_timeout = round((timeout_dt - now_dt).total_seconds() / 60.0, 2)
            enriched.append(
                {
                    **row,
                    "policy_session_bucket": session_bucket if session_bucket != "unknown" else None,
                    "policy_timeout_minutes": int(timeout_minutes) if timeout_minutes not in (None, "") else None,
                    "policy_dedupe_mode": int(dedupe_minutes) if dedupe_minutes not in (None, "") else None,
                    "policy_timeout_at_utc": timeout_at_utc,
                    "minutes_to_timeout": minutes_to_timeout,
                }
            )
        return enriched

    def get_position_snapshot(self, profile_id: str) -> dict[str, Any]:
        self.ensure_schema()
        executor = self._build_executor(profile_id)
        self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        self._refresh_profile_positions(profile_id)
        positions = self._augment_position_timeout_fields(self._query_position_rows(profile_id))
        account_info = dict(executor.get_account_info() or {})
        balance = float(account_info.get("balance") or 0.0)
        equity = float(account_info.get("equity") or 0.0)
        free_margin = float(account_info.get("free_margin") or 0.0)
        total_upl = float(sum(float(row.get("floating_pnl") or 0.0) for row in positions))
        used_margin = max(0.0, equity - free_margin)
        margin_level_pct = None
        if used_margin > 0:
            margin_level_pct = (equity / used_margin) * 100.0
        return {
            "profile_id": str(profile_id),
            "account": {
                "balance": balance,
                "equity": equity,
                "free_margin": free_margin,
                "used_margin": used_margin,
                "margin_level_pct": margin_level_pct,
                "open_positions": len(positions),
                "total_upl": total_upl,
            },
            "positions": positions,
        }

    def queue_execute_signal(
        self,
        *,
        profile_id: str,
        trade_id: str,
        requested_by: str,
        source: str = "cli",
        overrides: dict[str, Any] | None = None,
    ) -> int:
        signal_ref = {"trade_id": trade_id}
        payload = dict(overrides or {})
        idempotency_key = _sha(f"EXECUTE_SIGNAL|{profile_id}|{trade_id}|{_json_dumps(payload)}")
        return self._insert_request(
            requested_by=requested_by,
            source=source,
            action_type="EXECUTE_SIGNAL",
            profile_id=profile_id,
            broker_position_id=None,
            trade_id=trade_id,
            signal_ref=signal_ref,
            position_ref={},
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def queue_close_position(
        self,
        *,
        profile_id: str,
        broker_position_id: str,
        requested_by: str,
        source: str = "cli",
    ) -> int:
        trade_id = self._trade_id_for_position(profile_id, broker_position_id)
        idempotency_key = _sha(f"CLOSE_POSITION|{profile_id}|{broker_position_id}")
        return self._insert_request(
            requested_by=requested_by,
            source=source,
            action_type="CLOSE_POSITION",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            signal_ref={},
            position_ref={"broker_position_id": broker_position_id},
            payload={},
            idempotency_key=idempotency_key,
        )

    def queue_close_at_timeout(
        self,
        *,
        profile_id: str,
        broker_position_id: str,
        requested_by: str,
        source: str = "cli",
    ) -> int:
        trade_id = self._trade_id_for_position(profile_id, broker_position_id)
        idempotency_key = _sha(f"CLOSE_AT_TIMEOUT|{profile_id}|{broker_position_id}")
        return self._insert_request(
            requested_by=requested_by,
            source=source,
            action_type="CLOSE_AT_TIMEOUT",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            signal_ref={},
            position_ref={"broker_position_id": broker_position_id},
            payload={},
            idempotency_key=idempotency_key,
        )

    def queue_set_exact_sl_tp(
        self,
        *,
        profile_id: str,
        broker_position_id: str,
        sl: float,
        tp: float,
        requested_by: str,
        source: str = "cli",
    ) -> int:
        trade_id = self._trade_id_for_position(profile_id, broker_position_id)
        payload = {"new_sl": float(sl), "new_tp": float(tp)}
        idempotency_key = _sha(f"SET_EXACT_SL_TP|{profile_id}|{broker_position_id}|{payload['new_sl']}|{payload['new_tp']}")
        return self._insert_request(
            requested_by=requested_by,
            source=source,
            action_type="SET_EXACT_SL_TP",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            signal_ref={},
            position_ref={"broker_position_id": broker_position_id},
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def queue_move_breakeven(
        self,
        *,
        profile_id: str,
        broker_position_id: str,
        requested_by: str,
        source: str = "cli",
    ) -> int:
        trade_id = self._trade_id_for_position(profile_id, broker_position_id)
        idempotency_key = _sha(f"MOVE_SL_TO_BREAKEVEN|{profile_id}|{broker_position_id}")
        return self._insert_request(
            requested_by=requested_by,
            source=source,
            action_type="MOVE_SL_TO_BREAKEVEN",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            signal_ref={},
            position_ref={"broker_position_id": broker_position_id},
            payload={},
            idempotency_key=idempotency_key,
        )

    def queue_flatten_profile(
        self,
        *,
        profile_id: str,
        requested_by: str,
        source: str = "cli",
    ) -> int:
        idempotency_key = _sha(f"FLATTEN_PROFILE|{profile_id}")
        return self._insert_request(
            requested_by=requested_by,
            source=source,
            action_type="FLATTEN_PROFILE",
            profile_id=profile_id,
            broker_position_id=None,
            trade_id=None,
            signal_ref={},
            position_ref={},
            payload={},
            idempotency_key=idempotency_key,
        )

    def process_request(self, request_id: int, *, dry_run: bool = False) -> ActionResult:
        self.ensure_schema()
        request = self._load_request(request_id)
        if not request:
            raise ValueError(f"request_id={request_id} not found")
        action_type = str(request["action_type"] or "")
        if action_type not in VALID_ACTION_TYPES:
            raise ValueError(f"Unsupported action_type={action_type!r}")

        handler = {
            "EXECUTE_SIGNAL": self._handle_execute_signal,
            "CLOSE_POSITION": self._handle_close_position,
            "CLOSE_AT_TIMEOUT": self._handle_close_at_timeout,
            "SET_EXACT_SL_TP": self._handle_set_exact_sl_tp,
            "MOVE_SL_TO_BREAKEVEN": self._handle_move_breakeven,
            "FLATTEN_PROFILE": self._handle_flatten_profile,
        }[action_type]
        return handler(request, dry_run=dry_run)

    def _insert_request(
        self,
        *,
        requested_by: str,
        source: str,
        action_type: str,
        profile_id: str,
        broker_position_id: str | None,
        trade_id: str | None,
        signal_ref: dict[str, Any],
        position_ref: dict[str, Any],
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> int:
        self.ensure_schema()
        if action_type not in VALID_ACTION_TYPES:
            raise ValueError(f"Unsupported action_type={action_type!r}")
        now = _now_utc()
        with sqlite_conn(self.db_path) as con:
            cur = con.execute(
                """
                INSERT INTO vanguard_execution_requests (
                    requested_at_utc, requested_by, source, action_type, profile_id,
                    broker_position_id, trade_id, signal_ref_json, position_ref_json,
                    payload_json, idempotency_key, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (
                    now,
                    str(requested_by or "unknown"),
                    str(source or "cli"),
                    action_type,
                    str(profile_id or ""),
                    str(broker_position_id or "") or None,
                    str(trade_id or "") or None,
                    _json_dumps(signal_ref),
                    _json_dumps(position_ref),
                    _json_dumps(payload),
                    idempotency_key,
                ),
            )
            request_id = int(cur.lastrowid)
        return request_id

    def _load_request(self, request_id: int) -> dict[str, Any]:
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM vanguard_execution_requests WHERE request_id = ?",
                (int(request_id),),
            ).fetchone()
        return _rowdict(row)

    def _load_profile(self, profile_id: str) -> dict[str, Any]:
        for profile in self.runtime_config.get("profiles") or []:
            if str(profile.get("id") or "") == str(profile_id):
                return dict(profile)
        return {}

    def _load_context_source(self, profile: dict[str, Any]) -> dict[str, Any]:
        source_id = str(profile.get("context_source_id") or "").strip()
        if source_id:
            return dict((self.runtime_config.get("context_sources") or {}).get(source_id) or {})
        return dict((self.runtime_config.get("data_sources") or {}).get("mt5_local") or {})

    def _build_executor(self, profile_id: str) -> Any:
        cached = self._executor_cache.get(profile_id)
        if cached is not None:
            connect = getattr(cached, "connect", None)
            if connect is None or bool(connect()):
                return cached
            self._executor_cache.pop(profile_id, None)

        if self.executor_factory is not None:
            profile = self._load_profile(profile_id)
            executor = self.executor_factory(profile_id, profile, self.db_path)
            self._executor_cache[profile_id] = executor
            return executor

        profile = self._load_profile(profile_id)
        if not profile:
            raise ValueError(f"profile_id={profile_id!r} not found in runtime config")
        bridge = str(profile.get("execution_bridge") or "").lower()
        if not bridge.startswith("mt5_local"):
            raise ValueError(f"profile_id={profile_id!r} is not configured for DWX execution")
        source_cfg = self._load_context_source(profile)
        dwx_files_path = str(source_cfg.get("dwx_files_path") or "").strip()
        if not dwx_files_path:
            raise ValueError(f"profile_id={profile_id!r} missing dwx_files_path in runtime context source")
        symbol_suffix = source_cfg.get("symbol_suffix")
        if symbol_suffix is None:
            symbol_suffix = ".x"
        adapter = MT5DWXAdapter(
            dwx_files_path=dwx_files_path,
            db_path=self.db_path,
            symbol_suffix=str(symbol_suffix),
        )
        adapter.connect()
        executor = DWXExecutor(mt5_adapter=adapter, profile_id=profile_id, db_path=self.db_path)
        self._executor_cache[profile_id] = executor
        return executor

    def _timeout_close_enabled_for_profile(self, profile_id: str) -> bool:
        profile = self._load_profile(profile_id)
        if not profile:
            return False
        execution_bridge = str(profile.get("execution_bridge") or "").lower()
        if not execution_bridge.startswith("mt5_local"):
            return False
        cfg = ((self.runtime_config.get("position_manager") or {}).get("timeout_auto_close") or {})
        if not bool(cfg.get("enabled", False)):
            return False
        eligible = {
            str(value).strip()
            for value in (cfg.get("eligible_profiles") or [])
            if str(value).strip()
        }
        if eligible and str(profile_id) not in eligible:
            return False
        return True

    def _record_event(
        self,
        *,
        request_id: int,
        profile_id: str,
        broker_position_id: str | None,
        trade_id: str | None,
        event_type: str,
        event_payload: dict[str, Any] | None = None,
        broker_result: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> None:
        with sqlite_conn(self.db_path) as con:
            con.execute(
                """
                INSERT INTO vanguard_execution_events (
                    request_id, event_ts_utc, profile_id, broker_position_id,
                    trade_id, event_type, event_payload_json, broker_result_json, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(request_id),
                    _now_utc(),
                    str(profile_id or ""),
                    str(broker_position_id or "") or None,
                    str(trade_id or "") or None,
                    str(event_type or ""),
                    _json_dumps(event_payload),
                    _json_dumps(broker_result),
                    str(note or "") or None,
                ),
            )

    def _update_request_status(
        self,
        request_id: int,
        *,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
        submitted: bool = False,
        finished: bool = False,
    ) -> None:
        with sqlite_conn(self.db_path) as con:
            con.execute(
                """
                UPDATE vanguard_execution_requests
                   SET status = ?,
                       error_code = ?,
                       error_message = ?,
                       submitted_at_utc = CASE WHEN ? THEN COALESCE(submitted_at_utc, ?) ELSE submitted_at_utc END,
                       finished_at_utc = CASE WHEN ? THEN ? ELSE finished_at_utc END
                 WHERE request_id = ?
                """,
                (
                    status,
                    error_code,
                    error_message,
                    1 if submitted else 0,
                    _now_utc(),
                    1 if finished else 0,
                    _now_utc(),
                    int(request_id),
                ),
            )

    def _reject(
        self,
        request: dict[str, Any],
        *,
        error_code: str,
        error_message: str,
        note: str | None = None,
    ) -> ActionResult:
        request_id = int(request["request_id"])
        self._update_request_status(
            request_id,
            status="REJECTED",
            error_code=error_code,
            error_message=error_message,
            finished=True,
        )
        self._record_event(
            request_id=request_id,
            profile_id=str(request["profile_id"] or ""),
            broker_position_id=request.get("broker_position_id"),
            trade_id=request.get("trade_id"),
            event_type="VALIDATED_FAIL",
            event_payload={"error_code": error_code, "error_message": error_message},
            note=note,
        )
        return ActionResult(
            request_id=request_id,
            status="REJECTED",
            action_type=str(request["action_type"] or ""),
            profile_id=str(request["profile_id"] or ""),
            broker_position_id=request.get("broker_position_id"),
            trade_id=request.get("trade_id"),
            error_code=error_code,
            error_message=error_message,
        )

    def _validate_no_pending_conflict(
        self,
        *,
        profile_id: str,
        broker_position_id: str | None = None,
        trade_id: str | None = None,
        action_types: tuple[str, ...],
    ) -> Optional[str]:
        clauses = ["profile_id = ?", f"action_type IN ({','.join('?' for _ in action_types)})", f"status IN ({','.join('?' for _ in ACTIVE_REQUEST_STATUSES)})"]
        params: list[Any] = [profile_id, *action_types, *ACTIVE_REQUEST_STATUSES]
        if broker_position_id:
            clauses.append("COALESCE(broker_position_id, '') = ?")
            params.append(str(broker_position_id))
        if trade_id:
            clauses.append("COALESCE(trade_id, '') = ?")
            params.append(str(trade_id))
        sql = "SELECT request_id FROM vanguard_execution_requests WHERE " + " AND ".join(clauses) + " ORDER BY request_id DESC LIMIT 1"
        with sqlite_conn(self.db_path) as con:
            row = con.execute(sql, params).fetchone()
        if row:
            return str(row[0])
        return None

    def _load_context_position(self, profile_id: str, broker_position_id: str) -> dict[str, Any]:
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT *
                FROM vanguard_context_positions_latest
                WHERE profile_id = ?
                  AND ticket = ?
                ORDER BY received_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, str(broker_position_id)),
            ).fetchone()
        return _rowdict(row)

    def _load_latest_quote(self, profile_id: str, symbol: str) -> dict[str, Any]:
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT *
                FROM vanguard_context_quote_latest
                WHERE profile_id = ?
                  AND symbol = ?
                ORDER BY received_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, str(symbol or "").upper()),
            ).fetchone()
        return _rowdict(row)

    def _resolve_close_snapshot(
        self,
        *,
        profile_id: str,
        live_position: dict[str, Any],
        fallback_position: dict[str, Any],
    ) -> dict[str, Any]:
        symbol = str(live_position.get("symbol") or fallback_position.get("symbol") or "").upper()
        side = str(live_position.get("side") or fallback_position.get("direction") or fallback_position.get("side") or "").upper()
        quote = self._load_latest_quote(profile_id, symbol) if symbol else {}
        bid = quote.get("bid")
        ask = quote.get("ask")
        close_price = None
        if side == "LONG" and bid not in (None, ""):
            close_price = float(bid)
        elif side == "SHORT" and ask not in (None, ""):
            close_price = float(ask)
        elif fallback_position.get("current_price") not in (None, ""):
            close_price = float(fallback_position.get("current_price"))

        realized_pnl = float(live_position.get("pnl") or 0.0)
        holding_minutes = int(float(fallback_position.get("holding_minutes") or 0.0))

        broker_close_reason = None
        tp = live_position.get("current_tp")
        sl = live_position.get("current_sl")
        try:
            tp_val = float(tp) if tp not in (None, "") else None
        except Exception:
            tp_val = None
        try:
            sl_val = float(sl) if sl not in (None, "") else None
        except Exception:
            sl_val = None
        if close_price is not None:
            if side == "LONG":
                if tp_val is not None and close_price >= tp_val:
                    broker_close_reason = "LIKELY_TP_HIT"
                elif sl_val is not None and close_price <= sl_val:
                    broker_close_reason = "LIKELY_SL_HIT"
            elif side == "SHORT":
                if tp_val is not None and close_price <= tp_val:
                    broker_close_reason = "LIKELY_TP_HIT"
                elif sl_val is not None and close_price >= sl_val:
                    broker_close_reason = "LIKELY_SL_HIT"
        return {
            "close_price": close_price,
            "realized_pnl": realized_pnl,
            "holding_minutes": holding_minutes,
            "broker_close_reason": broker_close_reason,
        }

    def _upsert_context_position(
        self,
        *,
        profile_id: str,
        source: str,
        broker_position_id: str,
        symbol: str,
        side: str,
        lots: float,
        entry_price: float,
        current_price: float | None,
        sl: float | None,
        tp: float | None,
        floating_pnl: float | None,
        open_time_utc: str,
    ) -> None:
        with sqlite_conn(self.db_path) as con:
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
                    profile_id,
                    source,
                    str(broker_position_id),
                    "dwx",
                    None,
                    str(symbol or "").upper(),
                    str(side or "").upper(),
                    float(lots or 0.0),
                    float(entry_price or 0.0),
                    float(current_price) if current_price not in (None, "") else None,
                    float(sl) if sl not in (None, "") else None,
                    float(tp) if tp not in (None, "") else None,
                    float(floating_pnl) if floating_pnl not in (None, "") else 0.0,
                    None,
                    open_time_utc,
                    0.0,
                    _now_utc(),
                    "OK",
                    None,
                    _json_dumps({"source": "operator_actions"}),
                ),
            )

    def _delete_context_position(self, profile_id: str, broker_position_id: str) -> None:
        with sqlite_conn(self.db_path) as con:
            con.execute(
                "DELETE FROM vanguard_context_positions_latest WHERE profile_id = ? AND ticket = ?",
                (profile_id, str(broker_position_id)),
            )

    def _update_context_position_stops(self, profile_id: str, broker_position_id: str, sl: float, tp: float) -> None:
        with sqlite_conn(self.db_path) as con:
            con.execute(
                """
                UPDATE vanguard_context_positions_latest
                   SET sl = ?, tp = ?, received_ts_utc = ?, source_status = 'OK', source_error = NULL
                 WHERE profile_id = ? AND ticket = ?
                """,
                (float(sl), float(tp), _now_utc(), profile_id, str(broker_position_id)),
            )

    def _load_live_positions(self, executor: Any) -> list[dict[str, Any]]:
        positions = list(executor.get_open_positions() or [])
        normalized: list[dict[str, Any]] = []
        for pos in positions:
            ticket = str(pos.get("ticket") or pos.get("position_id") or pos.get("id") or "").strip()
            if not ticket:
                continue
            normalized.append(
                {
                    "broker_position_id": ticket,
                    "symbol": str(pos.get("symbol") or "").upper(),
                    "side": str(pos.get("side") or "").upper(),
                    "qty": float(pos.get("qty") or 0.0),
                    "entry_price": float(pos.get("entry_price") or 0.0),
                    "current_sl": pos.get("current_sl"),
                    "current_tp": pos.get("current_tp"),
                    "pnl": float(pos.get("pnl") or 0.0),
                }
            )
        return normalized

    def _position_snapshot_max_age_seconds(self) -> float:
        cfg = (self.runtime_config.get("position_manager") or {})
        return float(cfg.get("position_snapshot_max_age_seconds") or 90.0)

    def _ensure_live_position_source_fresh(self, executor: Any, *, profile_id: str) -> dict[str, Any]:
        meta = (
            executor.get_positions_snapshot_meta(max_age_seconds=self._position_snapshot_max_age_seconds())
            if hasattr(executor, "get_positions_snapshot_meta")
            else {"is_fresh": True, "reason": "", "age_seconds": 0.0}
        )
        if not bool(meta.get("is_fresh", False)):
            reason = str(meta.get("reason") or "orders snapshot stale")
            age = meta.get("age_seconds")
            age_msg = f" age={age:.0f}s" if isinstance(age, (int, float)) else ""
            raise PositionSourceStaleError(
                f"profile_id={profile_id} live position source stale ({reason}{age_msg})"
            )
        return meta

    def _refresh_profile_positions(self, profile_id: str) -> None:
        executor = self._build_executor(profile_id)
        self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        live_positions = self._load_live_positions(executor)
        live_by_ticket = {str(pos["broker_position_id"]): pos for pos in live_positions}
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT ticket FROM vanguard_context_positions_latest WHERE profile_id = ?",
                (profile_id,),
            ).fetchall()
        existing_tickets = {str(row["ticket"]) for row in rows}
        for ticket in existing_tickets - set(live_by_ticket):
            self._delete_context_position(profile_id, ticket)
        source = str((self._load_profile(profile_id) or {}).get("context_source_id") or "operator_actions")
        for ticket, pos in live_by_ticket.items():
            self._upsert_context_position(
                profile_id=profile_id,
                source=source,
                broker_position_id=ticket,
                symbol=str(pos["symbol"]),
                side=str(pos["side"]),
                lots=float(pos["qty"]),
                entry_price=float(pos["entry_price"]),
                current_price=float(pos["entry_price"]),
                sl=pos.get("current_sl"),
                tp=pos.get("current_tp"),
                floating_pnl=float(pos.get("pnl") or 0.0),
                open_time_utc=None,
            )

    def _record_lifecycle_action(
        self,
        *,
        trade_id: str,
        action_type: str,
        action_status: str,
        idempotency_key: str,
        note: str | None = None,
    ) -> None:
        if not trade_id:
            return
        self.ensure_schema()
        with sqlite_conn(self.db_path) as con:
            con.row_factory = sqlite3.Row
            latest = con.execute(
                "SELECT * FROM vanguard_position_lifecycle_latest WHERE trade_id = ? LIMIT 1",
                (trade_id,),
            ).fetchone()
            if latest is None:
                return
            payload = _rowdict(latest)
            payload.pop("review_id", None)
            payload["reviewed_at_utc"] = _now_utc()
            payload["last_action_type"] = action_type
            payload["last_action_submitted_at_utc"] = payload["reviewed_at_utc"]
            payload["last_action_status"] = action_status
            payload["last_action_idempotency_key"] = idempotency_key
            if note:
                base_note = str(payload.get("manager_note") or "").strip()
                payload["manager_note"] = f"{base_note} | {note}".strip(" |")
            cols = [
                "profile_id", "trade_id", "broker_position_id", "symbol", "side",
                "policy_id", "model_id", "model_family", "opened_cycle_ts_utc",
                "opened_at_utc", "reviewed_at_utc", "holding_minutes", "bars_elapsed",
                "lifecycle_phase", "review_action", "review_reason_codes_json",
                "extension_granted_count", "breakeven_moved",
                "last_action_type", "last_action_submitted_at_utc", "last_action_status",
                "last_action_idempotency_key", "thesis_snapshot_json",
                "same_side_signal_snapshot_json", "opposite_side_signal_snapshot_json",
                "latest_state_snapshot_json", "latest_health_snapshot_json",
                "latest_position_snapshot_json", "manager_note",
            ]
            placeholders = ", ".join(f":{col}" for col in cols)
            con.execute(
                f"""
                INSERT INTO vanguard_position_lifecycle ({', '.join(cols)})
                VALUES ({placeholders})
                """,
                {col: payload.get(col) for col in cols},
            )

    def _handle_execute_signal(self, request: dict[str, Any], *, dry_run: bool) -> ActionResult:
        request_id = int(request["request_id"])
        profile_id = str(request["profile_id"] or "")
        trade_id = str(request.get("trade_id") or "")
        trade = get_trade_row(self.db_path, trade_id)
        if not trade:
            return self._reject(request, error_code="SIGNAL_NOT_FOUND", error_message=f"trade_id={trade_id} missing")
        if str(trade.get("status") or "").upper() not in {"FORWARD_TRACKED", "PENDING_FILL"}:
            return self._reject(
                request,
                error_code="SIGNAL_NOT_EXECUTABLE",
                error_message=f"trade_id={trade_id} status={trade.get('status')}",
            )
        duplicate_req = self._validate_no_pending_conflict(
            profile_id=profile_id,
            trade_id=trade_id,
            action_types=("EXECUTE_SIGNAL",),
        )
        if duplicate_req and int(duplicate_req) != request_id:
            return self._reject(
                request,
                error_code="DUPLICATE_REQUEST",
                error_message=f"pending execute request already exists request_id={duplicate_req}",
            )

        profile = self._load_profile(profile_id)
        if not profile:
            return self._reject(request, error_code="PROFILE_NOT_FOUND", error_message=f"profile_id={profile_id} not found")

        execute_max_age = int(((self.runtime_config.get("position_manager") or {}).get("execute_signal_max_age_seconds") or 0))
        if execute_max_age > 0:
            approved_dt = datetime.fromisoformat(str(trade["approved_cycle_ts_utc"]).replace("Z", "+00:00"))
            age_seconds = max(0.0, (datetime.now(timezone.utc) - approved_dt.astimezone(timezone.utc)).total_seconds())
            if age_seconds > execute_max_age:
                return self._reject(
                    request,
                    error_code="SIGNAL_STALE",
                    error_message=f"signal age {age_seconds:.0f}s exceeds {execute_max_age}s",
                )

        executor = self._build_executor(profile_id)
        try:
            self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        except PositionSourceStaleError as exc:
            return self._reject(request, error_code="POSITION_SOURCE_STALE", error_message=str(exc))
        live_positions = self._load_live_positions(executor)
        symbol = str(trade.get("symbol") or "").upper()
        side = str(trade.get("side") or "").upper()
        for pos in live_positions:
            if str(pos["symbol"]) == symbol and str(pos["side"]) == side:
                return self._reject(
                    request,
                    error_code="SAME_SIDE_POSITION_OPEN",
                    error_message=f"{symbol} {side} already open on broker books",
                )

        payload = _json_load(request.get("payload_json"))
        lot_size = float(payload.get("override_lot_size") or trade.get("approved_qty") or 0.0)
        stop_loss = float(payload.get("override_sl") or trade.get("original_stop_price") or trade.get("expected_sl") or 0.0)
        take_profit = float(payload.get("override_tp") or trade.get("original_tp_price") or trade.get("expected_tp") or 0.0)
        if lot_size <= 0 or stop_loss <= 0 or take_profit <= 0:
            return self._reject(
                request,
                error_code="INVALID_CONTRACT",
                error_message="approved contract missing qty/sl/tp",
            )

        self._update_request_status(request_id, status="VALIDATED")
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=None,
            trade_id=trade_id,
            event_type="VALIDATED_OK",
            event_payload={"symbol": symbol, "side": side},
        )
        if dry_run:
            return ActionResult(
                request_id=request_id,
                status="VALIDATED",
                action_type="EXECUTE_SIGNAL",
                profile_id=profile_id,
                trade_id=trade_id,
                dry_run=True,
            )

        self._update_request_status(request_id, status="SUBMITTED", submitted=True)
        response = executor.execute_trade(
            symbol=symbol,
            direction=side,
            lot_size=lot_size,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if str(response.get("status") or "").lower() != "filled":
            error = str(response.get("error") or "execute_trade failed")
            self._update_request_status(
                request_id,
                status="FAILED",
                error_code="BROKER_SUBMIT_FAILED",
                error_message=error,
                finished=True,
            )
            self._record_event(
                request_id=request_id,
                profile_id=profile_id,
                broker_position_id=None,
                trade_id=trade_id,
                event_type="SUBMIT_FAIL",
                broker_result=response,
                note=error,
            )
            return ActionResult(
                request_id=request_id,
                status="FAILED",
                action_type="EXECUTE_SIGNAL",
                profile_id=profile_id,
                trade_id=trade_id,
                error_code="BROKER_SUBMIT_FAILED",
                error_message=error,
                broker_result=response,
            )

        broker_position_id = str(response.get("position_id") or response.get("orderId") or response.get("id") or "")
        fill_price = float(response.get("price") or trade.get("expected_entry") or 0.0)
        fill_ts = _now_utc()
        mark_open_from_operator(
            self.db_path,
            trade_id,
            broker_order_id=broker_position_id,
            broker_position_id=broker_position_id,
            fill_price=fill_price,
            fill_qty=lot_size,
            filled_at_utc=fill_ts,
            expected_entry=float(trade.get("expected_entry") or fill_price),
            side=side,
        )
        self._upsert_context_position(
            profile_id=profile_id,
            source=str(profile.get("context_source_id") or "operator_actions"),
            broker_position_id=broker_position_id,
            symbol=symbol,
            side=side,
            lots=lot_size,
            entry_price=fill_price,
            current_price=fill_price,
            sl=stop_loss,
            tp=take_profit,
            floating_pnl=0.0,
            open_time_utc=fill_ts,
        )
        self._update_request_status(request_id, status="RECONCILED", finished=True)
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id,
            event_type="SUBMIT_OK",
            broker_result=response,
        )
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id,
            event_type="BROKER_FILLED",
            broker_result=response,
        )
        self._record_lifecycle_action(
            trade_id=trade_id,
            action_type="EXECUTE_SIGNAL",
            action_status="RECONCILED",
            idempotency_key=str(request.get("idempotency_key") or ""),
            note="operator executed signal",
        )
        return ActionResult(
            request_id=request_id,
            status="RECONCILED",
            action_type="EXECUTE_SIGNAL",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id,
            broker_result=response,
        )

    def _handle_close_position(self, request: dict[str, Any], *, dry_run: bool) -> ActionResult:
        request_id = int(request["request_id"])
        profile_id = str(request["profile_id"] or "")
        broker_position_id = str(request.get("broker_position_id") or "")
        duplicate_req = self._validate_no_pending_conflict(
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            action_types=("CLOSE_POSITION", "CLOSE_AT_TIMEOUT", "SET_EXACT_SL_TP", "MOVE_SL_TO_BREAKEVEN"),
        )
        if duplicate_req and int(duplicate_req) != request_id:
            return self._reject(
                request,
                error_code="DUPLICATE_REQUEST",
                error_message=f"pending action already exists request_id={duplicate_req}",
            )

        executor = self._build_executor(profile_id)
        try:
            self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        except PositionSourceStaleError as exc:
            return self._reject(request, error_code="POSITION_SOURCE_STALE", error_message=str(exc))
        live_positions = self._load_live_positions(executor)
        live_position = next((pos for pos in live_positions if pos["broker_position_id"] == broker_position_id), None)
        if not live_position:
            return self._reject(
                request,
                error_code="POSITION_NOT_FOUND",
                error_message=f"broker_position_id={broker_position_id} missing on broker books",
            )
        context_position = self._load_context_position(profile_id, broker_position_id)
        self._update_request_status(request_id, status="VALIDATED")
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=None,
            event_type="VALIDATED_OK",
            event_payload={"symbol": live_position["symbol"], "side": live_position["side"]},
        )
        if dry_run:
            return ActionResult(
                request_id=request_id,
                status="VALIDATED",
                action_type="CLOSE_POSITION",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                dry_run=True,
            )

        self._update_request_status(request_id, status="SUBMITTED", submitted=True)
        response = executor.close_position(
            position_id=broker_position_id,
            symbol=str(live_position["symbol"] or ""),
            reason="POSITION_MANAGER_CLOSE",
        )
        if str(response.get("status") or "").lower() != "closed":
            error = str(response.get("error") or "close_position failed")
            self._update_request_status(
                request_id,
                status="FAILED",
                error_code="BROKER_CLOSE_FAILED",
                error_message=error,
                finished=True,
            )
            self._record_event(
                request_id=request_id,
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=None,
                event_type="SUBMIT_FAIL",
                broker_result=response,
                note=error,
            )
            return ActionResult(
                request_id=request_id,
                status="FAILED",
                action_type="CLOSE_POSITION",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                error_code="BROKER_CLOSE_FAILED",
                error_message=error,
                broker_result=response,
            )

        trade_id = ""
        if context_position or broker_position_id:
            trade = None
            with sqlite_conn(self.db_path) as con:
                con.row_factory = sqlite3.Row
                trade_row = con.execute(
                    """
                    SELECT trade_id
                    FROM vanguard_trade_journal
                    WHERE profile_id = ?
                      AND broker_position_id = ?
                      AND status = 'OPEN'
                    LIMIT 1
                    """,
                    (profile_id, broker_position_id),
                ).fetchone()
                trade = _rowdict(trade_row)
            trade_id = str(trade.get("trade_id") or "")
            if trade_id:
                close_snapshot = self._resolve_close_snapshot(
                    profile_id=profile_id,
                    live_position=live_position,
                    fallback_position=context_position,
                )
                update_journal_closed_from_auto(
                    self.db_path,
                    broker_position_id=broker_position_id,
                    close_price=close_snapshot.get("close_price"),
                    closed_at_utc=_now_utc(),
                    close_reason="MANUAL_CLOSE_POSITION",
                    realized_pnl=close_snapshot.get("realized_pnl"),
                    holding_minutes=int(close_snapshot.get("holding_minutes") or 0),
                    broker_close_reason=close_snapshot.get("broker_close_reason"),
                )
        self._delete_context_position(profile_id, broker_position_id)
        self._update_request_status(request_id, status="RECONCILED", finished=True)
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            event_type="SUBMIT_OK",
            broker_result=response,
        )
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            event_type="BROKER_CLOSED",
            broker_result=response,
        )
        if trade_id:
            self._record_lifecycle_action(
                trade_id=trade_id,
                action_type="CLOSE_POSITION",
                action_status="RECONCILED",
                idempotency_key=str(request.get("idempotency_key") or ""),
                note="operator closed position",
            )
        return ActionResult(
            request_id=request_id,
            status="RECONCILED",
            action_type="CLOSE_POSITION",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            broker_result=response,
        )

    def _handle_close_at_timeout(self, request: dict[str, Any], *, dry_run: bool) -> ActionResult:
        request_id = int(request["request_id"])
        profile_id = str(request["profile_id"] or "")
        broker_position_id = str(request.get("broker_position_id") or "")
        if not self._timeout_close_enabled_for_profile(profile_id):
            return self._reject(
                request,
                error_code="TIMEOUT_CLOSE_NOT_ENABLED",
                error_message=f"profile_id={profile_id} timeout-close is only enabled for FTMO demo validation",
            )

        duplicate_req = self._validate_no_pending_conflict(
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            action_types=("CLOSE_AT_TIMEOUT", "CLOSE_POSITION", "SET_EXACT_SL_TP", "MOVE_SL_TO_BREAKEVEN"),
        )
        if duplicate_req and int(duplicate_req) != request_id:
            return self._reject(
                request,
                error_code="DUPLICATE_REQUEST",
                error_message=f"pending action already exists request_id={duplicate_req}",
            )

        executor = self._build_executor(profile_id)
        try:
            self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        except PositionSourceStaleError as exc:
            return self._reject(request, error_code="POSITION_SOURCE_STALE", error_message=str(exc))
        live_positions = self._load_live_positions(executor)
        live_position = next((pos for pos in live_positions if pos["broker_position_id"] == broker_position_id), None)
        if not live_position:
            return self._reject(
                request,
                error_code="POSITION_NOT_FOUND",
                error_message=f"broker_position_id={broker_position_id} missing on broker books",
            )
        trade_id = self._trade_id_for_position(profile_id, broker_position_id)
        if not trade_id:
            return self._reject(
                request,
                error_code="TIMEOUT_POLICY_MISSING",
                error_message=f"broker_position_id={broker_position_id} has no linked trade_id for timeout policy",
            )
        policy_rows = self._load_timeout_policy_rows([trade_id])
        policy_row = policy_rows.get(trade_id) or {}
        timeout_minutes = policy_row.get("policy_timeout_minutes")
        approved_ts = policy_row.get("approved_cycle_ts_utc")
        if timeout_minutes in (None, "") or not approved_ts:
            return self._reject(
                request,
                error_code="TIMEOUT_POLICY_MISSING",
                error_message=f"trade_id={trade_id} missing timeout policy metadata",
            )
        timeout_dt = datetime.fromisoformat(str(approved_ts).replace("Z", "+00:00")).astimezone(timezone.utc) + timedelta(minutes=int(timeout_minutes))
        if datetime.now(timezone.utc) < timeout_dt:
            return self._reject(
                request,
                error_code="TIMEOUT_NOT_REACHED",
                error_message=f"timeout not reached until {timeout_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            )

        context_position = self._load_context_position(profile_id, broker_position_id)
        self._update_request_status(request_id, status="VALIDATED")
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id,
            event_type="TIMEOUT_ARMED",
            event_payload={
                "policy_timeout_minutes": int(timeout_minutes),
                "policy_dedupe_mode": policy_row.get("policy_dedupe_minutes"),
                "timeout_at_utc": timeout_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        if dry_run:
            return ActionResult(
                request_id=request_id,
                status="VALIDATED",
                action_type="CLOSE_AT_TIMEOUT",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=trade_id,
                dry_run=True,
            )

        self._update_request_status(request_id, status="SUBMITTED", submitted=True)
        response = executor.close_position(
            position_id=broker_position_id,
            symbol=str(live_position["symbol"] or ""),
            reason="TIMEOUT_POLICY_CLOSE",
        )
        if str(response.get("status") or "").lower() != "closed":
            error = str(response.get("error") or "close_position failed")
            self._update_request_status(
                request_id,
                status="FAILED",
                error_code="BROKER_CLOSE_FAILED",
                error_message=error,
                finished=True,
            )
            self._record_event(
                request_id=request_id,
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=trade_id,
                event_type="TIMEOUT_CLOSE_FAIL",
                broker_result=response,
                note=error,
            )
            return ActionResult(
                request_id=request_id,
                status="FAILED",
                action_type="CLOSE_AT_TIMEOUT",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=trade_id,
                error_code="BROKER_CLOSE_FAILED",
                error_message=error,
                broker_result=response,
            )

        close_snapshot = self._resolve_close_snapshot(
            profile_id=profile_id,
            live_position=live_position,
            fallback_position=context_position,
        )
        update_journal_closed_from_auto(
            self.db_path,
            broker_position_id=broker_position_id,
            close_price=close_snapshot.get("close_price"),
            closed_at_utc=_now_utc(),
            close_reason="TIMEOUT_POLICY_CLOSE",
            realized_pnl=close_snapshot.get("realized_pnl"),
            holding_minutes=int(close_snapshot.get("holding_minutes") or 0),
            broker_close_reason=close_snapshot.get("broker_close_reason"),
        )
        self._delete_context_position(profile_id, broker_position_id)
        self._update_request_status(request_id, status="RECONCILED", finished=True)
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id,
            event_type="TIMEOUT_TRIGGERED",
            event_payload={"policy_timeout_minutes": int(timeout_minutes)},
        )
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id,
            event_type="TIMEOUT_CLOSE_OK",
            broker_result=response,
        )
        self._record_lifecycle_action(
            trade_id=trade_id,
            action_type="CLOSE_AT_TIMEOUT",
            action_status="RECONCILED",
            idempotency_key=str(request.get("idempotency_key") or ""),
            note="timeout policy close",
        )
        return ActionResult(
            request_id=request_id,
            status="RECONCILED",
            action_type="CLOSE_AT_TIMEOUT",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id,
            broker_result=response,
        )

    def _validate_exact_prices(
        self,
        *,
        side: str,
        current_price: float | None,
        new_sl: float,
        new_tp: float,
    ) -> tuple[bool, str | None]:
        if current_price in (None, ""):
            return True, None
        current = float(current_price)
        if str(side).upper() == "LONG":
            if not (new_sl < current < new_tp):
                return False, "LONG requires SL < market < TP"
        else:
            if not (new_tp < current < new_sl):
                return False, "SHORT requires TP < market < SL"
        return True, None

    def _handle_set_exact_sl_tp(self, request: dict[str, Any], *, dry_run: bool) -> ActionResult:
        request_id = int(request["request_id"])
        profile_id = str(request["profile_id"] or "")
        broker_position_id = str(request.get("broker_position_id") or "")
        duplicate_req = self._validate_no_pending_conflict(
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            action_types=("CLOSE_POSITION", "CLOSE_AT_TIMEOUT", "SET_EXACT_SL_TP", "MOVE_SL_TO_BREAKEVEN"),
        )
        if duplicate_req and int(duplicate_req) != request_id:
            return self._reject(
                request,
                error_code="DUPLICATE_REQUEST",
                error_message=f"pending action already exists request_id={duplicate_req}",
            )
        executor = self._build_executor(profile_id)
        try:
            self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        except PositionSourceStaleError as exc:
            return self._reject(request, error_code="POSITION_SOURCE_STALE", error_message=str(exc))
        live_positions = self._load_live_positions(executor)
        live_position = next((pos for pos in live_positions if pos["broker_position_id"] == broker_position_id), None)
        if not live_position:
            return self._reject(
                request,
                error_code="POSITION_NOT_FOUND",
                error_message=f"broker_position_id={broker_position_id} missing on broker books",
            )
        payload = _json_load(request.get("payload_json"))
        new_sl = float(payload.get("new_sl") or 0.0)
        new_tp = float(payload.get("new_tp") or 0.0)
        current_sl = live_position.get("current_sl")
        current_tp = live_position.get("current_tp")
        if current_sl is not None and current_tp is not None:
            if abs(float(current_sl) - new_sl) < 1e-9 and abs(float(current_tp) - new_tp) < 1e-9:
                return self._reject(
                    request,
                    error_code="NO_CHANGE",
                    error_message="new SL/TP match current live values",
                )
        context_position = self._load_context_position(profile_id, broker_position_id)
        is_valid, invalid_reason = self._validate_exact_prices(
            side=str(live_position["side"] or ""),
            current_price=context_position.get("current_price"),
            new_sl=new_sl,
            new_tp=new_tp,
        )
        if not is_valid:
            return self._reject(request, error_code="INVALID_SL_TP", error_message=str(invalid_reason))

        self._update_request_status(request_id, status="VALIDATED")
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=None,
            event_type="VALIDATED_OK",
            event_payload={"new_sl": new_sl, "new_tp": new_tp},
        )
        if dry_run:
            return ActionResult(
                request_id=request_id,
                status="VALIDATED",
                action_type="SET_EXACT_SL_TP",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                dry_run=True,
            )

        self._update_request_status(request_id, status="SUBMITTED", submitted=True)
        response = executor.modify_position(ticket=broker_position_id, sl=new_sl, tp=new_tp)
        if str(response.get("status") or "").lower() != "modified":
            error = str(response.get("error") or "modify_position failed")
            self._update_request_status(
                request_id,
                status="FAILED",
                error_code="BROKER_MODIFY_FAILED",
                error_message=error,
                finished=True,
            )
            self._record_event(
                request_id=request_id,
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=None,
                event_type="SUBMIT_FAIL",
                broker_result=response,
                note=error,
            )
            return ActionResult(
                request_id=request_id,
                status="FAILED",
                action_type="SET_EXACT_SL_TP",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                error_code="BROKER_MODIFY_FAILED",
                error_message=error,
                broker_result=response,
            )

        self._update_context_position_stops(profile_id, broker_position_id, new_sl, new_tp)
        trade_id = self._trade_id_for_position(profile_id, broker_position_id)
        self._update_request_status(request_id, status="RECONCILED", finished=True)
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            event_type="SUBMIT_OK",
            broker_result=response,
        )
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            event_type="BROKER_MODIFIED",
            broker_result=response,
        )
        if trade_id:
            self._record_lifecycle_action(
                trade_id=trade_id,
                action_type="SET_EXACT_SL_TP",
                action_status="RECONCILED",
                idempotency_key=str(request.get("idempotency_key") or ""),
                note="operator updated exact sl/tp",
            )
        return ActionResult(
            request_id=request_id,
            status="RECONCILED",
            action_type="SET_EXACT_SL_TP",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            broker_result=response,
        )

    def _trade_id_for_position(self, profile_id: str, broker_position_id: str) -> str:
        with sqlite_conn(self.db_path) as con:
            row = con.execute(
                """
                SELECT trade_id
                FROM vanguard_trade_journal
                WHERE profile_id = ?
                  AND broker_position_id = ?
                ORDER BY CASE status WHEN 'OPEN' THEN 0 ELSE 1 END, approved_cycle_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, str(broker_position_id)),
            ).fetchone()
        return str(row[0] if row else "")

    def _handle_move_breakeven(self, request: dict[str, Any], *, dry_run: bool) -> ActionResult:
        request_id = int(request["request_id"])
        profile_id = str(request["profile_id"] or "")
        broker_position_id = str(request.get("broker_position_id") or "")
        duplicate_req = self._validate_no_pending_conflict(
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            action_types=("CLOSE_POSITION", "CLOSE_AT_TIMEOUT", "SET_EXACT_SL_TP", "MOVE_SL_TO_BREAKEVEN"),
        )
        if duplicate_req and int(duplicate_req) != request_id:
            return self._reject(
                request,
                error_code="DUPLICATE_REQUEST",
                error_message=f"pending action already exists request_id={duplicate_req}",
            )
        executor = self._build_executor(profile_id)
        try:
            self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        except PositionSourceStaleError as exc:
            return self._reject(request, error_code="POSITION_SOURCE_STALE", error_message=str(exc))
        live_positions = self._load_live_positions(executor)
        live_position = next((pos for pos in live_positions if pos["broker_position_id"] == broker_position_id), None)
        if not live_position:
            return self._reject(
                request,
                error_code="POSITION_NOT_FOUND",
                error_message=f"broker_position_id={broker_position_id} missing on broker books",
            )
        trade_id = self._trade_id_for_position(profile_id, broker_position_id)
        trade = get_trade_row(self.db_path, trade_id) if trade_id else None
        fill_anchor = None
        if trade:
            fill_anchor = trade.get("fill_price") or trade.get("expected_entry")
        if fill_anchor in (None, ""):
            fill_anchor = live_position.get("entry_price")
        if fill_anchor in (None, ""):
            return self._reject(
                request,
                error_code="BREAKEVEN_REFERENCE_MISSING",
                error_message="missing fill/entry reference for breakeven move",
            )
        new_sl = float(fill_anchor)
        current_tp = live_position.get("current_tp")
        if current_tp in (None, ""):
            return self._reject(
                request,
                error_code="TP_MISSING",
                error_message="current live TP is missing; cannot preserve TP for breakeven move",
            )
        current_sl = live_position.get("current_sl")
        if current_sl is not None and abs(float(current_sl) - new_sl) < 1e-9:
            return self._reject(
                request,
                error_code="NO_CHANGE",
                error_message="SL already at breakeven anchor",
            )
        self._update_request_status(request_id, status="VALIDATED")
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            event_type="VALIDATED_OK",
            event_payload={"breakeven_sl": new_sl, "preserve_tp": float(current_tp)},
        )
        if dry_run:
            return ActionResult(
                request_id=request_id,
                status="VALIDATED",
                action_type="MOVE_SL_TO_BREAKEVEN",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=trade_id or None,
                dry_run=True,
            )

        self._update_request_status(request_id, status="SUBMITTED", submitted=True)
        response = executor.modify_position(ticket=broker_position_id, sl=new_sl, tp=float(current_tp))
        if str(response.get("status") or "").lower() != "modified":
            error = str(response.get("error") or "modify_position failed")
            self._update_request_status(
                request_id,
                status="FAILED",
                error_code="BROKER_MODIFY_FAILED",
                error_message=error,
                finished=True,
            )
            self._record_event(
                request_id=request_id,
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=trade_id or None,
                event_type="SUBMIT_FAIL",
                broker_result=response,
                note=error,
            )
            return ActionResult(
                request_id=request_id,
                status="FAILED",
                action_type="MOVE_SL_TO_BREAKEVEN",
                profile_id=profile_id,
                broker_position_id=broker_position_id,
                trade_id=trade_id or None,
                error_code="BROKER_MODIFY_FAILED",
                error_message=error,
                broker_result=response,
            )

        self._update_context_position_stops(profile_id, broker_position_id, new_sl, float(current_tp))
        self._update_request_status(request_id, status="RECONCILED", finished=True)
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            event_type="SUBMIT_OK",
            broker_result=response,
        )
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            event_type="BROKER_MODIFIED",
            broker_result=response,
            note="breakeven move applied",
        )
        if trade_id:
            self._record_lifecycle_action(
                trade_id=trade_id,
                action_type="MOVE_SL_TO_BREAKEVEN",
                action_status="RECONCILED",
                idempotency_key=str(request.get("idempotency_key") or ""),
                note="operator moved stop to breakeven",
            )
        return ActionResult(
            request_id=request_id,
            status="RECONCILED",
            action_type="MOVE_SL_TO_BREAKEVEN",
            profile_id=profile_id,
            broker_position_id=broker_position_id,
            trade_id=trade_id or None,
            broker_result=response,
        )

    def _handle_flatten_profile(self, request: dict[str, Any], *, dry_run: bool) -> ActionResult:
        request_id = int(request["request_id"])
        profile_id = str(request["profile_id"] or "")
        duplicate_req = self._validate_no_pending_conflict(
            profile_id=profile_id,
            action_types=("FLATTEN_PROFILE",),
        )
        if duplicate_req and int(duplicate_req) != request_id:
            return self._reject(
                request,
                error_code="DUPLICATE_REQUEST",
                error_message=f"pending flatten request already exists request_id={duplicate_req}",
            )
        executor = self._build_executor(profile_id)
        try:
            self._ensure_live_position_source_fresh(executor, profile_id=profile_id)
        except PositionSourceStaleError as exc:
            return self._reject(request, error_code="POSITION_SOURCE_STALE", error_message=str(exc))
        live_positions = self._load_live_positions(executor)
        self._update_request_status(request_id, status="VALIDATED")
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=None,
            trade_id=None,
            event_type="VALIDATED_OK",
            event_payload={"positions": [pos["broker_position_id"] for pos in live_positions]},
        )
        if dry_run:
            return ActionResult(
                request_id=request_id,
                status="VALIDATED",
                action_type="FLATTEN_PROFILE",
                profile_id=profile_id,
                dry_run=True,
            )

        child_ids: list[int] = []
        for pos in live_positions:
            child_id = self.queue_close_position(
                profile_id=profile_id,
                broker_position_id=str(pos["broker_position_id"]),
                requested_by=str(request["requested_by"] or "flatten"),
                source="flatten_profile",
            )
            child_ids.append(child_id)
            self.process_request(child_id, dry_run=False)
        self._update_request_status(request_id, status="RECONCILED", finished=True)
        self._record_event(
            request_id=request_id,
            profile_id=profile_id,
            broker_position_id=None,
            trade_id=None,
            event_type="RECONCILED",
            event_payload={"child_request_ids": child_ids},
            note="flatten dispatched child close requests",
        )
        return ActionResult(
            request_id=request_id,
            status="RECONCILED",
            action_type="FLATTEN_PROFILE",
            profile_id=profile_id,
            broker_result={"child_request_ids": child_ids},
        )


def get_service(
    db_path: str | None = None,
    runtime_config: dict[str, Any] | None = None,
    executor_factory: Optional[Callable[[str, dict[str, Any], str], Any]] = None,
) -> PositionActionService:
    return PositionActionService(
        db_path=db_path,
        runtime_config=runtime_config,
        executor_factory=executor_factory,
    )
