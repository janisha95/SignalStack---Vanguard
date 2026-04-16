"""
reconciler.py — Reconciliation engine (Phase 3c detect + Phase 3d act).

Compares broker state (MetaApi) vs vanguard_trade_journal (our authoritative source)
to identify and optionally resolve divergences.

Hard rules (Phase 3d):
  - ZERO broker calls beyond the read from 3a. No close_position(), no place_order().
  - Every DB mutation writes a reconciliation_log row with the specific resolved_action.
  - Each resolution is in a single transaction. Exceptions → rollback + RESOLUTION_FAILED log.

Divergence types:
  DB_OPEN_BROKER_CLOSED  — journal shows OPEN, broker doesn't have the position
  DB_CLOSED_BROKER_OPEN  — broker has position, journal shows CLOSED for that broker_position_id
  QTY_MISMATCH           — both sides have the position but qty differs
  UNKNOWN_POSITION       — broker has position, no journal row at all (manual trade)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIVERGENCE_TYPES = {
    "DB_OPEN_BROKER_CLOSED",
    "DB_CLOSED_BROKER_OPEN",
    "QTY_MISMATCH",
    "UNKNOWN_POSITION",
}

DDL = """
CREATE TABLE IF NOT EXISTS vanguard_reconciliation_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at_utc   TEXT NOT NULL,
    profile_id        TEXT NOT NULL,
    divergence_type   TEXT NOT NULL,
    broker_position_id TEXT,
    db_trade_id       TEXT,
    symbol            TEXT,
    details_json      TEXT NOT NULL,
    resolved_action   TEXT NOT NULL DEFAULT 'DETECTED_ONLY'
);
CREATE INDEX IF NOT EXISTS idx_reconlog_profile_time
    ON vanguard_reconciliation_log(profile_id, detected_at_utc);
