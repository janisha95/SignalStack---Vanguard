"""
sizing.py — Pure position-sizing math for Phase 2b policy engine.

All functions are pure (no DB reads, no side effects). Unit-testable in isolation.

Spread resolution priority (crypto):
  1. Live bid/ask from DB/context truth passed in by the caller

Missing live bid/ask is now a hard reject. We do not synthesize a live spread
from fallbacks or defaults in the active risk path.
"""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel for rejected sizing
_REJECT = object()


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _approve(qty: float, sl: float, tp: float, method: str, notes: list[str]) -> dict:
    return {"approved": True, "qty": qty, "sl_price": sl, "tp_price": tp,
            "sizing_method": method, "notes": notes, "reject_reason": None}


def _reject(reason: str, notes: list[str]) -> dict:
    return {"approved": False, "qty": 0.0, "sl_price": 0.0, "tp_price": 0.0,
            "sizing_method": "none", "notes": notes, "reject_reason": reason}


# ---------------------------------------------------------------------------
# Crypto spread-aware sizing (addendum spec)
# ---------------------------------------------------------------------------

def resolve_spread(
    symbol: str,
    bid: float | None,
    ask: float | None,
    mid_price: float,
    crypto_spread_config: dict,
) -> tuple[float, float, float, list[str]] | None:
    """
    Resolve bid, ask, spread for crypto. Returns (bid, ask, spread_abs, notes).

    Active path requires live bid/ask from DB/context truth.
    """
    notes: list[str] = []

    if bid is not None and ask is not None and bid > 0 and ask > 0:
        spread = ask - bid
        notes.append("spread_source=live")
        return float(bid), float(ask), spread, notes

    notes.append("spread_source=missing_live")
    logger.warning("sizing_crypto: %s missing live bid/ask; rejecting sizing", symbol)
    return None


def size_crypto_spread_aware(
    mid_price: float,
    bid: float | None,
    ask: float | None,
    atr: float,
    side: str,
    account_equity: float,
    policy_crypto: dict,
    crypto_spread_config: dict,
    symbol: str = "",
) -> dict:
    """
    Spread-aware crypto sizing per Phase 2B addendum.

    Sizing formula (both sourced from policy_crypto JSON — no magic numbers):
        risk_qty     = risk_dollars / (sl_distance * contract_size)
        notional_qty = max_notional_pct * equity / (mid * contract_size)
        qty          = min(risk_qty, notional_qty)

    contract_size  — from policy_crypto["contract_sizes"][symbol], default 1.
    max_notional_pct — from policy_crypto["max_notional_pct"], default 0.05.

    Returns dict with keys: approved, qty, sl_price, tp_price, sizing_method, notes, reject_reason.
    """
    notes: list[str] = []
    side_upper = str(side or "").upper()

    spread_result = resolve_spread(
        symbol, bid, ask, mid_price, crypto_spread_config
    )
    if spread_result is None:
        return _reject("BLOCKED_SPREAD_MISSING", notes + ["live_spread_missing"])
    resolved_bid, resolved_ask, spread, spread_notes = spread_result
    notes.extend(spread_notes)

    mid = (resolved_bid + resolved_ask) / 2.0
    spread_pct = (spread / mid) * 100.0 if mid > 0 else 0.0

    max_spread_pct = float(crypto_spread_config.get("max_allowed_spread_pct", 0.5))
    notes.append(f"spread={spread:.4f} spread_pct={spread_pct:.3f}%")

    # Gate 1: spread too wide
    if spread_pct > max_spread_pct:
        notes.append(f"max_spread_pct={max_spread_pct}%")
        return _reject(
            "BLOCKED_SPREAD_TOO_WIDE",
            notes + [f"spread_pct={spread_pct:.3f}% > max={max_spread_pct}%"],
        )

    min_sl_pct = float(policy_crypto.get("min_sl_pct", 0.008))
    min_tp_pct = float(policy_crypto.get("min_tp_pct", 0.024))
    sl_atr_mult = float(policy_crypto.get("sl_atr_multiple", 1.5))
    tp_atr_mult = float(policy_crypto.get("tp_atr_multiple", 3.0))
    min_qty = float(policy_crypto.get("min_qty", 0.000001))
    risk_pct = float(policy_crypto.get("risk_per_trade_pct", 0.005))

    sl_distance = max(min_sl_pct * mid, float(atr) * sl_atr_mult) if atr > 0 else min_sl_pct * mid
    tp_distance = max(min_tp_pct * mid, float(atr) * tp_atr_mult) if atr > 0 else min_tp_pct * mid

    # Gate 2: TP distance must clear at least N× spread
    min_tp_multiple = float(crypto_spread_config.get("min_tp_spread_multiple", 3.0))
    tp_spread_ratio = tp_distance / spread if spread > 0 else float("inf")
    notes.append(f"tp_distance={tp_distance:.4f} tp_spread_ratio={tp_spread_ratio:.2f}x")

    if tp_distance < min_tp_multiple * spread:
        return _reject(
            "BLOCKED_TP_UNREACHABLE",
            notes + [
                f"tp_distance={tp_distance:.4f} < {min_tp_multiple}× spread={spread:.4f}",
                f"min_multiple={min_tp_multiple}",
            ],
        )

    # SL/TP placement: MetaApi bid-trigger for LONG, ask-trigger for SHORT
    if side_upper == "LONG":
        effective_entry = resolved_ask
        sl_price = effective_entry - sl_distance
        tp_price = effective_entry + tp_distance
    else:  # SHORT
        effective_entry = resolved_bid
        sl_price = effective_entry + sl_distance
        tp_price = effective_entry - tp_distance

    # All sizing parameters come from policy_crypto JSON — no magic numbers in code.
    sym_upper = str(symbol or "").upper().replace(".X", "").replace("/", "")
    contract_size = float(
        (policy_crypto.get("contract_sizes") or {}).get(sym_upper)
        or (policy_crypto.get("contract_sizes") or {}).get("default")
        or 1.0
    )
    max_notional_pct = float(policy_crypto.get("max_notional_pct", 0.05))

    risk_dollars = account_equity * risk_pct

    denom = sl_distance * contract_size
    risk_qty = risk_dollars / denom if denom > 0 else 0.0

    notional_denom = mid * contract_size
    notional_qty = (max_notional_pct * account_equity) / notional_denom if notional_denom > 0 else risk_qty

    qty = min(risk_qty, notional_qty)
    qty = max(qty, min_qty)
    qty = math.floor(qty * 1_000_000) / 1_000_000

    notes.append(f"effective_entry={effective_entry:.4f}")
    notes.append(f"sl_price={sl_price:.4f} tp_price={tp_price:.4f}")
    notes.append(
        f"risk_dollars={risk_dollars:.2f} sl_distance={sl_distance:.4f} "
        f"contract_size={contract_size} risk_qty={risk_qty:.6f} "
        f"notional_qty={notional_qty:.6f} final_qty={qty:.6f}"
    )

    return _approve(qty, sl_price, tp_price, "risk_per_stop_spread_aware", notes)


