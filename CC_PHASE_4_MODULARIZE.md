# CC Phase 4 — Modularize Risk Engine

**Target env:** `Vanguard_QAenv`
**Est time:** 45 min – 1 hour
**Depends on:** Phase 2b + 3 PASS
**Prod-path touches:** NONE

---

## 0. Why this exists

After Phase 2b, `policy_engine.py` knows a lot. After Phase 3, `lifecycle_daemon.py` knows a lot. We want small, focused modules so that adding a new prop firm or a new reject reason is one file, not six. This phase is **mostly moving code**, not writing new logic.

**Explicit anti-goal:** no behavior changes. Output decisions for the same inputs must be byte-identical to Phase 2b+3.

---

## 1. Hard rules

1. QAenv only.
2. **No behavior changes.** Golden-test against Phase 2b output.
3. **No new rules.** Just relocation + interface cleanup.
4. Every module gets a docstring stating its single responsibility.
5. Circular imports forbidden. If `side_controls.py` needs something from `policy_engine.py`, the shared thing moves to `base.py`.

---

## 2. Target module layout

```
Vanguard_QAenv/vanguard/accounts/
  __init__.py
  runtime_config.py       (from 2a, no changes)
  runtime_universe.py     (from 2a, no changes)
  dynamic_universe_sources.py (from 2a)
  profiles.py             (NEW: profile lookup, active filter, policy assignment)
  account_state.py        (moved from risk/, no changes)

Vanguard_QAenv/vanguard/risk/
  __init__.py
  base.py                 (NEW: PolicyDecision dataclass, shared enums, reject reason constants)
  policy_engine.py        (from 2b, slimmer: orchestrates calls to specialized modules)
  side_controls.py        (NEW: allow_long/allow_short + temporary_overrides evaluation)
  position_limits.py      (NEW: max_positions, block_duplicate_symbols, heat_limit)
  holding_rules.py        (NEW: max_holding_minutes check, auto_close policy reads)
  drawdown_pause.py       (from 2b, no changes)
  blackout.py             (from 2b, no changes)
  reject_rules.py         (NEW: single source of reject reason constants + descriptions)
  sizing.py               (from 2b, no changes)
  sizing_crypto.py        (NEW: just size_crypto_spread_aware extracted)
  sizing_forex.py         (NEW: just size_forex_pips extracted)
  sizing_equity.py        (NEW: just size_equity_atr extracted)
```

---

## 3. Module responsibilities

### `accounts/profiles.py`
```python
def get_active_profiles(config) -> list[dict]: ...
def get_profile_by_id(config, profile_id) -> dict | None: ...
def get_policy_for_profile(config, profile) -> dict: ...
def get_override_for_profile(config, profile_id, now_utc) -> dict | None:
    """Returns override only if not expired. None otherwise."""
```

### `risk/base.py`
```python
from dataclasses import dataclass
from enum import Enum

class Decision(str, Enum):
    APPROVED = "APPROVED"
    RESIZED = "RESIZED"
    BLOCKED_SCOPE = "BLOCKED_SCOPE"
    BLOCKED_SESSION = "BLOCKED_SESSION"
    BLOCKED_SIDE_CONTROL = "BLOCKED_SIDE_CONTROL"
    BLOCKED_POSITION_LIMIT = "BLOCKED_POSITION_LIMIT"
    BLOCKED_HEAT = "BLOCKED_HEAT"
    BLOCKED_DUPLICATE_SYMBOL = "BLOCKED_DUPLICATE_SYMBOL"
    BLOCKED_DRAWDOWN_PAUSE = "BLOCKED_DRAWDOWN_PAUSE"
    BLOCKED_BLACKOUT = "BLOCKED_BLACKOUT"
    BLOCKED_SPREAD_TOO_WIDE = "BLOCKED_SPREAD_TOO_WIDE"
    BLOCKED_TP_UNREACHABLE = "BLOCKED_TP_UNREACHABLE"

@dataclass(frozen=True)
class PolicyDecision: ...  # same as Phase 2b
```

### `risk/side_controls.py`
```python
def evaluate_side(side_requested, policy, override, now_utc) -> tuple[bool, str | None]:
    """Returns (allowed, reason_if_blocked)."""
```

### `risk/position_limits.py`
```python
def check_position_limit(open_positions, policy) -> tuple[bool, str | None]: ...
def check_duplicate_symbol(symbol, open_positions, policy) -> tuple[bool, str | None]: ...
def check_heat_limit(open_positions, new_risk, policy) -> tuple[bool, str | None]: ...
```

### `risk/holding_rules.py`
```python
def should_auto_close(position, policy, now_utc) -> tuple[bool, str | None]: ...
```

