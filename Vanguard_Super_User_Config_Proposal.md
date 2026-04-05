# Vanguard Super-User Config Proposal
**Status:** proposal only, not approved for implementation  
**Purpose:** consolidate scattered runtime config into one canonical file without breaking the currently working production loop.

---

## Executive recommendation

Do **not** flip production directly to a new super-user config in one step.

The right move is:

1. **Design the final config shape now**
2. **Shadow-load it in dev/staging first**
3. **Compare it against current runtime behavior**
4. **Flip one subsystem at a time**
5. **Only then make it authoritative in prod**

This gives you the simplicity of one config file without gambling the current working loop.

---

## Why this proposal exists

The current Vanguard runtime is getting harder to debug because live knobs are scattered across:
- multiple JSON files
- hardcoded constants
- DB-backed account profiles
- per-module config reads

That makes it difficult to answer simple questions like:
- which universe is actually active?
- which risk rules are currently governing V6?
- which execution route is live?
- which session/source rules are controlling the loop?

A single super-user config solves that **if** it is introduced safely.

---

## Core decision

Use one canonical file:

`config/vanguard_runtime.json`

That file should become the source of truth for **static runtime config**, while the DB stays the source of truth for **mutable state/history**.

### JSON should own
- runtime cadence and mode
- market data session/source rules
- universes
- risk defaults
- profile definitions
- execution defaults and routing
- Telegram defaults

### DB should own
- bars
- features/predictions
- shortlist outputs
- positions/orders/execution logs
- mutable runtime state/history

---

## Recommended config shape

Use **6 readable top-level blocks**, not too many, not too few:

```json
{
  "runtime": {},
  "market_data": {},
  "universes": {},
  "risk_defaults": {},
  "profiles": [],
  "execution": {},
  "telegram": {}
}
```

This is easier to reason about than many scattered files and clearer than a half-merged approach.

---

## Proposed meaning of each block

## 1. runtime
How the orchestrator runs.

Examples:
- execution mode
- cycle interval
- single-cycle defaults
- shortlist top-N
- force-assets override
- environment (`dev`, `staging`, `prod-shadow`, `prod`)

## 2. market_data
When markets are expected to be active and where bars come from.

Examples:
- session windows for equity / forex / crypto
- source routing by asset class
- Twelve Data symbol groups
- dynamic equity universe source (`ibkr`, `alpaca`, etc.)

## 3. universes
What symbols are eligible for each scope.

### Important distinction
#### TTP equity
Should remain **dynamic**
- JSON stores source + filters
- JSON should **not** list all 12k tickers

#### GFT
Should be **static explicit**
- JSON lists every allowed symbol
- if it is not listed, it is not eligible

#### FTMO / others
Can also be static or grouped here

## 4. risk_defaults
Shared/default risk knobs that are not profile-stateful.

Examples:
- GFT shared defaults
- stop/TP minimums
- portfolio heat defaults
- default risk-per-trade
- hardcoded V6 constants moved out of code

## 5. profiles
All active account profiles and their static rules.

Examples:
- `ttp_20k_swing_demo`
- `gft_5k`
- `gft_10k`
- `ftmo_100k`

Each profile should define:
- prop firm
- account type/environment
- instrument scope
- holding style
- execution bridge/route
- account size
- max positions
- daily loss limit
- max drawdown
- end-of-day rules
- ATR/risk knobs if truly profile-specific

## 6. execution
Execution defaults and route config.

Examples:
- execution mode
- SignalStack webhook env refs
- MT5 / MetaApi symbol map
- retry/timeout defaults

## 7. telegram
Telegram enablement and formatting defaults.

Examples:
- enabled
- bot token env ref
- chat id env ref
- message size limit

---

## Direct answer on TTP vs GFT universe design

## TTP Equity
Yes — keep it **dynamic**.

Do **not** put 12k tickers in JSON.

Store only:
- source
- filters
- eligibility rules

Example:
- min price
- min avg volume
- excluded exchanges
- suffix filters
- leveraged ETF exclusion
- source preference (`ibkr`, `alpaca`)

## GFT
Yes — make it **explicit**.

If GFT should only trade:
- its forex list
- its crypto list
- its metals list
- its indices/CFDs
- its 15 approved CFD stocks

then every eligible symbol should live in JSON.

That is the correct place for it.

### Will explicit GFT symbols break runtime?
Not by itself, as long as:
- symbol format matches what V1/V3/V5 use
- symbol mapping for execution is explicit
- V6 checks `instrument_scope == gft_universe`
- symbol normalization is consistent

So yes, this is possible without breaking prod — but only if introduced carefully.

---

## Biggest runtime risk

The biggest risk is **not** the JSON file itself.

The biggest risk is making JSON authoritative too early for things currently trusted from DB / hardcoded paths, especially:
- `account_profiles`
- V6 risk filters
- universe selection
- execution routing defaults

That is why a direct prod switch is not recommended.

---

## Safe rollout plan

## Phase 0 — design only
Do now:
- finalize config shape
- decide ownership boundaries
- do not change production behavior yet

## Phase 1 — shadow load
Add loader for `vanguard_runtime.json`
- load at startup
- validate structure
- resolve `ENV:` values
- log resolved snapshot
- **do not change live behavior yet**

Goal:
prove loader works safely.

## Phase 2 — mirror compare in dev/staging
Use:
- dev flag
- copied DB
- manual single-cycle
- short manual loop

Compare:
- profiles
- universes
- risk knobs
- V6 behavior
- Telegram behavior

Goal:
prove parity against current system.

## Phase 3 — flip one subsystem at a time
Recommended order:
1. GFT universe
2. GFT/shared risk defaults
3. execution/telegram defaults
4. profile sync into DB mirror
5. finally broader runtime authority

Do **not** flip everything in one pass.

## Phase 4 — production authority
Only after parity:
- make `vanguard_runtime.json` the canonical manual edit point
- stop editing scattered JSON files directly
- keep old files only as migration references or fallback during transition

---

## Recommended production safety rules

- keep a `runtime.environment` flag (`dev`, `staging`, `prod-shadow`, `prod`)
- support `prod-shadow` mode before real prod authority
- on startup, log the resolved config snapshot
- make config immutable for the process lifetime
- do not hot-reload during the same live process
- every change should apply only on orchestrator restart

That makes runtime behavior predictable.

---

## What should *not* move in this pass

Do **not** try to migrate everything at once.

Keep out of scope for this first pass:
- legacy strategy JSONs if that path is effectively dead
- deep V4/V5 logic redesign
- DB state/history redesign
- UI editing of runtime config

This pass is about making config understandable and safe.

---

## Recommendation on account profiles

Short term:
- keep `account_profiles` in DB as a **startup-synced mirror**
- JSON is the authority
- DB remains compatibility/storage for current consumers

That is safer than forcing V6/orchestrator to stop using DB immediately.

Long term:
- UI/admin edits should write the JSON source of truth, then resync DB
- not mutate DB-only config behind JSON’s back

---

## Final recommendation

Yes, build the super-user config.

But do it as:

> **design now, shadow first, compare in dev, flip gradually**

Not:

> **replace all live config in prod immediately**

That is the difference between simplification and runtime damage.

---

## Sleep-on-it verdict

You are right to pause before implementing.

This proposal is strong enough to justify the direction, but risky enough that it should be:
- reviewed once more
- implemented behind a shadow/dev flag
- rolled out gradually

If you sleep on it and still like it tomorrow, the next step should be a **shadow-mode implementation plan**, not a direct production rewrite.
