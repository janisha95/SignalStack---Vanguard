# SignalStack Next-Step Working Plan
**Purpose:** handoff/working plan for Grok during Claude downtime over the next 2–3 days.  
**Priority order:** UI redesign first, then V6/profile setup, then GFT unlock.

---

## 1. Current reality snapshot

### The machine now
SignalStack is no longer a hypothetical merge project. It is now a working multi-lane trading machine with real shortlist generation, real profile approvals, and a partially working operator console.

### Real active lanes
- **S1**: daily scorer lane
- **Meridian**: daily shortlist/model-ranked lane
- **Vanguard**: real intraday 5-minute lane

### Real product truths
- Vanguard is generating fresh shortlist updates every 5 minutes
- Equity + forex are already flowing in Vanguard
- V6 is already approving trades by profile
- The UI still needs a serious redesign because the system logic changed materially
- After UI, the next operational target is **GFT universe unlock + V6 filtering + MT5 execution**

---

## 2. Main problem statement

The UI and operating model are lagging the backend.

### Why the UI must be redesigned now
Old UI assumptions are no longer valid:
- Meridian Stage 4 and 5 changed
- Vanguard V4/V5 changed
- score meaning is no longer consistent across lanes
- Vanguard is no longer a placeholder
- execution profiles and routing are now first-class
- intraday multi-asset support is now real enough that asset-class-aware UI is mandatory

### Why V6 must be set next
Once the UI is structurally correct, the execution layer must be aligned to real accounts:
- **TTP 20k Swing**
- **GFT 5k**
- **GFT 10k**

---

## 3. Working order for the next 2–3 days

## Phase A — UI redesign first
Goal:
Build a clean, truthful operator console around the **actual current machine**, not old shell assumptions.

### Outcome needed
A final UI architecture that supports:
- daily and intraday lanes
- multi-asset candidates
- profile-aware execution
- lane-specific model provenance
- proper shortlist and approval workflow

### This must happen before GFT work
Because:
- current UI does not clearly express lane truth
- current UI does not properly express score/provenance differences
- current UI is not yet shaped around the real operator workflow

---

## Phase B — V6 profile and routing alignment
Goal:
Make Desk / execution fully align to:
- TTP 20k Swing
- GFT 5k
- GFT 10k

### Outcome needed
Clear account/profile logic for:
- allowed instruments
- holding style
- route destination
- approval/block/resize behavior
- execution destination clarity in Desk

---

## Phase C — GFT unlock
Goal:
Enable GFT as a real tradable universe and route.

### Outcome needed
- `gft_universe`
- V6 filter enforcement
- GFT profiles
- MT5 executor path
- use existing equity/forex models where appropriate

---

## 4. UI redesign target

## Product structure
The product should now be designed around **workflow**, not just pages.

### Primary workflow
1. See what is happening now
2. Inspect candidates across lanes
3. Preview under the selected profile
4. Execute to the correct destination
5. Monitor positions and machine health

## Primary navigation
Keep only:
- **Dashboard**
- **Candidates**
- **Desk**
- **Portfolio**

## Secondary / admin
Move under More / Admin / Settings:
- Profiles
- Control Center
- Reports
- Model / diagnostics
- Settings

### Rule
Profiles and Control matter, but they should not crowd the main operator path.

---

## 5. Final page roles

## Dashboard
Purpose:
Show the current machine state, not legacy placeholders.

Should show:
- lane snapshots
- latest shortlist highlights
- approval counts by profile
- freshness / readiness
- important warnings

## Candidates
Purpose:
Research and triage surface.

Should support:
- lane filter
- asset class filter
- side filter
- search
- saved views
- profile context

Must be:
- lane-aware
- asset-class-aware
- provenance-aware
- not fake-unified in score meaning

## Desk
Purpose:
Execution truth page.

Must show:
- active profile
- environment
- route destination
- requested vs approved size
- warnings / blocks / resize reasons
- rules fired
- route/account target

## Portfolio
Purpose:
Open positions + consequences.

Must show:
- positions by profile
- positions by asset class
- lane/source context
- P&L and risk utilization

## Profiles
Setup/admin surface, not primary nav.

Must support:
- TTP 20k Swing
- GFT 5k
- GFT 10k
- environment
- instrument scope
- holding style
- route destination
- live/staged enforcement flags if available

## Control Center
Keep as admin/ops only.

Must show:
- lane freshness
- stage freshness
- data/provider health
- model readiness
- execution/runtime readiness

---

## 6. UI rules that must be honored

