# CC Phase 2b — JSON Policy Engine + Crypto Spread Fix + Drawdown Pause + News Blackout

**Target env:** `Vanguard_QAenv`
**Est time:** 2–2.5 hours
**Depends on:** Phase 2a PASS (resolved universe working)
**Prod-path touches:** NONE

---

## 0. Why this exists

V6 today has hardcoded GFT constants, long/short logic trapped in Python, and sizing that doesn't account for crypto bid-ask spread (the GFT bleed on ETH $2 spread). This phase makes V6 a **pure interpreter of JSON policy**. No policy truth in Python.

Plus 3 safety features the user explicitly asked for: drawdown auto-pause, news blackout windows, and crypto spread-aware sizing.

---

## 1. Hard rules

1. QAenv only. Zero Prod references.
2. **Every policy knob that V6 reads must come from JSON.** If a Python constant remains that affects an approval/rejection/sizing decision, the phase is incomplete.
3. **V6 becomes an interpreter, not a decision-maker.** Its decisions are derivable from `(candidate, policy_json)` — reproducible offline given both inputs.
4. **Spread-aware sizing is mandatory for crypto.** SHORT entries price at bid; TP must clear the ask by a configured buffer, otherwise the trade is rejected or resized.
5. **Drawdown pause is hard-enforced, not advisory.** If profile is paused, every candidate for that profile gets `BLOCKED_DRAWDOWN_PAUSE`.
6. **No fallback to old hardcoded path.** Old code paths for GFT rules are deleted, not commented out. Audit rule: `grep -i "GFT" vanguard_risk_filters.py | grep -v "policy_id\|profile_id"` must return near-zero matches after this phase.

---
Addendum 2 — Profile/Policy Precedence
Paste target: CC_PHASE_2B_JSON_POLICY.md, new section before §2 "What to build"

1.5 Profile/policy precedence
Rule: when multiple rule layers apply, precedence is strictly:

temporary_overrides[profile_id] — time-bounded, whitelisted fields only
policy_templates[profile.policy_id] — the assigned policy template
risk_defaults — global fallbacks

Higher layer wins per-field. If a field is absent at layer 1, check layer 2. If absent at layer 2, check layer 3. If absent everywhere, raise PolicyFieldMissing at config load — never default silently.
Hard constraint — no inline per-profile policy overrides
A profile cannot override individual policy fields inline. This is invalid:
json// ❌ INVALID — do not allow
{
  "id": "gft_10k",
  "policy_id": "gft_standard_v1",
  "max_positions_override": 2,
  "risk_per_trade_pct_override": 0.003
}
If gft_10k needs different rules than gft_5k, create a separate policy template and point the profile to it:
json// ✅ CORRECT
{
  "policy_templates": {
    "gft_standard_v1": { "position_limits": {"max_open_positions": 3}, ... },
    "gft_10k_v1":      { "position_limits": {"max_open_positions": 2}, ... }
  },
  "profiles": [
    {"id": "gft_5k",  "policy_id": "gft_standard_v1"},
    {"id": "gft_10k", "policy_id": "gft_10k_v1"}
  ]
}
Why: policy templates stay auditable. Any given profile's effective rules = one named template, not a template + scattered inline patches. Future debugging becomes "read policy X" instead of "read template + read profile overrides + read defaults + diff".
Temporary overrides — strict whitelist
temporary_overrides[profile_id] may only contain keys from this whitelist:
FieldTypePurposeside"LONG_ONLY" | "SHORT_ONLY" | "BOTH"Temporarily restrict directionmax_positionsintTemporarily tighten position capis_pausedboolManually halt new entries for this profileexpires_at_utcISO 8601 strRequired on every overridereasonstrRequired — human-readable justification
Any other key in temporary_overrides is a config validation error. Config loader refuses to start.
If a profile needs a different sizing rule, reject rule, or holding rule temporarily → it's not a temporary override, it's a new policy template. Swap the policy_id on the profile instead.
Expiry
Expired overrides are ignored (not auto-deleted). Config admin UI should surface expired overrides for cleanup, but the policy engine treats now_utc > expires_at_utc as "override does not apply." No silent extension.
Resolver: Vanguard_QAenv/vanguard/accounts/policy_resolver.py (new)
pythondef resolve_effective_policy(config, profile_id, now_utc) -> dict:
    """
    Returns the effective policy dict for this profile at this moment.
    Steps:
      1. Load policy_templates[profile.policy_id] (layer 2)
      2. Overlay risk_defaults for any missing fields (layer 3 fallback)
      3. Overlay active temporary_overrides[profile_id] whitelisted fields (layer 1)
      4. Validate: all required fields present, else raise PolicyFieldMissing
    Output is the resolved dict the policy_engine consumes. Pure function.
    """

