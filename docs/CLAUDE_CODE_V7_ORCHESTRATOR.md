# CLAUDE CODE — Build V7 Orchestrator
# Date: Mar 31, 2026
# Spec: VANGUARD_STAGE_V7_SPEC.md in project knowledge
# Depends on: V1-V6 all built and tested, run_single_cycle.py proven
# CRITICAL CONTEXT: Read this entire prompt before writing any code

---

## READ FIRST

```bash
# 1. Full V7 spec
find ~/SS -name "VANGUARD_STAGE_V7_SPEC.md" 2>/dev/null
# Read it if found, otherwise check project knowledge

# 2. The working single-cycle runner (V7 wraps this into a loop)
cat ~/SS/Vanguard/scripts/run_single_cycle.py

# 3. Current V7 file (should be empty)
cat ~/SS/Vanguard/stages/vanguard_orchestrator.py

# 4. Existing execution infrastructure
ls ~/SS/Vanguard/vanguard/execution/
cat ~/SS/Vanguard/vanguard/execution/signalstack_adapter.py | head -80
cat ~/SS/Vanguard/vanguard/execution/bridge.py | head -50

# 5. Current account profiles
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, name, must_close_eod, is_active FROM account_profiles
"

# 6. What clock/session helpers exist
ls ~/SS/Vanguard/vanguard/helpers/
grep -rn "session\|market_hours\|is_open\|ET\|timezone" ~/SS/Vanguard/vanguard/helpers/*.py 2>/dev/null | head -20

# 7. What V6 EOD flatten looks like
grep -n "eod\|flatten\|close_eod\|no_new" ~/SS/Vanguard/vanguard/helpers/eod_flatten.py 2>/dev/null | head -20

# 8. Telegram bot (if exists)
find ~/SS -name "telegram_alerts.py" -o -name "telegram*.py" 2>/dev/null
grep -rn "TELEGRAM\|telegram\|bot_token" ~/SS/Vanguard/config/ 2>/dev/null

# 9. Check the pre-execution gate (V6 enhancement, already built)
grep -n "pre_execution\|risk_gate\|preview_only" ~/SS/Vanguard/vanguard/api/trade_desk.py | head -10
```

Report ALL output before writing code.

---

## WHAT TO BUILD

`stages/vanguard_orchestrator.py` — the session lifecycle loop that runs
V1→V6 every 5 minutes during market hours and optionally executes trades.

---

## ARCHITECTURE

```
vanguard_orchestrator.py
│
├── Session Lifecycle
│   ├── 09:25 ET  Warm start (bootstrap bars, init portfolio state)
│   ├── 09:30 ET  Market open (wait — accumulate warm-up bars)
│   ├── 09:35 ET  First full cycle
│   ├── Every 5 min: V1→V2→V3→V4B→V5→V6→Execute
│   ├── 15:30 ET  No new positions (V6 time gate enforces)
│   ├── 15:50 ET  EOD flatten intraday accounts
│   ├── 16:00 ET  Session close + daily summary
│   └── 16:05 ET  Forward tracking snapshot + exit
│
├── Per-Cycle Pipeline
│   ├── V1: Ingest latest 5m bars (use existing vanguard_cache)
│   ├── V2: Refresh health checks (use existing vanguard_prefilter)
│   ├── V3: Compute features (use existing vanguard_factor_engine)
│   ├── V4B: Score survivors (use existing model predictions)
│   ├── V5: Strategy router + consensus (use existing vanguard_selection)
│   ├── V6: Risk filters per account (use existing vanguard_risk_filters)
│   └── Execute: Forward-track or send to broker (per account mode)
│
└── Execution Bridge
    ├── Mode: off (default) → write to DB + Telegram only
    ├── Mode: paper → send to demo account via SignalStack
    ├── Mode: live → send to funded account via SignalStack
    └── Writes to: execution_log (CANONICAL table, NOT vanguard_execution_log)
```

---

## CRITICAL RULES FROM THIS SESSION

1. **Canonical execution path is `unified_api + trade_desk + execution_log`.**
   V7's execution bridge MUST write to `execution_log`, NOT `vanguard_execution_log`.
   Use the same `signalstack_adapter.send_order()` that trade_desk.py uses.

2. **TTP actions: buy, sell, cancel, close.** NOT sell_short or buy_to_cover.
   The adapter already handles this with `broker="ttp"`.

