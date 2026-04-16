# Vanguard Handover: Timeout Automation, Position Manager, Analytics, and QA/Prod State

Date: 2026-04-15
Primary repos:
- `/Users/sjani008/SS/Vanguard`
- `/Users/sjani008/SS/Vanguard_QAenv`

Purpose:
- Capture the architectural and operational state after the recent automation, timeout-close, analytics, and execution work.
- Make the current system legible without relying on chat history.
- Provide a restart point for future work on model, execution, risk, and telemetry.

This document is intentionally detailed. It is meant to serve as a working handover and reference.

## 1. Executive Summary

Over the last few days, the system moved from:
- signal generation and manual review,

to:
- live QA FTMO-demo entry through DWX/MT5,
- timeout-driven close support through the existing lifecycle daemon,
- position-manager/operator-action controls,
- session-specific timeout policy display in Telegram,
- candidate-level analytics for approved/executed/skipped signals,
- candidate forward-truth checkpoints,
- and a first pass at slot-constrained automation analysis.

The key architectural shift is this:
- execution is no longer the only truth surface,
- candidate decision truth and forward truth now exist separately,
- and timeout/session policy is now stored in data rather than only inferred during ad hoc replays.

The codebase is still not “finished,” but it is now in a state where:
- QA FTMO-demo can run unattended,
- open/close behavior is observable,
- candidate decisions are logged,
- timeout-close is auditable,
- and repeated-thesis spam in Telegram is reduced.

There are still known issues:
- the DWX/MT5 bridge settings still govern max orders and maximum broker-side lot size,
- context position rows can arrive from multiple sources for the same ticket, so consumers must dedupe,
- `RECONCILED_CLOSED` is a bookkeeping outcome, not a true causal close reason,
- and broker-demo sizing versus “shadow 5k/10k” economic comparison is not yet formalized as a full analytics layer.

## 2. System Map

### 2.1 Main subsystems

1. Context / market-state layer
- pulls and stores quotes, account state, symbol specs, positions, pair-state, and health
- sources include Alpaca, Twelve Data, local DWX/MT5, and account/profile context tables

2. Model and selection layer
- V2 prefilter
- V3 factor engine
- V4B scorer / model predictions
- V5.1 tradeability
- V5.2 selection

3. Risk / approval layer
- V6 risk filters and Risky policy evaluation
- sizing and route eligibility
- duplicate symbol, active thesis, position-limit, and health checks

4. Execution layer
- live route into MT5/DWX for `mt5_local_*`
- operator-action / request-event path for position management actions
- position manager CLI

5. Lifecycle / reconciliation layer
- lifecycle daemon mirrors broker truth
- resolves journal vs broker divergence
- applies timeout auto-close through the operator-action path

6. Analytics layer
- trade forward checkpoints
- timeout policy summary
- shell replay table
- candidate decision log
- candidate forward checkpoints

7. Operator / Telegram layer
- shortlist
- diagnostics
- timeout policy display
- repeated-thesis suppression into a separate section

### 2.2 Repo split

`Vanguard`
- main repo / prod-aligned codebase
- contains the same core modules and now mirrors the analytics and dedupe fixes

`Vanguard_QAenv`
- QA execution repo
- used for FTMO demo validation
- contains the live wrapper scripts and QA runtime config used in the recent proving pass

Design rule followed during this work:
- when a fix was core to orchestration, timeout policy, context reading, or analytics, it was mirrored into both prod and QA unless explicitly QA-only.

## 3. Pipeline Refresher

### 3.1 Stages

V1
- poll and aggregate market data
- source health

V2
- prefilter / universe health

V3
- feature computation

V4B
- scorer / predictions

V5.1
- tradeability economics

V5.2
- selection / ranking / route metadata

V6
- risk filters
- route-ready approval decision

V7
- execution routing
- Telegram output
- analytics refresh

### 3.2 Key current direction

The system is no longer being treated as “shell-first.”

The current operating thesis is:
- timeout-first by session,
- emergency rails for broker safety,
- analytics should separate:
  - candidate truth,
  - trade truth,
  - and broker truth.

Current timeout guidance shown to operators:
- London: 30m (dedupe 120)
- NY: 60m (dedupe 120)
- Asian: 120m (dedupe 90)

This is advisory/policy guidance, not proof of final production optimality.