"""


# ---------------------------------------------------------------------------
# Divergence dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Divergence:
    divergence_type: str
    profile_id: str
    broker_position_id: Optional[str]
    db_trade_id: Optional[str]
    symbol: Optional[str]
    details: dict


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def ensure_table(db_path: str) -> None:
    """Create vanguard_reconciliation_log if it does not exist."""
    with sqlite3.connect(db_path) as con:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        con.commit()


# ---------------------------------------------------------------------------
# Detection (pure — no I/O except the inputs)
# ---------------------------------------------------------------------------

def detect_divergences(
    db_path: str,
    profile_id: str,
    broker_positions: list[dict[str, Any]],
    db_open_positions: list[dict[str, Any]],
    db_journal_open: list[dict[str, Any]],
) -> list[Divergence]:
    """
    Pure detection function. No writes.

    Compares broker_positions vs db_journal_open as the authoritative comparison.
    vanguard_open_positions (db_open_positions) is accepted but not used for
    the primary divergence logic; journal is authoritative.

    Logic:
      For each broker_position BP:
        j = find journal row where broker_position_id == BP.broker_position_id AND status==OPEN
        if j is None:
          check if any journal row with that broker_position_id exists (any status)
          → if exists (status != OPEN): DB_CLOSED_BROKER_OPEN
          → if not exists: UNKNOWN_POSITION
        elif abs(float(j.fill_qty or j.approved_qty) - BP.qty) > 1e-4:
          → QTY_MISMATCH

      For each db_journal_open J:
        bp = find in broker_positions where broker_position_id == J.broker_position_id
        if bp is None:
          → DB_OPEN_BROKER_CLOSED

    Args:
        db_path:          Path to SQLite DB (used to look up any-status journal rows).
        profile_id:       Account profile being reconciled.
        broker_positions: List of dicts from MetaApiClient.get_open_positions().
        db_open_positions: List of rows from vanguard_open_positions (informational only).
        db_journal_open:  List of journal rows where status=OPEN for profile_id.

    Returns:
        List of Divergence objects (may be empty).
    """
    divergences: list[Divergence] = []

    # Build lookup maps
    broker_by_id: dict[str, dict] = {
        str(bp.get("broker_position_id") or ""): bp
        for bp in broker_positions
        if bp.get("broker_position_id")
    }
    journal_open_by_broker_id: dict[str, dict] = {
        str(j.get("broker_position_id") or ""): j
        for j in db_journal_open
        if j.get("broker_position_id")
    }

    # ── Part 1: For each broker position → check journal ──────────────────
    for bp in broker_positions:
        bp_id  = str(bp.get("broker_position_id") or "")
        bp_qty = float(bp.get("qty") or 0.0)
        bp_sym = str(bp.get("symbol") or "")

        if not bp_id:
            continue

        j = journal_open_by_broker_id.get(bp_id)
        if j is None:
            # Not in open journal — is it in journal at all (any status)?
            any_status_row = _lookup_journal_any_status(db_path, profile_id, bp_id)
            if any_status_row is not None:
                # We have a journal row but it's not OPEN → DB_CLOSED_BROKER_OPEN
                divergences.append(Divergence(
                    divergence_type="DB_CLOSED_BROKER_OPEN",
                    profile_id=profile_id,
                    broker_position_id=bp_id,
                    db_trade_id=str(any_status_row.get("trade_id") or ""),
                    symbol=bp_sym,
                    details={
                        "broker_qty":    bp_qty,
                        "broker_symbol": bp_sym,
                        "db_status":     str(any_status_row.get("status") or ""),
                        "db_trade_id":   str(any_status_row.get("trade_id") or ""),
                        "note": "Journal row exists but is not OPEN — broker still shows it open",
                    },
                ))
            else:
                # No journal row at all → manual trade
                divergences.append(Divergence(
                    divergence_type="UNKNOWN_POSITION",
                    profile_id=profile_id,
                    broker_position_id=bp_id,
                    db_trade_id=None,
                    symbol=bp_sym,
                    details={
                        "broker_qty":    bp_qty,
                        "broker_symbol": bp_sym,
                        "note": "Broker has position with no matching journal entry — likely manually opened",
                    },
                ))
        else:
            # Found in open journal — check qty
            j_qty = float(j.get("fill_qty") or j.get("approved_qty") or 0.0)
            if abs(j_qty - bp_qty) > 1e-4:
                divergences.append(Divergence(
                    divergence_type="QTY_MISMATCH",
                    profile_id=profile_id,
                    broker_position_id=bp_id,
                    db_trade_id=str(j.get("trade_id") or ""),
                    symbol=bp_sym,
                    details={
                        "broker_qty":  bp_qty,
                        "journal_qty": j_qty,
                        "broker_symbol": bp_sym,
                        "db_trade_id": str(j.get("trade_id") or ""),
                        "note": f"Qty mismatch: journal={j_qty}, broker={bp_qty}",
                    },
                ))

    # ── Part 2: For each open journal row → check broker ──────────────────
    for j in db_journal_open:
        j_broker_id = str(j.get("broker_position_id") or "")
        j_trade_id  = str(j.get("trade_id") or "")
        j_symbol    = str(j.get("symbol") or "")

        if not j_broker_id:
            # Journal open row with no broker_position_id — can't reconcile, skip
            logger.debug(
                "[reconciler] journal trade_id=%s has no broker_position_id — skipping",
                j_trade_id,
            )
            continue

        bp = broker_by_id.get(j_broker_id)
        if bp is None:
            divergences.append(Divergence(
                divergence_type="DB_OPEN_BROKER_CLOSED",
                profile_id=profile_id,
                broker_position_id=j_broker_id,
                db_trade_id=j_trade_id,
                symbol=j_symbol,
                details={
                    "journal_qty":    float(j.get("fill_qty") or j.get("approved_qty") or 0.0),
                    "journal_symbol": j_symbol,
                    "db_trade_id":    j_trade_id,
                    "note": "Journal shows OPEN but broker has no matching position — likely closed by TP/SL/manual",
                },
            ))

    logger.info(
        "[reconciler] profile=%s broker_positions=%d journal_open=%d divergences=%d",
        profile_id, len(broker_positions), len(db_journal_open), len(divergences),
    )
    return divergences


# ---------------------------------------------------------------------------
# Log divergences (only write to reconciliation_log)
# ---------------------------------------------------------------------------

def log_divergences(
    db_path: str,
    divergences: list[Divergence],
    detected_at_utc: str,
) -> int:
    """
    Insert one row per divergence into vanguard_reconciliation_log.

    Only touches vanguard_reconciliation_log. Zero mutations elsewhere.

    Returns number of rows inserted.
    """
    if not divergences:
        return 0

    ensure_table(db_path)

    rows_inserted = 0
    with sqlite3.connect(db_path) as con:
        con.execute("BEGIN")
        try:
            for d in divergences:
                con.execute(
                    """
                    INSERT INTO vanguard_reconciliation_log (
                        detected_at_utc, profile_id, divergence_type,
                        broker_position_id, db_trade_id, symbol,
                        details_json, resolved_action
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'DETECTED_ONLY')
                    """,
                    (
                        detected_at_utc,
                        d.profile_id,
                        d.divergence_type,
                        d.broker_position_id,
                        d.db_trade_id,
                        d.symbol,
                        json.dumps(d.details, default=str),
                    ),
                )
                rows_inserted += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    logger.info(
        "[reconciler] logged %d divergences at %s",
        rows_inserted, detected_at_utc,
    )
    return rows_inserted


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_open_positions(db_path: str, profile_id: str) -> list[dict[str, Any]]:
    """Load rows from vanguard_open_positions for profile_id."""
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM vanguard_open_positions WHERE profile_id = ?",
                (profile_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []  # Table doesn't exist yet


def load_journal_open(db_path: str, profile_id: str) -> list[dict[str, Any]]:
    """Load journal rows where status=OPEN for profile_id."""
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT * FROM vanguard_trade_journal
                WHERE profile_id = ? AND status = 'OPEN'
                """,
                (profile_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []  # Table doesn't exist yet


def _lookup_journal_any_status(
    db_path: str,
    profile_id: str,
    broker_position_id: str,
) -> Optional[dict[str, Any]]:
    """Return the journal row for this broker_position_id (any status), or None."""
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT * FROM vanguard_trade_journal
                WHERE broker_position_id = ?
                  AND profile_id = ?
                LIMIT 1
                """,
                (broker_position_id, profile_id),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


# ---------------------------------------------------------------------------
# Resolution helpers (Phase 3d)
# ---------------------------------------------------------------------------

def _write_recon_log(
    con: sqlite3.Connection,
    divergence: Divergence,
    detected_at_utc: str,
    resolved_action: str,
    extra_details: dict | None = None,
) -> None:
    """Insert one row into reconciliation_log on the given open connection."""
    details = dict(divergence.details)
    if extra_details:
        details.update(extra_details)
    con.execute(
        """
        INSERT INTO vanguard_reconciliation_log (
            detected_at_utc, profile_id, divergence_type,
            broker_position_id, db_trade_id, symbol,
            details_json, resolved_action
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            detected_at_utc,
            divergence.profile_id,
            divergence.divergence_type,
            divergence.broker_position_id,
            divergence.db_trade_id,
            divergence.symbol,
            json.dumps(details, default=str),
            resolved_action,
        ),
    )


