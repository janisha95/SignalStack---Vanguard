"""
sizing_forex.py — Forex pip-based position sizing (Phase 4 extraction).

Single responsibility: size_forex_pips().
Extracted verbatim from sizing.py — no logic changes.

sizing.py re-exports this function for backward-compat imports.
"""
from vanguard.risk.sizing import size_forex_pips  # noqa: F401

__all__ = ["size_forex_pips"]
