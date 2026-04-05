Use this as your markdown:

V6 Profiles Business Requirements Memo

SignalStack / Vanguard final build

Purpose

Define the business requirements for the V6 profile-based execution policy and sizing layer.

This memo is for turning the current partial profile system into a strict live trading policy layer that governs preview, execute, sizing, warnings, blocks, and audit behavior.

Core decision

Do not build a second profile system.

Reuse and extend the existing live profile stack:
	•	account_profiles registry
	•	accounts.py
	•	trade_desk.py
	•	existing profile selector/admin surfaces

JSON can still be used for seed/export/import, but the live source of truth should remain the existing profile registry.

⸻

1. Objective

V6 must become a profile-based execution policy and sizing layer, not just a generic risk gate.

The selected trading profile must affect:
	•	trade preview
	•	trade execution
	•	sizing
	•	warnings
	•	hard blocks
	•	audit trail

⸻

2. Scope

In scope now
	•	profile registry
	•	profile selection
	•	preview/execute enforcement
	•	profile-based sizing
	•	profile-based warnings and blocks
	•	audit trail
	•	operator visibility of active profile
	•	missing profile variants
	•	environment distinction where needed

Out of scope for first implementation
	•	automatic candidate-page asset switching by profile
	•	profile-driven model switching
	•	broad broker-routing redesign
	•	rich UI profile editor beyond what is needed now

⸻

3. Current-state conclusion

A real profile system already exists and should be extended.

What exists today:
	•	live profile registry
	•	persisted account_profiles
	•	seeded profiles
	•	profile selector in /trades
	•	profile_id in preview/execute path
	•	live enforcement in trade_desk
	•	partial settings/admin UI
	•	dormant helpers for sizing and EOD behavior

What is incomplete:
	•	many stored profile fields are not enforced
	•	demo / eval / funded separation is weak
	•	FTMO intraday and TopStep futures variants are not modeled correctly enough
	•	profile influence is strong in Trade Desk, weak elsewhere
	•	sizing is not fully profile-driven yet

⸻

4. Required supported profiles

V6 must explicitly support these named profiles:
	•	ttp_20k_swing
	•	ttp_40k_swing
	•	ttp_50k_intraday
	•	ttp_100k_intraday
	•	ftmo_100k_intraday
	•	topstep_100k_futures

Important

The current seeded FTMO and TopStep entries are not sufficient if they do not correctly represent:
	•	intraday behavior for FTMO
	•	futures-only behavior for TopStep

Those variants must be corrected or added explicitly.

⸻

5. Core business rules

5.1 Active profile is mandatory

A valid selected profile must be present for live Trade Desk preview and execute.

If profile is:
	•	missing
	•	invalid
	•	inactive

then:
	•	preview must show explicit failure context
	•	execute must be blocked

5.2 Preview and execute must use the same profile

The profile used for preview must be the profile used for submit, unless the operator explicitly changes the profile and re-runs preview.

5.3 Profile affects live behavior

The selected profile must affect, at minimum:
	•	sizing
	•	approval/rejection
	•	warnings
	•	hard blocks

5.4 No silent fallback to a hidden generic profile

Silent fallback is not acceptable for live operator use.

If fallback exists for technical reasons, it must be:
	•	explicit
	•	visible
	•	auditable

5.5 One profile system only

Do not create a second parallel registry.

Use the existing live profile registry and extend it.

⸻

6. Profile data model requirements

Each profile must support the following business fields.

Identity and status
	•	profile id
	•	profile name
	•	prop firm
	•	account type
	•	account size
	•	active flag

Environment / mode
	•	demo
	•	eval
	•	funded
	•	live

Instrument eligibility

At minimum, profiles must be able to restrict allowed instruments or asset classes such as:
	•	equities
	•	forex
	•	commodities
	•	futures
	•	indices
	•	crypto

Holding style
	•	intraday
	•	swing

Core risk limits
	•	daily loss limit
	•	max drawdown
	•	trailing drawdown, if applicable
	•	max positions
	•	single-position concentration limit
	•	batch exposure limit
	•	portfolio heat / total risk limit
	•	sector concentration limit
	•	duplicate symbol policy

Schedule and hold rules
	•	no new positions after
	•	must close EOD
	•	EOD flatten time
	•	overnight/weekend restrictions where applicable

Additional policy fields
	•	weekly loss limit
	•	max trades per day
	•	earnings hold restrictions
	•	halted-name restrictions
	•	volume restrictions
	•	consistency-rule fields where applicable
	•	broker or venue routing hints where needed
	•	plan type

Important

Many of these fields already exist in storage.
The requirement is to expose and enforce them consistently in the live path.

⸻

7. Enforcement requirements

7.1 Profile-based sizing

Sizing must become profile-driven.

The selected profile must influence:
	•	approved quantity
	•	approved notional
	•	exposure
	•	concentration checks

If reusable sizing helpers already exist, they should be wired into the live path instead of inventing a brand-new sizing engine first.

7.2 Daily loss and drawdown

Daily loss and drawdown remain hard gate checks.

If a profile requires trailing drawdown, that must be modeled explicitly, not assumed away.

7.3 Intraday vs swing rules

Intraday profiles must support:
	•	no-new-positions-after
	•	EOD flattening
	•	same-day operational restrictions