def _write_recon_log_failure(
    db_path: str,
    divergence: Divergence,
    detected_at_utc: str,
    error_msg: str,
) -> None:
    """Write a RESOLUTION_FAILED row in a separate transaction (called after rollback)."""
    try:
        with sqlite3.connect(db_path) as con:
            details = dict(divergence.details)
            details["resolution_error"] = error_msg
            con.execute(
                """
                INSERT INTO vanguard_reconciliation_log (
                    detected_at_utc, profile_id, divergence_type,
                    broker_position_id, db_trade_id, symbol,
                    details_json, resolved_action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RESOLUTION_FAILED')
                """,
                (
                    detected_at_utc,
                    divergence.profile_id,
                    divergence.divergence_type,
                    divergence.broker_position_id,
                    divergence.db_trade_id,
                    divergence.symbol,
                    json.dumps(details, default=str),
                ),
            )
            con.commit()
    except Exception as exc:
        logger.error("[reconciler] Failed to write RESOLUTION_FAILED log: %s", exc)


def _compute_holding_minutes(opened_at_utc: Optional[str], now_utc: str) -> Optional[int]:
    """Compute holding time in minutes between opened_at_utc and now_utc."""
    if not opened_at_utc:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        opened = datetime.strptime(str(opened_at_utc), fmt).replace(tzinfo=timezone.utc)
        closed = datetime.strptime(now_utc, fmt).replace(tzinfo=timezone.utc)
        return max(0, int((closed - opened).total_seconds() / 60))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Individual resolvers (Phase 3d)
