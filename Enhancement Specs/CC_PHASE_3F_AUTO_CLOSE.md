# CC Phase 3f — Auto-Close at Max Holding Minutes (Real Broker Close Action)

**Target env:** `Vanguard_QAenv`
**Est time:** 45 min – 1 hour
**Depends on:** Phase 3e PASS (daemon running clean for ≥ 10 min)
**Risk level:** HIGH — this slice **closes real positions at the broker**. Everything before it only read or wrote DB.
**Prod-path touches:** NONE

---

## 0. Why this slice exists alone

Every prior slice was safe: read, write, log, detect, update DB. This slice adds the one dangerous capability: **sending a close command to MetaApi** when a position exceeds `max_holding_minutes`. This is the first slice where the system acts on the real-money broker account.

Safety through isolation: the daemon from 3e can run for weeks without ever needing 3f. We only add this once 3e is proven stable.

---

## 1. Hard rules

1. QAenv only. Uses **paper/demo MetaApi account**. Never the live GFT 5k/10k.
2. **Both Telegram notifications fire: initiated AND success (or failed).** Not one or the other. If Telegram fails to send, close still proceeds but logs a warning.
3. **Close attempt is idempotent.** If a close is already in-flight (tracked via in-memory lock), skip re-issuing.
4. **If close fails, send FAILED alert with enough info for Shan to manually close.** Never silent failure.
5. **Pre-close sanity check.** Before calling `close_position()`, verify: (a) the position is still actually open per latest broker state, (b) holding_minutes still exceeds threshold, (c) policy hasn't been changed to `auto_close_after_max_holding=false`.
6. **Bounded rate.** Max 3 auto-closes per profile per minute (circuit breaker in case of buggy holding calculation).
7. Circuit breaker: if 3 consecutive auto-close attempts fail across any profile, **pause all auto-close** and alert Shan.

---

## 2. What to build

### 2.1 File: `Vanguard_QAenv/vanguard/execution/auto_close.py` (new)

```python
class AutoCloseBreakerTripped(Exception): pass

class AutoCloseManager:
    def __init__(self, config, db_path, telegram_client, metaapi_clients):
        self.config = config
        self.db_path = db_path
        self.telegram = telegram_client
        self.clients = metaapi_clients
        self._in_flight = set()         # (profile_id, broker_position_id) currently closing
        self._recent_closes = {}        # profile_id -> list of close timestamps (for rate limit)
        self._consecutive_failures = 0
        self._breaker_tripped = False

    async def check_and_close(self, profile, policy, broker_positions, now_utc):
        """
        For each broker_position, check if it exceeds max_holding and close if so.
        Returns list of (broker_position_id, result) tuples.
        """
        if self._breaker_tripped:
            return []

        if not policy.get("position_limits", {}).get("auto_close_after_max_holding", False):
            return []

        max_minutes = policy["position_limits"]["max_holding_minutes"]
        results = []

        for bp in broker_positions:
            holding = self._holding_minutes(bp["opened_at_utc"], now_utc)
            if holding < max_minutes:
                continue

            key = (profile["id"], bp["broker_position_id"])
            if key in self._in_flight:
                continue

            if not self._rate_limit_ok(profile["id"], now_utc):
                log_warning(f"auto_close rate limit hit for {profile['id']}")
                continue

            self._in_flight.add(key)
            try:
                result = await self._close_one(profile, bp, holding)
                results.append((bp["broker_position_id"], result))
            finally:
                self._in_flight.discard(key)

        return results

    async def _close_one(self, profile, broker_position, holding_minutes):
        # 1. Pre-close sanity check
        current_positions = await self.clients[profile["id"]].get_open_positions()
        still_open = any(p["broker_position_id"] == broker_position["broker_position_id"] for p in current_positions)
        if not still_open:
            return {"status": "SKIPPED_ALREADY_CLOSED"}

        # 2. Send initiated Telegram
        self._send_telegram_safe(
            f"🔔 LIFECYCLE_AUTO_CLOSE_INITIATED\n"
            f"profile={profile['id']} symbol={broker_position['symbol']} "
            f"held={holding_minutes}m max={self._max_minutes(profile)}m"
        )

        # 3. Submit close
        try:
            close_result = await self.clients[profile["id"]].close_position(broker_position["broker_position_id"])
        except Exception as e:
            self._consecutive_failures += 1
            self._send_telegram_safe(
                f"🚨 LIFECYCLE_AUTO_CLOSE_FAILED\n"
                f"profile={profile['id']} symbol={broker_position['symbol']}\n"
                f"error={str(e)[:200]}\n"
                f"MANUAL CLOSE REQUIRED"
            )
            if self._consecutive_failures >= 3:
                self._breaker_tripped = True
                self._send_telegram_safe("🔥 AUTO_CLOSE_CIRCUIT_BREAKER_TRIPPED — all auto-closes paused")
            raise

        self._consecutive_failures = 0  # reset on success
        self._record_close_time(profile["id"])

        # 4. Update journal
        update_journal_closed_from_auto(
            self.db_path, broker_position["broker_position_id"],
            close_price=close_result["close_price"],
            closed_at_utc=close_result["close_time"],
            close_reason="AUTO_CLOSED_MAX_HOLD",
            realized_pnl=close_result.get("realized_pnl"),
            holding_minutes=holding_minutes
        )

        # 5. Send success Telegram
        self._send_telegram_safe(
            f"✅ LIFECYCLE_AUTO_CLOSE_SUCCESS\n"
            f"profile={profile['id']} symbol={broker_position['symbol']}\n"
            f"close_price={close_result['close_price']} pnl={close_result.get('realized_pnl')}"
        )

        return {"status": "CLOSED", **close_result}

    def _send_telegram_safe(self, msg):
        try:
            self.telegram.send(msg)
        except Exception as e:
            log_warning(f"Telegram send failed: {e}. Msg was: {msg}")
```

