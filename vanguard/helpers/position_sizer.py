"""
position_sizer.py — ATR-based position sizing for V6 Risk Filters.

Supports:
  - Equities (TTP): shares = floor(risk_dollars / stop_distance_per_share)
  - Forex (FTMO): lots = risk_dollars / (stop_pips × pip_value_per_lot)
  - Futures: contracts = floor(risk_dollars / (stop_ticks × tick_value))

ATR is computed from vanguard_bars_5m (intraday, 14-period Wilder's ATR).

Location: ~/SS/Vanguard/vanguard/helpers/position_sizer.py
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
_DB_PATH = str(_ROOT / "data" / "vanguard_universe.db")


# ---------------------------------------------------------------------------
# ATR computation
# ---------------------------------------------------------------------------

def compute_atr(
    symbol: str,
    db_path: str = _DB_PATH,
    period: int = 14,
    lookback: int = 60,
) -> float:
    """
    Compute Wilder's ATR(period) from the most recent `lookback` 5-minute bars.
    Returns 0.0 if insufficient data.
    """
    with sqlite3.connect(db_path) as con:
        rows = con.execute(
            """
            SELECT high, low, close
            FROM vanguard_bars_5m
            WHERE symbol = ?
            ORDER BY bar_ts_utc DESC
            LIMIT ?
            """,
            (symbol, lookback + 1),
        ).fetchall()

    if len(rows) < period + 1:
        return 0.0

    rows = list(reversed(rows))  # oldest first
    highs  = np.array([r[0] for r in rows], dtype=float)
    lows   = np.array([r[1] for r in rows], dtype=float)
    closes = np.array([r[2] for r in rows], dtype=float)

    # True Range
    n = len(rows)
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1]),
        ),
    )

    # Wilder smoothing
    atr = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period

    return float(atr)


# ---------------------------------------------------------------------------
# Risk dollar computation
# ---------------------------------------------------------------------------

def compute_risk_dollars(
    account_size: float,
    risk_per_trade_pct: float,
    daily_budget_remaining: float,
    drawdown_budget_remaining: float,
) -> float:
    """
    Risk dollars for this trade = min of three constraints:
      1. risk_per_trade_pct × account_size  (per-trade max risk)
      2. daily_budget_remaining × 0.5       (don't blow half the day on one trade)
      3. drawdown_budget_remaining × 0.25   (preserve drawdown cushion)
    """
    per_trade_max  = risk_per_trade_pct * account_size
    daily_capped   = max(0.0, daily_budget_remaining  * 0.5)
    drawdown_capped = max(0.0, drawdown_budget_remaining * 0.25)
    return min(per_trade_max, daily_capped, drawdown_capped)


# ---------------------------------------------------------------------------
# Equity sizing
# ---------------------------------------------------------------------------

def size_equity(
    price: float,
    atr: float,
    stop_atr_multiple: float,
    tp_atr_multiple: float,
    risk_dollars: float,
    direction: str,
) -> dict:
    """
    Size an equity position using ATR-based stops.

    Returns dict with:
        shares, stop_price, tp_price, stop_distance,
        position_value, actual_risk, risk_pct_of_price
    """
    if atr <= 0 or risk_dollars <= 0:
        return _empty_sizing()

    stop_distance = atr * stop_atr_multiple
    if stop_distance <= 0:
        return _empty_sizing()

    shares = math.floor(risk_dollars / stop_distance)
    if shares <= 0:
        return _empty_sizing()

    if direction == "LONG":
        stop_price = round(price - stop_distance, 2)
        tp_price   = round(price + atr * tp_atr_multiple, 2)
    else:  # SHORT
        stop_price = round(price + stop_distance, 2)
        tp_price   = round(price - atr * tp_atr_multiple, 2)

    return {
        "shares":            shares,
        "stop_price":        stop_price,
        "tp_price":          tp_price,
        "stop_distance":     round(stop_distance, 4),
        "position_value":    round(shares * price, 2),
        "actual_risk":       round(shares * stop_distance, 2),
        "risk_pct_of_price": round(stop_distance / price, 6) if price > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Forex sizing
# ---------------------------------------------------------------------------

_PIP_VALUE_PER_LOT_USD = 10.0   # standard lot USD pairs ($10 per pip)


def size_forex(
    price: float,
    atr_pips: float,
    stop_atr_multiple: float,
    tp_atr_multiple: float,
    risk_dollars: float,
    direction: str,
    pip_value_per_lot: float = _PIP_VALUE_PER_LOT_USD,
) -> dict:
    """
    Size a forex position in lots.

    atr_pips : ATR expressed in pips (e.g., 8.5 for EURUSD)
    """
    if atr_pips <= 0 or risk_dollars <= 0:
        return _empty_sizing()

    stop_pips = atr_pips * stop_atr_multiple
    tp_pips   = atr_pips * tp_atr_multiple
    pip_size  = 0.0001  # standard forex pip for 4-digit pairs

    lots = risk_dollars / (stop_pips * pip_value_per_lot)
    lots = round(lots, 2)  # round to 0.01 lots

    if direction == "LONG":
        stop_price = round(price - stop_pips * pip_size, 5)
        tp_price   = round(price + tp_pips * pip_size, 5)
    else:
        stop_price = round(price + stop_pips * pip_size, 5)
        tp_price   = round(price - tp_pips * pip_size, 5)

    return {
        "shares":         lots,       # "shares" = lots in forex context
        "stop_price":     stop_price,
        "tp_price":       tp_price,
        "stop_distance":  round(stop_pips * pip_size, 6),
        "position_value": round(lots * 100_000 * price, 2),  # notional
        "actual_risk":    round(lots * stop_pips * pip_value_per_lot, 2),
        "risk_pct_of_price": 0.0,
    }


# ---------------------------------------------------------------------------
# Futures sizing
# ---------------------------------------------------------------------------

def size_futures(
    atr_ticks: float,
    stop_atr_multiple: float,
    risk_dollars: float,
    tick_value: float,
) -> dict:
    """
    Size a futures position in contracts.
    tick_value : $ value per tick per contract (e.g., 12.5 for /MES)
    """
    if atr_ticks <= 0 or risk_dollars <= 0 or tick_value <= 0:
        return _empty_sizing()

    stop_ticks  = atr_ticks * stop_atr_multiple
    contracts   = math.floor(risk_dollars / (stop_ticks * tick_value))
    actual_risk = contracts * stop_ticks * tick_value

    return {
        "shares":         contracts,
        "stop_price":     None,
        "tp_price":       None,
        "stop_distance":  stop_ticks,
        "position_value": None,
        "actual_risk":    round(actual_risk, 2),
        "risk_pct_of_price": 0.0,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_sizing() -> dict:
    return {
        "shares":            0,
        "stop_price":        None,
        "tp_price":          None,
        "stop_distance":     0.0,
        "position_value":    0.0,
        "actual_risk":       0.0,
        "risk_pct_of_price": 0.0,
    }