# ---------------------------------------------------------------------------

def resolve_db_open_broker_closed(
    db_path: str,
    divergence: Divergence,
    broker_last_known_deal: Optional[dict] = None,
) -> None:
    """
    Broker says position is gone; mark our journal row as RECONCILED_CLOSED.

    DB mutations (single transaction):
      - UPDATE vanguard_trade_journal: status=RECONCILED_CLOSED, close_reason='RECONCILED',
          close_price (from deal if available), closed_at_utc, holding_minutes
      - DELETE FROM vanguard_open_positions WHERE broker_position_id=?
      - INSERT into vanguard_reconciliation_log resolved_action='JOURNAL_MARKED_CLOSED'

    On failure: ROLLBACK, then INSERT resolved_action='RESOLUTION_FAILED'.
    """
    ensure_table(db_path)
    now_utc = _now_utc()

    close_price   = float(broker_last_known_deal["close_price"]) if broker_last_known_deal and broker_last_known_deal.get("close_price") else None
    closed_at_utc = str(broker_last_known_deal["closed_at_utc"]) if broker_last_known_deal and broker_last_known_deal.get("closed_at_utc") else now_utc

    trade_id       = divergence.db_trade_id
    broker_pos_id  = divergence.broker_position_id

    try:
        with sqlite3.connect(db_path) as con:
            con.execute("BEGIN")

            # Look up journal row for holding_minutes
            row = con.execute(
                """
                SELECT approved_cycle_ts_utc, fill_price, notes
                FROM vanguard_trade_journal
                WHERE trade_id = ?
                """,
                (trade_id,),
            ).fetchone()
            open_pos_row = con.execute(
                """
                SELECT unrealized_pnl
                FROM vanguard_open_positions
                WHERE broker_position_id = ?
                """,
                (broker_pos_id,),
            ).fetchone()
            holding_mins = None
            realized_pnl = None
            realized_pnl_source = None
            if row:
                holding_mins = _compute_holding_minutes(row[0], closed_at_utc)
            if open_pos_row and open_pos_row[0] not in (None, ""):
                realized_pnl = float(open_pos_row[0])
                realized_pnl_source = "open_positions_unrealized_snapshot"

            existing_notes = str(row[2] or "") if row and len(row) >= 3 else ""
            note_parts = [part.strip() for part in existing_notes.split(";") if part.strip()]
            recon_note = "reconciled_external_close"
            if realized_pnl_source:
                recon_note = f"{recon_note}; realized_pnl_source={realized_pnl_source}"
            if recon_note not in note_parts:
                note_parts.append(recon_note)
            new_notes = "; ".join(note_parts) if note_parts else None

            # Update journal
            con.execute(
                """
                UPDATE vanguard_trade_journal
                   SET status           = 'RECONCILED_CLOSED',
                       close_price      = ?,
                       closed_at_utc    = ?,
                       close_reason     = 'RECONCILED',
                       broker_close_reason = 'UNKNOWN_EXTERNAL_CLOSE',
                       close_detected_via = 'reconciler',
                       realized_pnl     = ?,
                       holding_minutes  = ?,
                       last_synced_at_utc = ?,
                       notes            = ?
                 WHERE trade_id = ?
                """,
                (close_price, closed_at_utc, realized_pnl, holding_mins, now_utc, new_notes, trade_id),
            )

            # Delete from open_positions mirror
            con.execute(
                "DELETE FROM vanguard_open_positions WHERE broker_position_id = ?",
                (broker_pos_id,),
            )

            # Log the resolution
            _write_recon_log(con, divergence, now_utc, "JOURNAL_MARKED_CLOSED",
                             {"close_price": close_price, "closed_at_utc": closed_at_utc,
                              "holding_minutes": holding_mins})
            con.execute("COMMIT")

        logger.info(
            "[reconciler] DB_OPEN_BROKER_CLOSED resolved: trade_id=%s → RECONCILED_CLOSED",
            trade_id,
        )
    except Exception as exc:
        logger.error("[reconciler] resolve_db_open_broker_closed failed: %s", exc)
        _write_recon_log_failure(db_path, divergence, now_utc, str(exc))
        raise