def validate_override_whitelist(overrides_block: dict) -> list[str]:
    """
    Returns list of validation errors. Called at config load.
    Rejects any key not in the whitelist.
    """
The policy engine never reads policy_templates or temporary_overrides directly. It only sees the output of resolve_effective_policy().
Acceptance tests (add to Phase 2b §3)
Test 13 — Precedence: override beats policy
Policy sets max_positions=3, override sets max_positions=1 (unexpired). Resolved policy has max_positions=1. Inject 2 candidates — second blocked.
Test 14 — Precedence: expired override ignored
Override with expires_at_utc in the past. Resolved policy uses layer 2 value.
Test 15 — Whitelist enforcement at config load
Add temporary_overrides.gft_10k.risk_per_trade_pct: 0.01 (non-whitelisted key). Start orchestrator.
Expect: ConfigValidationError: temporary_overrides.gft_10k contains non-whitelisted key 'risk_per_trade_pct'. Orchestrator refuses to start.
Test 16 — Inline profile override rejected
Add max_positions_override: 2 to a profile entry. Start orchestrator.
Expect: ConfigValidationError: profile 'gft_10k' contains inline field 'max_positions_override' — use a separate policy_template instead.
Test 17 — Missing required field fails loud
Remove position_limits.max_open_positions from policy template and risk_defaults. Call resolve_effective_policy().
Expect: raises PolicyFieldMissing: position_limits.max_open_positions. No silent default.
Test 18 — Two profiles, two policies, zero cross-contamination
gft_5k → gft_standard_v1 (max=3), gft_10k → gft_10k_v1 (max=2). Verify resolved policies differ for the same field. Verify modifying one template does not affect the other profile's resolved policy.

## 2. What to build

### 2.1 Extend `vanguard_runtime.json`

Add `policy_templates`, `temporary_overrides`, `calendar_blackouts`, `execution` blocks:

```json
{
  "config_version": "2026.04.05.02",
  "policy_templates": {
    "gft_standard_v1": {
      "side_controls": {"allow_long": true, "allow_short": true},
      "position_limits": {
        "max_open_positions": 3,
        "block_duplicate_symbols": true,
        "max_holding_minutes": 240,
        "auto_close_after_max_holding": true
      },
      "drawdown_rules": {
        "daily_loss_pause_pct": 0.03,
        "trailing_drawdown_pause_pct": 0.05,
        "pause_until_next_day": true
      },
      "sizing_by_asset_class": {
        "crypto": {
          "method": "risk_per_stop_spread_aware",
          "risk_per_trade_pct": 0.005,
          "min_qty": 0.000001,
          "min_sl_pct": 0.008,
          "min_tp_pct": 0.016,
          "spread_buffer_pct": 0.002,
          "max_spread_pct_to_enter": 0.005
        },
        "forex": {
          "method": "risk_per_stop_pips",
          "risk_per_trade_pct": 0.005,
          "min_sl_pips": 20,
          "min_tp_pips": 40,
          "pip_value_usd_per_standard_lot": {"EURUSD.x": 10.0, "default": 10.0}
        },
        "equity": {
          "method": "atr_position_size",
          "risk_per_trade_pct": 0.005,
          "stop_atr_multiple": 1.5,
          "tp_atr_multiple": 3.0
        }
      },
      "reject_rules": {
        "enforce_universe": true,
        "enforce_session": true,
        "enforce_position_limit": true,
        "enforce_heat_limit": true,
        "enforce_duplicate_block": true,
        "enforce_drawdown_pause": true,
        "enforce_blackout": true
      }
    },
    "ttp_swing_v1": { "...similar structure, TTP-specific values..." }
  },
  "temporary_overrides": {
    "gft_10k": {
      "side": "SHORT_ONLY",
      "expires_at_utc": "2026-04-06T12:00:00Z",
      "reason": "Crypto execution preference"
    }
  },
  "calendar_blackouts": [
    {
      "event": "FOMC",
      "start_utc": "2026-04-10T18:00:00Z",
      "end_utc": "2026-04-10T18:45:00Z",
      "blocks_asset_classes": ["forex", "equity"],
      "action": "block_new_entries"
    }
  ],
  "execution": {
    "bridges": {
      "metaapi_gft": {"account_id": "<redacted>", "base_url": "<redacted>"}
    },
    "symbol_mapping": {"BTCUSD": "BTCUSD.x"}
  }
}
```

