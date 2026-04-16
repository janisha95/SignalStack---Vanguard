# Vanguard_QAenv Phase 2a + 2b Implementation Spec
**Scope:** QAenv only  
**Repo:** `/Users/sjani008/SS/Vanguard_QAenv`  
**Purpose:** make QA runtime actually obey active profiles and JSON policy templates, without touching production and without drifting into broader refactors.

---

## Objective

Implement only what is necessary to prove the new config-driven QA architecture works.

### Phase 2a
Make **active profiles define the analyzed universe before V1/V2**.

### Phase 2b
Make **V6 decisions and sizing come from JSON policy templates**, not hidden hardcoded behavior.

Do not spec or build beyond that in this pass.

---

# Phase 2a — Active-Profile Universe Control

## Goal
Before V1/V2 run, resolve the runtime symbol scope from:
- active profiles
- their instrument scopes
- currently open asset sessions
- optional `--force-assets` or runtime override

Then either:
- only report that resolved universe (`observe`)
- or actually constrain the pipeline to it (`enforce`)

## Why this is needed
Right now QA can still analyze symbols that active accounts cannot trade, then reject them in V6 later.  
That creates:
- shortlist spam
- wasted analysis
- noisy GFT-only testing
- poor QA signal

The desired behavior is:

### If only `gft_5k` and `gft_10k` are active
- crypto-only hours → only GFT crypto symbols analyzed
- forex hours → only GFT crypto + forex symbols analyzed
- equity-open hours → only GFT crypto + forex + approved GFT equity/CFD names analyzed

### If TTP is active
- dynamic TTP equity universe is allowed
- JSON stores filters/source, not 12k tickers

---

## Required implementation

### 1. Read active profiles from runtime config
Use `config/vanguard_runtime.json`.

Profiles with:
```json
"is_active": true
```
are the only ones that should contribute to the runtime universe.

### 2. Resolve universe membership
For each active profile:
- read `instrument_scope`
- resolve the underlying symbol set from `universes`
- union the symbol sets across active profiles

### 3. Filter by current session
Use current session/open-market logic to determine which asset classes are expected right now.

Expected modes:
- `crypto_only`
- `crypto_forex`
- `crypto_forex_equity`

### 4. Add runtime mode switch
Add explicit runtime behavior like:

```json
"runtime": {
  "resolved_universe_mode": "observe"
}
```

Supported values:
- `observe`
- `enforce`

#### observe
- compute the resolved universe
- report/log it
- do not constrain pipeline yet

#### enforce
- only resolved symbols should enter V1/V2/V3/V4/V5

### 5. Wire resolver into orchestrator pre-V1/V2
The resolved universe must be used **before** broad symbol analysis begins.

### 6. Add QA debug visibility
Add one of:
- `GET /api/v1/runtime/resolved-universe`
- or an equivalent QA debug output/report/log artifact

It should show:
- active profiles
- current session mode
- expected asset classes
- resolved symbols
- optionally excluded symbols + reasons

---

## Acceptance criteria for Phase 2a

### AC1
If only `gft_5k` and `gft_10k` are active during crypto-only hours, only GFT crypto symbols are analyzed.

### AC2
If only `gft_5k` and `gft_10k` are active during forex hours, only GFT crypto + forex symbols are analyzed.

### AC3
If only `gft_5k` and `gft_10k` are active during equity-open hours, only GFT crypto + forex + approved GFT equity names are analyzed.

### AC4
If TTP is active, the dynamic TTP equity scope resolves from source + filters, not an explicit huge ticker list in JSON.

### AC5
`observe` mode and `enforce` mode behave differently and truthfully.

---

# Phase 2b — JSON Policy Templates for V6

## Goal
Make V6 a **policy interpreter** driven by JSON, not a hidden branchy rule box.

Profiles should point to policy templates, and V6 should use those templates to decide:
- whether longs are allowed
- whether shorts are allowed
- position limits
- duplicate-symbol rules
- session/universe enforcement
- asset-specific sizing
- reject reasons

---

## Required config pattern

Profiles should reference a policy template:

```json
{
  "profiles": [
    {
      "id": "gft_10k",
      "is_active": true,
      "instrument_scope": "gft_universe",
      "policy_id": "gft_standard_v1"
    }
  ]
}
```

Policy templates should define behavior like:

