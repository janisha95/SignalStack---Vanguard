"""
sizing_equity.py — Equity/index ATR-based position sizing (Phase 4 extraction).

Single responsibility: size_equity_atr().
Extracted verbatim from sizing.py — no logic changes.

sizing.py re-exports this function for backward-compat imports.
"""
from vanguard.risk.sizing import size_equity_atr  # noqa: F401

__all__ = ["size_equity_atr"]
