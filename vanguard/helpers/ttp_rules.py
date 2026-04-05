"""
ttp_rules.py — TTP-specific risk rules for V6 Risk Filters.

Implements:
  - Daily Pause level (fixed at start of day)
  - Scaling reset (when profit > 3× Daily Loss, max loss resets to initial balance)
  - Consistency rule (no single trade > X% of total valid profit)
  - Scaling trigger (profit hits target → BP + Daily Pause increase)
  - Min trade range check (TTP: 10 cents minimum TP - entry distance)

Location: ~/SS/Vanguard/vanguard/helpers/ttp_rules.py
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def compute_daily_pause_level(account_size: float, daily_loss_limit: float) -> float:
    """
    TTP Daily Pause level = account balance at which trading is paused for the day.
    Calculated ONCE at start of day from current balance. Stays fixed intraday.

    daily_pause_level = account_size - daily_loss_limit
    """
    return account_size - daily_loss_limit


def compute_max_loss_level(account_size: float, max_drawdown: float) -> float:
    """
    TTP Max Loss (drawdown) level = account balance at which account is terminated.
    max_loss_level = account_size - max_drawdown
    """
    return account_size - max_drawdown


def check_scaling_reset(
    total_pnl: float,
    daily_loss_limit: float,
    account_size: float,
) -> float | None:
    """
    TTP scaling reset: when realized profit >= 3 × daily_loss_limit,
    the Max Loss resets to the initial account_size (i.e., the drawdown
    cushion is now the full account — effectively you've "earned" the floor back).

    Returns the new max_loss_level if reset triggered, else None.
    """
    if total_pnl >= 3.0 * daily_loss_limit:
        logger.info(
            f"TTP scaling reset triggered: pnl=${total_pnl:,.2f} >= "
            f"3×DLL=${3*daily_loss_limit:,.2f} → max_loss resets to ${account_size:,.0f}"
        )
        return account_size
    return None


def check_consistency_rule(
    best_trade_pnl: float,
    total_valid_pnl: float,
    consistency_rule_pct: float,
) -> bool:
    """
    TTP Consistency Rule: no single trade can represent more than
    consistency_rule_pct of total valid profit.

    Returns True if the rule is VIOLATED (best_trade_pnl > threshold).
    Returns False if compliant.
    """
    if total_valid_pnl <= 0 or consistency_rule_pct is None:
        return False
    threshold = total_valid_pnl * consistency_rule_pct
    violated = best_trade_pnl > threshold
    if violated:
        logger.warning(
            f"TTP Consistency rule violated: best_trade=${best_trade_pnl:,.2f} "
            f"> {consistency_rule_pct:.0%} of total=${total_valid_pnl:,.2f} "
            f"(threshold=${threshold:,.2f})"
        )
    return violated


def apply_scaling(
    account_size: float,
    total_pnl: float,
    scaling_profit_trigger_pct: float | None,
    scaling_bp_increase_pct: float | None,
    scaling_pause_increase_pct: float | None,
    current_daily_loss_limit: float,
) -> dict | None:
    """
    TTP Scaling: when total_pnl reaches scaling_profit_trigger_pct of account_size,
    buying power and daily pause both increase.

    Returns dict with new values if triggered, else None:
        {"new_account_size": ..., "new_daily_loss_limit": ...}
    """
    if not all([scaling_profit_trigger_pct, scaling_bp_increase_pct, scaling_pause_increase_pct]):
        return None

    trigger_amount = account_size * scaling_profit_trigger_pct
    if total_pnl < trigger_amount:
        return None

    new_account_size      = round(account_size * (1 + scaling_bp_increase_pct), 2)
    new_daily_loss_limit  = round(current_daily_loss_limit * (1 + scaling_pause_increase_pct), 2)

    logger.info(
        f"TTP Scaling triggered: pnl=${total_pnl:,.2f} >= trigger=${trigger_amount:,.2f} → "
        f"new_BP=${new_account_size:,.0f} new_DLL=${new_daily_loss_limit:,.2f}"
    )
    return {
        "new_account_size":     new_account_size,
        "new_daily_loss_limit": new_daily_loss_limit,
    }


def check_min_trade_range(
    entry_price: float,
    tp_price: float,
    min_cents: int,
) -> bool:
    """
    TTP Min Trade Range: abs(tp_price - entry_price) must be >= min_cents/100.
    Returns True if the check PASSES (range is acceptable).
    """
    if min_cents <= 0:
        return True
    range_dollars = abs(tp_price - entry_price)
    return range_dollars >= (min_cents / 100.0)
