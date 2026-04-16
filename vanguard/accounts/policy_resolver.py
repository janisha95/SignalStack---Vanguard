"""
policy_resolver.py — Policy precedence resolver for Phase 2b.

Precedence (highest → lowest, per-field):
  1. temporary_overrides[profile_id]  — time-bounded, whitelist-only
  2. policy_templates[profile.policy_id]  — assigned policy template
  3. risk_defaults                    — global fallbacks

Hard constraint: profiles cannot carry inline policy overrides.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Module-level sentinel used by _deep_get and the required-field validation loop.
# Must be a single object so `val is _MISSING` comparisons work correctly.
_MISSING = object()

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PolicyFieldMissing(Exception):
    """Raised when a required policy field is absent from all three layers."""


class ConfigValidationError(Exception):
    """Raised at config load when override whitelist or inline override check fails."""


# ---------------------------------------------------------------------------
# Override whitelist
# ---------------------------------------------------------------------------

OVERRIDE_WHITELIST = frozenset({"side", "max_positions", "is_paused", "expires_at_utc", "reason"})

# Inline profile keys that are forbidden (signal the user should use a separate template)
_INLINE_OVERRIDE_SUFFIXES = ("_override", "_overrides")


def validate_override_whitelist(overrides_block: dict) -> list[str]:
    """
    Validate temporary_overrides block at config load.
    Returns list of error strings (empty = valid).
    """
    errors: list[str] = []
    for profile_id, override in (overrides_block or {}).items():
        if not isinstance(override, dict):
            errors.append(f"temporary_overrides.{profile_id} must be a dict")
            continue
        for key in override:
            if key not in OVERRIDE_WHITELIST:
                errors.append(
                    f"temporary_overrides.{profile_id} contains non-whitelisted key {key!r}"
                )
    return errors


def validate_no_inline_profile_overrides(profiles: list[dict]) -> list[str]:
    """
    Ensure no profile carries inline _override fields.
    Returns list of error strings (empty = valid).
    """
    errors: list[str] = []
    for profile in (profiles or []):
        pid = str(profile.get("id") or "")
        for key in profile:
            if any(key.endswith(sfx) for sfx in _INLINE_OVERRIDE_SUFFIXES):
                errors.append(
                    f"profile {pid!r} contains inline field {key!r} — "
                    f"use a separate policy_template instead"
                )
    return errors


# ---------------------------------------------------------------------------
# Required policy fields (dot-notation paths)
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = [
    ("side_controls", "allow_long"),
    ("side_controls", "allow_short"),
    ("position_limits", "max_open_positions"),
    ("position_limits", "block_duplicate_symbols"),
    ("drawdown_rules", "daily_loss_pause_pct"),
    ("drawdown_rules", "trailing_drawdown_pause_pct"),
    ("reject_rules", "enforce_drawdown_pause"),
    ("reject_rules", "enforce_blackout"),
    ("reject_rules", "enforce_position_limit"),
    ("reject_rules", "enforce_duplicate_block"),
]


def _deep_get(d: dict, *keys: str) -> Any:
    """Safely fetch d[key1][key2]... returning _MISSING sentinel if absent."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]  # type: ignore[index]
    return cur


def _merge_nested(base: dict, overlay: dict) -> dict:
    """Merge overlay into base (per-key override, nested dicts merged)."""
    result = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge_nested(dict(result[k]), v)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------

def resolve_effective_policy(
    config: dict[str, Any],
    profile_id: str,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """
    Returns the effective policy dict for profile_id at now_utc.

    Steps:
      1. Load policy_templates[profile.policy_id]   (layer 2)
      2. Merge risk_defaults for any missing fields  (layer 3 fallback)
      3. Apply active temporary_overrides[profile_id] whitelisted fields  (layer 1)
      4. Validate all required fields present, else raise PolicyFieldMissing

    Pure function — no DB access, no side effects.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    # Find profile
    profiles = config.get("profiles") or []
    profile = next((p for p in profiles if str(p.get("id") or "") == profile_id), None)
    if profile is None:
        raise PolicyFieldMissing(f"Profile {profile_id!r} not found in config")

    policy_id = str(profile.get("policy_id") or "")
    if not policy_id:
        raise PolicyFieldMissing(f"Profile {profile_id!r} has no policy_id")

    # Layer 2: policy template
    templates = config.get("policy_templates") or {}
    template = templates.get(policy_id)
    if template is None:
        raise PolicyFieldMissing(
            f"policy_templates.{policy_id!r} not found (required by profile {profile_id!r})"
        )
    policy: dict[str, Any] = dict(template)

    # Layer 3: risk_defaults as fallback (merge under template, template wins)
    risk_defaults = config.get("risk_defaults") or {}
    # Only fill in top-level keys that are absent from the template
    for k, v in risk_defaults.items():
        if k not in policy:
            policy[k] = v

    # Layer 1: active temporary_overrides — whitelist-only, expiry enforced
    overrides_block = config.get("temporary_overrides") or {}
    override = overrides_block.get(profile_id)
    if override and isinstance(override, dict):
        expires_str = str(override.get("expires_at_utc") or "")
        expired = False
        if expires_str:
            try:
                exp_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                expired = now_utc > exp_dt
            except ValueError:
                logger.warning(
                    "policy_resolver: invalid expires_at_utc %r for profile %s — treating as expired",
                    expires_str, profile_id,
                )
                expired = True

        if not expired:
            # Apply whitelisted fields from override into policy
            side_override = str(override.get("side") or "").upper().strip()
            if side_override in ("LONG_ONLY", "SHORT_ONLY", "BOTH"):
                sc = dict(policy.get("side_controls") or {})
                if side_override == "LONG_ONLY":
                    sc["allow_long"] = True
                    sc["allow_short"] = False
                elif side_override == "SHORT_ONLY":
                    sc["allow_long"] = False
                    sc["allow_short"] = True
                else:
                    sc["allow_long"] = True
                    sc["allow_short"] = True
                policy = dict(policy)
                policy["side_controls"] = sc
                policy["_override_source"] = f"temporary_overrides[{profile_id}]"

            max_pos_override = override.get("max_positions")
            if max_pos_override is not None:
                pl = dict(policy.get("position_limits") or {})
                pl["max_open_positions"] = int(max_pos_override)
                policy = dict(policy)
                policy["position_limits"] = pl
                policy.setdefault("_override_source", f"temporary_overrides[{profile_id}]")

            is_paused_override = override.get("is_paused")
            if is_paused_override is not None:
                policy = dict(policy)
                policy["_is_paused_override"] = bool(is_paused_override)
                policy.setdefault("_override_source", f"temporary_overrides[{profile_id}]")

            policy["_override_reason"] = str(override.get("reason") or "")
        else:
            logger.debug(
                "policy_resolver: override for %s expired at %s — ignoring",
                profile_id, expires_str,
            )

    # Validate required fields (uses module-level _MISSING sentinel)
    for path in _REQUIRED_FIELDS:
        val = _deep_get(policy, *path)
        if val is _MISSING:
            raise PolicyFieldMissing(
                f"{'.'.join(path)} — absent from policy_templates[{policy_id!r}], "
                f"risk_defaults, and temporary_overrides"
            )

    return policy
