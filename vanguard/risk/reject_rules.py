"""
reject_rules.py — Centralized reject-reason constants for the Vanguard risk engine (Phase 4).

Single responsibility: REJECT_REASONS is the single source of truth for all
human-readable reject reason templates. format_reason() renders a template with
kwargs. No business logic lives here.

Usage:
    from vanguard.risk.reject_rules import format_reason
    msg = format_reason("max_positions_reached", current=3, max=3)
"""
from __future__ import annotations


REJECT_REASONS: dict[str, str] = {
    # Side controls
    "long_not_allowed_by_policy":
        "Policy side_controls.allow_long is false",
    "long_not_allowed_by_override":
        "Active temporary override restricts to SHORT_ONLY",
    "short_not_allowed_by_policy":
        "Policy side_controls.allow_short is false",
    "short_not_allowed_by_override":
        "Active temporary override restricts to LONG_ONLY",

    # Position limits
    "max_positions_reached":
        "Profile has {current} open positions, max is {max}",
    "duplicate_symbol":
        "Profile already has open position on {symbol}",
    "heat_limit_exceeded":
        "Adding this trade would exceed heat cap of {cap}%",

    # Drawdown / pause
    "daily_loss_limit_hit":
        "Daily P&L {pnl_pct:.2f}% <= pause threshold -{threshold:.2f}%",
    "trailing_dd_hit":
        "Trailing drawdown {dd_pct:.2f}% >= pause threshold {threshold:.2f}%",
    "manual_pause":
        "Profile manually paused until {until}",
    "manual_pause_override":
        "Profile paused by active temporary_override (is_paused=True)",

    # Blackout / calendar
    "blackout_active":
        "Blackout window active: {event} ({start} to {end})",

    # Spread / sizing
    "spread_too_wide":
        "Current spread {spread_pct:.3f}% > max allowed {max_pct:.3f}%",
    "tp_unreachable":
        "Spread-adjusted TP cannot clear broker minimum (tp_distance < {min_multiple}× spread)",

    # Scope / session
    "not_in_scope":
        "Symbol not in profile's instrument_scope",
    "session_closed":
        "Asset class {asset_class} session is closed at this time",

    # Invalid data
    "invalid_price_or_atr":
        "entry={entry} or atr={atr} is zero/negative — cannot size",
    "no_sizing_rule":
        "No sizing_by_asset_class[{asset_class!r}] found in policy",
}


def format_reason(key: str, **kwargs: object) -> str:
    """
    Render a reject reason template.

    Unknown keys return the key itself (safe fallback — never raises).
    """
    template = REJECT_REASONS.get(key, key)
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError):
        return template
