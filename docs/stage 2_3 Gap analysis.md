Use this as the combined markdown doc.

Stage 2–3 Go-Live Gap Analysis

SignalStack / Vanguard final build
Purpose: assess whether Stage 2 and Stage 3 are go-live ready individually and as a chained production path, identify real strengths and gaps, and define the honest operator/go-live stance.

⸻

1. Executive Summary

Stage 2 and Stage 3 should be judged as one operational chain, not just as two separate code modules.

Current truth
	•	Stage 2 is split and partial
	•	Stage 3 is split and partial
	•	The Stage 2 → Stage 3 chain is not production-trustworthy
	•	Real go-live stance for the combined chain is no-go

Why this is the right judgment

Stage 2 is supposed to be the live tradability/health layer feeding Stage 3.
But in current reality:
	•	Stage 2 standalone code exists and writes real rows
	•	Stage 2 is not the true live orchestrated gate it claims to be
	•	Stage 2 runtime data is stale
	•	Stage 2 session logic is equity-centric, not true multi-asset
	•	Stage 3 is real and useful, but only partial
	•	Stage 3 output contract does not match spec
	•	Stage 3 runtime coverage is tiny relative to intended survivor coverage
	•	Stage 3 is not consistently consuming fresh Stage 2 truth each cycle

Final system-level conclusion

The biggest issue is not just that V2 and V3 each have gaps.

The bigger issue is:

The V2 → V3 production chain is not trustworthy because Stage 2 is not the live authoritative tradability gate, and Stage 3 does not reliably consume fresh, complete Stage 2 output on each cycle.

That makes the combined chain no-go for production-truth claims.

⸻

2. Combined Current Truth

What Stage 2 currently is
	•	a real standalone prefilter / health monitor
	•	writes vanguard_health
	•	can run manually
	•	has useful logic
	•	but is not the live authoritative runtime layer described by spec

What Stage 3 currently is
	•	a real factor engine
	•	writes vanguard_features
	•	can run manually / from orchestrator
	•	has real factor modules
	•	but output contract and runtime coverage do not match production claims

What the chain currently is
	•	not a clean live V2 → V3 machine
	•	more like:
	•	V2 standalone health snapshots
	•	V3 partial factor computation
	•	orchestrator/runtime path skipping or weakening the intended dependency

⸻

3. Stage 2 Findings

3.1 What Stage 2 is supposed to be

According to the intended design, Stage 2 should be the live tradability / health gate that:
	•	monitors instrument readiness
	•	enforces session-aware health logic
	•	filters the live symbol universe
	•	runs on a recurring cadence
	•	feeds Stage 3 with trustworthy survivors

3.2 What Stage 2 actually is

Current code truth says Stage 2 is:
	•	a runnable standalone health monitor
	•	writing to vanguard_health
	•	using vanguard_bars_5m
	•	using equity-centric market clock logic
	•	not wired into the main orchestrated runtime path as intended

3.3 What is already good in Stage 2
	•	real standalone code exists
	•	output table exists
	•	health status model is simple and usable
	•	DB/runtime path is understandable
	•	threshold loading has some reuse potential
	•	good foundation for a health layer exists

3.4 Main Stage 2 gaps

Gap 1 — not in the live runtime chain

Stage 2 is not truly part of the actual production loop.

That alone is a major go-live issue.

Gap 2 — wrong session logic for multi-asset claims

Stage 2 uses equity-hours logic.
That is incompatible with:
	•	forex
	•	crypto
	•	futures
	•	multi-session truth

Gap 3 — stale runtime truth

Latest known Stage 2 cycle data is stale.
That means operators cannot trust it as a live gate.

Gap 4 — contract mismatch

Spec expects richer output and behavior than the actual vanguard_health table and runtime provide.

Gap 5 — wrong effective scope

Spec implies broader multi-asset health behavior.
Current behavior is much closer to:
	•	equity-centric health snapshots over symbols with bars

⸻

4. Stage 3 Findings

4.1 What Stage 3 is supposed to be

According to intended design, Stage 3 should be the factor layer that:
	•	consumes fresh eligible survivors
	•	computes factor truth across the intended universe
	•	produces a reliable downstream contract for selection/routing/model logic

4.2 What Stage 3 actually is

