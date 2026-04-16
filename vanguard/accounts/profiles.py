"""
profiles.py — Profile lookup, filtering, and policy assignment (Phase 4).

Single responsibility: functions for querying and resolving profile metadata
from the runtime config. No DB access, no side effects.

The policy resolution chain (template → risk_defaults → overrides) is handled
by policy_resolver.py. This module provides helpers that sit one level above:
  - enumerate active profiles
  - look up a single profile by ID
  - get the resolved policy for a profile (delegates to policy_resolver)
  - get the active override for a profile (expiry enforced)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile enumeration
# ---------------------------------------------------------------------------

def get_active_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return all profiles from config["profiles"] where is_active is truthy.
    Returns [] if the profiles key is absent or not a list.
    """
    profiles = config.get("profiles") or []
    if not isinstance(profiles, list):
        return []
    return [dict(p) for p in profiles if p.get("is_active")]


def get_profile_by_id(
    config: dict[str, Any],
    profile_id: str,
) -> dict[str, Any] | None:
    """
    Return the first profile dict with id == profile_id, or None if not found.
    """
    profiles = config.get("profiles") or []
    pid = str(profile_id or "").strip()
    for p in profiles:
        if str(p.get("id") or "").strip() == pid:
            return dict(p)
    return None


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------

def get_policy_for_profile(
    config: dict[str, Any],
    profile: dict[str, Any],
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """
    Return the resolved effective policy for the given profile dict.

    Delegates to vanguard.accounts.policy_resolver.resolve_effective_policy().
    Raises PolicyFieldMissing if the profile or template is not found.
    """
    from vanguard.accounts.policy_resolver import resolve_effective_policy
    profile_id = str(profile.get("id") or "")
    return resolve_effective_policy(config, profile_id, now_utc=now_utc)


def get_override_for_profile(
    config: dict[str, Any],
    profile_id: str,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    """
    Return the active temporary_override dict for profile_id if it is not expired.
    Returns None if no override exists or it has expired.

    Expiry is enforced against now_utc (defaults to current UTC time).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    overrides_block = config.get("temporary_overrides") or {}
    override = overrides_block.get(str(profile_id or ""))
    if not override or not isinstance(override, dict):
        return None

    expires_str = str(override.get("expires_at_utc") or "")
    if not expires_str:
        return dict(override)  # no expiry = always active

    try:
        exp_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if now_utc > exp_dt:
            logger.debug(
                "profiles: override for %s expired at %s — returning None",
                profile_id, expires_str,
            )
            return None
    except ValueError:
        logger.warning(
            "profiles: invalid expires_at_utc %r for profile %s — treating as expired",
            expires_str, profile_id,
        )
        return None

    return dict(override)
