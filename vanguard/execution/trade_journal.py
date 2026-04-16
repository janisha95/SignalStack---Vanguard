"""
trade_journal.py — Per-trade lifecycle journal for Phase 3b.

Semantics:
  - insert_approval_row() : append-once on approval (status=PENDING_FILL or FORWARD_TRACKED)
  - update_submitted()    : set broker_order_id + submitted_at_utc (PENDING_FILL → PENDING_FILL)
  - update_filled()       : compute slippage, set status=OPEN (PENDING_FILL → OPEN)
  - update_rejected_by_broker() : set status=REJECTED_BY_BROKER (PENDING_FILL → REJECTED_BY_BROKER)

All writer functions enforce status preconditions. Violating them raises JournalStateError.
Close/reconcile fields (close_price, closed_at_utc, etc.) are populated by slices 3d/3f.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = {
    "PENDING_FILL",
    "OPEN",
    "CLOSED",
    "REJECTED_BY_BROKER",
    "FORWARD_TRACKED",
    "RECONCILED_CLOSED",
}

DDL = """
CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
    trade_id                TEXT PRIMARY KEY,
    profile_id              TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    asset_class             TEXT NOT NULL,
    side                    TEXT NOT NULL,
    -- Approval phase
    approved_cycle_ts_utc   TEXT NOT NULL,
    approved_qty            REAL NOT NULL,
    expected_entry          REAL NOT NULL,
    expected_sl             REAL NOT NULL,
    expected_tp             REAL NOT NULL,
    policy_id               TEXT,
    model_id                TEXT,
    model_family            TEXT,
    original_entry_ref_price REAL,
    original_stop_price     REAL,
    original_tp_price       REAL,
    original_risk_dollars   REAL,
    original_r_distance_native REAL,
    original_rr_multiple    REAL,
    original_r_multiple_target REAL,
    approval_reasoning_json TEXT NOT NULL,
    spread_pct_at_approval  REAL,
    -- Execution phase
    broker_order_id         TEXT,
    broker_position_id      TEXT,
    submitted_at_utc        TEXT,
    fill_price              REAL,
    fill_qty                REAL,
    filled_at_utc           TEXT,
    slippage_price          REAL,
    slippage_bps            REAL,
    commission              REAL,
    -- Lifecycle phase (populated by slices 3d/3f — stays null here)
    status                  TEXT NOT NULL DEFAULT 'PENDING_FILL',
    close_price             REAL,
    closed_at_utc           TEXT,
    close_reason            TEXT,
    broker_close_reason     TEXT,
    manager_close_reason    TEXT,
    close_detected_via      TEXT,
    close_order_id          TEXT,
    close_fill_price        REAL,
    close_fill_ts_utc       TEXT,
    realized_pnl            REAL,
    realized_rrr            REAL,
    holding_minutes         INTEGER,
    last_synced_at_utc      TEXT NOT NULL,
    notes                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_profile_status
    ON vanguard_trade_journal(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_journal_symbol
    ON vanguard_trade_journal(symbol);
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class JournalStateError(Exception):
    """Raised when a writer function finds the row in an unexpected status."""


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def ensure_table(db_path: str) -> None:
    """Create vanguard_trade_journal and its indexes if they do not exist."""
    with sqlite3.connect(db_path) as con:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        _ensure_column(con, "policy_id", "TEXT")
        _ensure_column(con, "model_id", "TEXT")
        _ensure_column(con, "model_family", "TEXT")
        _ensure_column(con, "original_entry_ref_price", "REAL")
        _ensure_column(con, "original_stop_price", "REAL")
        _ensure_column(con, "original_tp_price", "REAL")
        _ensure_column(con, "original_risk_dollars", "REAL")
        _ensure_column(con, "original_r_distance_native", "REAL")
        _ensure_column(con, "original_rr_multiple", "REAL")
        _ensure_column(con, "original_r_multiple_target", "REAL")
        _ensure_column(con, "broker_close_reason", "TEXT")
        _ensure_column(con, "manager_close_reason", "TEXT")
        _ensure_column(con, "close_detected_via", "TEXT")
        _ensure_column(con, "close_order_id", "TEXT")
        _ensure_column(con, "close_fill_price", "REAL")
        _ensure_column(con, "close_fill_ts_utc", "TEXT")
        con.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _policy_decision_to_json(policy_decision: Any) -> str:
    """Serialize policy_decision to a JSON string.

    Accepts:
      - a dataclass instance (uses asdict)
      - a dict with an 'as_dict' method
      - a plain dict
      - an object with an 'as_dict()' method
    """
    if policy_decision is None:
        return "{}"
    if is_dataclass(policy_decision) and not isinstance(policy_decision, type):
        return json.dumps(asdict(policy_decision), default=str)
    if hasattr(policy_decision, "as_dict"):
        return json.dumps(policy_decision.as_dict(), default=str)
    if isinstance(policy_decision, dict):
        return json.dumps(policy_decision, default=str)
    return json.dumps({"raw": str(policy_decision)})


def _get_row_status(con: sqlite3.Connection, trade_id: str) -> Optional[str]:
    """Return current status for trade_id, or None if not found."""
    row = con.execute(
        "SELECT status FROM vanguard_trade_journal WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    return row[0] if row else None


def get_trade_row(db_path: str, trade_id: str) -> Optional[dict[str, Any]]:
    """Return a journal row as a dict, or None when trade_id is missing."""
    ensure_table(db_path)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM vanguard_trade_journal WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _ensure_column(con: sqlite3.Connection, name: str, ddl: str) -> None:
    cols = {
        row[1]
        for row in con.execute("PRAGMA table_info(vanguard_trade_journal)").fetchall()
    }
    if name not in cols:
        con.execute(f"ALTER TABLE vanguard_trade_journal ADD COLUMN {name} {ddl}")


def _extract_reasoning_field(policy_decision: Any, *keys: str) -> Any:
    if policy_decision is None:
        return None
    if is_dataclass(policy_decision) and not isinstance(policy_decision, type):
        payload = asdict(policy_decision)
    elif hasattr(policy_decision, "as_dict"):
        payload = policy_decision.as_dict()
    elif isinstance(policy_decision, dict):
        payload = policy_decision
    else:
        return None
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def insert_approval_row(
    db_path: str,
    profile_id: str,
    candidate: dict,
    policy_decision: Any,
    cycle_ts_utc: str,
    *,
    status: str = "PENDING_FILL",
) -> str:
    """
    Insert a new journal row on trade approval.

    Args:
        db_path:        Path to SQLite DB.
        profile_id:     Account profile ID (e.g. 'gft_10k').
        candidate:      Dict with keys: symbol, asset_class, side (or direction),
                        entry_price, stop_price (or expected_sl), tp_price (or expected_tp),
                        shares_or_lots (or approved_qty), spread_pct (optional).
        policy_decision: PolicyDecision dataclass, dict, or object with as_dict().
                        Serialized as approval_reasoning_json.
        cycle_ts_utc:   ISO-8601 UTC cycle timestamp.
        status:         Initial status (default PENDING_FILL; use FORWARD_TRACKED for
                        test/manual modes).

    Returns:
        trade_id (UUID4 string).
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}. Must be one of {VALID_STATUSES}")

    ensure_table(db_path)

    trade_id = str(uuid.uuid4())

    symbol      = str(candidate.get("symbol") or "").upper()
    asset_class = str(candidate.get("asset_class") or "unknown").lower()
    side        = str(
        candidate.get("side") or candidate.get("direction") or ""
    ).upper()
    approved_qty  = float(
        candidate.get("approved_qty") or candidate.get("shares_or_lots") or 0.0
    )
    expected_entry = float(candidate.get("entry_price") or candidate.get("entry") or 0.0)
    expected_sl    = float(
        candidate.get("expected_sl") or candidate.get("stop_price") or
        candidate.get("stop_loss") or 0.0
    )
    expected_tp    = float(
        candidate.get("expected_tp") or candidate.get("tp_price") or
        candidate.get("take_profit") or 0.0
    )
    spread_pct = candidate.get("spread_pct") or candidate.get("spread_pct_at_approval")
    spread_pct = float(spread_pct) if spread_pct is not None else None
    policy_id = candidate.get("policy_id") or _extract_reasoning_field(policy_decision, "policy_id")
    model_id = candidate.get("model_id") or _extract_reasoning_field(policy_decision, "model_id")
    model_family = candidate.get("model_family") or _extract_reasoning_field(policy_decision, "model_family")
    original_entry_ref_price = float(candidate.get("original_entry_ref_price") or expected_entry or 0.0)
    original_stop_price = float(candidate.get("original_stop_price") or expected_sl or 0.0)
    original_tp_price = float(candidate.get("original_tp_price") or expected_tp or 0.0)
    original_risk_dollars = candidate.get("original_risk_dollars")
    if original_risk_dollars is None:
        original_risk_dollars = _extract_reasoning_field(
            policy_decision, "original_risk_dollars", "risk_dollars", "approved_risk_dollars"
        )
    original_risk_dollars = float(original_risk_dollars) if original_risk_dollars not in (None, "") else None
    original_r_distance_native = candidate.get("original_r_distance_native")
    if original_r_distance_native is None and original_entry_ref_price and original_stop_price:
        original_r_distance_native = abs(original_entry_ref_price - original_stop_price)
    original_r_distance_native = (
        float(original_r_distance_native) if original_r_distance_native not in (None, "") else None
    )
    original_rr_multiple = candidate.get("original_rr_multiple")
    if original_rr_multiple is None:
        original_rr_multiple = _extract_reasoning_field(policy_decision, "original_rr_multiple", "rr_multiple")
    original_rr_multiple = float(original_rr_multiple) if original_rr_multiple not in (None, "") else None
    original_r_multiple_target = candidate.get("original_r_multiple_target")
    if original_r_multiple_target is None:
        original_r_multiple_target = _extract_reasoning_field(
            policy_decision, "original_r_multiple_target", "r_multiple_target", "target_rr_multiple"
        )
    original_r_multiple_target = (
        float(original_r_multiple_target) if original_r_multiple_target not in (None, "") else None
    )

    reasoning_json = _policy_decision_to_json(policy_decision)
    now = _now_utc()

    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            INSERT INTO vanguard_trade_journal (
                trade_id, profile_id, symbol, asset_class, side,
                approved_cycle_ts_utc, approved_qty, expected_entry,
                expected_sl, expected_tp, policy_id, model_id, model_family,
                original_entry_ref_price, original_stop_price, original_tp_price,
                original_risk_dollars, original_r_distance_native,
                original_rr_multiple, original_r_multiple_target,
                approval_reasoning_json,
                spread_pct_at_approval, status, last_synced_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id, profile_id, symbol, asset_class, side,
                cycle_ts_utc, approved_qty, expected_entry,
                expected_sl, expected_tp, policy_id, model_id, model_family,
                original_entry_ref_price, original_stop_price, original_tp_price,
                original_risk_dollars, original_r_distance_native,
                original_rr_multiple, original_r_multiple_target,
                reasoning_json,
                spread_pct, status, now,
            ),
        )
        con.commit()

    logger.info(
        "[trade_journal] inserted trade_id=%s profile=%s symbol=%s side=%s status=%s",
        trade_id, profile_id, symbol, side, status,
    )
    return trade_id


def update_submitted(
    db_path: str,
    trade_id: str,
    broker_order_id: str,
    submitted_at_utc: str,
) -> None:
    """
    Record that the order was submitted to the broker.

    Precondition: status must be PENDING_FILL.
    Does NOT change status (broker hasn't confirmed fill yet).
    """
    with sqlite3.connect(db_path) as con:
        current = _get_row_status(con, trade_id)
        if current is None:
            raise JournalStateError(f"trade_id={trade_id!r} not found in vanguard_trade_journal")
        if current != "PENDING_FILL":
            raise JournalStateError(
                f"update_submitted requires status=PENDING_FILL, got {current!r} "
                f"(trade_id={trade_id!r})"
            )
        con.execute(
            """
            UPDATE vanguard_trade_journal
               SET broker_order_id    = ?,
                   submitted_at_utc   = ?,
                   last_synced_at_utc = ?
             WHERE trade_id = ?
            """,
            (str(broker_order_id or ""), submitted_at_utc, _now_utc(), trade_id),
        )
        con.commit()

    logger.info(
        "[trade_journal] update_submitted trade_id=%s broker_order_id=%s",
        trade_id, broker_order_id,
    )


def update_filled(
    db_path: str,
    trade_id: str,
    broker_position_id: str,
    fill_price: float,
    fill_qty: float,
    filled_at_utc: str,
    expected_entry: float,
    side: str,
    commission: Optional[float] = None,
) -> None:
    """
    Record fill details and compute slippage. Sets status=OPEN.

    Slippage sign convention (positive = WORSE for us):
      LONG:  slippage_price = fill_price - expected_entry
      SHORT: slippage_price = expected_entry - fill_price
      slippage_bps = slippage_price / expected_entry * 10_000

    Precondition: status must be PENDING_FILL.
    """
    fill_price     = float(fill_price)
    fill_qty       = float(fill_qty)
    expected_entry = float(expected_entry)
    side           = str(side or "").upper()

    if expected_entry == 0.0:
        slippage_price: Optional[float] = None
        slippage_bps: Optional[float]   = None
    else:
        if side == "LONG":
            slippage_price = fill_price - expected_entry
        else:  # SHORT
            slippage_price = expected_entry - fill_price
        slippage_bps = slippage_price / expected_entry * 10_000.0

    with sqlite3.connect(db_path) as con:
        current = _get_row_status(con, trade_id)
        if current is None:
            raise JournalStateError(f"trade_id={trade_id!r} not found in vanguard_trade_journal")
        if current != "PENDING_FILL":
            raise JournalStateError(
                f"update_filled requires status=PENDING_FILL, got {current!r} "
                f"(trade_id={trade_id!r})"
            )
        con.execute(
            """
            UPDATE vanguard_trade_journal
               SET broker_position_id = ?,
                   fill_price         = ?,
                   fill_qty           = ?,
                   filled_at_utc      = ?,
                   slippage_price     = ?,
                   slippage_bps       = ?,
                   commission         = ?,
                   status             = 'OPEN',
                   last_synced_at_utc = ?
             WHERE trade_id = ?
            """,
            (
                str(broker_position_id or ""),
                fill_price, fill_qty, filled_at_utc,
                slippage_price, slippage_bps,
                float(commission) if commission is not None else None,
                _now_utc(),
                trade_id,
            ),
        )
        con.commit()

    logger.info(
        "[trade_journal] update_filled trade_id=%s fill=%.5f slip=%.5f (%.2f bps) status=OPEN",
        trade_id, fill_price,
        slippage_price if slippage_price is not None else 0.0,
        slippage_bps if slippage_bps is not None else 0.0,
    )


def mark_open_from_operator(
    db_path: str,
    trade_id: str,
    *,
    broker_order_id: str,
    broker_position_id: str,
    fill_price: float,
    fill_qty: float,
    filled_at_utc: str,
    expected_entry: float,
    side: str,
    commission: Optional[float] = None,
) -> None:
    """
    Transition a journal row into OPEN from operator-assisted execution.

    Accepted pre-statuses:
      - FORWARD_TRACKED (manual review flow)
      - PENDING_FILL    (direct routed flow)
    """
    fill_price = float(fill_price)
    fill_qty = float(fill_qty)
    expected_entry = float(expected_entry)
    side = str(side or "").upper()

    if expected_entry == 0.0:
        slippage_price: Optional[float] = None
        slippage_bps: Optional[float] = None
    else:
        if side == "LONG":
            slippage_price = fill_price - expected_entry
        else:
            slippage_price = expected_entry - fill_price
        slippage_bps = slippage_price / expected_entry * 10_000.0

    with sqlite3.connect(db_path) as con:
        current = _get_row_status(con, trade_id)
        if current is None:
            raise JournalStateError(f"trade_id={trade_id!r} not found in vanguard_trade_journal")
        if current not in {"FORWARD_TRACKED", "PENDING_FILL"}:
            raise JournalStateError(
                f"mark_open_from_operator requires status in "
                f"{{'FORWARD_TRACKED','PENDING_FILL'}}, got {current!r} "
                f"(trade_id={trade_id!r})"
            )
        con.execute(
            """
            UPDATE vanguard_trade_journal
               SET broker_order_id    = ?,
                   broker_position_id = ?,
                   submitted_at_utc   = COALESCE(submitted_at_utc, ?),
                   fill_price         = ?,
                   fill_qty           = ?,
                   filled_at_utc      = ?,
                   slippage_price     = ?,
                   slippage_bps       = ?,
                   commission         = ?,
                   status             = 'OPEN',
                   last_synced_at_utc = ?
             WHERE trade_id = ?
            """,
            (
                str(broker_order_id or broker_position_id or ""),
                str(broker_position_id or ""),
                filled_at_utc,
                fill_price,
                fill_qty,
                filled_at_utc,
                slippage_price,
                slippage_bps,
                float(commission) if commission is not None else None,
                _now_utc(),
                trade_id,
            ),
        )
        con.commit()

    logger.info(
        "[trade_journal] mark_open_from_operator trade_id=%s fill=%.5f status=OPEN",
        trade_id,
        fill_price,
    )


def update_journal_closed_from_auto(
    db_path: str,
    broker_position_id: str,
    close_price: Optional[float],
    closed_at_utc: Optional[str],
    close_reason: str,
    realized_pnl: Optional[float],
    holding_minutes: int,
    broker_close_reason: Optional[str] = None,
    close_detected_via: str = "manager",
) -> None:
    """
    Mark a journal row as CLOSED following an auto-close at the broker.

    Finds the OPEN row by broker_position_id (status='OPEN'). If no OPEN row
    exists, tries any row with that broker_position_id (e.g. recon_* rows) and
    logs a warning. If still not found, logs a warning and returns.

    Computes realized_rrr = (abs(close_price - fill_price) / abs(expected_sl - fill_price)) * sign
      where sign = +1 if profitable:
        LONG:  profitable if close_price > fill_price
        SHORT: profitable if close_price < fill_price
      If close_price is None or fill_price is 0, realized_rrr is set to None.

    Sets: status=CLOSED, close_price, closed_at_utc, close_reason, realized_pnl,
          realized_rrr, holding_minutes, last_synced_at_utc.
    """
    now = _now_utc()
    closed_at = closed_at_utc or now
    close_reason_str = str(close_reason or "AUTO_CLOSED_MAX_HOLD")

    with sqlite3.connect(db_path) as con:
        # Try OPEN row first (normal path)
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT trade_id, fill_price, expected_sl, side, status
              FROM vanguard_trade_journal
             WHERE broker_position_id = ?
               AND status = 'OPEN'
             LIMIT 1
            """,
            (str(broker_position_id),),
        ).fetchone()

        if row is None:
            # Fallback: any row with this broker_position_id (e.g. recon_* RECONCILED_CLOSED rows)
            row = con.execute(
                """
                SELECT trade_id, fill_price, expected_sl, side, status
                  FROM vanguard_trade_journal
                 WHERE broker_position_id = ?
                 LIMIT 1
                """,
                (str(broker_position_id),),
            ).fetchone()
            if row is None:
                logger.warning(
                    "[trade_journal] update_journal_closed_from_auto: no row found for "
                    "broker_position_id=%s — close recorded at broker but not in journal",
                    broker_position_id,
                )
                return
            logger.warning(
                "[trade_journal] update_journal_closed_from_auto: row found with "
                "status=%s (not OPEN) for broker_position_id=%s — updating anyway",
                row["status"], broker_position_id,
            )

        trade_id   = row["trade_id"]
        fill_price = float(row["fill_price"] or 0.0)
        expected_sl= float(row["expected_sl"] or 0.0)
        side       = str(row["side"] or "").upper()

        # Compute realized_rrr
        realized_rrr: Optional[float] = None
        if close_price is not None and fill_price != 0.0:
            sl_distance = abs(expected_sl - fill_price)
            if sl_distance > 0.0:
                trade_distance = abs(close_price - fill_price)
                ratio = trade_distance / sl_distance
                # sign: +1 profitable, -1 loss
                if side == "LONG":
                    sign = 1.0 if float(close_price) > fill_price else -1.0
                else:
                    sign = 1.0 if float(close_price) < fill_price else -1.0
                realized_rrr = ratio * sign

        con.execute(
            """
            UPDATE vanguard_trade_journal
               SET status             = 'CLOSED',
                   close_price        = ?,
                   closed_at_utc      = ?,
                   close_reason       = ?,
                   manager_close_reason = ?,
                   broker_close_reason = ?,
                   close_detected_via = ?,
                   close_order_id     = ?,
                   close_fill_price   = ?,
                   close_fill_ts_utc  = ?,
                   realized_pnl       = ?,
                   realized_rrr       = ?,
                   holding_minutes    = ?,
                   last_synced_at_utc = ?
             WHERE trade_id = ?
            """,
            (
                float(close_price) if close_price is not None else None,
                closed_at,
                close_reason_str,
                close_reason_str,
                str(broker_close_reason) if broker_close_reason else None,
                str(close_detected_via or "manager"),
                str(broker_position_id),
                float(close_price) if close_price is not None else None,
                closed_at,
                float(realized_pnl) if realized_pnl is not None else None,
                realized_rrr,
                int(holding_minutes),
                now,
                trade_id,
            ),
        )
        con.commit()

    logger.info(
        "[trade_journal] update_journal_closed_from_auto: trade_id=%s "
        "broker_position_id=%s close_price=%s rrr=%s",
        trade_id, broker_position_id, close_price, realized_rrr,
    )


def update_rejected_by_broker(
    db_path: str,
    trade_id: str,
    rejection_reason: str,
) -> None:
    """
    Mark a trade as rejected by the broker. Sets status=REJECTED_BY_BROKER.

    Precondition: status must be PENDING_FILL.
    """
    with sqlite3.connect(db_path) as con:
        current = _get_row_status(con, trade_id)
        if current is None:
            raise JournalStateError(f"trade_id={trade_id!r} not found in vanguard_trade_journal")
        if current != "PENDING_FILL":
            raise JournalStateError(
                f"update_rejected_by_broker requires status=PENDING_FILL, got {current!r} "
                f"(trade_id={trade_id!r})"
            )
        con.execute(
            """
            UPDATE vanguard_trade_journal
               SET status             = 'REJECTED_BY_BROKER',
                   notes              = ?,
                   last_synced_at_utc = ?
             WHERE trade_id = ?
            """,
            (str(rejection_reason or ""), _now_utc(), trade_id),
        )
        con.commit()

    logger.info(
        "[trade_journal] update_rejected trade_id=%s reason=%s",
        trade_id, rejection_reason,
    )
