Use this as one consolidated markdown doc.

Universe Builder + Stage 1 Go-Live Gap Analysis

SignalStack / Vanguard final build
Purpose: identify the real current state of Universe Builder and Stage 1, define what is actually go-live ready, what is partial, what is staged, and what must be fixed before Stage 1 can be called a reliable production machine.

⸻

1. Executive Summary

Stage 1 is in a split transition state.

There are now two real Stage 1 realities:

Reality A — older live path
	•	vanguard_cache.py
	•	Alpaca REST
	•	equity-first
	•	5m bars + derived 1h
	•	real scheduled runner
	•	currently the clearest single live Stage 1 runtime

Reality B — newer normalized path
	•	normalized 1m storage in vanguard_bars_1m
	•	Alpaca websocket for equities
	•	Twelve Data for forex / broad non-futures instruments
	•	backfills running
	•	real data is being written
	•	but not yet clearly unified under one primary runtime orchestrator

Current go-live truth
	•	Equities: live
	•	Forex / broad non-futures: partial
	•	CFD-like non-equity coverage: partial
	•	Commodities / metals / energy / agriculture / indices: partial where Twelve Data is configured and writing, but not yet fully orchestrated as one live Stage 1 machine
	•	Futures: staged, not go-live ready
	•	Universe Builder: real, but not yet the single canonical input bus for all Stage 1 sources

Bottom-line judgment

Stage 1 is good enough to describe as a real live ingestion layer in transition, but not yet honest to market as one fully unified multi-asset live machine.

The correct go-live stance is:
	•	Go-live for equities
	•	Restricted / partial go-live for Twelve Data-backed non-futures
	•	No-go for futures
	•	No-go for claiming one fully unified Stage 1 orchestration path yet

⸻

2. Scope of this gap analysis

This document covers only:
	•	Universe Builder
	•	Stage 1 market-data ingestion and normalization
	•	the interface between Universe Builder and Stage 1
	•	source/provider truth
	•	operator/UI implications
	•	QA go-live checks

This document does not cover:
	•	v-1 Executor
	•	broader Stage 2+ feature/routing/model layers except where they depend on Stage 1 truth

⸻

3. Current System Truth

3.1 Universe Builder current truth

Universe Builder exists and is real.

It currently supports:
	•	TTP equities
	•	FTMO-style symbol sets
	•	TopStep futures config metadata

But it is only partially wired into the live Stage 1 path.

What is true today
	•	Universe config supports multiple named universes
	•	Dynamic equity selection exists
	•	Static FTMO / non-equity symbol configuration exists
	•	Static TopStep futures metadata exists
	•	optional materialization target exists in vanguard_universe_members

What is not true today
	•	Universe Builder is not yet the single canonical source for all Stage 1 sources
	•	vanguard_universe_members is not currently the active live truth table
	•	Twelve Data is not currently driven from Universe Builder
	•	futures config existing does not mean futures ingest is live

⸻

3.2 Stage 1 current truth

Stage 1 has two overlapping implementations:

Legacy/live runner
	•	equity-focused
	•	Alpaca REST
	•	writes 5m
	•	derives 1h
	•	strongest current orchestration truth

New normalized ingest layer
	•	writes normalized 1m
	•	Alpaca websocket for equities
	•	Twelve Data for forex / non-futures multi-asset coverage
	•	real backfills and recent rows exist
	•	but not yet clearly the single declared Stage 1 runtime

⸻

3.3 Normalized 1m storage truth

A real normalized Stage 1 storage contract now exists:
	•	vanguard_bars_1m

This is the strongest sign of forward progress.

Both:
	•	Alpaca websocket
	•	Twelve Data

target the same general 1m normalized table shape.

That means Stage 1 now has the beginnings of a real source-agnostic ingest contract.

This is a major strength.

⸻

4. Strengths / What is Already Good

4.1 Equities are genuinely live

The equity ingest path is real and already doing useful work.

Even if the orchestration is older and REST-based, this is not speculative.

4.2 New normalized 1m layer is real

This is the biggest positive development.

The system is no longer trapped in provider-specific shapes only.

A normalized bar contract now exists and is being populated.

4.3 Twelve Data integration is real

Twelve Data is not just a future idea:
	•	adapter exists
	•	symbol config exists
	•	backfill path exists
	•	normalized bars are being written

That is meaningful progress toward multi-asset coverage.

4.4 Alpaca websocket integration is real