### 2.2 File: `Vanguard_QAenv/vanguard/risk/policy_engine.py` (new)

Core interpreter. Exports:

```python
@dataclass(frozen=True)
class PolicyDecision:
    decision: str           # APPROVED | RESIZED | BLOCKED_*
    reject_reason: str | None
    approved_qty: float
    approved_sl: float
    approved_tp: float
    approved_side: str      # LONG | SHORT
    original_qty: float
    sizing_method: str
    notes: list[str]

def evaluate_candidate(
    candidate: dict,        # from V5 shortlist: symbol, direction, entry, atr, spread_bid, spread_ask, asset_class, etc.
    profile: dict,          # from runtime_config.profiles
    policy: dict,           # resolved policy_template for this profile
    overrides: dict | None, # temporary_overrides for this profile
    blackouts: list,        # active calendar_blackouts at cycle_ts
    account_state: dict,    # {equity, daily_pnl_pct, trailing_dd_pct, paused_until, open_positions: [...]}
    cycle_ts_utc: datetime
) -> PolicyDecision:
    """
    Hard order of checks (first failing check wins):
    1. drawdown_pause  -> BLOCKED_DRAWDOWN_PAUSE
    2. blackout window -> BLOCKED_BLACKOUT
    3. universe scope  -> BLOCKED_SCOPE (already filtered in 2a, defense-in-depth)
    4. session match   -> BLOCKED_SESSION
    5. side control    -> BLOCKED_SIDE_CONTROL (checks overrides first, then policy)
    6. duplicate       -> BLOCKED_DUPLICATE_SYMBOL
    7. position limit  -> BLOCKED_POSITION_LIMIT
    8. heat limit      -> BLOCKED_HEAT (sum risk across open positions vs cap)
    9. sizing          -> compute qty, sl, tp per policy.sizing_by_asset_class
    10. crypto spread gate (if asset_class=crypto and method=risk_per_stop_spread_aware):
        - reject if current_spread_pct > max_spread_pct_to_enter -> BLOCKED_SPREAD_TOO_WIDE
        - for SHORT: entry=bid, tp must be <= bid*(1 - min_tp_pct) - spread_buffer
        - for LONG:  entry=ask, tp must be >= ask*(1 + min_tp_pct) + spread_buffer
        - if computed tp would be worse than broker minimum -> RESIZED with note
    11. minimum viable SL/TP -> BLOCKED_TP_UNREACHABLE if TP math doesn't work after spread
    Return PolicyDecision.
    """
```

### 2.3 File: `Vanguard_QAenv/vanguard/risk/sizing.py` (new)

Pure sizing math, called by policy_engine:

```python
def size_crypto_spread_aware(entry_mid, bid, ask, atr, side, policy_crypto) -> tuple[qty, sl, tp, notes]:
    # Implements the spread-aware logic from §2.2 crypto block
    # Returns final qty, sl_price, tp_price, and notes explaining any adjustments

def size_forex_pips(entry, atr, side, policy_forex, symbol, account_equity) -> tuple[qty, sl, tp, notes]:
    # Uses min_sl_pips/min_tp_pips + pip_value_usd_per_standard_lot

def size_equity_atr(entry, atr, side, policy_equity, account_equity) -> tuple[qty, sl, tp, notes]:
    # ATR-multiple based
```