## 4. Context Layer and State Tables

### 4.1 Core context tables touched by the recent work

- `vanguard_context_quote_latest`
- `vanguard_context_account_latest`
- `vanguard_context_positions_latest`
- `vanguard_context_symbol_specs`
- `vanguard_forex_pair_state`
- `vanguard_crypto_symbol_state`

These tables feed:
- V6 live enrichment,
- operator position snapshots,
- Telegram state,
- timeout policy enrichment,
- and context-health checks.

### 4.2 Important nuance: duplicate position sources

One real broker position can appear twice in `vanguard_context_positions_latest` because the same ticket may be mirrored from:
- `mt5_dwx`
- `ftmo_mt5_local`

This is a real system behavior, not necessarily a broken write.

The bug was in the readers:
- Risky and Telegram were counting those as two open positions.

Fix applied:
- consumers now collapse to the latest row per broker `ticket`.

This affects:
- Risky open-position counts
- thesis-display `OPEN` state
- account header `Pos N`
- incumbent snapshot logic

This was one of the reasons you saw false `POSITION LIMIT` blocks.

## 5. V5.1, V5.2, V6, and Risky

### 5.1 V5.1

Tradeability (`vanguard_v5_tradeability`) freezes:
- economics state
- predicted move
- spread and after-cost move
- tradeability metrics and reasons

This remains the main economics gate before selection.

### 5.2 V5.2

Selection (`vanguard_v5_selection`) freezes:
- selected / state
- selection rank
- route tier
- display label
- streak/followthrough metadata

This is where ranking/ordering metadata comes from for later execution and display.

### 5.3 V6 / Risky

Risky consumes:
- candidate enriched with live quote / state / account context
- compiled profile policy
- account state
- active-thesis state

Major checks:
- drawdown pause
- blackout
- side control
- duplicate symbol
- active thesis reentry
- position limit
- sizing
- health/context readiness

Current nuance:
- Risky sizes off the actual profile/account policy.
- In QA FTMO demo, that means the `ftmo_demo_100k` policy sizes like a 100k account.

This is the current deliberate choice:
- keep automation testing closer to the actual execution profile,
- do not add a virtual shadow budget into the live sizing path yet.

Reason:
- a virtual 5k/10k execution override would have wide downstream impact across sizing, risk, analytics, and operator expectations.

## 6. Position Manager and Operator Actions

### 6.1 Position manager role

The position manager provides:
- broker-open position visibility,
- timeout metadata visibility,
- and a request/event-driven control surface for close/modify actions.