Alpaca websocket is implemented and targets the same normalized storage pattern.

Even if it is not yet the single active runtime path, it is real code, real ingest, and the right architectural direction.

4.5 Universe Builder has the right directional idea

Universe Builder already knows about multiple universes and multiple styles of instrument sets.

So the problem is not “no builder exists.”
The problem is “it is not yet the single source-aware contract layer.”

⸻

5. Main Gaps

5.1 No single Stage 1 runtime truth

This is the biggest issue.

You currently have:
	•	old main runner
	•	new normalized ingest layer

Both are real.
But the system does not yet have one clearly declared primary Stage 1 truth.

Why this matters

Without one declared Stage 1 runtime truth:
	•	docs drift
	•	UI claims drift
	•	QA scope drifts
	•	downstream stages don’t know what to trust
	•	operators don’t know which freshness model is authoritative

Go-live impact

This is a major gap.
Not necessarily a full blocker for restricted go-live, but a blocker for calling Stage 1 fully unified.

⸻

5.2 Universe Builder is not yet the canonical Stage 1 bus

Today:
	•	Alpaca equity lane uses it
	•	Twelve Data appears to bypass it
	•	MT5/backfill partly bypasses it
	•	futures are staged

Why this matters

The machine cannot be called cleanly source-aware if one provider gets:
	•	dynamic builder membership,
while another gets:
	•	separate hand-managed config,
and another remains:
	•	metadata only

Go-live impact

This is a significant gap.
It weakens auditability and makes Stage 1 less explainable.

⸻

5.3 No canonical universe row shape across all sources

Current shapes are inconsistent:
	•	TTP equities → symbol strings
	•	Twelve Data config → grouped symbols by pseudo-asset bucket
	•	FTMO-style lists → separate config concepts
	•	TopStep futures → contract dicts with richer metadata

Why this matters

Downstream logic cannot rely on one stable instrument contract.

That means later stages will keep compensating with:
	•	heuristics
	•	defaults
	•	provider-specific branches
	•	asset-class guessing

Go-live impact

This is a serious contract gap.

⸻

5.4 Asset-class and instrument typing is still weak

There is still too much:
	•	implicit classification
	•	default-to-equity behavior
	•	non-first-class CFD handling
	•	inconsistent provider-driven typing

Why this matters

If instrument class is guessed incorrectly:
	•	session handling breaks
	•	V2/V3 filtering breaks
	•	V5 model routing breaks
	•	V6 profile restrictions break
	•	UI truth breaks

Go-live impact

This is a major correctness risk.

⸻

5.5 Futures are not live-complete

You have:
	•	futures config
	•	futures metadata
	•	intended universe support

You do not yet have:
	•	clear live futures ingest path in Stage 1
	•	unified futures orchestration
	•	fully active broker/provider source path for futures bars

Why this matters

Config existence is not operational support.

Go-live impact

Hard no-go for futures go-live claims.

⸻

5.6 Twelve Data is real but orchestration is incomplete

Twelve Data is not fake. That part is good.

But the gap is:
	•	adapter exists
	•	backfills exist
	•	normalized writes exist
	•	current repo truth does not clearly prove it is integrated into the main Stage 1 runtime loop as a first-class live path

Why this matters

There is a difference between:
	•	“data path exists and works”
and
	•	“the live Stage 1 machine runs it as an orchestrated source”

Go-live impact

This supports partial go-live, not full-source parity claims.

⸻

5.7 vanguard_universe_members is not yet live truth

This table looks like the intended materialized Universe Builder → Stage 1 contract.

But it is not being maintained as live truth today.

Why this matters

Without it, you lack:
	•	materialized membership truth
	•	freshness timestamp
	•	clear universe identity
	•	auditable source-to-stage handoff

Go-live impact

Not fatal for restricted go-live, but a major architecture gap.

⸻

5.8 Freshness and stale-source truth is incomplete

You have bar tables and some metadata, but not a clean source-aware freshness contract across all Stage 1 paths.

Why this matters

Operators need to know:
	•	what source is current
	•	what source is stale
	•	what universe fed the cycle
	•	what was skipped

Go-live impact

This is a UI + QA + audit gap.

⸻

6. Source-by-Source Go-Live Assessment

Area	Status	Judgment
Equities	Live	Acceptable for go-live
Forex	Partial	Acceptable only as restricted/partial support
CFD-like non-equity instruments	Partial	Acceptable only if explicitly labeled partial
Commodities / metals / energy / agriculture	Partial	Not full-source-parity go-live yet
Indices	Partial	Same as above
Futures	Staged	No-go

