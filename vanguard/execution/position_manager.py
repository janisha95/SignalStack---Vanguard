"""
position_manager.py — Post-entry lifecycle review engine for open positions.

This module is intentionally conservative in v1:
  - append-only lifecycle reviews
  - no live execution integration by default
  - safe HOLD defaults when signal or broker truth is unavailable
  - policy binding prefers the original trade contract over runtime defaults
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS vanguard_position_lifecycle (
    review_id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id                   TEXT NOT NULL,
    trade_id                     TEXT NOT NULL,
    broker_position_id           TEXT,
    symbol                       TEXT NOT NULL,
    side                         TEXT NOT NULL,
    policy_id                    TEXT,
    model_id                     TEXT,
    model_family                 TEXT,
    opened_cycle_ts_utc          TEXT,
    opened_at_utc                TEXT,
    reviewed_at_utc              TEXT NOT NULL,
    holding_minutes              REAL,
    bars_elapsed                 INTEGER,
    lifecycle_phase              TEXT NOT NULL,
    review_action                TEXT NOT NULL,
    review_reason_codes_json     TEXT NOT NULL,
    extension_granted_count      INTEGER NOT NULL DEFAULT 0,
    breakeven_moved              INTEGER NOT NULL DEFAULT 0,
    last_action_type             TEXT,
    last_action_submitted_at_utc TEXT,
    last_action_status           TEXT,
    last_action_idempotency_key  TEXT,
    thesis_snapshot_json         TEXT NOT NULL,
    same_side_signal_snapshot_json TEXT NOT NULL,
    opposite_side_signal_snapshot_json TEXT NOT NULL,
    latest_state_snapshot_json   TEXT NOT NULL,
    latest_health_snapshot_json  TEXT NOT NULL,
    latest_position_snapshot_json TEXT NOT NULL,
    manager_note                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_position_lifecycle_trade_time
    ON vanguard_position_lifecycle(trade_id, reviewed_at_utc);
"""

LATEST_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS vanguard_position_lifecycle_latest AS
SELECT vpl.*
FROM vanguard_position_lifecycle vpl
JOIN (
    SELECT trade_id, MAX(review_id) AS max_review_id
    FROM vanguard_position_lifecycle
    GROUP BY trade_id
) latest
  ON latest.trade_id = vpl.trade_id
 AND latest.max_review_id = vpl.review_id;
