# Vanguard QAenv Future-State Roadmap — Phases 2, 3, 4, 5
**Audience:** xAI Grok / Aider / coding agent working in `Vanguard_QAenv`  
**Intent:** implement the future-state runtime step by step in QAenv, without breaking production.  
**Basis:** current architecture map, super-user config proposal, and the agreed product goal that **active profiles should define the analyzed universe**, while **policy templates should define V6 behavior**.

---

## 0. Why this roadmap exists

Today, the system is operational, but the runtime truth is still too scattered:
- universe membership can be broader than the active accounts actually need
- GFT-style restrictions are still partly discovered too late in V6
- risk/sizing/reject rules are still too hidden in Python behavior
- trade lifecycle state is not yet first-class enough for automated management and UI truth
- config is not yet truly user-driven

The future state we want is:

1. **Active profiles drive what gets analyzed**
2. **JSON policy templates drive what gets approved, rejected, resized, or auto-closed**
3. **Code modules interpret policy cleanly**
4. **UI eventually edits that config safely**
5. **QAenv proves all of this before prod changes**

This document covers **Phases 2, 3, 4, and 5** only.

---

# 1. Global principles for all phases

## 1.1 QAenv only
All work in this roadmap should land in:

`/Users/sjani008/SS/Vanguard_QAenv`

Do **not** change prod first.

## 1.2 JSON owns rules, Python owns interpretation
- JSON = universes, active profiles, risk limits, side controls, account rules
- Python = loader, resolver, policy engine, lifecycle engine, execution mechanics

## 1.3 Safe rollout
For all major behavior changes, prefer:
- `observe` mode first
- then `enforce` mode

## 1.4 No fake UI assumptions
Backend must expose explicit machine truth:
- expected asset classes
- resolved universe
- policy-driven decisions
- trade/position lifecycle state

## 1.5 Active profiles must define runtime scope before V2
This is the key architectural shift.

We do **not** want:
- analyze everything
- reject most of it in V6

We **do** want:
- analyze only what active profiles can actually trade

---

# 2. Target config model (foundation for Phases 2–5)

Use `config/vanguard_runtime.json` in QAenv as the super-user config source.

Recommended top-level shape:

```json
{
  "runtime": {},
  "market_data": {},
  "universes": {},
  "risk_defaults": {},
  "profiles": [],
  "policy_templates": {},
  "temporary_overrides": {},
  "execution": {},
  "telegram": {}
}
```

## 2.1 Meaning of each block

### `runtime`
Controls how the QA orchestrator runs:
- environment (`dev`, `qa-shadow`, `qa-enforce`)
- execution mode
- cycle interval
- force-assets override
- shortlist sizing
- resolved-universe mode (`observe`, `enforce`)

### `market_data`
Defines:
- session windows by asset class
- source routing by asset class
- fallback sources
- dynamic equity universe source

### `universes`
Defines eligible symbols/scopes:
- dynamic TTP equity universe
- static explicit GFT universe
- optional FTMO / TopStep scopes later

### `risk_defaults`
Shared defaults not tied to one profile:
- GFT shared risk defaults
- common ATR/SL/TP defaults
- heat / duplicate limits

### `profiles`
Defines active prop-firm accounts:
- `id`
- `is_active`
- `instrument_scope`
- `policy_id`
- account size
- environment
- execution bridge

### `policy_templates`
Defines prop-firm/account behavior:
- allow long / allow short
- asset-specific sizing
- reject rules
- max positions
- holding duration
- auto-close behavior

### `temporary_overrides`
Profile-scoped temporary runtime overrides, e.g.:
- short-only for 2 hours on crypto
- temporarily block longs for a profile
- temporary max positions change

### `execution`
Execution routes and symbol maps:
- SignalStack webhooks
- MT5 / MetaApi mapping
- retry / timeout defaults

### `telegram`
Notification behavior

---

# 3. Phase 2 — Active-profile runtime control + JSON policy-driven V6

---

## Phase 2a — Active-Profile Universe Control

## Goal
Make the pipeline analyze only symbols that are relevant to the currently active profiles.

## Current problem
Today, broad universe membership can enter V1/V2/V3/V4/V5, and only later V6 says:
- not in GFT universe
- not in profile scope
- wrong session
- wrong asset