Main QA operator command:

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
python3 scripts/position_manager_cli.py --profile ftmo_demo_100k --list
```

It currently shows:
- broker ticket
- symbol
- direction
- lots
- entry, SL, TP
- PnL
- hold minutes
- timeout
- timeout_at
- timeout_left
- session
- dedupe
- trade_id

### 6.2 Request/event audit tables

- `vanguard_execution_requests`
- `vanguard_execution_events`

These are authoritative for:
- operator-action close paths
- timeout-close path through the daemon

They are **not** the full truth for auto-entry, because the orchestrator live entry path still bypasses this request/event trail.

That is why candidate decision logging was added separately.

## 7. Timeout Policy and Timeout Close

### 7.1 Trade forward checkpoints

`vanguard_forward_checkpoints`
- one row per `(trade_id, checkpoint_minutes)`
- used for trade-truth timeout analytics

Important fix:
- checkpoint maturity now uses bar-end semantics instead of exact second matching.
- before the fix, almost everything stayed `PARTIAL`.

### 7.2 Timeout policy enrichment

`vanguard_timeout_policy_summary`
- aggregated summary by session / dedupe / timeout

`vanguard_forward_shell_replays`
- shell-overlay analytics on top of trade-truth checkpoints

`timeout_policy.py`
- enriches trade checkpoints with:
  - `session_bucket`
  - `policy_timeout_minutes`
  - `policy_dedupe_minutes`
  - dedupe-chain metadata

Fallback improvement:
- if pair-state session is missing, timeout policy now falls back to time-based session inference

### 7.3 Auto-close path

Timeout auto-close is owned by the existing lifecycle daemon, not a new daemon.

Flow:
1. lifecycle daemon polls broker positions
2. mirrors broker/account truth
3. resolves divergences
4. if timeout-policy auto-close is enabled for the profile:
   - evaluates timeout eligibility
   - queues/runs `CLOSE_AT_TIMEOUT`
   - logs request/event trail

In QA this is enabled for:
- `ftmo_demo_100k`

It is not enabled broadly for prod.

### 7.4 One important caveat

`RECONCILED_CLOSED` is not a real close reason.

It means:
- the journal thought the trade was open,
- the broker no longer had the position,
- the reconciler marked the journal row closed to match broker truth.

So it does **not** tell you whether the true cause was:
- timeout close,
- TP/SL,
- manual close,
- or some other broker-side action.

Only explicit request/event trails tell you that.

## 8. Candidate Analytics Layer

### 8.1 Why it was added

Once auto-execution fills limited slots, you need to know:
- what was approved,
- what was executed,
- what was skipped,
- and what skipped signals would have done anyway.

Without that, you cannot measure allocator quality under slot pressure.

### 8.2 Tables

`vanguard_signal_decision_log`
- one row per approved/evaluated candidate
- stores:
  - symbol / direction / session
  - edge / predicted return / rank
  - lot size / risk snapshot
  - timeout policy / dedupe policy
  - open positions count / max slots / free slots
  - execution decision
  - reason code
  - incumbent snapshot
  - linkage to trade id / broker position id where applicable

`vanguard_signal_forward_checkpoints`
- one row per `(decision_id, checkpoint_minutes)`
- covers executed and skipped candidates
- stores:
  - entry
  - checkpoint price
  - close move pips / bps / R
  - MFE / MAE in pips and R
  - direction-positive outcome

These tables are the basis for:
- executed vs skipped expectancy
- missed alpha
- slot efficiency
- future incumbent-regret analysis

### 8.3 Model metadata

The decision layer also stores model attribution fields so future model swaps do not require schema redesign:
- `model_id`
- `model_family`
- `model_readiness`
- `gate_policy`
- `v6_state`

This is deliberate. It should let future analysis answer:
- is the problem model quality?
- or route / risk / timeout / execution behavior?

## 9. Telegram / Operator Display Changes

### 9.1 Session policy header

Telegram now shows:
- London 30m (dedupe 120)
- NY 60m (dedupe 120)
- Asian 120m (dedupe 90)

### 9.2 Repeated-thesis display policy

Repeated same-symbol same-side theses are no longer shown as fresh actionable entries in the main shortlist section.

Current behavior:
- broker-open same-symbol same-side => `OPEN`
- same-symbol same-side recently seen => moved into `Repeated Thesis`
- still shows timeout guide and thesis state

This is a display-layer dedupe, not a trading-policy dedupe.

### 9.3 Account header

The account header uses context-account + context-position truth.

It now dedupes positions by broker ticket before counting `Pos N` or summing UPL, to avoid double-counting the same position from multiple context sources.

## 10. QA Wrapper Scripts

Added in QA:

- `scripts/start_ftmo_live_stack.sh`
- `scripts/monitor_ftmo_live_stack.py`
- `scripts/stop_ftmo_live_stack.sh`

Purpose:
- start the QA FTMO live stack with one command
- validate bridge/profile gates
- run orchestrator, lifecycle daemon, and monitor together
- send Telegram monitor/heartbeat messages

### 10.1 Emergency stop

`scripts/emergency_stop.sh`

Current role:
- hard-stop the QA FTMO live stack
- kill pid-file-managed processes first
- fall back to targeted `pkill` patterns for QA orphan processes

This script was fixed because the previous version only used broad `pkill` patterns and did not cleanly handle the wrapper-managed PID model.

## 11. QA Runtime Changes

### 11.1 Universe scope

The QA FTMO profile was moved from the broad mixed `ftmo_universe` to a dedicated forex-only universe.

Reason:
- the broad scope caused DWX crypto/time-series polling that wasted cycle time and had nothing to do with the FTMO forex execution path.

### 11.2 Quote freshness threshold

QA quote freshness threshold was raised from 5s to 10s.

Reason:
- real live cycles were holding otherwise valid candidates as `QUOTE_STALE` because enrichment and cycle timing pushed quote age slightly beyond 5 seconds.

### 11.3 Bridge lot safety cap

QA bridge config has:
- `execution.bridges.mt5_local_ftmo_demo_100k.max_lots_safety`

This is the Python-side cap that the DWX executor reads.

Current state:
- set high enough that it should no longer be the active limiter
- but the MT5 EA can still independently cap lots on the broker side

## 12. MT5 / DWX Bridge Findings

### 12.1 Real source of max orders

The `OPEN_ORDER_MAXIMUM_NUMBER` rejection came from the MT5 EA itself, not the Python code.

Source found:
- `/Users/Shared/DWX_MT5/Experts/Advisors/DWX_Server_MT5.mq5`

Key inputs:
- `MaximumOrders`
- `MaximumLotSize`

This is why changing only Python config was insufficient earlier.

### 12.2 DWX parser fix

The DWX client previously crashed its background thread on concatenated JSON payloads with `JSONDecodeError: Extra data`.

That parser path was hardened in both prod and QA.

### 12.3 Symbol subscription noise

There is still noisy bridge message traffic for unsupported symbols/subscriptions. This is not the primary blocker for the current QA forex path, but it is still a source of operational noise.

## 13. Prod / QA Differences

### 13.1 QA-specific

- live FTMO-demo profile
- QA wrapper scripts
- QA live proving path
- FTMO demo runtime config

### 13.2 Prod-specific

- prod code mirrors the core analytics/parser/timeout fixes
- prod is **not** armed the same way as QA FTMO-demo for unattended operation

### 13.3 Important design choice

Where practical, core fixes were mirrored to prod immediately so the code paths do not drift:
- timeout checkpoint maturity
- timeout policy enrichment
- candidate decision log
- candidate forward checkpoints
- duplicate broker-ticket dedupe in readers
- DWX parser fix
- repeated-thesis Telegram display

## 14. Why Some Earlier Things Happened

### 14.1 Why old AUDCHF was `RECONCILED_CLOSED`

Because the broker position disappeared and the reconciler updated the journal afterward.

There was no timeout-close request/event for that ticket, so it was not the new timeout path.

### 14.2 Why 3 of 4 orders failed earlier

Because the EA had `MaximumOrders = 1`.

The first trade filled.
The next three were rejected by the bridge with `OPEN_ORDER_MAXIMUM_NUMBER`.

### 14.3 Why `Pos 2` was shown with only one real trade

Because the same broker position was present twice in context under two sources, and consumers were counting both.

This is now patched in the readers.

### 14.4 Why QA later sent 3-lot orders

Because Risky sized them large for the 100k profile and the bridge cap allowed 3.0 instead of forcing `0.01`.

So the 3-lot fills were not a mysterious default lot size.
They were the result of:
- large approved size,
- then capping to the currently allowed bridge/EA lot limit.

## 15. Current Recommended Operating Mode

For now:
- keep QA FTMO demo sized as the actual 100k profile
- do not introduce a virtual 5k/10k sizing budget into the live execution path yet
- if you want apples-to-apples 5k/10k comparisons, add them later as analytics/shadow PnL, not as execution overrides

Reason:
- execution-layer shadow budgets create too much configuration and semantics drift too early
- the current priority is proving:
  - entry
  - close
  - candidate logging
  - timeout behavior
  - slot-constrained analytics

## 16. What Is Stored Where

Approval truth:
- `vanguard_tradeable_portfolio`

Candidate decision truth:
- `vanguard_signal_decision_log`

Candidate forward truth:
- `vanguard_signal_forward_checkpoints`

Trade truth:
- `vanguard_trade_journal`

Trade forward truth:
- `vanguard_forward_checkpoints`

Timeout summary:
- `vanguard_timeout_policy_summary`

Shell research:
- `vanguard_forward_shell_replays`

Open-position mirror:
- `vanguard_open_positions`

Context position mirror:
- `vanguard_context_positions_latest`

Operator-action audit:
- `vanguard_execution_requests`
- `vanguard_execution_events`

Reconciliation outcomes:
- `vanguard_reconciliation_log`

## 17. Practical Commands

Start QA live stack:

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
bash scripts/start_ftmo_live_stack.sh
```