```json
{
  "policy_templates": {
    "gft_standard_v1": {
      "side_controls": {
        "allow_long": true,
        "allow_short": true
      },
      "position_limits": {
        "max_open_positions": 3,
        "block_duplicate_symbols": true,
        "max_holding_minutes": 240,
        "auto_close_after_max_holding": true
      },
      "sizing_by_asset_class": {
        "crypto": {
          "method": "risk_per_stop",
          "risk_per_trade_pct": 0.005,
          "min_qty": 0.000001,
          "min_sl_pct": 0.08,
          "min_tp_pct": 0.16
        },
        "forex": {
          "method": "risk_per_stop",
          "risk_per_trade_pct": 0.005,
          "min_sl_pips": 20,
          "min_tp_pips": 40
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
        "enforce_heat_limit": true
      }
    }
  }
}
```

---

## Required implementation

### 1. Wire policy engine into current V6 path
The new policy engine must be used by QA V6, not just exist as unused scaffolding.

### 2. Support side controls
Policy must support:
- long-only
- short-only
- both enabled

At minimum:
- `allow_long`
- `allow_short`

### 3. Support temporary profile-scoped override
Recommended shape:

```json
{
  "temporary_overrides": {
    "gft_10k": {
      "side": "SHORT_ONLY",
      "expires_at": "2026-04-05T12:30:00-04:00",
      "reason": "Crypto execution preference"
    }
  }
}
```

This should override base side controls for that profile until expiry.

### 4. Support position limits
Use JSON policy for:
- `max_open_positions`
- `block_duplicate_symbols`

### 5. Support asset-specific sizing
Use policy template rules for:
- crypto sizing
- forex sizing
- equity sizing

No hidden GFT constants should remain the real authority over these rules.

### 6. Emit explicit decisions
For each candidate/profile evaluation, emit machine-readable status and reasons such as:
- `APPROVED`
- `RESIZED`
- `BLOCKED_SCOPE`
- `BLOCKED_SESSION`
- `BLOCKED_SIDE_CONTROL`
- `BLOCKED_POSITION_LIMIT`
- `BLOCKED_HEAT`
- `BLOCKED_DUPLICATE_SYMBOL`

### 7. Preserve current working QA path otherwise
Do not redesign the whole V6 or execution architecture in this pass.
Only wire policy-driven behavior into the current QA path.

---

## Acceptance criteria for Phase 2b

### AC1
If `allow_long=false`, no long approvals for that profile.

### AC2
If `allow_short=false`, no short approvals for that profile.

### AC3
If temporary override sets `SHORT_ONLY`, that profile becomes short-only until expiry.

### AC4
Asset-specific sizing rules change approved size in a real measurable way.

### AC5
Reject reasons are explicit and machine-readable.

### AC6
No hidden GFT-specific constants remain more authoritative than JSON policy for the rules touched in this phase.

---

# Out of scope for this spec

Do **not** do any of the following in this pass:
- no production repo changes
- no React config/admin UI
- no full lifecycle daemon
- no auto-close implementation beyond config presence
- no major router refactor
- no broad orchestrator rewrite
- no full Phase 3 / 4 / 5 work
- no “clean architecture” polishing unless necessary to wire 2a/2b

---

# Proof run requirements

After implementation, run QA proof tests and report:

## 1. Resolved universe proof
Show:
- active profiles
- session mode
- resolved symbols
- whether pipeline was observe or enforce

## 2. Shortlist proof
Show latest shortlist rows after Phase 2a enforcement.

## 3. V6 policy proof
Show:
- one approved example
- one blocked example
- one side-control example if tested
- one sizing example if tested

## 4. Diff-friendly output
The report should be easy to compare before/after a JSON edit.

---

# Expected coding-agent output

At the end of the pass, return:

1. exact files changed
2. where orchestrator now calls the runtime universe resolver
3. where V6 now calls the policy engine
4. whether resolved-universe debug output/endpoint was added
5. sample config used
6. proof run summary
7. remaining blockers before Phase 3

---

# Recommendation on Claude usage

Because the system is currently working and Claude usage is down to ~4%, the safer call is:

- **save Claude for tomorrow unless this is truly urgent tonight**
- use tomorrow’s refreshed budget for this Phase 2a/2b pass
- do not burn the last usage on a rushed run unless you absolutely need movement tonight

This pass needs careful wiring + proof, not a panicked partial implementation.
