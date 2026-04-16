"""
risky_policy_adapter.py — Compile Risky JSON into the legacy Vanguard policy shape.

This keeps the current V6 / policy_engine surface stable while moving active
policy truth into Risky/config/risk_rules.json. Runtime remains a binding layer
only: profile -> risk_profile_id -> compiled policy.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SS_ROOT = REPO_ROOT.parent
DEFAULT_RISK_RULES_PATH = SS_ROOT / "Risky" / "config" / "risk_rules.json"


def load_risk_rules(path: str | Path = DEFAULT_RISK_RULES_PATH) -> dict[str, Any]:
    cfg_path = Path(path).expanduser()
    return json.loads(cfg_path.read_text())


def resolve_risk_profile_id(profile: dict[str, Any]) -> str:
    return str(profile.get("risk_profile_id") or profile.get("id") or "").strip()


def compile_effective_risk_policy(
    profile: dict[str, Any],
    runtime_config: dict[str, Any],
    risk_rules: dict[str, Any],
) -> dict[str, Any]:
    """
    Compile the active policy object for the current policy_engine.

    Runtime binds the profile to a risk profile. Risky JSON owns the rule body.
    """
    risk_profile_id = resolve_risk_profile_id(profile)
    per_account = risk_rules.get("per_account_profiles") or {}
    account_rules = per_account.get(risk_profile_id)
    if account_rules is None:
        raise KeyError(f"risk profile {risk_profile_id!r} not found in risk_rules.json")

    policy = {
        "side_controls": dict(account_rules.get("side_controls") or {}),
        "position_limits": dict(account_rules.get("position_limits") or {}),
        "drawdown_rules": dict(account_rules.get("drawdown_rules") or {}),
        "reject_rules": dict(account_rules.get("reject_rules") or {}),
        "leverage": dict(account_rules.get("leverage") or {}),
        "sizing_by_asset_class": dict(account_rules.get("sizing_by_asset_class") or {}),
        "crypto_spread_config": dict((risk_rules.get("universal_rules") or {}).get("crypto_spread_config") or {}),
        "_risk_profile_id": risk_profile_id,
        "_risk_config_version": str(risk_rules.get("config_version") or "unknown"),
        "_risk_mode": str(
            account_rules.get("mode")
            or (risk_rules.get("global") or {}).get("default_mode")
            or "check_only"
        ),
        "_risk_universal_rules": dict(risk_rules.get("universal_rules") or {}),
        "_risk_session_rules": dict(risk_rules.get("per_symbol_class_session_rules") or {}),
        "_risk_closing_buffer": dict(risk_rules.get("closing_buffer") or {}),
        "_risk_cost_model": dict(account_rules.get("cost_model") or {}),
    }
    return policy


def load_blackouts_or_none(
    profile: dict[str, Any],
    runtime_config: dict[str, Any],
    risk_rules: dict[str, Any],
    cycle_ts_utc: Any | None = None,
) -> list[dict[str, Any]]:
    """
    Placeholder for a future Risky-owned blackout/calendar plane.

    Current minimal-blast-radius path disables runtime blackout ownership.
    """
    return []
