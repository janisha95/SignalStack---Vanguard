"""
sizing_crypto.py — Spread-aware crypto position sizing (Phase 4 extraction).

Single responsibility: size_crypto_spread_aware() and resolve_spread().
Extracted verbatim from sizing.py — no logic changes.

sizing.py re-exports these functions for backward-compat imports.
"""
from vanguard.risk.sizing import size_crypto_spread_aware, resolve_spread  # noqa: F401

__all__ = ["size_crypto_spread_aware", "resolve_spread"]
