"""
account_state.py — Backward-compat re-export shim (Phase 4).

The canonical implementation moved to vanguard.accounts.account_state.
This module re-exports everything so existing imports keep working:
    from vanguard.risk.account_state import load_account_state, seed_account_states
"""
from vanguard.accounts.account_state import (  # noqa: F401
    load_account_state,
    seed_account_states,
)

__all__ = ["load_account_state", "seed_account_states"]