def resolve_unknown_position(
    db_path: str,
    divergence: Divergence,
    broker_position: dict,
) -> None:
    """
    Broker has a position with no journal record. Insert a reconciled journal row.

    DB mutations (single transaction):
      - INSERT into vanguard_trade_journal with trade_id='recon_'+broker_position_id, status=OPEN
      - UPSERT into vanguard_open_positions
      - INSERT into vanguard_reconciliation_log resolved_action='JOURNAL_ROW_INSERTED'
    """
    from vanguard.execution.open_positions_writer import ensure_table as op_ensure
    ensure_table(db_path)
    op_ensure(db_path)

    now_utc = _now_utc()
    bp_id     = str(broker_position.get("broker_position_id") or divergence.broker_position_id or "")
    trade_id  = f"recon_{bp_id}"
    symbol    = str(broker_position.get("symbol") or divergence.symbol or "")
    side      = str(broker_position.get("side") or "LONG").upper()
    qty       = float(broker_position.get("qty") or 0.0)
    entry_px  = float(broker_position.get("entry_price") or 0.0)
    opened_at = str(broker_position.get("opened_at_utc") or now_utc)

    try:
        with sqlite3.connect(db_path) as con:
            con.execute("BEGIN")

            # Insert journal row
            con.execute(
                """
                INSERT OR IGNORE INTO vanguard_trade_journal (
                    trade_id, profile_id, symbol, asset_class, side,
                    approved_cycle_ts_utc, approved_qty, expected_entry,
                    expected_sl, expected_tp, approval_reasoning_json,
                    broker_position_id, fill_price, fill_qty, filled_at_utc,
                    status, last_synced_at_utc, notes
                ) VALUES (?, ?, ?, 'unknown', ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    trade_id, divergence.profile_id, symbol, side,
                    now_utc, qty, entry_px,
                    json.dumps({"origin": "reconciled_unknown"}),
                    bp_id, entry_px, qty, opened_at,
                    now_utc,
                    "Position discovered via reconciliation, no system approval record.",
                ),
            )

            # Upsert into open_positions mirror
            con.execute(
                """
                INSERT OR REPLACE INTO vanguard_open_positions (
                    profile_id, broker_position_id, symbol, side, qty,
                    entry_price, opened_at_utc, last_synced_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    divergence.profile_id, bp_id, symbol, side, qty,
                    entry_px, opened_at, now_utc,
                ),
            )

            _write_recon_log(con, divergence, now_utc, "JOURNAL_ROW_INSERTED",
                             {"new_trade_id": trade_id})
            con.execute("COMMIT")

        logger.info(
            "[reconciler] UNKNOWN_POSITION resolved: inserted trade_id=%s for broker_pos=%s",
            trade_id, bp_id,
        )
    except Exception as exc:
        logger.error("[reconciler] resolve_unknown_position failed: %s", exc)
        _write_recon_log_failure(db_path, divergence, now_utc, str(exc))
        raise