# ---------------------------------------------------------------------------
# Forex pip-based sizing
# ---------------------------------------------------------------------------

def size_forex_pips(
    entry: float,
    atr: float,
    side: str,
    policy_forex: dict,
    symbol: str,
    account_equity: float,
) -> dict:
    """
    Forex position sizing using pip-based stop/tp.
    Returns dict with keys: approved, qty, sl_price, tp_price, sizing_method, notes, reject_reason.
    """
    notes: list[str] = []
    side_upper = str(side or "").upper()

    is_jpy = str(symbol or "").upper().replace("/", "").endswith("JPY")
    pip_size = 0.01 if is_jpy else 0.0001

    min_sl_pips = float(policy_forex.get("min_sl_pips", 20))
    min_tp_pips = float(policy_forex.get("min_tp_pips", 40))
    risk_pct = float(policy_forex.get("risk_per_trade_pct", 0.005))

    # Compute stop distance in price units
    atr_pips = float(atr) / pip_size if atr > 0 and pip_size > 0 else 0.0
    sl_pips = max(min_sl_pips, atr_pips * 1.5) if atr > 0 else min_sl_pips
    tp_pips = max(min_tp_pips, atr_pips * 3.0) if atr > 0 else min_tp_pips

    sl_distance = sl_pips * pip_size
    tp_distance = tp_pips * pip_size

    if side_upper == "LONG":
        sl_price = entry - sl_distance
        tp_price = entry + tp_distance
    else:
        sl_price = entry + sl_distance
        tp_price = entry - tp_distance

    # Pip value lookup
    pip_values = policy_forex.get("pip_value_usd_per_standard_lot") or {}
    sym_upper = str(symbol or "").upper().replace("/", "").strip()
    pip_value = float(pip_values.get(sym_upper) or pip_values.get("default") or 10.0)

    risk_dollars = account_equity * risk_pct
    lots = risk_dollars / (sl_pips * pip_value) if sl_pips > 0 and pip_value > 0 else 0.01
    lots = max(0.01, round(lots, 2))
    lots = min(lots, 50.0)

    notes.append(f"sl_pips={sl_pips:.1f} tp_pips={tp_pips:.1f}")
    notes.append(f"pip_value={pip_value} lots={lots:.2f}")

    return _approve(lots, sl_price, tp_price, "risk_per_stop_pips", notes)


# ---------------------------------------------------------------------------
# Equity ATR sizing
# ---------------------------------------------------------------------------

def size_equity_atr(
    entry: float,
    atr: float,
    side: str,
    policy_equity: dict,
    account_equity: float,
) -> dict:
    """
    Equity/index ATR-multiple position sizing.
    Returns dict with keys: approved, qty, sl_price, tp_price, sizing_method, notes, reject_reason.
    """
    notes: list[str] = []
    side_upper = str(side or "").upper()

    stop_mult = float(policy_equity.get("stop_atr_multiple", 1.5))
    tp_mult = float(policy_equity.get("tp_atr_multiple", 3.0))
    risk_pct = float(policy_equity.get("risk_per_trade_pct", 0.005))

    if float(atr) <= 0 or float(entry) <= 0:
        return _reject("BLOCKED_INVALID_PRICE_OR_ATR", notes + ["atr=0 or entry=0"])

    sl_distance = float(atr) * stop_mult
    tp_distance = float(atr) * tp_mult

    if side_upper == "LONG":
        sl_price = entry - sl_distance
        tp_price = entry + tp_distance
    else:
        sl_price = entry + sl_distance
        tp_price = entry - tp_distance

    risk_dollars = account_equity * risk_pct
    qty = risk_dollars / sl_distance if sl_distance > 0 else 1.0
    qty = max(0.1, round(qty, 2))
    qty = min(qty, 10000.0)

    notes.append(f"atr={atr:.4f} sl_dist={sl_distance:.4f} tp_dist={tp_distance:.4f}")
    notes.append(f"risk_dollars={risk_dollars:.2f} qty={qty:.2f}")

    return _approve(qty, sl_price, tp_price, "atr_position_size", notes)