That creates:
- wasted polling/analyzing
- noisy shortlist spam
- confusing QA results
- higher runtime load than necessary

## Desired future state
Before V1/V2 begin, QAenv should resolve:

1. which profiles are `is_active=true`
2. which universes/scopes those profiles reference
3. which asset classes are expected right now given current session
4. the final symbol set that is in scope for this cycle

That resolved set should become the analysis scope.

## Required behavior

### If only `gft_5k` and `gft_10k` are active
#### During crypto-only hours
Analyze only the GFT crypto symbols.

#### During forex hours
Analyze only:
- GFT crypto
- GFT forex

#### During equity-open hours
Analyze only:
- GFT crypto
- GFT forex
- GFT approved CFD/equity names

### If TTP is active
TTP should bring in:
- the dynamic TTP equity universe
- based on source + filters, not a hardcoded 12k ticker list in JSON

## Design requirement
Support:

```json
"runtime": {
  "resolved_universe_mode": "observe"
}
```

and

```json
"runtime": {
  "resolved_universe_mode": "enforce"
}
```

### Observe mode
- compute the resolved universe
- log/report it
- do not constrain pipeline yet

### Enforce mode
- constrain pipeline to resolved universe
- only those symbols should enter V1/V2/V3/V4/V5

## Modules expected to participate
- `vanguard/accounts/runtime_universe.py`
- orchestrator wiring before V1/V2
- config loader for active profiles/universes
- session helper logic

## Required debug visibility
Add one QA endpoint/report such as:

`GET /api/v1/runtime/resolved-universe`

It should show:
- active profiles
- current session mode
- expected asset classes
- resolved in-scope symbols
- optionally excluded symbols + reasons

## Acceptance criteria
1. GFT-only active + crypto open => only GFT crypto symbols analyzed
2. GFT-only active + forex open => only GFT crypto/forex symbols analyzed
3. GFT-only active + equity open => only GFT crypto/forex/equity symbols analyzed
4. TTP-only active => dynamic TTP equity scope resolves correctly
5. Observe vs enforce mode behaves distinctly and safely

## Non-goals in 2a
- no lifecycle auto-close
- no UI config editor yet
- no broad router rewrite yet
- no prod rollout

---

## Phase 2b — JSON Policy Templates for V6

## Goal
Make V6 a policy interpreter, not a hidden black box.

## Current problem
Even if profiles/universes are defined in JSON, V6 still risks behaving like:
- hardcoded Python logic
- GFT constants trapped in code
- decisions that are hard to inspect or override

## Desired future state
Profiles reference `policy_templates`, and V6 reads them to decide:
- whether longs are allowed
- whether shorts are allowed
- asset-specific sizing
- reject rules
- max positions
- duplicate blocking
- max holding duration
- whether auto-close should occur later in lifecycle

## Recommended config pattern