### `risk/reject_rules.py`
```python
REJECT_REASONS = {
    "long_not_allowed_by_policy": "Policy side_controls.allow_long is false",
    "long_not_allowed_by_override": "Active temporary override restricts to SHORT_ONLY",
    "short_not_allowed_by_policy": "Policy side_controls.allow_short is false",
    "short_not_allowed_by_override": "Active temporary override restricts to LONG_ONLY",
    "max_positions_reached": "Profile has {current} open positions, max is {max}",
    "duplicate_symbol": "Profile already has open position on {symbol}",
    "heat_limit_exceeded": "Adding this trade would exceed heat cap of {cap}",
    "daily_loss_limit_hit": "Daily P&L {pnl_pct} <= pause threshold {threshold}",
    "trailing_dd_hit": "Trailing drawdown {dd_pct} >= pause threshold {threshold}",
    "manual_pause": "Profile manually paused until {until}",
    "blackout_active": "Blackout window active: {event} ({start} to {end})",
    "spread_too_wide": "Current spread {spread_pct} > max allowed {max_pct}",
    "tp_unreachable": "Spread-adjusted TP cannot clear broker minimum",
    "not_in_scope": "Symbol not in profile's instrument_scope",
    "session_closed": "Asset class {asset_class} session is closed"
}

def format_reason(key: str, **kwargs) -> str:
    return REJECT_REASONS[key].format(**kwargs)
```

### `risk/policy_engine.py` (slimmer)
Orchestrator only. Delegates to specialized modules:

```python
def evaluate_candidate(candidate, profile, policy, override, blackouts, account_state, cycle_ts):
    # 1. drawdown check
    paused, reason = check_drawdown_pause(account_state, policy)
    if paused: return blocked(Decision.BLOCKED_DRAWDOWN_PAUSE, reason)
    # 2. blackout
    blocked_by_blackout, event = is_in_blackout(cycle_ts, candidate.asset_class, blackouts)
    if blocked_by_blackout: return blocked(Decision.BLOCKED_BLACKOUT, event)
    # 3. side
    allowed, reason = evaluate_side(candidate.side, policy, override, cycle_ts)
    if not allowed: return blocked(Decision.BLOCKED_SIDE_CONTROL, reason)
    # 4. duplicates, position limits
    ...
    # 5. dispatch to sizing module by asset_class
    sizer = {"crypto": size_crypto_spread_aware, "forex": size_forex_pips, "equity": size_equity_atr}[candidate.asset_class]
    qty, sl, tp, notes = sizer(...)
    # 6. final validation + return
```

### Sizing split
`sizing_crypto.py`, `sizing_forex.py`, `sizing_equity.py` each contain exactly ONE function. `sizing.py` re-exports them for backwards-compat imports.

---

## 4. Golden test — behavior parity

Before refactor: capture outputs of `evaluate_candidate()` for **100 synthetic candidates** × **3 profiles** × **realistic account states** = 300 decisions. Save as `/tmp/phase2b_golden.json`.

After refactor: run same inputs through new module layout, capture to `/tmp/phase4_golden.json`.

**Acceptance:** the two files must be byte-identical (after JSON sort_keys).

### Golden test script: `Vanguard_QAenv/scripts/golden_parity_test.py`

```python
# Generates the 300 synthetic test cases with fixed random seed
# Runs evaluate_candidate() on each
# Writes JSON with sort_keys=True
# CLI: --pre (Phase 2b layout) vs --post (Phase 4 layout)
# Exit 0 if files match, 1 if diff

# Usage:
#   git checkout phase-2b-complete -- Vanguard_QAenv/vanguard
#   python3 scripts/golden_parity_test.py --out /tmp/phase2b_golden.json
#   git checkout phase-4-complete  -- Vanguard_QAenv/vanguard
#   python3 scripts/golden_parity_test.py --out /tmp/phase4_golden.json
#   diff /tmp/phase2b_golden.json /tmp/phase4_golden.json
```

---

## 5. Acceptance tests

### Test 1 — Golden parity
Run golden_parity_test.py before and after refactor.
**Expect:** zero diff.

### Test 2 — No circular imports
`python3 -c "import vanguard.risk.policy_engine; import vanguard.accounts.profiles"`
**Expect:** no ImportError.

### Test 3 — Every module has single responsibility docstring
```bash
for f in Vanguard_QAenv/vanguard/risk/*.py Vanguard_QAenv/vanguard/accounts/*.py; do
  head -5 "$f" | grep -q '"""' || echo "MISSING: $f"
done
```
**Expect:** no MISSING output.

### Test 4 — Reject reason constants centralized
`grep -r "BLOCKED_SIDE_CONTROL\|BLOCKED_POSITION_LIMIT" Vanguard_QAenv/vanguard/ --include="*.py"` should show each constant used only via import from `reject_rules.py` or `base.py`. No raw string literals.

### Test 5 — Add a fake new reject reason in 1 file
Add `BLOCKED_TEST_REASON` to `reject_rules.py` and `base.py` Decision enum. Import + use in one branch of policy_engine. Verify nothing else needs editing for the new reason to surface in output.
Then remove it.

### Test 6 — Running orchestrator end-to-end produces same V6 output
Run single orchestrator cycle pre-refactor, capture `vanguard_tradeable_portfolio` rows. Run same cycle post-refactor. Diff.
**Expect:** zero diff (bit-for-bit identical).

---

## 6. Report-back criteria

CC must produce:
1. File tree before + after showing new structure
2. Golden test output (diff of pre/post JSON)
3. Tests 1–6 output verbatim
4. Line-count summary: `policy_engine.py` should be smaller post-refactor
5. One-paragraph description of what a future "add FTMO policy" task would touch (proof of modularity)

---

## 7. Non-goals (this phase)

- No new policy rules
- No new asset class support
- No UI changes
- No performance optimization

---

## 8. Stop-the-line triggers

- Golden test produces ANY diff
- Any test from Phase 2b regresses
- Circular import appears
- `policy_engine.py` grows instead of shrinks