Current code truth says Stage 3 is:
	•	a real factor engine
	•	with real factor modules
	•	writing vanguard_features
	•	usable in dev/sandbox terms
	•	but not matching spec contract or go-live coverage expectations

4.3 What is already good in Stage 3
	•	factor engine exists
	•	eight factor modules are active
	•	runtime artifacts exist
	•	can feed downstream sandbox logic
	•	useful development asset already built

4.4 Main Stage 3 gaps

Gap 1 — output contract mismatch

Spec expects a more explicit factor matrix contract.
Actual storage is JSON blobs in vanguard_features.

That may be workable for dev, but not clean go-live contract truth.

Gap 2 — coverage is too small

Latest runtime coverage is tiny relative to intended active symbol set.

That is not just a cosmetic issue. It means the factor layer is not proving production breadth.

Gap 3 — stale dependency chain

Stage 3 is effectively downstream of stale or misaligned Stage 2 truth.

Gap 4 — multi-asset behavior is not proven

Large parts of factor logic remain:
	•	SPY-centric
	•	equity-session-centric
	•	placeholder-based for broader cross-asset truth

Gap 5 — placeholder feature truth remains live

Some features are placeholders or neutral defaults.
That is acceptable in dev but dangerous if operators or later stages treat them as mature signal truth.

⸻

5. V2 → V3 Chain Failure Analysis

This is the most important section.

5.1 Intended chain

The intended machine is:
	•	fresh market-data truth
	•	Stage 2 health / tradability gate
	•	fresh eligible active universe
	•	Stage 3 factor computation on that fresh active universe
	•	downstream selection/model/risk logic

5.2 Actual chain

The actual chain is more like:
	•	Stage 2 may or may not be current
	•	Stage 2 is not guaranteed to be in live orchestrated runtime
	•	Stage 3 may run against stale or partial active symbols
	•	symbol coverage can collapse dramatically
	•	single-cycle harness may reuse existing artifacts instead of regenerating fresh truth

5.3 Why this matters

Even if both stages are “real,” the chain is not production-trustworthy because:
	•	the handoff is weak
	•	freshness is weak
	•	coverage is weak
	•	runtime orchestration is weak

This means the machine cannot honestly claim:
	•	fresh tradability truth
	•	fresh factor truth
	•	trustworthy symbol coverage
	•	production-grade Stage 2 → Stage 3 gating

5.4 Core chain-level blocker

Stage 3 is not reliably driven by fresh, authoritative Stage 2 output on each cycle.

That is the main go-live blocker for the combined chain.

⸻

6. Strengths Across the Combined Chain

Even though the answer is no-go, there is real substance here.

Combined strengths
	•	Stage 2 exists and writes usable health rows
	•	Stage 3 exists and writes real factor artifacts
	•	both are understandable and testable
	•	both have enough reality to support a disciplined fix path
	•	the system is not missing these stages; it is failing on orchestration, contract truth, and runtime coverage

This is important:
you are not starting from zero.
You are fixing a real but not yet trustworthy chain.

⸻

7. Go-Live Blockers

These block the chain from being called production-ready.

Blocker 1

Stage 2 is not integrated into the live orchestrated runtime as the authoritative health/tradability layer.

Blocker 2

Stage 2 session logic is equity-based, not true multi-asset/session-aware logic.

Blocker 3

Stage 2 runtime truth is stale.

Blocker 4

Stage 2 output contract does not match the intended production contract.

Blocker 5

Stage 3 does not reliably consume fresh Stage 2 output each cycle.

Blocker 6

Stage 3 symbol coverage is far too small relative to expected active universe size.

Blocker 7

Stage 3 stored contract is not the intended downstream contract.

Blocker 8

Multi-asset factor/session behavior is not production-proven.

Blocker 9

Single-cycle/runtime harness behavior does not reflect the intended full V2 → V3 live chain.

⸻

8. Non-Blocking Gaps

These matter, but they are not the first go-live blockers.

For Stage 2
	•	threshold/config naming drift
	•	narrower health taxonomy than spec
	•	richer diagnostics not stored

For Stage 3
	•	JSON storage could survive as dev artifact
	•	feature naming drift could be tolerated temporarily if documented
	•	some quality/meta fields are useful even if undocumented

Combined
	•	operator UI could remain minimal for first fix cycle
	•	richer per-stage dashboards can come later