```json
{
  "profiles": [
    {
      "id": "gft_10k",
      "is_active": true,
      "instrument_scope": "gft_universe",
      "policy_id": "gft_standard_v1"
    }
  ],
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

## Side control requirement
This must support:
- long-only
- short-only
- both
- temporary per-profile overrides

Recommended profile-scoped override shape:

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

## Required V6 behavior
For each profile/candidate combination, V6 should explicitly determine:
- scope match / no match
- session match / no match
- long/short allowed / blocked
- duplicate symbol blocked or not
- position limit blocked or not
- approved size
- resized size and reason
- reject reason(s)

## Expected outputs
V6 should write machine-readable status such as:
- `APPROVED`
- `RESIZED`
- `BLOCKED_SCOPE`
- `BLOCKED_SESSION`
- `BLOCKED_SIDE_CONTROL`
- `BLOCKED_POSITION_LIMIT`
- `BLOCKED_HEAT`
- `BLOCKED_DUPLICATE_SYMBOL`

## Modules expected to participate
- `vanguard/accounts/policies.py`
- `vanguard/risk/policy_engine.py`
- current V6 integration point

## Acceptance criteria
1. `allow_long=false` prevents long approvals
2. `allow_short=false` prevents short approvals
3. temporary override works until expiry
4. asset-specific sizing changes approved size
5. reject reasons are explicit and policy-driven
6. no hidden GFT-specific constants remain authoritative over JSON policy

## Non-goals in 2b
- no full lifecycle daemon yet
- no UI config editor yet
- no prod rollout yet

---

# 4. Phase 3 — Broker/Position State + Auto-Close Lifecycle

## Goal
Make trade state persistent and operationally meaningful after entry.

## Current problem
V6 can approve an entry, but that is not enough.

The system also needs to know:
- did the trade actually open?
- is it still open?
- what is current P/L?
- did it hit TP/SL?
- has it exceeded max holding time?
- should it be auto-closed?

Without that, Desk/Portfolio truth remains partial, and automation is incomplete.

## Desired future state
Add a **position lifecycle service** independent from the shortlist loop.

It should:
1. poll execution bridges / broker state
2. update DB with open/closed positions
3. record fill/open/close metadata
4. maintain live status for UI and DB
5. auto-close positions that exceed policy max holding time

## Required responsibilities

### 3.1 Position state sync
Per profile, poll:
- open positions
- open orders if useful
- account balance/equity if available
- broker timestamps / IDs

### 3.2 Canonical DB state
Need canonical position fields such as:
- `profile_id`
- `route`
- `symbol`
- `asset_class`
- `broker_order_id`
- `broker_position_id`
- `open_ts`
- `fill_price`
- `close_ts`
- `close_price`
- `close_reason`
- `status`
- `unrealized_pnl`
- `realized_pnl`

### 3.3 Auto-close on max hold
If `max_holding_minutes=240` and a position remains open:
- submit close order
- record `AUTO_CLOSED_MAX_HOLD`
- update DB/UI state

### 3.4 Independence from shortlist scoring
This service must run even if:
- there are no new candidates
- a cycle has no approvals
- scoring is temporarily quiet

It should not depend on the existence of new shortlist rows.

## Execution bridge implications
This phase requires clean broker/account state access:
- MT5 / MetaApi for GFT
- SignalStack or broker feedback for TTP / other routes

## Acceptance criteria
1. open trades appear as open in DB/UI
2. closed trades appear closed with reason/timestamp
3. 4-hour max-hold trade auto-closes and records reason
4. lifecycle state remains correct even when no new candidates are produced

## Non-goals in Phase 3
- no full UI config editor yet
- no deep orchestrator redesign yet
- no prod rollout yet

---

# 5. Phase 4 — Modular Rule Engine and Account Modules

## Goal
Break the monolithic policy/risk logic into maintainable modules.

## Why this phase still matters
Even if JSON becomes the rule source, a giant `vanguard_risk_filters.py` is still hard to reason about.

You still need code modules because:
- JSON defines **what the rules are**
- code defines **how those rules are interpreted and enforced**

## Desired future state
Introduce a modular backend shape such as:

```text
vanguard/accounts/
  __init__.py
  policies.py
  runtime_universe.py
  profiles.py

vanguard/risk/
  __init__.py
  policy_engine.py
  sizing.py
  reject_rules.py
  side_controls.py
  holding_rules.py
  position_lifecycle.py
