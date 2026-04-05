# Stage 7 Go-Live Gap Analysis
**SignalStack / Vanguard final build**  
**Purpose:** assess whether Stage 7 is go-live ready as the runtime orchestration / execution layer, identify the real operational blockers, and define the honest live, paper, and forward-track stance.

---

## 1. Executive Summary

Stage 7 is **functionally real**, but **not fully production-ready**.

### What is real
- orchestrator exists
- session lifecycle exists
- execution modes exist
- EOD flatten exists
- Telegram alerts exist
- session logging exists
- paper / off / live mode handling exists
- tests exist

### Biggest problem
The main blockers are not “missing orchestration code.”
They are:
- V1 and V2 are not in the live cycle
- execution depends on pre-existing upstream truth rather than fresh in-cycle truth
- there are two execution log tables
- there is no true production process supervision
- account/profile seeding is still a hidden dependency

### Current go-live stance
- **forward-track/off mode:** ready
- **paper mode:** can go live with restrictions
- **live mode:** **no-go**

---

## 2. Current Stage 7 Truth

### What Stage 7 is supposed to do
Stage 7 should be the production runtime loop that:
- starts the session
- runs the cycle repeatedly
- consumes fresh upstream stage outputs
- executes approved trades when allowed
- manages session lifecycle and EOD behavior
- provides auditability and operator trust

### What Stage 7 actually does
It runs a session lifecycle and repeatedly executes:
- V3
- V5
- V6

It does **not** currently execute:
- V1
- V2
- V4B

This means the runtime is real, but it is not self-contained as a full fresh-pipeline orchestrator.

### Current outputs
- `execution_log`
- `vanguard_session_log`
- reads approved rows from `vanguard_tradeable_portfolio`

---

## 3. Strengths

## 3.1 Session lifecycle is complete
Startup, loop, wait, wind-down, and flatten behavior all exist.

## 3.2 Execution modes are real
The system has:
- `off`
- `paper`
- `live`

That is strong architectural maturity.

## 3.3 Forward-tracked mode is valuable
This gives you a safe operational mode that can run continuously without real submission.

## 3.4 Session logging exists
The session log is a real strength for operator auditability.

## 3.5 Telegram and retry behavior exist
These are strong operational features, especially for a solo system.

## 3.6 EOD flatten exists
This is important and already useful for profile-sensitive behavior.

---

## 4. Main Gaps

## Gap 1 — V1 and V2 are absent from the live cycle
This is the biggest runtime truth problem.

Stage 7 calls:
- V3
- V5
- V6

but not:
- V1
- V2

### Why it matters
The machine can therefore execute on:
- stale bars
- stale health state
- stale features

unless those layers are being kept fresh independently.

That makes Stage 7 **not self-sufficient** as a production orchestrator.

---

## Gap 2 — account/profile seeding is a hidden dependency
If `account_profiles` is empty or inactive:
- Stage 7 runs
- but nothing executes

### Why it matters
That is a valid runtime dependency, but it must not remain hidden operator truth.

---

## Gap 3 — duplicate execution log truth
There are two log concepts:
- `execution_log`
- `vanguard_execution_log`

Even if one is effectively unused, this creates reporting and operator confusion.

### Why it matters
A live machine needs one execution truth.

---

## Gap 4 — no production supervision
There is no strong process supervision / service management / auto-restart layer.

### Why it matters
For live operation, this is a real reliability blocker.

Paper mode can tolerate this more than live mode.
Live mode should not.

---

## Gap 5 — some operational safeguards are still absent
Examples:
- no cycle lag monitoring
- no startup data connection verification
- no warm-start handling
- no stronger regime-aware runtime behavior from V5 return data

These are not all equal in severity, but together they weaken live-mode trust.

---

## 5. Auditability / Failure-Handling Gaps

## 5.1 Runtime crash recovery is weak
If the process dies mid-session, there is no built-in true recovery mechanism.

## 5.2 Consecutive-failure abort is not enough
It exists, which is good.
But without strong surfacing and restart strategy, it is not full production resilience.

## 5.3 Regime output is underused
If V5 returns regime context but Stage 7 ignores it operationally, then the chain is not using all the protective context it has.

## 5.4 Cycle timing truth is weak
There is no strong wall-clock cycle lag measurement and alerting.

## 5.5 Profile/account availability should be surfaced harder
Empty account set should be loud and explicit, not quiet.

---

## 6. Go-Live Blockers

These block Stage 7 from being called fully live-ready.

### Blocker 1
V1 and V2 are not in the orchestrated cycle.

### Blocker 2
There is no single execution-log truth across all relevant code paths.

### Blocker 3
No real production supervision / auto-restart / service management exists.

### Blocker 4
Active account/profile dependency can silently prevent execution.

### Blocker 5
Live mode depends on upstream truth being maintained outside the orchestrator.

---

## 7. Non-Blocking Gaps

These matter, but do not block restricted operation:

- MT5 / FTMO live execution path absent
- warm-start bars not implemented
- daily pause behavior not fully integrated
- cycle lag timing instrumentation absent
- startup connection verification absent
- some regime-awareness not propagated deeply enough

These weaken maturity, but are not equal to the core blockers above.

---

## 8. UI / Operator Truth Gaps

Operators should be able to know:
- whether the orchestrator is currently active
- current mode (`off`, `paper`, `live`)
- whether upstream V1/V2 freshness is guaranteed or external
- latest session cycle time
- latest successful execution time
- whether account profiles are active
- which execution log is authoritative

Operators should **not** be led to believe:
- Stage 7 is self-sufficient if it still depends on externally refreshed upstream stages
- “live mode available” means “live mode ready”
- the dashboard has one perfect execution truth if two log tables still exist

---

## 9. QA Go-Live Checks

Before go-live, verify:

### Runtime truth
- account profiles are seeded and active
- V3, V5, V6 all run cleanly in a test cycle
- approved rows can be read from `vanguard_tradeable_portfolio`
- off mode writes expected audit trail
- paper mode can run safely end-to-end

### Orchestration truth
- confirm what upstream freshness dependency exists for V1/V2
- prove bars are fresh before Stage 7 cycles
- prove stale upstream conditions are detectable

### Logging truth
- designate and verify one canonical execution log
- ensure dashboards/reporting consume the right log

### Operational resilience
- verify session start/stop behavior
- verify EOD flatten behavior
- verify alerting
- verify crash/restart operating procedure for paper mode

---

## 10. Mode-by-Mode Go-Live Recommendation

## Forward-track / off mode
**Ready now**

Reason:
- safest mode
- strong audit value
- no real execution dependency
- useful for continuous validation

## Paper mode
**Go with restrictions**

Required restrictions:
- active profiles seeded
- upstream bars/features kept fresh
- one known execution truth
- at least basic process supervision or controlled restart procedure

## Live mode
**No-go**

Reason:
- Stage 7 is not yet a fully self-sufficient production orchestrator
- upstream stage freshness is not guaranteed in-cycle
- execution-log truth is split
- process supervision is not production-grade

---

## 11. Final Judgment

Stage 7 is not fake or immature.  
It is **operationally meaningful** and already useful.

But it is not yet honest to describe as:
- fully self-contained live production orchestrator
- fully hardened live trading runtime

### Final stance
- **Forward-track/off:** go
- **Paper:** go with restrictions
- **Live:** no-go

### Best summary line
> Stage 7 is orchestration-real but production-ops-incomplete. It is ready for forward-tracked use, conditionally ready for paper mode, and not yet ready for live mode.