"""


@dataclass
class PositionDecision:
    action: str
    phase: str
    reason_codes: list[str]
    note: str = ""
    target_sl: Optional[float] = None


def _parse_utc(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, "", b""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _rowdict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


class PositionManager:
    """Conservative post-entry lifecycle manager for open trades."""

    def __init__(self, config: dict[str, Any], db_path: str) -> None:
        self.config = config
        self.db_path = db_path
        pm_cfg = config.get("position_manager") or {}
        self.enabled = bool(pm_cfg.get("enabled", False))
        self.poll_interval_seconds = int(pm_cfg.get("poll_interval_seconds") or 30)
        self.default_policy_by_asset_class = dict(pm_cfg.get("default_policy_by_asset_class") or {})
        self.policies = dict(pm_cfg.get("policies") or {})

    def ensure_schema(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            for stmt in DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    con.execute(stmt)
            con.execute("DROP VIEW IF EXISTS vanguard_position_lifecycle_latest")
            con.execute(LATEST_VIEW_SQL)
            con.commit()

    def review_profile(self, profile: dict[str, Any], reviewed_at_utc: str | None = None) -> list[dict[str, Any]]:
        """Review currently open positions for one profile and append lifecycle rows."""
        self.ensure_schema()
        reviewed_at = reviewed_at_utc or _iso_now()
        profile_id = str(profile.get("id") or "")
        if not profile_id:
            return []
        rows: list[dict[str, Any]] = []
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            trades = self._load_open_trades(con, profile_id)
            for trade in trades:
                record = self._review_trade(con, profile, trade, reviewed_at)
                self._append_review(con, record)
                rows.append(record)
            con.commit()
        return rows

    def _load_open_trades(self, con: sqlite3.Connection, profile_id: str) -> list[sqlite3.Row]:
        return list(
            con.execute(
                """
                SELECT *
                FROM vanguard_trade_journal
                WHERE profile_id = ?
                  AND status = 'OPEN'
                ORDER BY filled_at_utc ASC, approved_cycle_ts_utc ASC
                """,
                (profile_id,),
            ).fetchall()
        )

    def _review_trade(
        self,
        con: sqlite3.Connection,
        profile: dict[str, Any],
        trade: sqlite3.Row,
        reviewed_at_utc: str,
    ) -> dict[str, Any]:
        trade_row = _rowdict(trade)
        policy_id, policy = self._resolve_policy(profile, trade_row)
        symbol = str(trade_row.get("symbol") or "").upper()
        side = str(trade_row.get("side") or "").upper()
        asset_class = str(trade_row.get("asset_class") or "").lower()
        reviewed_dt = _parse_utc(reviewed_at_utc) or datetime.now(timezone.utc)
        opened_dt = _parse_utc(trade_row.get("filled_at_utc") or trade_row.get("submitted_at_utc") or trade_row.get("approved_cycle_ts_utc"))
        opened_cycle_dt = _parse_utc(trade_row.get("approved_cycle_ts_utc"))
        holding_minutes = self._holding_minutes(opened_dt, reviewed_dt)
        bars_elapsed = self._bars_elapsed(opened_cycle_dt, reviewed_dt, policy)

        broker_position = self._load_broker_position(
            con,
            profile_id=str(profile.get("id") or ""),
            broker_position_id=str(trade_row.get("broker_position_id") or ""),
            symbol=symbol,
        )
        same_tradeability = self._load_latest_tradeability(
            con, str(profile.get("id") or ""), symbol, side
        )
        opposite_side = "SHORT" if side == "LONG" else "LONG"
        opposite_tradeability = self._load_latest_tradeability(
            con, str(profile.get("id") or ""), symbol, opposite_side
        )
        same_selection = self._load_latest_selection(
            con, str(profile.get("id") or ""), symbol, side
        )
        opposite_selection = self._load_latest_selection(
            con, str(profile.get("id") or ""), symbol, opposite_side
        )
        state_snapshot = self._load_state_snapshot(
            con, str(profile.get("id") or ""), symbol, asset_class
        )
        health_snapshot = self._load_health_snapshot(
            con, str(profile.get("id") or ""), symbol, side
        )
        latest_review = self._load_latest_review(con, str(trade_row.get("trade_id") or ""))

        decision = self._evaluate(
            trade_row=trade_row,
            policy=policy,
            broker_position=broker_position,
            same_tradeability=same_tradeability,
            opposite_tradeability=opposite_tradeability,
            same_selection=same_selection,
            opposite_selection=opposite_selection,
            state_snapshot=state_snapshot,
            health_snapshot=health_snapshot,
            holding_minutes=holding_minutes,
            bars_elapsed=bars_elapsed,
            latest_review=latest_review,
        )

        extension_count = int((latest_review or {}).get("extension_granted_count") or 0)
        if "EXTENSION_GRANTED" in decision.reason_codes:
            extension_count += 1
        breakeven_moved = int((latest_review or {}).get("breakeven_moved") or 0)
        if decision.action == "MODIFY_SL" and decision.target_sl is not None:
            breakeven_moved = 1

        return {
            "profile_id": str(profile.get("id") or ""),
            "trade_id": str(trade_row.get("trade_id") or ""),
            "broker_position_id": str(trade_row.get("broker_position_id") or trade_row.get("broker_order_id") or ""),
            "symbol": symbol,
            "side": side,
            "policy_id": policy_id,
            "model_id": trade_row.get("model_id"),
            "model_family": trade_row.get("model_family"),
            "opened_cycle_ts_utc": trade_row.get("approved_cycle_ts_utc"),
            "opened_at_utc": trade_row.get("filled_at_utc") or trade_row.get("submitted_at_utc"),
            "reviewed_at_utc": reviewed_at_utc,
            "holding_minutes": holding_minutes,
            "bars_elapsed": bars_elapsed,
            "lifecycle_phase": decision.phase,
            "review_action": decision.action,
            "review_reason_codes_json": json.dumps(decision.reason_codes, sort_keys=True),
            "extension_granted_count": extension_count,
            "breakeven_moved": breakeven_moved,
            "last_action_type": (latest_review or {}).get("last_action_type"),
            "last_action_submitted_at_utc": (latest_review or {}).get("last_action_submitted_at_utc"),
            "last_action_status": (latest_review or {}).get("last_action_status"),
            "last_action_idempotency_key": (latest_review or {}).get("last_action_idempotency_key"),
            "thesis_snapshot_json": json.dumps(self._build_thesis_snapshot(trade_row), sort_keys=True, default=str),
            "same_side_signal_snapshot_json": json.dumps(
                {"tradeability": same_tradeability, "selection": same_selection}, sort_keys=True, default=str
            ),
            "opposite_side_signal_snapshot_json": json.dumps(
                {"tradeability": opposite_tradeability, "selection": opposite_selection}, sort_keys=True, default=str
            ),
            "latest_state_snapshot_json": json.dumps(state_snapshot, sort_keys=True, default=str),
            "latest_health_snapshot_json": json.dumps(health_snapshot, sort_keys=True, default=str),
            "latest_position_snapshot_json": json.dumps(broker_position, sort_keys=True, default=str),
            "manager_note": decision.note,
        }

    def _resolve_policy(self, profile: dict[str, Any], trade_row: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        policy_id = str(trade_row.get("policy_id") or "").strip() or None
        policy = dict(self.policies.get(policy_id or "", {}) or {})
        if policy:
            return policy_id, policy
        asset_class = str(trade_row.get("asset_class") or "").lower()
        fallback_id = str(self.default_policy_by_asset_class.get(asset_class) or "").strip() or None
        if fallback_id:
            return fallback_id, dict(self.policies.get(fallback_id) or {})
        return policy_id, {}

    def _load_broker_position(
        self,
        con: sqlite3.Connection,
        *,
        profile_id: str,
        broker_position_id: str,
        symbol: str,
    ) -> dict[str, Any]:
        row = None
        if broker_position_id:
            row = con.execute(
                """
                SELECT *
                FROM vanguard_context_positions_latest
                WHERE profile_id = ?
                  AND ticket = ?
                ORDER BY received_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, broker_position_id),
            ).fetchone()
        if row is None:
            row = con.execute(
                """
                SELECT *
                FROM vanguard_context_positions_latest
                WHERE profile_id = ?
                  AND symbol = ?
                ORDER BY received_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, symbol),
            ).fetchone()
        return _rowdict(row)

    def _load_latest_tradeability(
        self,
        con: sqlite3.Connection,
        profile_id: str,
        symbol: str,
        direction: str,
    ) -> dict[str, Any]:
        row = con.execute(
            """
            SELECT *
            FROM vanguard_v5_tradeability
            WHERE profile_id = ?
              AND symbol = ?
              AND direction = ?
            ORDER BY cycle_ts_utc DESC
            LIMIT 1
            """,
            (profile_id, symbol, direction),
        ).fetchone()
        return _rowdict(row)

    def _load_latest_selection(
        self,
        con: sqlite3.Connection,
        profile_id: str,
        symbol: str,
        direction: str,
    ) -> dict[str, Any]:
        row = con.execute(
            """
            SELECT *
            FROM vanguard_v5_selection
            WHERE profile_id = ?
              AND symbol = ?
              AND direction = ?
            ORDER BY cycle_ts_utc DESC
            LIMIT 1
            """,
            (profile_id, symbol, direction),
        ).fetchone()
        return _rowdict(row)

    def _load_state_snapshot(
        self,
        con: sqlite3.Connection,
        profile_id: str,
        symbol: str,
        asset_class: str,
    ) -> dict[str, Any]:
        if asset_class == "forex":
            row = con.execute(
                """
                SELECT *
                FROM vanguard_forex_pair_state
                WHERE profile_id = ?
                  AND symbol = ?
                ORDER BY cycle_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, symbol),
            ).fetchone()
            return _rowdict(row)
        if asset_class == "crypto":
            row = con.execute(
                """
                SELECT *
                FROM vanguard_crypto_symbol_state
                WHERE profile_id = ?
                  AND symbol = ?
                ORDER BY cycle_ts_utc DESC
                LIMIT 1
                """,
                (profile_id, symbol),
            ).fetchone()
            return _rowdict(row)
        return {}

    def _load_health_snapshot(
        self,
        con: sqlite3.Connection,
        profile_id: str,
        symbol: str,
        direction: str,
    ) -> dict[str, Any]:
        row = con.execute(
            """
            SELECT *
            FROM vanguard_v6_context_health
            WHERE profile_id = ?
              AND symbol = ?
              AND direction = ?
            ORDER BY cycle_ts_utc DESC
            LIMIT 1
            """,
            (profile_id, symbol, direction),
        ).fetchone()
        data = _rowdict(row)
        if data.get("reasons_json"):
            data["reasons"] = _json_load(data.get("reasons_json"), [])
        return data

    def _load_latest_review(self, con: sqlite3.Connection, trade_id: str) -> dict[str, Any]:
        row = con.execute(
            """
            SELECT *
            FROM vanguard_position_lifecycle_latest
            WHERE trade_id = ?
            LIMIT 1
            """,
            (trade_id,),
        ).fetchone()
        return _rowdict(row)

    def _evaluate(
        self,
        *,
        trade_row: dict[str, Any],
        policy: dict[str, Any],
        broker_position: dict[str, Any],
        same_tradeability: dict[str, Any],
        opposite_tradeability: dict[str, Any],
        same_selection: dict[str, Any],
        opposite_selection: dict[str, Any],
        state_snapshot: dict[str, Any],
        health_snapshot: dict[str, Any],
        holding_minutes: float,
        bars_elapsed: int,
        latest_review: dict[str, Any],
    ) -> PositionDecision:
        side = str(trade_row.get("side") or "").upper()
        hard_close_bars = int(policy.get("hard_close_bars") or 12)

        if latest_review and latest_review.get("last_action_status") in {"PENDING_SUBMIT", "SUBMITTED"}:
            return PositionDecision(
                action="HOLD",
                phase=self._phase_from_bars(bars_elapsed, hard_close_bars),
                reason_codes=["ACTION_LOCKED_PENDING"],
                note="Previous manager action still pending.",
            )

        if self._is_fatal_health(health_snapshot, policy):
            return PositionDecision(
                action="CLOSE",
                phase=self._phase_from_bars(bars_elapsed, hard_close_bars),
                reason_codes=["HEALTH_EXIT"],
                note="Fatal post-entry health condition detected.",
            )

        if bars_elapsed >= hard_close_bars:
            return PositionDecision(
                action="CLOSE",
                phase="FINALIZED",
                reason_codes=["EXTENDED_HOLD_60M"],
                note="Hard close horizon reached.",
            )

        if not broker_position:
            return PositionDecision(
                action="HOLD",
                phase=self._phase_from_bars(bars_elapsed, hard_close_bars),
                reason_codes=["POSITION_TRUTH_UNAVAILABLE"],
                note="Missing broker position truth; hold until confirmed fatal.",
            )

        signal_missing = (
            not same_tradeability
            or not same_selection
            or not opposite_tradeability
            or not opposite_selection
        )
        if signal_missing:
            return PositionDecision(
                action="HOLD",
                phase=self._phase_from_bars(bars_elapsed, hard_close_bars),
                reason_codes=["SIGNAL_UNAVAILABLE"],
                note="Same-side or opposite-side lifecycle signal unavailable.",
            )

        if holding_minutes < 10:
            return PositionDecision(
                action="HOLD",
                phase="INITIAL_PROTECTION",
                reason_codes=["INITIAL_FLIP_GRACE"],
                note="Within initial flip-grace window.",
            )

        return PositionDecision(
            action="HOLD",
            phase=self._phase_from_bars(bars_elapsed, hard_close_bars),
            reason_codes=["NO_MANAGER_EXIT"],
            note=f"Monitoring {trade_row.get('symbol')} {side} without exit trigger.",
        )

    def _append_review(self, con: sqlite3.Connection, record: dict[str, Any]) -> None:
        con.execute(
            """
            INSERT INTO vanguard_position_lifecycle (
                profile_id, trade_id, broker_position_id, symbol, side,
                policy_id, model_id, model_family, opened_cycle_ts_utc,
                opened_at_utc, reviewed_at_utc, holding_minutes, bars_elapsed,
                lifecycle_phase, review_action, review_reason_codes_json,
                extension_granted_count, breakeven_moved,
                last_action_type, last_action_submitted_at_utc, last_action_status,
                last_action_idempotency_key, thesis_snapshot_json,
                same_side_signal_snapshot_json, opposite_side_signal_snapshot_json,
                latest_state_snapshot_json, latest_health_snapshot_json,
                latest_position_snapshot_json, manager_note
            ) VALUES (
                :profile_id, :trade_id, :broker_position_id, :symbol, :side,
                :policy_id, :model_id, :model_family, :opened_cycle_ts_utc,
                :opened_at_utc, :reviewed_at_utc, :holding_minutes, :bars_elapsed,
                :lifecycle_phase, :review_action, :review_reason_codes_json,
                :extension_granted_count, :breakeven_moved,
                :last_action_type, :last_action_submitted_at_utc, :last_action_status,
                :last_action_idempotency_key, :thesis_snapshot_json,
                :same_side_signal_snapshot_json, :opposite_side_signal_snapshot_json,
                :latest_state_snapshot_json, :latest_health_snapshot_json,
                :latest_position_snapshot_json, :manager_note
            )
            """,
            record,
        )

    def _is_fatal_health(self, health_snapshot: dict[str, Any], policy: dict[str, Any]) -> bool:
        if not health_snapshot:
            return False
        reasons = set(_json_load(health_snapshot.get("reasons_json"), health_snapshot.get("reasons") or []))
        fatal_codes = set(policy.get("extension_disallowed_post_entry_health_codes") or [])
        if fatal_codes.intersection(reasons):
            return True
        return str(health_snapshot.get("v6_state") or "").upper() == "ERROR"

    @staticmethod
    def _holding_minutes(opened_dt: Optional[datetime], reviewed_dt: datetime) -> float:
        if opened_dt is None:
            return 0.0
        return max(0.0, (reviewed_dt - opened_dt).total_seconds() / 60.0)

    @staticmethod
    def _bars_elapsed(opened_cycle_dt: Optional[datetime], reviewed_dt: datetime, policy: dict[str, Any]) -> int:
        if opened_cycle_dt is None:
            return 0
        bar_minutes = int(policy.get("bar_interval_minutes") or 5)
        if bar_minutes <= 0:
            bar_minutes = 5
        delta_minutes = max(0.0, (reviewed_dt - opened_cycle_dt).total_seconds() / 60.0)
        return int(delta_minutes // bar_minutes)

    @staticmethod
    def _phase_from_bars(bars_elapsed: int, hard_close_bars: int) -> str:
        if bars_elapsed < 2:
            return "INITIAL_PROTECTION"
        if bars_elapsed < 6:
            return "MONITORING_PRE_30M"
        if bars_elapsed < hard_close_bars:
            return "EXTENDED_TO_60M"
        return "FINALIZED"

    @staticmethod
    def _build_thesis_snapshot(trade_row: dict[str, Any]) -> dict[str, Any]:
        return {
            "approved_cycle_ts_utc": trade_row.get("approved_cycle_ts_utc"),
            "expected_entry": trade_row.get("expected_entry"),
            "expected_sl": trade_row.get("expected_sl"),
            "expected_tp": trade_row.get("expected_tp"),
            "approved_qty": trade_row.get("approved_qty"),
            "policy_id": trade_row.get("policy_id"),
            "model_id": trade_row.get("model_id"),
            "model_family": trade_row.get("model_family"),
            "original_entry_ref_price": trade_row.get("original_entry_ref_price"),
            "original_stop_price": trade_row.get("original_stop_price"),
            "original_tp_price": trade_row.get("original_tp_price"),
            "original_risk_dollars": trade_row.get("original_risk_dollars"),
            "original_r_distance_native": trade_row.get("original_r_distance_native"),
            "original_rr_multiple": trade_row.get("original_rr_multiple"),
            "original_r_multiple_target": trade_row.get("original_r_multiple_target"),
        }
