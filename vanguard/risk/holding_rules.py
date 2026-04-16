"""
holding_rules.py — Max-holding-minutes policy reads for the Vanguard risk engine (Phase 4).

Single responsibility: should_auto_close(), auto_close_after_max_holding(), and
max_holding_minutes() are the single source of truth for holding-duration policy.

Also re-exported by policy_engine for backward-compat imports from
position_lifecycle.py:
    from vanguard.risk.policy_engine import auto_close_after_max_holding, max_holding_minutes
"""
from __future__ import annotations

from datetime import datetime, timezone


def max_holding_minutes(policy: dict, fallback: int = 240) -> int:
    """
    Return the configured max_holding_minutes from policy.position_limits.
    Falls back to `fallback` (default 240) if absent or zero.
    """
    pl = policy.get("position_limits") or {}
    val = pl.get("max_holding_minutes")
    if val is None:
        return fallback
    try:
        result = int(val)
        return result if result > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def auto_close_after_max_holding(policy: dict) -> bool:
    """
    Return True if the policy enables auto-close when max_holding_minutes is exceeded.
    Defaults to False if absent.
    """
    pl = policy.get("position_limits") or {}
    return bool(pl.get("auto_close_after_max_holding", False))


def should_auto_close(
    position: dict,
    policy: dict,
    now_utc: datetime | None = None,
) -> tuple[bool, str | None]:
    """
    Returns (should_close: bool, reason: str | None).

    Checks whether `position` has exceeded the max holding window defined in `policy`.
    `position` must have "opened_at_utc" (ISO-8601 string).

    Returns (False, None) if:
      - auto_close_after_max_holding is False
      - opened_at_utc is missing / unparseable
      - position age < max_holding_minutes
    """
    if not auto_close_after_max_holding(policy):
        return False, None

    max_min = max_holding_minutes(policy)
    opened_raw = position.get("opened_at_utc") or position.get("filled_at") or position.get("executed_at")
    if not opened_raw:
        return False, None

    try:
        opened_dt = datetime.fromisoformat(str(opened_raw).replace("Z", "+00:00"))
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False, None

    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_minutes = (now - opened_dt).total_seconds() / 60.0

    if age_minutes >= max_min:
        return True, f"held={age_minutes:.1f}m >= max={max_min}m"
    return False, None