### 2.2 Wire into lifecycle daemon

In `LifecycleDaemon._sync_profile()` from 3e, after reconciliation:

```python
# 7. Auto-close check (NEW in 3f)
policy = get_policy_for_profile(self.config, profile)
await self.auto_close_manager.check_and_close(profile, policy, broker_positions, now_utc)
```

### 2.3 Update journal helper

```python
def update_journal_closed_from_auto(db_path, broker_position_id, close_price, closed_at_utc,
                                    close_reason, realized_pnl, holding_minutes):
    """
    Finds journal row by broker_position_id where status='OPEN'.
    Updates: status=CLOSED, close_price, closed_at_utc, close_reason, realized_pnl, holding_minutes.
    Also computes realized_rrr = (abs(close - fill) / abs(expected_sl - fill)) * sign.
    If no matching OPEN row found, log warning (could be a 'recon_' row — still update it).
    """
```

### 2.4 Manual unbreaker CLI

`Vanguard_QAenv/scripts/reset_autoclose_breaker.sh`:
```bash
#!/bin/bash
# Resets the consecutive-failure counter. Run after Shan has investigated failures.
# Does this by sending SIGUSR1 to daemon, or by writing a flag file the daemon reads.
touch /tmp/autoclose_breaker_reset
echo "Breaker reset flag written. Daemon will pick up within 1 iteration."
```

Daemon reads `/tmp/autoclose_breaker_reset` at top of each iteration, resets breaker + removes file if present.

---

## 3. Acceptance tests

All tests use the paper MetaApi account. **Do not run against live accounts.**

### Test 1 — Below threshold: no close
Set policy `max_holding_minutes=240`. Place test trade. After 2 minutes (far below threshold), verify daemon does NOT close.
**Expect:** position still open, no auto-close Telegram messages.

### Test 2 — Above threshold: close fires
Set policy `max_holding_minutes=3`. Place test trade. Wait 4 minutes.
**Expect:**
- Telegram receives LIFECYCLE_AUTO_CLOSE_INITIATED
- Broker position closed
- Telegram receives LIFECYCLE_AUTO_CLOSE_SUCCESS
- Journal row status=CLOSED, close_reason=AUTO_CLOSED_MAX_HOLD, realized_pnl populated
- vanguard_open_positions row removed on next mirror update