Stop QA live stack:

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
bash scripts/stop_ftmo_live_stack.sh
```

Emergency stop:

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
bash scripts/emergency_stop.sh
```

List QA positions:

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
python3 scripts/position_manager_cli.py --profile ftmo_demo_100k --list
```

Tail QA logs:

```bash
tail -f /tmp/vanguard_qa_ftmo_orchestrator.log
tail -f /tmp/lifecycle_daemon.log
tail -f /tmp/vanguard_qa_ftmo_monitor.log
```

## 18. Known Open Questions

1. Should there be a true causal close-reason layer beyond `RECONCILED_CLOSED`?
- probably yes
- current reconciliation reason is too generic for analytics

2. Should auto-entry eventually use the same request/event audit surface as auto-close?
- probably yes
- candidate decision logging solves visibility, but operator-action parity would still improve audit quality

3. Should there be shadow 5k/10k PnL analytics?
- yes, probably
- but as post-trade analytics, not execution-layer sizing overrides yet

4. Should MT5 EA input state be mirrored into DB/runtime telemetry?
- yes
- otherwise broker-side constraints can still surprise the Python operator layer

5. Should multiple context position sources be collapsed at write time instead of read time?
- maybe
- current fix is read-side dedupe because it is safer and less invasive

## 19. Suggested Next Steps

1. Prove timeout-close cleanly on a natural open position:
- keep daemon running
- wait for timeout
- verify request/event trail exists

2. Keep QA entry sizing aligned with the intended testing mode:
- if using full profile sizing, ensure MT5 EA lot cap is also aligned

3. Add a post-trade analytics layer for:
- 5k shadow PnL
- 10k shadow PnL
- broker-actual PnL

4. Improve close causality:
- distinguish reconciled-close detected after disappearance from known TP/SL/manual/timeout causes where possible

5. Continue using the candidate decision and candidate forward tables as the main truth for automation-quality analysis

## 20. File Map (Recent High-Impact Files)

Core orchestrator:
- `Vanguard/stages/vanguard_orchestrator.py`
- `Vanguard_QAenv/stages/vanguard_orchestrator.py`

Risk filters:
- `Vanguard/stages/vanguard_risk_filters.py`
- `Vanguard_QAenv/stages/vanguard_risk_filters.py`

Timeout policy / checkpoints:
- `Vanguard/vanguard/execution/forward_checkpoints.py`
- `Vanguard/vanguard/execution/timeout_policy.py`
- `Vanguard_QAenv/vanguard/execution/forward_checkpoints.py`
- `Vanguard_QAenv/vanguard/execution/timeout_policy.py`

Candidate analytics:
- `Vanguard/vanguard/execution/signal_decision_log.py`
- `Vanguard/vanguard/execution/signal_forward_checkpoints.py`
- `Vanguard_QAenv/vanguard/execution/signal_decision_log.py`
- `Vanguard_QAenv/vanguard/execution/signal_forward_checkpoints.py`

Position manager / operator actions:
- `Vanguard_QAenv/scripts/position_manager_cli.py`
- `Vanguard_QAenv/vanguard/execution/operator_actions.py`

Lifecycle / reconciler:
- `Vanguard_QAenv/vanguard/execution/lifecycle_daemon.py`
- `Vanguard_QAenv/vanguard/execution/reconciler.py`

DWX executor / adapter / client:
- `Vanguard_QAenv/vanguard/executors/dwx_executor.py`
- `Vanguard_QAenv/vanguard/data_adapters/mt5_dwx_adapter.py`
- `Vanguard_QAenv/vanguard/data_adapters/dwx/dwx_client.py`

QA runtime and wrapper scripts:
- `Vanguard_QAenv/config/vanguard_runtime.json`
- `Vanguard_QAenv/scripts/start_ftmo_live_stack.sh`
- `Vanguard_QAenv/scripts/stop_ftmo_live_stack.sh`
- `Vanguard_QAenv/scripts/emergency_stop.sh`
- `Vanguard_QAenv/scripts/monitor_ftmo_live_stack.py`

Broker-side EA source:
- `/Users/Shared/DWX_MT5/Experts/Advisors/DWX_Server_MT5.mq5`

## 21. Closing Note

The current system is not “done,” but it is no longer opaque.

The most important structural wins from the recent work are:
- candidate truth separated from trade truth,
- timeout-close on the audited operator-action path,
- repeat-thesis reduction in Telegram,
- deduped live position counting,
- and a clearer distinction between:
  - broker truth,
  - journal truth,
  - candidate truth,
  - and analytics truth.

That separation is what should prevent the next round of work from turning into another blind rewrite.
