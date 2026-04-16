"""
side_controls.py — Side-direction enforcement for the Vanguard risk engine (Phase 4).

Single responsibility: evaluate_side() checks allow_long / allow_short from the
resolved policy (including any active temporary_override side field).

Pure function — no DB access, no side effects.
"""
from __future__ import annotations


def evaluate_side(
    side_requested: str,
    policy: dict,
    profile_id: str = "",
) -> tuple[bool, str | None]:
    """
    Returns (allowed: bool, reason_key: str | None).

    reason_key is one of the REJECT_REASONS keys from reject_rules.py:
      - "long_not_allowed_by_policy"
      - "long_not_allowed_by_override"
      - "short_not_allowed_by_policy"
      - "short_not_allowed_by_override"
    Returns (True, None) if direction is allowed.
    """
    sc = policy.get("side_controls") or {}
    allow_long  = bool(sc.get("allow_long",  True))
    allow_short = bool(sc.get("allow_short", True))

    # _override_source is injected by policy_resolver when an active override
    # restricted the side_controls block.
    is_from_override = bool(policy.get("_override_source"))
    direction = str(side_requested or "").upper().strip()

    if direction == "LONG" and not allow_long:
        key = "long_not_allowed_by_override" if is_from_override else "long_not_allowed_by_policy"
        return False, key

    if direction == "SHORT" and not allow_short:
        key = "short_not_allowed_by_override" if is_from_override else "short_not_allowed_by_policy"
        return False, key

    return True, None