```

## Module responsibilities

### `accounts/`
Own:
- active profile resolution
- profile lookup
- policy assignment
- instrument scope resolution
- runtime universe resolution

### `risk/`
Own:
- side controls
- position sizing
- reject rules
- duplicate blocking
- exposure/heat checks
- holding-time rules
- lifecycle close rules

## What Phase 4 should achieve
- “add a new prop firm” becomes a contained config + module task
- V6 becomes orchestration over smaller rule engines
- debugging becomes easier
- testing becomes more isolated

## Important note
This does **not** mean moving rule truth out of JSON.
It means moving **JSON interpretation out of one giant file**.

## Acceptance criteria
1. V6/policy behavior is delegated to modular helpers
2. account/profile resolution is clearly separated from risk logic
3. new prop-firm additions do not require giant branched logic in one file
4. tests can target smaller rule modules

## Non-goals in Phase 4
- no UI config editor yet
- no prod rollout yet
- no broad frontend work

---

# 6. Phase 5 — React Config Admin UI

## Goal
Make the super-user config editable through the app safely.

## Current state
React app exists at:

`/Users/sjani008/SS/Meridian/ui/signalstack-app`

It already has:
- shell
- Candidates
- Desk
- Portfolio
- some settings/profile surfaces

But it does **not** yet have:
- safe runtime config admin for Vanguard
- full edit surfaces for profiles/universes/policy templates
- backend config read/write contract

## Desired future state
Add a config admin surface such as:
- `/settings/vanguard-config`
or
- a dedicated settings tab

This UI should eventually let a super-user manage:
- active profiles on/off
- profile -> universe assignment
- profile -> policy assignment
- universe symbol lists
- long/short toggles
- per-asset-class sizing fields
- max holding time
- auto-close toggle
- dynamic TTP universe snapshot/test controls

## Backend prerequisites
Before UI editing exists, backend should expose safe config endpoints.

Recommended:
- `GET /api/v1/config/runtime`
- `PUT /api/v1/config/runtime`

Preferably also:
- `GET /api/v1/config/profiles`
- `PUT /api/v1/config/profiles`
- `GET /api/v1/config/universes`
- `PUT /api/v1/config/universes`
- `GET /api/v1/config/policy-templates`
- `PUT /api/v1/config/policy-templates`

## Important UI rule
Do **not** make the first version a raw JSON text editor unless explicitly needed for super-user mode.

Prefer forms and structured editors for:
- profiles
- universes
- policy templates
- temporary overrides

## UX requirements
The config UI should be able to express:
- activate only GFT 5k + 10k
- short-only on crypto for next 2 hours
- max positions = 2 instead of 3
- remove/add one coin from GFT universe
- change ATR sizing defaults for equities
- test a dynamic TTP universe snapshot without enabling it in prod

## Acceptance criteria
1. runtime config can be read safely from backend
2. profiles/universes/policies can be edited without raw file hacking
3. config changes validate before write
4. config has version/updated_at metadata
5. the UI is super-user oriented, not a generic consumer UI

## Non-goals in Phase 5
- direct prod rollout by default
- broad dashboard redesign
- exposing unsafe raw internals to normal users

---

# 7. Recommended implementation order

## Strict order
1. **Phase 2a**
2. **Phase 2b**
3. **Phase 3**
4. **Phase 4**
5. **Phase 5**

## Why this order
- Phase 2a solves the biggest operational pain immediately: wrong symbols entering pipeline
- Phase 2b makes V6 behavior finally user-driven
- Phase 3 makes automation real and trackable
- Phase 4 makes the system maintainable
- Phase 5 exposes the controls only after the backend truth is stable

Do **not** start with Phase 5.
Do **not** start with full prod config replacement.
Do **not** skip 2a and expect V6 alone to solve runtime spam cleanly.

---

# 8. Recommended proof checkpoints after each phase

## After Phase 2a
Prove:
- only active-profile symbols are analyzed
- crypto-only / forex / equity session resolution behaves as expected
- resolved universe debug endpoint/report is truthful

## After Phase 2b
Prove:
- long-only / short-only / both works
- per-asset sizing works
- reject reasons are correct
- temporary overrides expire correctly

## After Phase 3
Prove:
- position state updates correctly
- open/closed status is maintained
- max-hold auto-close works
- Desk/Portfolio can trust lifecycle data

## After Phase 4
Prove:
- rule modules are actually called
- account vs risk separation is real
- adding a new policy is cleaner than before

## After Phase 5
Prove:
- UI edits survive restart
- validation prevents bad config writes
- super-user can manage active profiles/universes/policies safely

---

# 9. What xAI/Grok should avoid

- no prod changes during QAenv roadmap implementation
- no broad unscoped refactors
- no skipping proof checkpoints
- no UI admin page before backend config contract exists
- no giant monolithic rewrite in one pass
- no fake “implemented” claims without wiring/runtime proof

---

# 10. Final roadmap summary

## Phase 2
Make runtime scope and V6 policy behavior **active-profile driven and JSON-driven**

## Phase 3
Make trade/position lifecycle state **persistent, auto-managed, and UI-usable**

## Phase 4
Make the backend **modular enough to support more prop firms cleanly**

## Phase 5
Expose the config safely through the **React admin UI**

---

## Final recommendation to the coding agent
Do **one phase at a time**, and after each phase provide:
- exact files changed
- what got wired
- what remains scaffold-only
- proof run output
- acceptance-test result

That is the only safe way to get from current QAenv to the future-state runtime.
