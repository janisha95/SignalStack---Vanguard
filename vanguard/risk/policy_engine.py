"""
policy_engine.py — Orchestrator for the Vanguard risk engine (Phase 4).

Single responsibility: evaluate_candidate() is the single public entry point.
It delegates each check to the appropriate specialized module and returns a
PolicyDecision. Given identical inputs it always produces identical output.

Backward-compat re-exports (so existing importers need no changes):
    from vanguard.risk.policy_engine import evaluate_candidate, PolicyDecision
    from vanguard.risk.policy_engine import auto_close_after_max_holding, max_holding_minutes

Decision codes:
  APPROVED
  RESIZED
  BLOCKED_DRAWDOWN_PAUSE
  BLOCKED_BLACKOUT
  BLOCKED_SCOPE
  BLOCKED_SESSION
  BLOCKED_SIDE_CONTROL
  BLOCKED_ACTIVE_THESIS_REENTRY
  BLOCKED_DUPLICATE_SYMBOL
  BLOCKED_POSITION_LIMIT
  BLOCKED_HEAT
  BLOCKED_SPREAD_TOO_WIDE
  BLOCKED_TP_UNREACHABLE
  BLOCKED_INVALID_PRICE_OR_ATR
  BLOCKED_NO_SIZING_RULE
  BLOCKED_LOW_STREAK
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

# ── Re-exports for backward compat ─────────────────────────────────────────
from vanguard.risk.base import PolicyDecision, make_approved, make_blocked  # noqa: F401
from vanguard.risk.holding_rules import (  # noqa: F401
    auto_close_after_max_holding,
    max_holding_minutes,
    should_auto_close,
)

from vanguard.risk.blackout import is_in_blackout
from vanguard.risk.drawdown_pause import check_drawdown_pause
from vanguard.risk.holding_rules import auto_close_after_max_holding, max_holding_minutes
from vanguard.risk.position_limits import (
    check_active_thesis_reentry,
    check_duplicate_symbol,
    check_position_limit,
)
from vanguard.risk.side_controls import evaluate_side
from vanguard.risk.risky_sizing import evaluate_risky_sizing
from vanguard.risk.sizing import (
    size_crypto_spread_aware,
    size_equity_atr,
    size_forex_pips,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Legacy aliases kept for any callers that used internal helpers directly
# ---------------------------------------------------------------------------

def _blocked(reason: str, notes: list[str], side: str = "") -> PolicyDecision:
    return make_blocked(reason, notes, side)


def _approved(
    qty: float, sl: float, tp: float, side: str, method: str,
    notes: list[str], resized: bool = False,
) -> PolicyDecision:
    return make_approved(qty, sl, tp, side, method, notes, resized)


# ---------------------------------------------------------------------------
# Main evaluator — delegates to specialized modules
# ---------------------------------------------------------------------------

def evaluate_candidate(
    candidate: dict[str, Any],
    profile: dict[str, Any],
    policy: dict[str, Any],
    overrides: dict[str, Any] | None,
    blackouts: list[dict[str, Any]],
    account_state: dict[str, Any],
    cycle_ts_utc: datetime,
) -> PolicyDecision:
    """
    Evaluate one candidate against one profile's resolved policy.

    candidate keys (from V5 shortlist):
      symbol, direction, entry_price, atr, asset_class,
      bid (optional), ask (optional), spread_bid/spread_ask (optional)

    Hard check order (first failing check wins):
      1.  drawdown_pause      → BLOCKED_DRAWDOWN_PAUSE
      2.  blackout window     → BLOCKED_BLACKOUT
      3.  side control        → BLOCKED_SIDE_CONTROL
      4.  active thesis reentry → BLOCKED_ACTIVE_THESIS_REENTRY
      5.  duplicate symbol      → BLOCKED_DUPLICATE_SYMBOL
      6.  position limit        → BLOCKED_POSITION_LIMIT
      7.  sizing                → compute per policy.sizing_by_asset_class
      8.  spread gate (crypto)  → BLOCKED_SPREAD_TOO_WIDE / BLOCKED_TP_UNREACHABLE
    """
    if cycle_ts_utc.tzinfo is None:
        cycle_ts_utc = cycle_ts_utc.replace(tzinfo=timezone.utc)

    symbol     = str(candidate.get("symbol") or "").upper().strip()
    direction  = str(candidate.get("direction") or "").upper().strip()
    entry      = float(candidate.get("entry_price") or candidate.get("entry") or 0.0)
    atr        = float(candidate.get("intraday_atr") or candidate.get("atr") or 0.0)
    asset_class = str(candidate.get("asset_class") or "").lower().strip()

    bid = candidate.get("bid") or candidate.get("spread_bid")
    ask = candidate.get("ask") or candidate.get("spread_ask")
    bid = float(bid) if bid is not None else None
    ask = float(ask) if ask is not None else None

    reject_rules = policy.get("reject_rules") or {}
    profile_id   = str(profile.get("id") or "")
    account_equity = float(account_state.get("equity") or 0.0)

    # ── Check 1: Drawdown pause ────────────────────────────────────────────
    if bool(reject_rules.get("enforce_drawdown_pause", True)):
        is_paused, pause_reason = check_drawdown_pause(account_state, policy)
        if is_paused:
            return _blocked(
                "BLOCKED_DRAWDOWN_PAUSE",
                [f"profile={profile_id}", f"reason={pause_reason}"],
                direction,
            )

    # ── Check 2: Blackout window ──────────────────────────────────────────
    if bool(reject_rules.get("enforce_blackout", True)):
        blacked, event = is_in_blackout(cycle_ts_utc, asset_class, blackouts)
        if blacked:
            return _blocked(
                "BLOCKED_BLACKOUT",
                [f"event={event}", f"asset_class={asset_class}"],
                direction,
            )

    # ── Check 3: Side control ─────────────────────────────────────────────
    allowed, reason_key = evaluate_side(direction, policy, profile_id)
    if not allowed:
        return _blocked(
            "BLOCKED_SIDE_CONTROL",
            [reason_key or "side_control", f"profile={profile_id}"],
            direction,
        )

    # ── Check 4: Active thesis reentry ────────────────────────────────────
    blocked_reentry, reentry_note = check_active_thesis_reentry(
        symbol, direction, account_state, policy
    )
    if blocked_reentry:
        return _blocked(
            "BLOCKED_ACTIVE_THESIS_REENTRY",
            [reentry_note or f"symbol={symbol} side={direction}", f"profile={profile_id}"],
            direction,
        )

    # ── Check 5: Duplicate symbol ─────────────────────────────────────────
    if bool(reject_rules.get("enforce_duplicate_block", True)):
        blocked_dup, dup_note = check_duplicate_symbol(symbol, account_state, policy)
        if blocked_dup:
            return _blocked(
                "BLOCKED_DUPLICATE_SYMBOL",
                [dup_note or f"symbol={symbol}", f"profile={profile_id}"],
                direction,
            )

    # ── Check 6: Position limit ───────────────────────────────────────────
    if bool(reject_rules.get("enforce_position_limit", True)):
        blocked_pos, pos_note = check_position_limit(account_state, policy)
        if blocked_pos:
            return _blocked(
                "BLOCKED_POSITION_LIMIT",
                [pos_note or "position_limit", f"profile={profile_id}"],
                direction,
            )

    # ── Check 7: Streak minimum (Sniper Filter) ──────────────────────────
    if bool(reject_rules.get("enforce_streak_minimum", False)):
        min_streak = int(reject_rules.get("min_streak_required", 4))
        # Default to min_streak when streak absent — avoids blocking the entire
        # shortlist if the column is not yet populated in vanguard_shortlist.
        candidate_streak = candidate.get("streak")
        if candidate_streak is None:
            candidate_streak = min_streak
        else:
            candidate_streak = int(candidate_streak)
        if candidate_streak < min_streak:
            return _blocked(
                "BLOCKED_LOW_STREAK",
                [
                    f"streak={candidate_streak} < required={min_streak}",
                    f"symbol={symbol}",
                    f"profile={profile_id}",
                ],
                direction,
            )

    # ── Checks 8–9: Sizing ────────────────────────────────────────────────
    sizing_cfg = (policy.get("sizing_by_asset_class") or {}).get(asset_class)
    if sizing_cfg is None:
        return _blocked(
            "BLOCKED_NO_SIZING_RULE",
            [f"no sizing_by_asset_class[{asset_class!r}] in policy", f"profile={profile_id}"],
            direction,
        )

    method = str(sizing_cfg.get("method") or "atr_position_size").lower()

    if asset_class in {"crypto", "forex"}:
        return evaluate_risky_sizing(
            candidate=candidate,
            profile=profile,
            policy=policy,
            account_state=account_state,
            sizing_cfg=sizing_cfg,
        )

    else:  # atr_position_size (equity, index, default)
        if entry <= 0 or atr <= 0:
            return _blocked(
                "BLOCKED_INVALID_PRICE_OR_ATR",
                [f"entry={entry} atr={atr}"],
                direction,
            )
        result = size_equity_atr(
            entry=entry, atr=atr, side=direction,
            policy_equity=sizing_cfg, account_equity=account_equity,
        )
        if not result["approved"]:
            return _blocked(result["reject_reason"], result["notes"], direction)
        return _approved(
            result["qty"], result["sl_price"], result["tp_price"],
            direction, result["sizing_method"], result["notes"],
        )