3. **Pre-execution gate runs before any webhook.** Call the same gate function
   that POST /api/v1/execute uses (in trade_desk.py). Don't duplicate risk logic.

4. **Default execution mode: OFF.** Forward-track only until operator explicitly
   enables paper/live mode. This is a safety requirement.

5. **ML probs are currently 0.17-0.31** (below 0.50 gate). V5 will produce
   0 candidates in normal mode. The orchestrator should handle this gracefully
   (log "no candidates above ML gate" and continue to next cycle, don't crash).

6. **IBKR not available until April 8.** V1 uses Alpaca IEX data. V7 must work
   with current data source. IBKR adapter integration is a separate task.

---

## CORE IMPLEMENTATION

```python
#!/usr/bin/env python3
"""
Vanguard Orchestrator — V7
Session lifecycle loop: V1→V6 every 5 min during market hours.

Usage:
    python3 stages/vanguard_orchestrator.py                    # Full session
    python3 stages/vanguard_orchestrator.py --single-cycle     # One cycle only
    python3 stages/vanguard_orchestrator.py --dry-run           # No DB writes, no execution
    python3 stages/vanguard_orchestrator.py --execution-mode paper  # Paper trading
    python3 stages/vanguard_orchestrator.py --status            # Show current state
    python3 stages/vanguard_orchestrator.py --flatten-now       # Force EOD flatten
"""
```

### Key functions to implement:

```python
class VanguardOrchestrator:
    def __init__(self, config, execution_mode="off", dry_run=False):
        self.config = config
        self.execution_mode = execution_mode  # off / paper / live
        self.dry_run = dry_run
        self.cycle_count = 0
        self.failed_cycles = 0
        self.session_start = None

    def run_session(self):
        """Full session lifecycle: startup → main loop → wind-down."""
        self.startup()
        self.main_loop()
        self.wind_down()

    def startup(self):
        """09:25 ET — Bootstrap today's session."""
        # 1. Check market calendar (is today a trading day?)
        # 2. Initialize portfolio state for each active account
        # 3. Calculate daily pause levels for TTP accounts
        # 4. Verify data connections
        # 5. Wait until 09:35 ET for first cycle

    def main_loop(self):
        """09:35-15:25 ET — Run V1→V6 every 5 minutes."""
        while self.is_within_session():
            cycle_start = time.time()
            self.run_cycle()
            self.wait_for_next_bar()

    def run_cycle(self):
        """Single V1→V6 pipeline execution."""
        cycle_ts = datetime.now(timezone.utc).isoformat()

        try:
            # V1: bars (existing data, no new fetch needed if cache is warm)
            # V2: health check
            survivors = self.run_v2()
            if not survivors:
                self.log("No survivors, skipping cycle")
                return

            # V3: features
            features = self.run_v3(survivors)

            # V4B: scoring
            predictions = self.run_v4b(features)

            # V5: strategy router
            shortlist = self.run_v5(features, predictions, cycle_ts)
            if shortlist.empty:
                self.log("V5 produced 0 candidates (all below ML gate)")
                return  # Don't crash, just skip

            # V6: risk filters
            portfolios = self.run_v6(shortlist, cycle_ts)

            # Execute (if enabled)
            self.execute(portfolios, cycle_ts)

            self.cycle_count += 1

        except Exception as e:
            self.failed_cycles += 1
            self.log(f"Cycle FAILED: {e}")
            if self.failed_cycles >= 3:
                self.log("3+ consecutive failures — aborting session")
                raise

    def execute(self, portfolios, cycle_ts):
        """Execute approved trades or forward-track."""
        for account_id, trades in portfolios.items():
            approved = [t for t in trades if t.get("status") == "APPROVED"]
            if not approved:
                continue

            if self.execution_mode == "off":
                self.forward_track(account_id, approved, cycle_ts)
            elif self.execution_mode in ("paper", "live"):
                self.send_to_broker(account_id, approved, cycle_ts)

    def forward_track(self, account_id, trades, cycle_ts):
        """Write picks to DB + Telegram without sending to broker."""
        # Write to execution_log with status="FORWARD_TRACKED"
        # Send Telegram alert with picks

    def send_to_broker(self, account_id, trades, cycle_ts):
        """Send approved trades through pre-execution gate → SignalStack."""
        # Use the SAME signalstack_adapter that trade_desk.py uses
        # Write to execution_log (canonical table)
        # Send Telegram alert with fill results

    def wind_down(self):
        """15:30-16:05 ET — Session close."""
        # 15:50: EOD flatten for intraday accounts (must_close_eod=True)
        # 16:00: Write session summary to vanguard_session_log
        # 16:05: Snapshot forward tracking results

    def is_within_session(self):
        """Check if current time is within session hours."""
        # Use pytz or zoneinfo for ET conversion
        # Session: 09:35-15:25 ET on weekdays

    def wait_for_next_bar(self):
        """Sleep until next 5-minute boundary."""
        # Calculate next bar time (aligned to :00, :05, :10, etc.)
        # Add 10-second buffer for bar completion
```

---

## SESSION LOG TABLE

```sql
CREATE TABLE IF NOT EXISTS vanguard_session_log (
    date TEXT PRIMARY KEY,
    session_start_utc TEXT,
    session_end_utc TEXT,
    total_cycles INTEGER DEFAULT 0,
    successful_cycles INTEGER DEFAULT 0,
    failed_cycles INTEGER DEFAULT 0,
    trades_executed INTEGER DEFAULT 0,
    trades_forward_tracked INTEGER DEFAULT 0,
    regime_summary TEXT,
    daily_pnl_by_account TEXT,
    errors TEXT,
    status TEXT DEFAULT 'ACTIVE'
);
```

---

## TELEGRAM ALERTS

If Telegram env vars exist (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), send alerts:

| Event | Message |
|---|---|
| Session start | "🟢 Vanguard session started. 6 accounts active." |
| Cycle complete | "📊 Cycle 14: 3 approved, 2 rejected. Regime: ACTIVE" |
| Trade executed | "🔔 LONG AAPL 100 shares @ $185.50 (paper)" |
| Forward tracked | "📝 Forward: LONG AAPL 100 @ $185.50 (not executed)" |
| V5 empty | "⚠️ Cycle 14: 0 candidates above ML gate" |
| Cycle failed | "🔴 Cycle 14 FAILED: {error}" |
| EOD flatten | "📤 EOD flatten: closed 3 positions for ttp_50k_intraday" |
| Session end | "🏁 Session complete. 72 cycles, 12 trades, 0 errors" |

Use existing Telegram bot code if available, otherwise simple requests.post.

---

## CONFIG

```json
{
    "orchestrator": {
        "bar_interval": "5Min",
        "session_start_et": "09:35",
        "session_end_et": "15:25",
        "no_new_positions_after_et": "15:30",
        "eod_flatten_time_et": "15:50",
        "session_close_et": "16:00",
        "max_consecutive_failures": 3,
        "cycle_timeout_seconds": 120,
        "bar_completion_buffer_seconds": 10
    },
    "execution": {
        "default_mode": "off",
        "require_confirmation_for_live": true
    },
    "telegram": {
        "enabled": true,
        "bot_token": "ENV:TELEGRAM_BOT_TOKEN",
        "chat_id": "ENV:TELEGRAM_CHAT_ID"
    }
}
```

---

## TESTS

```
tests/test_vanguard_orchestrator.py:
1. Session start/stop lifecycle
2. is_within_session() correctly detects market hours
3. wait_for_next_bar() calculates correct sleep time
4. run_cycle() handles 0 survivors gracefully
5. run_cycle() handles 0 V5 candidates gracefully (ML gate)
6. run_cycle() handles V6 all-rejected gracefully
7. execute() in "off" mode writes FORWARD_TRACKED to execution_log
8. execute() in "paper" mode calls signalstack_adapter
9. wind_down() triggers EOD flatten for intraday accounts only
10. wind_down() skips flatten for swing accounts (must_close_eod=False)
11. 3 consecutive failures trigger session abort
12. Session log written to vanguard_session_log
13. Telegram alerts sent (mock)
14. Config-driven timing (not hardcoded)
```

---

## VERIFY

```bash
# Compile check
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_orchestrator.py

# Run tests
cd ~/SS/Vanguard && python3 -m pytest tests/test_vanguard_orchestrator.py -v

# Single cycle (uses existing data)
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --dry-run

# Status
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --status
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: V7 Orchestrator — session lifecycle loop with execution bridge"
```
