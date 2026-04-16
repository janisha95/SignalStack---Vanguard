"""
position_limits.py — Position-count and duplicate-symbol guards (Phase 4).

Single responsibility: check_position_limit(), check_duplicate_symbol(), and
check_heat_limit() read the policy's position_limits block and compare against
the current account_state. All functions are pure — no DB access, no writes.
"""
from __future__ import annotations


def check_position_limit(
    account_state: dict,
    policy: dict,
) -> tuple[bool, str | None]:
    """
    Returns (blocked: bool, note: str | None).

    Blocked when open_positions count >= max_open_positions.
    """
    pl = policy.get("position_limits") or {}
    max_pos   = int(pl.get("max_open_positions", 5))
    open_count = len(account_state.get("open_positions") or [])
    if open_count >= max_pos:
        return True, f"open={open_count} >= max={max_pos}"
    return False, None


def check_duplicate_symbol(
    symbol: str,
    account_state: dict,
    policy: dict,
) -> tuple[bool, str | None]:
    """
    Returns (blocked: bool, note: str | None).

    Blocked when block_duplicate_symbols is True and symbol is already open.
    """
    pl = policy.get("position_limits") or {}
    if not bool(pl.get("block_duplicate_symbols", True)):
        return False, None
    sym_upper  = str(symbol or "").upper().strip()
    open_syms  = {str(p.get("symbol") or "").upper() for p in (account_state.get("open_positions") or [])}
    if sym_upper in open_syms:
        return True, f"symbol={sym_upper} already open"
    return False, None


def check_active_thesis_reentry(
    symbol: str,
    side: str,
    account_state: dict,
    policy: dict,
) -> tuple[bool, str | None]:
    """
    Returns (blocked: bool, note: str | None).

    Blocked when position_limits.block_same_direction_reentry is true and the
    account state already carries an active thesis for the same (symbol, side).

    The account_state["active_theses"] payload is assembled upstream from
    broker truth plus recent journal rows. This helper stays pure.
    """
    pl = policy.get("position_limits") or {}
    if not bool(pl.get("block_same_direction_reentry", False)):
        return False, None

    sym_upper = str(symbol or "").upper().strip()
    side_upper = str(side or "").upper().strip()
    active_theses = list(account_state.get("active_theses") or [])
    for thesis in active_theses:
        thesis_symbol = str(thesis.get("symbol") or "").upper().strip()
        thesis_side = str(thesis.get("side") or "").upper().strip()
        if thesis_symbol != sym_upper or thesis_side != side_upper:
            continue
        source = str(thesis.get("source") or "unknown")
        status = str(thesis.get("status") or "").upper() or "UNKNOWN"
        age_minutes = thesis.get("age_minutes")
        age_note = ""
        if age_minutes is not None:
            try:
                age_note = f" age={float(age_minutes):.1f}m"
            except Exception:
                age_note = f" age={age_minutes}"
        return True, f"active thesis exists source={source} status={status}{age_note}"
    return False, None


def check_heat_limit(
    new_risk_dollars: float,
    account_state: dict,
    policy: dict,
) -> tuple[bool, str | None]:
    """
    Returns (blocked: bool, note: str | None).

    Heat limit is optional; returns (False, None) if enforce_heat_limit is False
    or the policy has no heat_limit_pct configured.
    """
    reject_rules = policy.get("reject_rules") or {}
    if not bool(reject_rules.get("enforce_heat_limit", False)):
        return False, None

    pl        = policy.get("position_limits") or {}
    heat_cap  = float(pl.get("heat_limit_pct", 1.0))
    equity    = float(account_state.get("equity") or 0.0)
    if equity <= 0:
        return False, None

    existing_risk = sum(
        float(p.get("risk_dollars", 0) or 0)
        for p in (account_state.get("open_positions") or [])
    )
    total_heat_pct = (existing_risk + new_risk_dollars) / equity * 100.0
    if total_heat_pct > heat_cap:
        return True, f"total_heat_pct={total_heat_pct:.2f}% > cap={heat_cap}%"
    return False, None
