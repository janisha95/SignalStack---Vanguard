"""Resolve account policy templates from the QA runtime config."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from vanguard.config.runtime_config import get_policy_templates_config, get_risk_defaults


def _is_gft_profile(profile: dict[str, Any]) -> bool:
    return str(profile.get("id") or "").lower().startswith("gft_") or str(
        profile.get("instrument_scope") or ""
    ) == "gft_universe"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in dict(override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _default_policy(profile: dict[str, Any]) -> dict[str, Any]:
    gft_defaults = dict((get_risk_defaults().get("gft") or {}))
    return {
        "policy_id": profile.get("policy_id"),
        "side_controls": {
            "allow_long": True,
            "allow_short": True,
        },
        "position_limits": {
            "max_open_positions": int(
                gft_defaults.get("max_positions", 3)
                if _is_gft_profile(profile)
                else profile.get("max_positions", 5)
                or 5
            ),
            "block_duplicate_symbols": bool(int(profile.get("block_duplicate_symbols", 1) or 0)),
            "max_holding_minutes": 240,
            "auto_close_after_max_holding": True,
        },
        "sizing_by_asset_class": {
            "crypto": {
                "method": "risk_per_stop",
                "risk_per_trade_pct": float(
                    gft_defaults.get("risk_per_trade_pct", profile.get("risk_per_trade_pct", 0.005))
                ),
                "min_qty": float(gft_defaults.get("min_crypto_qty", 0.000001)),
                "min_sl_pct": float(gft_defaults.get("min_crypto_sl_pct", 0.08)),
                "min_tp_pct": float(gft_defaults.get("min_crypto_tp_pct", 0.16)),
            },
            "forex": {
                "method": "risk_per_stop",
                "risk_per_trade_pct": float(
                    gft_defaults.get("risk_per_trade_pct", profile.get("risk_per_trade_pct", 0.005))
                ),
                "min_sl_pips": float(gft_defaults.get("min_sl_pips", 20.0)),
                "min_tp_pips": float(gft_defaults.get("min_tp_pips", 40.0)),
            },
            "equity": {
                "method": "atr_position_size",
                "risk_per_trade_pct": float(profile.get("risk_per_trade_pct", 0.005) or 0.005),
                "stop_atr_multiple": float(profile.get("stop_atr_multiple", 1.5) or 1.5),
                "tp_atr_multiple": float(profile.get("tp_atr_multiple", 3.0) or 3.0),
            },
        },
        "reject_rules": {
            "enforce_universe": True,
            "enforce_session": True,
            "enforce_position_limit": True,
            "enforce_heat_limit": True,
        },
    }


def _temporary_override_active(policy: dict[str, Any], now_et: datetime | None = None) -> dict[str, Any] | None:
    override = policy.get("temporary_side_override")
    if not isinstance(override, dict):
        return None
    expires_at = override.get("expires_at")
    if not expires_at:
        return override
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except Exception:
        return None
    current = now_et or datetime.now(expiry.tzinfo)
    if current.tzinfo is None and expiry.tzinfo is not None:
        current = current.replace(tzinfo=expiry.tzinfo)
    if expiry.tzinfo is not None and current.tzinfo is not None:
        current = current.astimezone(expiry.tzinfo)
    return override if current <= expiry else None


def resolve_profile_policy(profile: dict[str, Any], now_et: datetime | None = None) -> dict[str, Any]:
    """Return the effective policy for one account profile."""
    policy_templates = get_policy_templates_config()
    policy_id = str(profile.get("policy_id") or "").strip()
    if not policy_id and _is_gft_profile(profile) and "gft_standard_v1" in policy_templates:
        policy_id = "gft_standard_v1"

    policy = _default_policy(profile)
    if policy_id and policy_id in policy_templates:
        policy = _deep_merge(policy, dict(policy_templates.get(policy_id) or {}))
        policy["policy_id"] = policy_id

    active_override = _temporary_override_active(policy, now_et=now_et)
    if active_override:
        policy["temporary_side_override"] = active_override
    else:
        policy.pop("temporary_side_override", None)

    return policy
