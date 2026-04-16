# DWX Position Manager, Auto Execution, and Auto Closing State

Date: 2026-04-15

## Purpose

This note captures the current end-to-end state of:

- DWX-backed position management
- auto execution
- timeout-based auto closing
- gaps that remain before unattended trading analytics are trustworthy

It also proposes the next data-layer changes required once slot-constrained automation is turned on.

## Current Runtime State

### 1. DWX position manager

The new operator-action / position-manager surface is live in both:

- `Vanguard`
- `Vanguard_QAenv`

Core capabilities now present:

- list live positions from DWX-backed broker truth
- show account header
- show timeout metadata on linked positions
- queue and process:
  - `EXECUTE_SIGNAL`
  - `CLOSE_POSITION`
  - `SET_EXACT_SL_TP`
  - `MOVE_SL_TO_BREAKEVEN`
  - `CLOSE_AT_TIMEOUT`
  - `FLATTEN_PROFILE`

Supporting data layer now present in both repos:

- `vanguard_execution_requests`
- `vanguard_execution_events`
- `vanguard_forward_checkpoints`
- `vanguard_forward_shell_replays`
- `vanguard_timeout_policy_summary`

### 2. Auto execution

Auto execution already exists on the orchestrator side for `mt5_local_*` bridges.

Meaning:

- if a profile is active
- if the orchestrator is running in live execution mode
- if approvals pass gating

then the orchestrator can submit live DWX market orders automatically.

This is not part of the new position manager. It is the older orchestrator-driven execution path.

### 3. Timeout policy guidance

Timeout policy is now computed and surfaced in both:

- Telegram/manual output
- position manager snapshots

Current candidate session policy:

- London: `30m` timeout, `dedupe 120`
- NY: `60m` timeout, `dedupe 120`
- Asian: `120m` timeout, `dedupe 90`

This is currently a candidate policy, not final production truth.

### 4. Timeout auto close

Timeout auto close now runs through the existing `lifecycle_daemon`, not a new daemon.

The daemon:

- reads open positions
- checks timeout metadata through the position-manager snapshot
- queues `CLOSE_AT_TIMEOUT`
- processes the request through the existing operator-action path
- writes request/event/journal reconciliation through the same audit system

This means timeout close is now using one consistent execution path, not an ad hoc broker write.

## What Was Live-Validated

### QA FTMO demo validation

This was validated live on `ftmo_demo_100k`.

Validated steps:

1. Real FTMO demo trade opened through DWX executor
2. Real broker ticket linked into QA journal + timeout checkpoint
3. `--list` showed:
   - timeout minutes
   - timeout_at
   - minutes_to_timeout
   - session bucket
   - dedupe mode
4. Real `CLOSE_AT_TIMEOUT` worked through the CLI path
5. Real `CLOSE_AT_TIMEOUT` also worked through a single `lifecycle_daemon._sync_profile()` iteration
6. Broker position was actually closed
7. Journal moved to `CLOSED`
8. Request/event trail was written:
   - `TIMEOUT_ARMED`
   - `TIMEOUT_TRIGGERED`
   - `TIMEOUT_CLOSE_OK`
9. Context position row was removed

### What is proven

Proven live:

- DWX bridge execution on FTMO demo
- timeout metadata display on a real open QA position
- timeout close via operator-action path
- timeout close via daemon-owned unattended path
- request/event/journal reconciliation after timeout close

### What is not yet proven

Not yet proven end to end:

- live `EXECUTE_SIGNAL` operator-action path on FTMO demo
  - the timeout-close path was proven on a real broker position
  - but the open leg for that validation used direct DWX execution plus journal/checkpoint binding
- unattended multi-trade day where capacity fills and later signals are blocked
- production GFT unattended timeout close

## Current Control Model

### Prod

Code exists in prod, but practical unattended timeout close is still controlled by config.

Current runtime gate:

- `position_manager.timeout_auto_close.enabled = true`
- `eligible_profiles = ["ftmo_demo_100k"]`

So prod has the code, but the only eligible profile is the QA FTMO demo profile.

### QA

QA FTMO demo now supports:

- unattended timeout close via `lifecycle_daemon`
- DWX execution and DWX close
- audit trail through requests/events

## Important Architectural Truth

We now have two different strategy layers:

1. **Signal approval / execution layer**
   - what gets approved
   - what gets submitted

2. **Portfolio occupancy / timeout management layer**
   - what remains open
   - what blocks later signals
   - when positions are auto-cleared

The second layer changes the meaning of forward testing.

Once unattended automation is on, the system is no longer evaluating only:

- "did this signal work?"

It is also evaluating:

- did this signal get a slot?
- did an earlier position occupy a slot that a better later signal needed?
- did the timeout clear capacity in time?

That means pure signal-quality replay is no longer enough.

## Why Existing Forward Tracking Is Not Sufficient

