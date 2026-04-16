"""Execution-log lifecycle sync for max-holding auto-close in QA/manual runs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vanguard.accounts.policies import resolve_profile_policy
from vanguard.helpers.db import sqlite_conn
from vanguard.risk.policy_engine import auto_close_after_max_holding, max_holding_minutes


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_price(con, symbol: str) -> float | None:
    row = con.execute(
        """
        SELECT close
        FROM vanguard_bars_5m
        WHERE symbol = ?
        ORDER BY bar_ts_utc DESC
        LIMIT 1
        """,
        (str(symbol or "").upper(),),
    ).fetchone()
    if not row or row[0] is None:
        return None
    try:
        return float(row[0])
    except Exception:
        return None


def sync_position_lifecycle(
    *,
    accounts: list[dict[str, Any]],
    db_path: str,
    now_utc: datetime | None = None,
    execution_mode: str = "manual",
) -> list[dict[str, Any]]:
    """
    Close stale local OPEN/FORWARD_TRACKED rows after each profile's max-hold window.

    In QA manual/test this updates local DB state only; no external broker calls.
    """
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    updates: list[dict[str, Any]] = []
    if str(execution_mode or "manual").lower() == "live":
        return updates

    with sqlite_conn(db_path) as con:
        rows = con.execute(
            """
            SELECT id, symbol, direction, account, entry_price, fill_price,
                   executed_at, filled_at, created_at, outcome, status
            FROM execution_log
            WHERE outcome IS NULL OR UPPER(outcome) = 'OPEN'
            """
        ).fetchall()

        for row in rows:
            account_id = str(row["account"] or "").strip()
            profile = next((p for p in accounts if str(p.get("id")) == account_id), None)
            if not profile:
                continue

            policy = resolve_profile_policy(profile)
            if not auto_close_after_max_holding(policy):
                continue

            opened_at = (
                _parse_ts(row["filled_at"])
                or _parse_ts(row["executed_at"])
                or _parse_ts(row["created_at"])
            )
            if opened_at is None:
                continue

            age_minutes = (now_utc - opened_at).total_seconds() / 60.0
            max_minutes = max_holding_minutes(policy, fallback=240)
            if age_minutes < max_minutes:
                continue

            entry_price = float(row["fill_price"] or row["entry_price"] or 0.0)
            exit_price = _latest_price(con, row["symbol"]) or entry_price or None
            shares = float(row["id"] and 0.0)  # placeholder guard overwritten below
            qty_row = con.execute("SELECT shares FROM execution_log WHERE id = ?", (row["id"],)).fetchone()
            shares = float(qty_row[0] or 0.0) if qty_row else 0.0
            direction = str(row["direction"] or "LONG").upper()
            pnl_dollars = None
            pnl_pct = None
            if exit_price is not None and entry_price > 0 and shares > 0:
                diff = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
                pnl_dollars = round(diff * shares, 2)
                pnl_pct = round((diff / entry_price) * 100.0, 4)

            con.execute(
                """
                UPDATE execution_log
                SET status = ?,
                    outcome = ?,
                    exit_price = ?,
                    exit_date = ?,
                    pnl_dollars = COALESCE(?, pnl_dollars),
                    pnl_pct = COALESCE(?, pnl_pct),
                    days_held = ?,
                    notes = COALESCE(notes, '')
                WHERE id = ?
                """,
                (
                    "AUTO_CLOSED_MAX_HOLD",
                    "TIMEOUT",
                    exit_price,
                    now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    pnl_dollars,
                    pnl_pct,
                    max(1, int(age_minutes // (24 * 60))),
                    row["id"],
                ),
            )
            updates.append(
                {
                    "id": row["id"],
                    "account_id": account_id,
                    "symbol": row["symbol"],
                    "direction": direction,
                    "age_minutes": round(age_minutes, 1),
                    "max_holding_minutes": max_minutes,
                    "exit_price": exit_price,
                    "status": "AUTO_CLOSED_MAX_HOLD",
                }
            )
        con.commit()
    return updates
