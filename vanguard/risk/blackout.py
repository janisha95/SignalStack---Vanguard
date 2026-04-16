"""
blackout.py — Calendar blackout window check for Phase 2b.

is_in_blackout() is pure — no DB access.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def active_blackouts(
    config: dict[str, Any],
    cycle_ts_utc: datetime | str,
) -> list[dict[str, Any]]:
    """
    Filter calendar_blackouts to those active at cycle_ts_utc.
    Returns list of active blackout dicts.
    """
    blackouts = config.get("calendar_blackouts") or []
    if not blackouts:
        return []

    if isinstance(cycle_ts_utc, str):
        try:
            cycle_dt = datetime.fromisoformat(cycle_ts_utc.replace("Z", "+00:00"))
        except ValueError:
            return []
    else:
        cycle_dt = cycle_ts_utc

    if cycle_dt.tzinfo is None:
        cycle_dt = cycle_dt.replace(tzinfo=timezone.utc)

    active: list[dict] = []
    for blk in blackouts:
        try:
            start_str = str(blk.get("start_utc") or "")
            end_str = str(blk.get("end_utc") or "")
            if not start_str or not end_str:
                continue
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if start_dt <= cycle_dt <= end_dt:
                active.append(blk)
        except (ValueError, TypeError) as exc:
            logger.debug("blackout parse error: %s — %s", blk, exc)

    return active


def is_in_blackout(
    cycle_ts_utc: datetime | str,
    asset_class: str,
    blackouts: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    """
    Returns (blocked, event_name).

    A blackout blocks asset_class if:
      - The blackout covers cycle_ts_utc (already filtered by active_blackouts)
      - asset_class is in blackout.blocks_asset_classes (or list is empty → blocks all)
      - action is "block_new_entries"
    """
    if not blackouts:
        return False, None

    ac_lower = str(asset_class or "").lower().strip()

    for blk in blackouts:
        action = str(blk.get("action") or "block_new_entries").lower()
        if "block" not in action:
            continue
        blocked_classes = [str(c).lower() for c in (blk.get("blocks_asset_classes") or [])]
        if not blocked_classes or ac_lower in blocked_classes:
            return True, str(blk.get("event") or "BLACKOUT")

    return False, None