Current forward tracking mainly tells us:

- direction quality
- timeout quality
- shell overlay quality

It does **not** yet tell us:

- how many approved signals were skipped because slots were full
- whether skipped signals would have outperformed incumbents
- whether the first few auto-opened trades crowded out better later trades
- whether session policy improved actual portfolio throughput or just isolated signal stats

Once automation is on, this missing layer becomes critical.

## Proposed Data-Layer Changes

### 1. Add a signal decision log

New table:

- `vanguard_signal_decision_log`

One row for every approved candidate, whether executed or not.

Suggested columns:

- `decision_id`
- `cycle_ts_utc`
- `profile_id`
- `trade_id` nullable
- `symbol`
- `direction`
- `session_bucket`
- `timeout_policy_minutes`
- `dedupe_policy_minutes`
- `model_id`
- `model_family`
- `edge_score`
- `selection_rank`
- `approval_status`
- `execution_decision`
  - `EXECUTED`
  - `SKIPPED_NO_CAPACITY`
  - `SKIPPED_DUPLICATE`
  - `SKIPPED_POLICY`
  - `SKIPPED_STALE`
  - `SKIPPED_WEAKER_THAN_INCUMBENTS`
- `decision_reason_json`
- `created_at_utc`

This becomes the canonical "what happened to each approved candidate?" table.

### 2. Add portfolio decision snapshots

New table:

- `vanguard_portfolio_decision_snapshots`

One row per approved candidate at the time the execution decision was made.

Suggested columns:

- `decision_id`
- `cycle_ts_utc`
- `profile_id`
- `open_positions_count`
- `free_slots`
- `max_slots`
- `open_symbols_json`
- `incumbent_trade_ids_json`
- `incumbent_tickets_json`
- `incumbent_ages_minutes_json`
- `incumbent_unrealized_pnl_json`
- `incumbent_timeout_remaining_json`
- `created_at_utc`

This makes slot contention analyzable without rebuilding portfolio state after the fact.

### 3. Add incumbent-vs-candidate comparison

New table:

- `vanguard_signal_block_comparisons`

Only for approved signals that were blocked or skipped due to capacity.

Suggested columns:

- `decision_id`
- `profile_id`
- `candidate_trade_id`
- `candidate_symbol`
- `candidate_direction`
- `candidate_session_bucket`
- `candidate_timeout_minutes`
- `candidate_dedupe_minutes`
- `candidate_edge_score`
- `incumbent_trade_id`
- `incumbent_ticket`
- `incumbent_symbol`
- `incumbent_direction`
- `incumbent_age_minutes`
- `incumbent_unrealized_pnl`
- `incumbent_timeout_remaining_minutes`
- `comparison_reason`
- `created_at_utc`

This is the table that later tells us whether blocked later signals would have beaten the incumbents that occupied capacity.

### 4. Keep existing timeout tables

Do not replace these:

- `vanguard_forward_checkpoints`
- `vanguard_forward_shell_replays`
- `vanguard_timeout_policy_summary`

They are still needed for:

- baseline signal quality
- timeout quality
- shell overlay research

The new tables are a second analytics layer for automation under capacity constraints.

## Metrics That Matter Once Automation Is On

### Signal quality metrics

Still needed:

- win rate
- avg pips
- profit factor
- MFE / MAE
- realized R
- by session / timeout / dedupe / direction

### Portfolio quality metrics

New required metrics:

- execution rate
- capacity block rate
- skipped-no-capacity count
- missed alpha from blocked signals
- incumbent regret
- slot utilization
- slot efficiency (PnL per slot-hour)
- first-wave crowding rate
- timeout-clear latency

### Most important new questions

After enough unattended data, the key questions become:

1. Did the first 2-4 trades crowd out stronger later signals?
2. Did session-specific timeout logic free slots fast enough?
3. Were skipped signals actually better than incumbents?
4. Is the automated portfolio stronger than the isolated signal replay suggested?

## Recommended Operating Sequence

### Now

- Keep auto-open path available on QA FTMO demo
- Keep timeout auto-close on QA FTMO demo through `lifecycle_daemon`
- Keep prod code in place but effectively inactive by eligible-profile gate

### Next

Implement the three new decision/snapshot tables and start logging every approved candidate under unattended QA runs.

### Only after enough data

Use the combined data to decide:

- whether session timeout policy is actually helping portfolio outcomes
- whether capacity logic needs prioritization/ranking
- whether signal quality is still good once real slot constraints are applied

## Practical Bottom Line

The DWX position manager and timeout-close automation now work end to end on QA FTMO demo.

The biggest remaining gap is no longer "can the trade open/close automatically?"

The biggest remaining gap is:

- "can we measure portfolio-quality truth once auto execution starts filling capacity and blocking later signals?"

That is now the correct next data-layer problem.