### Test 3 — auto_close_after_max_holding=false disables
Set `auto_close_after_max_holding=false`, `max_holding_minutes=3`. Place trade, wait 4 min.
**Expect:** no close, no Telegram. Trade still open.

### Test 4 — Pre-close sanity check: manually closed during window
Set max_holding=3. Place trade. After 3 min 10s, manually close in MT5 BEFORE daemon's next iteration.
**Expect:** daemon detects position already gone, returns SKIPPED_ALREADY_CLOSED, no Telegram initiated message sent (or it sends initiated then catches the already-closed on second check — acceptable either way, just no double-close attempt).

### Test 5 — Close failure triggers FAILED alert
Temporarily break close_position() to always raise. Place a trade that exceeds max_hold.
**Expect:**
- LIFECYCLE_AUTO_CLOSE_INITIATED sent
- LIFECYCLE_AUTO_CLOSE_FAILED sent with error details
- Journal row NOT updated to CLOSED (still OPEN)
- Consecutive failure counter = 1

Restore close_position() implementation.

### Test 6 — Circuit breaker trips after 3 failures
Break close_position() again. Manually force 3 positions over max_hold.
**Expect:** 3 FAILED alerts, then AUTO_CLOSE_CIRCUIT_BREAKER_TRIPPED alert, then no further close attempts.
Run `scripts/reset_autoclose_breaker.sh`. Verify breaker clears on next iteration.

### Test 7 — Rate limit
Set max_holding=1. Force 5 positions to exceed simultaneously.
**Expect:** at most 3 closes in the first 60s, rest deferred or blocked by rate limit warnings in log.

### Test 8 — Idempotency via in-flight lock
Mock a slow close (add artificial delay). Confirm second call to `check_and_close` during that window does NOT re-issue close for same position.

### Test 9 — realized_rrr correctness
Place test trade: entry=100, sl=99, tp=102. Auto-close at 101.5.
**Expect:** realized_rrr = |101.5-100| / |99-100| = 1.5 with sign = +1 (profitable LONG).
Place SHORT trade: entry=100, sl=101, tp=98. Auto-close at 98.5.
**Expect:** realized_rrr = |98.5-100| / |101-100| = 1.5 with sign = +1.

### Test 10 — Telegram failure doesn't block close
Break Telegram webhook. Trigger auto-close.
**Expect:** warning logged about Telegram failure, but close still proceeds, journal still updates.
Restore Telegram.

### Test 11 — Reconciled (unknown) position still auto-closes
Manually open position in MT5 (creates `recon_*` journal row via 3d). Wait > max_hold.
**Expect:** daemon still auto-closes it, journal `recon_*` row updated to CLOSED.

---

## 4. Report-back criteria

CC produces:
1. `auto_close.py`, updated daemon, breaker reset script
2. Test 1–11 output verbatim
3. Telegram log screenshots or text captures for Tests 2, 5, 6
4. Journal rows before/after Test 2
5. Confirmation: breaker reset mechanism works

---

## 5. Stop-the-line triggers

- **Any close fires on the wrong position** (wrong symbol, wrong profile_id) → STOP IMMEDIATELY, kill daemon, investigate
- Telegram initiated message fires but no close attempt follows
- Close succeeds at broker but journal not updated (orphan close)
- Circuit breaker doesn't trip after 3 failures
- Rate limit not enforced
- Position with `auto_close_after_max_holding=false` still closes

---

## 6. Post-deploy watchdog (first 24 hours live)

After promoting this to prod, for the first 24 hours Shan should:
- Monitor every LIFECYCLE_AUTO_CLOSE_* Telegram
- Verify each close was the correct position at the correct time
- Spot-check journal realized_pnl matches broker deal history
- If anything looks wrong, run `scripts/stop_lifecycle_daemon.sh` immediately

This is the most dangerous code in the system. Trust but verify.
