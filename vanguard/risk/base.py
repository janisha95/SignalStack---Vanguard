"""
base.py — Shared types for the Vanguard risk engine (Phase 4).

Single responsibility: PolicyDecision dataclass, Decision enum, and the two
factory helpers (_blocked / _approved) used by policy_engine and its delegates.

No business logic lives here. No DB access. No imports from sibling risk modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Decision codes
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    APPROVED                = "APPROVED"
    RESIZED                 = "RESIZED"
    BLOCKED_SCOPE           = "BLOCKED_SCOPE"
    BLOCKED_SESSION         = "BLOCKED_SESSION"
    BLOCKED_SIDE_CONTROL    = "BLOCKED_SIDE_CONTROL"
    BLOCKED_POSITION_LIMIT  = "BLOCKED_POSITION_LIMIT"
    BLOCKED_HEAT            = "BLOCKED_HEAT"
    BLOCKED_ACTIVE_THESIS_REENTRY = "BLOCKED_ACTIVE_THESIS_REENTRY"
    BLOCKED_DUPLICATE_SYMBOL      = "BLOCKED_DUPLICATE_SYMBOL"
    BLOCKED_DRAWDOWN_PAUSE        = "BLOCKED_DRAWDOWN_PAUSE"
    BLOCKED_BLACKOUT              = "BLOCKED_BLACKOUT"
    BLOCKED_SPREAD_TOO_WIDE       = "BLOCKED_SPREAD_TOO_WIDE"
    BLOCKED_TP_UNREACHABLE        = "BLOCKED_TP_UNREACHABLE"
    BLOCKED_INVALID_PRICE_OR_ATR  = "BLOCKED_INVALID_PRICE_OR_ATR"
    BLOCKED_NO_SIZING_RULE        = "BLOCKED_NO_SIZING_RULE"


# ---------------------------------------------------------------------------
# PolicyDecision — immutable result of evaluate_candidate()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyDecision:
    """
    Immutable result returned by evaluate_candidate().

    decision:       One of the APPROVED / RESIZED / BLOCKED_* strings.
    reject_reason:  Same as decision when blocked; None when approved/resized.
    approved_qty:   Computed lot/share quantity (0.0 when blocked).
    approved_sl:    Computed stop-loss price (0.0 when blocked).
    approved_tp:    Computed take-profit price (0.0 when blocked).
    approved_side:  LONG | SHORT (echoed from candidate, even when blocked).
    original_qty:   Same as approved_qty (reserved for resize tracking).
    sizing_method:  Name of the sizing function used; "none" when blocked.
    notes:          Ordered list of diagnostic strings for logging/audit.
    checks_json:    Structured risk/sizing check snapshot.
    sizing_inputs_json: Structured sizing inputs used for the decision.
    sizing_outputs_json: Structured sizing outputs used for the decision.
    """
    decision:      str
    reject_reason: str | None
    approved_qty:  float
    approved_sl:   float
    approved_tp:   float
    approved_side: str
    original_qty:  float
    sizing_method: str
    notes:         list[str] = field(default_factory=list)
    checks_json:   dict = field(default_factory=dict)
    sizing_inputs_json: dict = field(default_factory=dict)
    sizing_outputs_json: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "decision":      self.decision,
            "reject_reason": self.reject_reason,
            "approved_qty":  self.approved_qty,
            "approved_sl":   self.approved_sl,
            "approved_tp":   self.approved_tp,
            "approved_side": self.approved_side,
            "original_qty":  self.original_qty,
            "sizing_method": self.sizing_method,
            "notes":         list(self.notes),
            "checks_json":   dict(self.checks_json),
            "sizing_inputs_json": dict(self.sizing_inputs_json),
            "sizing_outputs_json": dict(self.sizing_outputs_json),
        }


# ---------------------------------------------------------------------------
# Factory helpers (used by policy_engine and its delegates)
# ---------------------------------------------------------------------------

def make_blocked(reason: str, notes: list[str], side: str = "") -> PolicyDecision:
    """Return a blocked PolicyDecision with the given reason code and notes."""
    return PolicyDecision(
        decision=reason,
        reject_reason=reason,
        approved_qty=0.0,
        approved_sl=0.0,
        approved_tp=0.0,
        approved_side=side,
        original_qty=0.0,
        sizing_method="none",
        notes=notes,
        checks_json={},
        sizing_inputs_json={},
        sizing_outputs_json={},
    )


def make_approved(
    qty: float,
    sl: float,
    tp: float,
    side: str,
    method: str,
    notes: list[str],
    resized: bool = False,
) -> PolicyDecision:
    """Return an approved (or resized) PolicyDecision."""
    return PolicyDecision(
        decision="RESIZED" if resized else "APPROVED",
        reject_reason=None,
        approved_qty=qty,
        approved_sl=sl,
        approved_tp=tp,
        approved_side=side,
        original_qty=qty,
        sizing_method=method,
        notes=notes,
        checks_json={},
        sizing_inputs_json={},
        sizing_outputs_json={},
    )