def resolve_qty_mismatch(
    db_path: str,
    divergence: Divergence,
    broker_position: dict,
) -> None:
    """
    Broker qty differs from DB. Update DB open_positions to broker truth.

    DB mutations (single transaction):
      - UPDATE vanguard_open_positions SET qty=broker_qty WHERE broker_position_id=?
      - UPDATE vanguard_trade_journal SET notes=... (append, not overwrite) for OPEN row
      - INSERT into vanguard_reconciliation_log resolved_action='DB_QTY_SYNCED'
    """
    ensure_table(db_path)
    now_utc = _now_utc()

    broker_qty  = float(broker_position.get("qty") or 0.0)
    broker_pos_id = str(broker_position.get("broker_position_id") or divergence.broker_position_id or "")
    journal_qty = float(divergence.details.get("journal_qty") or 0.0)

    try:
        with sqlite3.connect(db_path) as con:
            con.execute("BEGIN")

            # Update open_positions mirror
            con.execute(
                "UPDATE vanguard_open_positions SET qty = ?, last_synced_at_utc = ? WHERE broker_position_id = ?",
                (broker_qty, now_utc, broker_pos_id),
            )

            # Append note to journal row (do NOT change approved_qty — that's historical truth)
            note_text = f"qty updated via reconciliation from {journal_qty} to {broker_qty} at {now_utc}"
            existing = con.execute(
                "SELECT notes FROM vanguard_trade_journal WHERE broker_position_id = ? AND status = 'OPEN'",
                (broker_pos_id,),
            ).fetchone()
            if existing:
                old_notes = existing[0] or ""
                new_notes = f"{old_notes}; {note_text}".strip("; ")
                con.execute(
                    "UPDATE vanguard_trade_journal SET notes = ?, last_synced_at_utc = ? WHERE broker_position_id = ? AND status = 'OPEN'",
                    (new_notes, now_utc, broker_pos_id),
                )

            _write_recon_log(con, divergence, now_utc, "DB_QTY_SYNCED",
                             {"broker_qty": broker_qty, "journal_qty": journal_qty})
            con.execute("COMMIT")

        logger.info(
            "[reconciler] QTY_MISMATCH resolved: broker_pos=%s qty %s → %s",
            broker_pos_id, journal_qty, broker_qty,
        )
    except Exception as exc:
        logger.error("[reconciler] resolve_qty_mismatch failed: %s", exc)
        _write_recon_log_failure(db_path, divergence, now_utc, str(exc))
        raise