Swing profiles may allow overnight or weekend holds, but only if the selected profile permits them.

7.4 Instrument restrictions

TopStep futures must not behave like an equity profile.

Profile rules must be able to restrict by instrument type or asset class.

7.5 Environment separation

The system must distinguish:
	•	demo
	•	eval
	•	funded
	•	live

A demo account must not silently inherit funded behavior unless explicitly intended.

7.6 Hard blocks vs warnings

The system must clearly separate:
	•	advisory warnings
	•	blocking rules

The operator must be able to see why a trade was:
	•	resized
	•	warned
	•	blocked
	•	approved

⸻

8. Trade Desk requirements

8.1 Strict profile connection

Trade Desk requires strict connection to profiles.

This is a hard rule.

The active profile must control:
	•	preview
	•	execute
	•	sizing
	•	block/warning logic
	•	audit output

8.2 Operator visibility

The active profile must be visible in the trading workspace at all times during preview and execute.

8.3 Compact profile summary

Trade Desk should show a compact profile summary that explains the current rule context without forcing the operator into settings.

8.4 Decision explanation

If a trade is resized, blocked, or warned due to profile rules, the Trade Desk must show the profile-driven reason.

⸻

9. Candidate page requirements

9.1 Secondary for first implementation

Candidate page profile-awareness is desirable, but secondary.

The first strict implementation requirement is Trade Desk enforcement.

9.2 No forced auto-switching yet

Automatic asset-class switching by profile is not required for first implementation.

Example:
	•	selecting FTMO does not need to automatically reshape the candidate workspace immediately

That can come later.

9.3 Future extension allowed

Later, candidate page may use profile context for:
	•	filtering
	•	allowed asset classes
	•	operator warnings
	•	source presets

But that is not the first V6 requirement.

⸻

10. API and contract requirements

10.1 Profile exposure

The live API must expose the existing profile registry from account_profiles.

10.2 Execute contract

The execute request must carry profile identity explicitly.

10.3 Preview and execute response requirements

Preview/execute responses must return:
	•	selected profile
	•	environment or mode
	•	triggered rules
	•	warnings
	•	blocks
	•	sizing basis
	•	final approved size or exposure
	•	final approval/rejection result

10.4 Auditability

The live system must be able to explain:
	•	which profile was selected
	•	which environment was selected
	•	which rules fired
	•	whether the trade was resized
	•	why the trade was approved or rejected

10.5 Settings/profile CRUD

Profile API and settings UI must eventually expose the real live field set, not only the smaller current subset.

⸻

11. UI behavior rules

11.1 No fake defaults

If a profile field is missing, unavailable, or not enforced yet:
	•	show it as missing
	•	do not invent a business-looking value

11.2 Profile truth over UI hints

Backend profile gate is authoritative.

Any client-side estimate or hint is advisory only.

11.3 Visible profile context

The operator must always know:
	•	which profile is active
	•	what environment is active
	•	which rule set is governing preview/execute

⸻

12. QA acceptance requirements

P0 acceptance

The following must be true:
	•	no execute without valid active profile
	•	preview and execute use the same profile unless operator explicitly changes it
	•	the same trade idea can produce different outcomes under different profiles
	•	profile-driven warnings and blocks are visible and auditable
	•	futures profiles do not behave like equity profiles
	•	intraday profiles do not behave like swing profiles

P1 acceptance

The following should also be true:
	•	dormant stored profile fields that are declared in scope are either enforced or explicitly marked not-yet-live
	•	FTMO intraday and TopStep futures variants are modeled correctly
	•	demo / eval / funded / live separation is explicit
	•	operator UI clearly shows active profile and profile-driven decision reasons

⸻

13. Required profile matrix

Profile	Holding style	Primary instrument scope	Must be live in V6
TTP 20k Swing	Swing	Equities	Yes
TTP 40k Swing	Swing	Equities	Yes
TTP 50k Intraday	Intraday	Equities	Yes
TTP 100k Intraday	Intraday	Equities	Yes
FTMO 100k Intraday	Intraday	Forex / commodities / indices as allowed	Yes
TopStep 100k Futures	Intraday	Futures	Yes


⸻

14. Recommended implementation order
	1.	Treat account_profiles as the live registry.
	2.	Clean up and complete the required profile set.
	3.	Expand the API schemas to expose the fields already stored in the registry.
	4.	Extend trade_desk to enforce the stored profile fields before inventing new profile logic.
	5.	Wire profile-based sizing into preview and execute.
	6.	Add explicit environment or mode modeling.
	7.	Improve UI visibility of profile context and profile-driven decision reasons.
	8.	Add candidate-page profile-awareness later if useful.

⸻

15. Explicit non-goals for first build

These are not first-pass V6 requirements:
	•	per-instrument model switching
	•	automatic candidate-page asset switching by profile
	•	second profile registry
	•	cosmetic-only UI changes without enforcement
	•	broad broker-routing redesign before profile enforcement is solid

⸻

16. Final decision

V6 must be specified as a profile-based execution policy and sizing layer, not just a generic risk gate.

The system already contains enough foundation to do this by extending the live registry and wiring its dormant fields into the current execution path.

That is the correct business direction for the final build.

If you want, next we do Stage 1 in the same style:
	•	business requirements
	•	UI implications
	•	QA implications
	•	contract implications
	•	operator rules