Interpretation

The system is not “not working.”
It is:
	•	working strongly for equities
	•	meaningfully progressing for non-futures multi-asset
	•	not ready for futures claims

⸻

7. Operator / UI Truth Gaps

Even if Stage 1 has little dedicated UI today, the following operator truths will matter:

Must be visible later
	•	active source/provider used
	•	active universe used
	•	last universe refresh time
	•	survivor count or symbol count
	•	last successful fetch time
	•	latest bar timestamp by source
	•	whether path is live, partial, backfill, or staged

Must not be implied
	•	Twelve Data is fully unified in the main live runner if it is not
	•	FTMO live Stage 1 is complete if it is not
	•	futures are operational just because config exists
	•	one universal multi-asset runtime already exists if the runtime is still split

UI/business implication

Stage 1 operator surfaces must be source-aware, not just “bars are flowing.”

⸻

8. QA Go-Live Gaps

Must-test areas

Universe Builder
	•	correct equity filtering
	•	symbol deduplication
	•	unknown universe handling
	•	unknown account handling
	•	canonical row-shape gap
	•	asset-class classification correctness
	•	no accidental default-to-equity for unsupported instruments

Stage 1 ingest
	•	Alpaca equity truth
	•	Alpaca websocket normalized write truth
	•	Twelve Data normalized write truth
	•	bar end-time consistency
	•	old runner vs normalized layer divergence
	•	no-survivor behavior
	•	stale-source behavior
	•	source/provider metadata correctness
	•	partial source support labeling

Futures
	•	verify no false-positive “supported live” implication
	•	verify staged treatment is explicit

QA conclusion

You now need QA to validate:
	•	not only whether bars exist
	•	but whether the system is honest about what is live, partial, and staged

⸻

9. Go-Live Blockers

These are the issues that block calling Stage 1 a fully unified multi-asset go-live machine:

Blocker 1

No single declared Stage 1 runtime truth.

Blocker 2

Universe Builder is not yet the universal source-aware input layer.

Blocker 3

No canonical universe row contract across all supported instruments.

Blocker 4

Futures are still staged.

Blocker 5

Twelve Data is not yet clearly integrated into one primary orchestrated live loop.

⸻

10. Non-Blocking Gaps

These are serious, but do not block a restricted Stage 1 go-live:
	•	vanguard_universe_members not yet live-authoritative
	•	incomplete UI visibility of source/freshness
	•	incomplete per-source stale detection
	•	partial CFD/non-equity typing
	•	mixed legacy/new-path coexistence during transition

These should be documented, not hidden.

⸻

11. Go-Live Recommendation

Recommended stance

Stage 1 is:
	•	Go-live for equities
	•	Restricted/partial go-live for Twelve Data-backed non-futures
	•	No-go for futures
	•	No-go for claiming one fully unified multi-asset runtime yet

Correct wording

Use this wording:

Stage 1 is mid-migration from an equity-first REST cache to a multi-source normalized 1m ingestion layer.
Equities are live.
Twelve Data-backed non-futures support is real and writing normalized bars, but unified live orchestration remains incomplete.
Futures remain staged and are not go-live ready.

That is the most honest and useful go-live statement.

⸻

12. Required Fixes Before Calling Stage 1 Fully Ready

These are the practical fixes needed to close the major gaps:

Priority 1

Declare one primary Stage 1 runtime truth.

Priority 2

Define and enforce one canonical universe row schema.

Priority 3

Make Universe Builder the real source-aware input layer for all supported Stage 1 providers.

Priority 4

Define explicit source freshness / stale-state metadata.

Priority 5

Keep futures staged until a real live ingest path exists.

⸻

13. Final Judgment

If asked “is Stage 1 ready?”

Answer:

Yes, with limits
	•	ready enough to operate as a real market-data layer
	•	ready enough to support live equity ingestion
	•	meaningfully advanced on non-futures multi-asset support

No, as a final unified claim
	•	not yet one fully unified multi-source Stage 1 runtime
	•	not yet true futures-capable live Stage 1
	•	not yet backed by one canonical Universe Builder → Stage 1 contract

Final go-live decision

Go live with restrictions and explicit truth labels.
Do not present Stage 1 as fully unified multi-asset live yet.

⸻

When you’re ready, I’ll do the same single-document go-live gap analysis for Stage 2.