⸻

9. UI / Operator Truth Gaps

Even if these stages are mostly backend, operator truth still matters.

Operators should be able to see
	•	latest Stage 2 cycle time
	•	latest Stage 3 cycle time
	•	symbol count entering Stage 2
	•	ACTIVE count leaving Stage 2
	•	symbol count entering Stage 3
	•	symbol count successfully processed by Stage 3
	•	whether Stage 2 and Stage 3 are fresh or stale
	•	whether runtime used fresh or reused artifacts

Operators should not be led to believe
	•	Stage 2 is live every 5 minutes if it is not
	•	Stage 2 is true multi-asset session-aware if it is not
	•	Stage 3 represents full active-universe factor truth if it only covers a tiny subset
	•	V2 → V3 is a trustworthy production chain if the runtime still reuses stale data

UI truth rule

Until fixed, operator-facing language should present:
	•	Stage 2 as partial/staged health truth
	•	Stage 3 as partial/sandbox factor truth
	•	the chain as not yet production-trustworthy

⸻

10. QA Go-Live Checks

These are the checks needed before go-live can be reconsidered.

Stage 2 checks
	•	verify Stage 2 is invoked from the real runtime loop
	•	verify cycle updates on intended cadence
	•	verify asset-class/session logic behaves correctly
	•	verify 1m/5m freshness use if required by intended design
	•	verify output count matches intended universe
	•	verify stale-state detection exists and works

Stage 3 checks
	•	verify Stage 3 runs immediately after fresh Stage 2 output
	•	verify Stage 3 symbol coverage matches intended active set or a clearly documented subset
	•	verify stored output contract matches intended downstream consumer needs
	•	verify placeholder features are not silently treated as production truth
	•	verify session/benchmark behavior for non-equity assets

Chain-level checks
	•	verify one fresh V2 cycle produces one fresh V3 cycle
	•	verify coverage counts are consistent across the handoff
	•	verify no stale artifact reuse is being mistaken for fresh computation
	•	verify runtime and single-cycle harness represent the same truth model

⸻

11. Recommended Go-Live Stance

Stage 2 individually

No-go

Reason:
	•	not live-integrated enough
	•	stale
	•	session logic wrong for broader claims
	•	contract mismatch

Stage 3 individually

No-go

Reason:
	•	partial coverage
	•	wrong stored contract
	•	stale dependency risk
	•	unproven multi-asset/session truth

Combined Stage 2 → Stage 3 chain

No-go

Reason:
	•	broken production handoff
	•	weak freshness truth
	•	weak coverage truth
	•	weak orchestration truth

⸻

12. Smallest Honest Operational Position

If you absolutely had to describe current reality, say this:

Stage 2 and Stage 3 are real development-stage components with useful standalone behavior, but the production V2 → V3 chain is not yet trustworthy for go-live.
Stage 2 is not yet the live authoritative tradability gate.
Stage 3 is not yet the full-production factor truth layer.
The combined chain should be treated as partial/staged until runtime orchestration, freshness, coverage, and contract truth are corrected.

That is the honest machine-level statement.

⸻

13. What Must Be True Before Reconsidering Go-Live

Before you revisit go-live, these conditions should be true:

Required condition 1

Stage 2 is part of the real live orchestrated path.

Required condition 2

Stage 2 freshness is live and auditable.

Required condition 3

Stage 2 session logic is appropriate for the supported asset classes.

Required condition 4

Stage 3 runs directly from fresh Stage 2 output every cycle.

Required condition 5

Stage 3 symbol coverage is real and close to intended survivor volume.

Required condition 6

Stage 3 output contract matches the real downstream contract.

Required condition 7

Operators can see freshness, coverage, and chain integrity.

⸻

14. Final Judgment

Machine-level judgment

The V2 → V3 part of the trading machine is not yet production-ready.

Why

Not because the code is fake.
Not because the ideas are wrong.
But because:
	•	the runtime chain is incomplete
	•	the freshness truth is weak
	•	the session model is weak
	•	the output contracts drift from spec
	•	the operator cannot trust the chain as production truth

Final answer

Stage 2–3 = no-go for go-live.
Treat them as real but partial/staged system components until the chain itself is corrected.

When you’re ready, I’ll do the same single-document go-live gap analysis for Stage 5 next.