All three functions must be **pure** (no DB reads, no side effects). Unit-testable in isolation.

### 2.4 File: `Vanguard_QAenv/vanguard/risk/account_state.py` (new)

```python
def load_account_state(profile_id: str, db_path: str, cycle_ts_utc: datetime) -> dict:
    """
    Returns:
      {
        "equity": float,
        "starting_equity_today": float,
        "daily_pnl_pct": float,
        "trailing_dd_pct": float,
        "paused_until_utc": str | None,
        "open_positions": [ {symbol, side, qty, entry, open_ts_utc, unrealized_pnl}, ... ]
      }
    Pulls from vanguard_account_state + vanguard_open_positions tables (Phase 3 populates those;
    for 2b, if tables are empty, return sane defaults: equity=declared_account_size, pnl=0, no positions).
    """
```

### 2.5 File: `Vanguard_QAenv/vanguard/risk/drawdown_pause.py` (new)

```python
def check_drawdown_pause(account_state, policy) -> tuple[bool, str | None]:
    """
    Returns (is_paused, reason).
    - If daily_pnl_pct <= -policy.drawdown_rules.daily_loss_pause_pct -> (True, "daily_loss_limit_hit")
    - If trailing_dd_pct >= policy.drawdown_rules.trailing_drawdown_pause_pct -> (True, "trailing_dd_hit")
    - If account_state.paused_until_utc > now -> (True, "manual_pause")
    - Else (False, None)
    """

def persist_pause(profile_id: str, pause_until_utc: datetime, reason: str, db_path: str):
    """Writes to vanguard_account_state. Fires Telegram notification."""
```

### 2.6 File: `Vanguard_QAenv/vanguard/risk/blackout.py` (new)

```python
def is_in_blackout(cycle_ts_utc, asset_class, blackouts) -> tuple[bool, str | None]:
    """Returns (blocked, event_name)."""
```

### 2.7 Rewrite V6: `Vanguard_QAenv/stages/vanguard_risk_filters.py` (major edit)

Replace all hardcoded logic with calls to `policy_engine.evaluate_candidate()` per (candidate, profile) pair. V6 becomes:

```python
def run(dry_run=False, cycle_ts=None):
    config = load_runtime_config()
    shortlist = load_shortlist(cycle_ts)
    active_profiles = [p for p in config["profiles"] if p["is_active"]]
    blackouts = active_blackouts(config, cycle_ts)

    decisions = []
    for profile in active_profiles:
        policy = config["policy_templates"][profile["policy_id"]]
        overrides = config.get("temporary_overrides", {}).get(profile["id"])
        state = load_account_state(profile["id"], DB_PATH, cycle_ts)
        for cand in shortlist:
            dec = evaluate_candidate(cand, profile, policy, overrides, blackouts, state, cycle_ts)
            decisions.append((profile["id"], cand, dec))

    if not dry_run:
        write_tradeable_portfolio(decisions, cycle_ts)
    return decisions
```

**Delete** (do not comment out):
- Hardcoded GFT universe lists in V6
- Hardcoded max_positions, risk_pct, stop_multipliers in V6
- Hardcoded side controls

### 2.8 New DB table: `vanguard_account_state`

```sql
CREATE TABLE IF NOT EXISTS vanguard_account_state (
  profile_id TEXT PRIMARY KEY,
  equity REAL NOT NULL,
  starting_equity_today REAL NOT NULL,
  daily_pnl_pct REAL NOT NULL DEFAULT 0.0,
  trailing_dd_pct REAL NOT NULL DEFAULT 0.0,
  paused_until_utc TEXT,
  pause_reason TEXT,
  updated_at_utc TEXT NOT NULL
);
```

For Phase 2b, seed one row per active profile from the declared account size. Phase 3 replaces with MetaApi truth.

### 2.9 Log config_version every cycle

Append to `vanguard_resolved_universe_log`: already has `config_version` from 2a. Also log to stdout at cycle start:
`[cycle_ts=... config_version=2026.04.05.02 profiles=gft_5k,gft_10k,ttp_10k_swing]`

---

## 3. Acceptance tests

