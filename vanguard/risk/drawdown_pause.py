"""
drawdown_pause.py — Drawdown-triggered pause logic for Phase 2b.

check_drawdown_pause() is pure — no DB access.
persist_pause() writes to vanguard_account_state.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def check_drawdown_pause(
    account_state: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[bool, str | None]:
    """
    Returns (is_paused, reason_string).

    Check order:
      1. Manual pause: account_state.paused_until_utc > now_utc
      2. _is_paused_override from temporary_overrides (applied by policy_resolver)
      3. Daily loss limit: daily_pnl_pct <= -daily_loss_pause_pct
      4. Trailing drawdown: trailing_dd_pct >= trailing_drawdown_pause_pct
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Manual pause (from prior persist_pause call)
    paused_until = account_state.get("paused_until_utc")
    if paused_until:
        try:
            pause_dt = datetime.fromisoformat(str(paused_until).replace("Z", "+00:00"))
            if pause_dt.tzinfo is None:
                pause_dt = pause_dt.replace(tzinfo=timezone.utc)
            if now_utc < pause_dt:
                reason = account_state.get("pause_reason") or "manual_pause"
                return True, reason
        except ValueError:
            pass

    # 2. Override-level pause (_is_paused_override from policy_resolver)
    if bool(policy.get("_is_paused_override", False)):
        return True, "manual_pause_override"

    dd_rules = policy.get("drawdown_rules") or {}
    daily_pnl_pct = float(account_state.get("daily_pnl_pct", 0.0))
    trailing_dd_pct = float(account_state.get("trailing_dd_pct", 0.0))

    # 3. Daily loss limit
    daily_limit = float(dd_rules.get("daily_loss_pause_pct", 0.03))
    if daily_pnl_pct <= -daily_limit:
        return True, "daily_loss_limit_hit"

    # 4. Trailing drawdown
    trailing_limit = float(dd_rules.get("trailing_drawdown_pause_pct", 0.05))
    if trailing_dd_pct >= trailing_limit:
        return True, "trailing_dd_hit"

    return False, None


def persist_pause(
    profile_id: str,
    pause_until_utc: datetime,
    reason: str,
    db_path: str,
) -> None:
    """
    Write pause state to vanguard_account_state.
    Fires Telegram notification (Phase 3 wires the actual send; logged for now).
    """
    from vanguard.helpers.db import sqlite_conn

    if pause_until_utc.tzinfo is None:
        pause_until_utc = pause_until_utc.replace(tzinfo=timezone.utc)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pause_iso = pause_until_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with sqlite_conn(db_path) as con:
            con.execute(
                "UPDATE vanguard_account_state "
                "SET paused_until_utc=?, pause_reason=?, updated_at_utc=? "
                "WHERE profile_id=?",
                (pause_iso, reason, now_iso, profile_id),
            )
            con.commit()
        logger.warning(
            "[drawdown_pause] %s: PAUSED until %s — reason=%s",
            profile_id, pause_iso, reason,
        )
    except Exception as exc:
        logger.error("persist_pause(%s): %s", profile_id, exc)