def resolve_db_closed_broker_open(
    db_path: str,
    divergence: Divergence,
    broker_position: dict,
    telegram_fn: Optional[Any] = None,
) -> None:
    """
    Rare: journal says CLOSED, broker says still open. Flag for manual review.
    Does NOT reopen the original journal row (preserves audit trail).

    DB mutations (single transaction):
      - INSERT a new journal row with trade_id='recon_reopen_' + broker_position_id
      - INSERT into vanguard_reconciliation_log resolved_action='FLAGGED_FOR_MANUAL_REVIEW'

    Also fires telegram_fn alert if provided.
    """
    ensure_table(db_path)
    now_utc = _now_utc()

    bp_id    = str(broker_position.get("broker_position_id") or divergence.broker_position_id or "")
    trade_id = f"recon_reopen_{bp_id}"
    symbol   = str(broker_position.get("symbol") or divergence.symbol or "")
    side     = str(broker_position.get("side") or "LONG").upper()
    qty      = float(broker_position.get("qty") or 0.0)
    entry_px = float(broker_position.get("entry_price") or 0.0)

    try:
        with sqlite3.connect(db_path) as con:
            con.execute("BEGIN")

            con.execute(
                """
                INSERT OR IGNORE INTO vanguard_trade_journal (
                    trade_id, profile_id, symbol, asset_class, side,
                    approved_cycle_ts_utc, approved_qty, expected_entry,
                    expected_sl, expected_tp, approval_reasoning_json,
                    broker_position_id, fill_price, fill_qty, filled_at_utc,
                    status, last_synced_at_utc, notes
                ) VALUES (?, ?, ?, 'unknown', ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    trade_id, divergence.profile_id, symbol, side,
                    now_utc, qty, entry_px,
                    json.dumps({"origin": "reconciled_reopen", "db_trade_id": divergence.db_trade_id}),
                    bp_id, entry_px, qty, now_utc,
                    now_utc,
                    f"DB_CLOSED_BROKER_OPEN: original trade_id={divergence.db_trade_id} — flagged for manual review",
                ),
            )

            _write_recon_log(con, divergence, now_utc, "FLAGGED_FOR_MANUAL_REVIEW",
                             {"new_trade_id": trade_id, "original_trade_id": divergence.db_trade_id})
            con.execute("COMMIT")

        logger.warning(
            "[reconciler] DB_CLOSED_BROKER_OPEN flagged: broker_pos=%s symbol=%s — MANUAL REVIEW REQUIRED",
            bp_id, symbol,
        )

        if telegram_fn:
            try:
                telegram_fn(
                    f"⚠️ DB_CLOSED_BROKER_OPEN detected for {symbol} (broker_pos={bp_id}). "
                    f"Journal shows CLOSED but broker still has it open. Manual review required."
                )
            except Exception as tex:
                logger.warning("[reconciler] Telegram alert failed: %s", tex)

    except Exception as exc:
        logger.error("[reconciler] resolve_db_closed_broker_open failed: %s", exc)
        _write_recon_log_failure(db_path, divergence, now_utc, str(exc))
        raise


# ---------------------------------------------------------------------------
# Main resolver dispatcher (Phase 3d)
# ---------------------------------------------------------------------------

def resolve_divergences(
    db_path: str,
    divergences: list[Divergence],
    broker_positions: list[dict[str, Any]],
    telegram_fn: Optional[Any] = None,
) -> dict[str, int]:
    """
    For each divergence, dispatch to the correct resolver.

    Hard rules:
      - ZERO broker calls (no close_position, no place_order, no modify_order).
      - Each resolution in its own transaction.
      - On exception: rollback + RESOLUTION_FAILED log row.

    Returns dict with counts: {resolved: N, failed: N}.
    """
    broker_by_id: dict[str, dict] = {
        str(bp.get("broker_position_id") or ""): bp
        for bp in broker_positions
        if bp.get("broker_position_id")
    }

    resolved = 0
    failed   = 0

    for divergence in divergences:
        div_type  = divergence.divergence_type
        bp_id     = str(divergence.broker_position_id or "")
        broker_pos = broker_by_id.get(bp_id, {})

        try:
            if div_type == "DB_OPEN_BROKER_CLOSED":
                resolve_db_open_broker_closed(db_path, divergence, broker_last_known_deal=None)
            elif div_type == "UNKNOWN_POSITION":
                resolve_unknown_position(db_path, divergence, broker_pos)
            elif div_type == "QTY_MISMATCH":
                resolve_qty_mismatch(db_path, divergence, broker_pos)
            elif div_type == "DB_CLOSED_BROKER_OPEN":
                resolve_db_closed_broker_open(db_path, divergence, broker_pos, telegram_fn)
            else:
                logger.warning("[reconciler] Unknown divergence_type: %s — skipping", div_type)
                continue
            resolved += 1
        except Exception as exc:
            logger.error("[reconciler] Resolution failed for %s %s: %s", div_type, bp_id, exc)
            failed += 1

    logger.info("[reconciler] resolve_divergences: resolved=%d failed=%d", resolved, failed)
    return {"resolved": resolved, "failed": failed}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