### Test 1 — allow_long=false blocks longs
Set `policy_templates.gft_standard_v1.side_controls.allow_long=false`. Inject a LONG crypto candidate. Run V6.
**Expect:** decision=`BLOCKED_SIDE_CONTROL`, reject_reason mentions "long_not_allowed_by_policy".

### Test 2 — Temporary override takes precedence
Set `temporary_overrides.gft_10k.side="SHORT_ONLY"` with future expiry. Inject a LONG candidate for gft_10k.
**Expect:** blocked with reject_reason "long_not_allowed_by_override". Same candidate for gft_5k (no override): approved if policy allows.

### Test 3 — Override expires
Set override expires_at_utc to past timestamp. Run V6.
**Expect:** override ignored, policy-level side_controls take over.

### Test 4 — Crypto spread-aware sizing rejects wide spread
Inject a crypto candidate with spread_pct=0.007 (above max 0.005).
**Expect:** `BLOCKED_SPREAD_TOO_WIDE`.

### Test 5 — Crypto spread-aware TP clears the ask
Inject ETH SHORT candidate: bid=2000, ask=2002 (0.1% spread), min_tp_pct=0.016, buffer=0.002. Required TP ≤ bid*(1-0.016) - buffer*bid = 2000*0.984 - 4 = 1964. Verify approved TP is ≤ 1964.
**Expect:** `APPROVED` with tp ≤ 1964, sizing notes include "spread_buffer_applied=4.0".

### Test 6 — Drawdown pause enforced
Seed `vanguard_account_state.gft_10k.daily_pnl_pct=-0.035` (exceeds 0.03 cap). Run V6 with any candidate.
**Expect:** `BLOCKED_DRAWDOWN_PAUSE`, reject_reason="daily_loss_limit_hit".

### Test 7 — Blackout window blocks forex
Add a blackout window covering cycle_ts for asset_classes=["forex"]. Inject forex candidate.
**Expect:** `BLOCKED_BLACKOUT` with event name in notes. Crypto candidate same cycle: not blocked.

### Test 8 — Position limit enforced
Seed `vanguard_account_state.gft_10k.open_positions` with 3 positions, policy max=3. Inject new candidate.
**Expect:** `BLOCKED_POSITION_LIMIT`.

### Test 9 — Duplicate symbol blocked
Seed open position for BTCUSD.x on gft_10k. Inject new BTCUSD.x candidate for gft_10k.
**Expect:** `BLOCKED_DUPLICATE_SYMBOL` (policy block_duplicate_symbols=true).

### Test 10 — Per-asset sizing produces distinct qty
Inject 3 candidates (crypto, forex, equity) same risk_per_trade_pct=0.005, equity=$10,000.
**Expect:** each uses its own sizing method; approved_qty and sl/tp are method-specific. Verify sizing_method field matches method in JSON.

### Test 11 — No hardcoded GFT constants remain
Run: `grep -n "GFT\|5k\|10k" Vanguard_QAenv/stages/vanguard_risk_filters.py | grep -v "profile_id\|policy_id\|# "`
**Expect:** zero matches.

### Test 12 — Reproducibility
Given same (candidate, policy, state, cycle_ts), call `evaluate_candidate()` 3 times.
**Expect:** identical PolicyDecision every time.

---

## 4. Report-back criteria

CC must produce:
1. List of new/edited files + line counts
2. Output of Tests 1–12 verbatim
3. `grep` output proving no hardcoded GFT constants in V6
4. Sample PolicyDecision JSON for each reject reason (one example each)
5. **Diff summary:** which V6 code was deleted, what replaced it
6. Confirmation: `grep -r "/SS/Vanguard/data" Vanguard_QAenv/` returns zero matches

---

## 5. Non-goals (this phase)

- Position lifecycle daemon (Phase 3)
- Real MetaApi account state sync (Phase 3 — for 2b, seed state manually)
- Trade journal (Phase 3)
- UI (Phase 5)

---

## 6. Stop-the-line triggers

- Any hardcoded policy constant remains in V6
- Test 5 (spread math) fails — crypto sizing must be provably correct
- Test 12 (reproducibility) fails — non-determinism in policy_engine is a bug
- `grep` for Prod paths returns any matches