### A. No fake unified score
Do not pretend S1, Meridian, and Vanguard scores are directly comparable.

### B. Lane-specific meaning
Each lane can expose its own score/provenance meaning.

### C. Asset class matters
The UI must handle:
- equities
- ETFs
- forex
- indices
- metals
- energy
- agriculture
- crypto
- futures

### D. Profile context matters
Candidate relevance and Desk execution must respect the selected profile.

### E. Vanguard is first-class now
Do not present Vanguard like a placeholder/staged-only lane.

---

## 7. V6 target setup

## Accounts to support now
### TTP
- `ttp_20k_swing_demo`

### GFT
- `gft_5k_demo`
- `gft_10k_demo`

## Profile behavior
### TTP 20k Swing
- instrument scope: equities / ETFs
- holding style: swing
- route: SignalStack webhook
- environment: demo for now

### GFT 5k / 10k
- instrument scope: `gft_universe`
- holding style: intraday
- execution path: MT5
- environment: demo/challenge depending current setup

## Desk requirement
No execute without selected profile.

Desk must show:
- profile
- route destination
- instrument scope
- approval summary
- block/resize reasons

---

## 8. GFT universe unlock plan

## Goal
Enable GFT accounts to trade only a restricted set of allowed instruments.

## Initial GFT universe
Use:
- the 15 approved stocks
- forex pairs
- index proxies / CFDs
- later metals / energy when confirmed in the universe spec

## Model usage
Do not build new model families first.

Use existing models:
- **stocks / stock CFDs** → existing equity model
- **index CFDs** → ETF/index proxy mapped to existing equity model
- **forex pairs** → existing forex model

## V6 filtering rule
If profile instrument scope = `gft_universe`:
- only those instruments are eligible
- everything else is blocked

## Execution path
Primary preferred route:
- **MT5 on Mac Silicon via Docker**
- not MetaApi-first

MetaApi is only fallback if the free/local MT5 route fails.

---

## 9. Known truths to preserve

### Meridian
- Stage 4 and Stage 5 changed materially
- LONG and SHORT should not be treated as symmetric without proving that
- UI must surface model/ranking provenance

### Vanguard
- intraday lane is real
- 5-minute shortlist is real
- equity + forex are already live
- V4/V5/V6 semantics changed
- no old “placeholder lane” language

### S1
- still a real lane
- adapter shaping/provenance matters
- should remain visible but not dominate the product

---

## 10. To-do list for today

## P0 — must do today
1. Lock the final UI architecture
2. Decide the exact main nav and page roles
3. Finalize Candidates page design principles
4. Finalize Desk page design principles
5. Finalize Dashboard role
6. Finalize V6 target profiles:
   - `ttp_20k_swing_demo`
   - `gft_5k_demo`
   - `gft_10k_demo`
7. Confirm profile instrument scopes
8. Confirm execution route expectations:
   - TTP via SignalStack
   - GFT via MT5
9. Confirm GFT universe approach:
   - restricted universe only
   - no new model family first

## P1 — should do next
10. Define the exact GFT universe contents
11. Define how V6 shows approved vs blocked vs resized
12. Decide whether Dashboard should show top picks, approvals, and machine health in one page or multiple blocks
13. Decide how model/source/provenance will surface per lane

## P2 — after UI
14. Implement `gft_universe` filtering in V6
15. Create GFT profiles
16. Build/verify MT5 execution path
17. Add route/account visibility into Desk
18. Add operator truth guide

---

## 11. What Grok should help with next

### First
Help redesign the UI around the real machine.

### Then
Help shape V6 profile/account behavior for:
- TTP 20k Swing
- GFT 5k
- GFT 10k

### Then
Help define the GFT unlock path:
- `gft_universe`
- MT5 execution
- V6 filtering
- profile-driven instrument eligibility

---

## 12. Short working brief for Grok

You are taking over a SignalStack system that is no longer in the old “shell unification” phase.

The current truths are:
- S1, Meridian, and Vanguard are all real lanes
- Vanguard is a real intraday 5-minute lane
- Meridian selection logic changed materially
- scores are lane-specific and not universally comparable
- profiles/execution/routing matter more than before
- the UI must be redesigned first
- after UI, the next move is V6 setup for TTP 20k Swing and GFT 5k/10k, then GFT universe unlock and MT5 execution

Design and implementation should be based on the **actual current machine**, not the old shell-phase docs.

---

## 13. Final note
For the next 2–3 days, the priority is:

**UI first → V6 account/profile logic second → GFT unlock third**

Do not let the work drift back into abstract architecture or outdated assumptions.
