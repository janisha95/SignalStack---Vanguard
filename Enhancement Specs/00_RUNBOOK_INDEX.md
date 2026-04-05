# CC Execution Runbook — Phases 0 → 6 + Sprint 2

**Total scope:** 6–8 hour CC session tonight (Phases 0 → 6), then Sprint 2 later this week.
**Target env:** `Vanguard_QAenv` for everything tonight. Prod only touched in Phase 6 canary.

---

## Execution order (strict)

| # | Spec | Est time | Risk | Gate to pass |
|---|---|---|---|---|
| 1 | `CC_PHASE_0_PARITY_HARNESS.md` | 30–45 min | LOW | QA matches Prod byte-for-byte on full pipeline |
| 2 | `CC_PHASE_2A_UNIVERSE_RESOLVER.md` | 1.5–2 hr | LOW | Resolved universe works in observe + enforce mode |
| 3 | `CC_PHASE_2B_JSON_POLICY.md` | 2–2.5 hr | MED | V6 is pure JSON interpreter, no hardcoded GFT constants |
| 4a | `CC_PHASE_3A_METAAPI_READ.md` | 30–45 min | LOW | MetaApi read works, outage handled safely |
| 4b | `CC_PHASE_3B_TRADE_JOURNAL.md` | 30–45 min | MED | Journal + slippage wired into execution path |
| 4c | `CC_PHASE_3C_RECONCILE_DETECT.md` | 30–45 min | LOW | Divergences detected, zero state mutation |
| 4d | `CC_PHASE_3D_RECONCILE_ACT.md` | 45 min – 1 hr | MED | DB updates reflect broker truth, no broker writes |
| 4e | `CC_PHASE_3E_LIFECYCLE_DAEMON.md` | 30–45 min | MED | Daemon loop runs 10+ min clean, no auto-close yet |
| 4f | `CC_PHASE_3F_AUTO_CLOSE.md` | 45 min – 1 hr | **HIGH** | Real broker close works, circuit breaker active |
| 5 | `CC_PHASE_4_MODULARIZE.md` | 45 min – 1 hr | LOW | Golden parity test passes, zero behavior change |
| 6 | `CC_PHASE_5_API.md` | 45 min – 1 hr | LOW | Config API + shadow exec log ready for UI |
| 7 | `CC_PHASE_6_PROMOTION.md` | 30–45 min | HIGH | Canary gft_10k in prod, then expand |
| 8 | `SPRINT_2_MTF_SPEC.md` | 8–10 days | LOW | MTF crypto + forex + equity models shipped |

**Phase 3 sliced into 6 parts** (3a → 3f) so each risky piece can be individually verified before the next lands. Total Phase 3 budget unchanged (~3 hrs), but the riskiest piece (3f auto-close) is isolated + gated by 5 prior passes.

**Hard budget:** ~8 hours for phases 0–6. If any phase runs over by > 50%, stop and triage. Phases 4, 5, 6 can slip to tomorrow if needed — 0, 2a, 2b, 3a-3e are the MUST-haves. **3f (auto-close) can defer to Day 2** if stability of 3a-3e isn't rock solid yet.

---

## Universal rules across all specs

1. **QAenv only**, every phase. Prod is touched only in Phase 6 Step 1 (backup) and Step 3 (rsync).
2. **No dead code**. Every feature wires into a code path that runs. If it doesn't, phase is incomplete.
3. **`grep -r "/SS/Vanguard/data" Vanguard_QAenv/` must return zero matches** after every phase.
4. **Report-back discipline**: each phase ends with CC producing explicit test outputs + file change list. No "done, trust me."
5. **Config-version everywhere**: every cycle logs which config_version ran. No mystery state.
6. **Stop-the-line triggers exist in every phase**. Hit one → CC stops, reports, waits for Shan.

---

## Before starting — setup

```bash
# 1. Confirm QAenv exists and is writable
ls -la /Users/sjani008/SS/Vanguard_QAenv/
sqlite3 /Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db "SELECT COUNT(*) FROM sqlite_master;"

# 2. Confirm Prod is running untouched
sqlite3 /Users/sjani008/SS/Vanguard/data/vanguard_universe.db "SELECT MAX(cycle_ts_utc) FROM vanguard_resolved_universe_log;" 2>/dev/null || echo "No resolved_universe_log yet (expected if not yet deployed)"

# 3. Create QA snapshot directory
mkdir -p /tmp/parity_snapshots

# 4. Git branch for tonight's work
cd /Users/sjani008/SS/Vanguard_QAenv && git checkout -b phase-0-through-6-$(date +%Y%m%d)
```

---

## How to hand each spec to Claude Code

For each spec, feed this to CC:

> Read `CC_PHASE_{N}_{NAME}.md` in full. Implement exactly as described. 
> Run every acceptance test in §3 (or §4/§5 depending on spec). 
> Produce the report-back artifacts listed in that spec.
> If you hit a stop-the-line trigger, stop and report. Do not improvise.
> Work only in `/Users/sjani008/SS/Vanguard_QAenv/`. Zero writes to `/Users/sjani008/SS/Vanguard/`.

CC must paste the test outputs verbatim when reporting done. Shan reviews before moving to next phase.

---

## Between phases — Shan's review checklist

- [ ] All acceptance tests in the spec passed?
- [ ] Report-back artifacts present and complete?
- [ ] `grep -r "/SS/Vanguard/data" Vanguard_QAenv/` returns 0 matches?
- [ ] `git diff --stat` looks reasonable (no mystery mass changes)?
- [ ] Orchestrator still starts cleanly?

If all green → commit with message `phase N complete: <spec name>` → proceed. If any red → roll back the phase, diagnose.

---

## Emergency rollback (any phase)

```bash
cd /Users/sjani008/SS/Vanguard_QAenv
git stash  # or git reset --hard HEAD~1 if committed
# verify orchestrator still starts
python3 stages/vanguard_orchestrator.py --dry-run --single-cycle
```

If Phase 6 canary goes sideways: `bash Vanguard/scripts/rollback_phase6.sh /tmp/prod_pre_phase6_backup_*.db`.

---

## Phases that could be deferred if time runs out

Priority order (MUST → nice-to-have):
1. **Phase 0** — MUST. Without parity, building on QA is building on sand.
2. **Phase 2a** — MUST. Stops universe spam.
3. **Phase 2b** — MUST. Fixes crypto spread + makes V6 config-driven.
4. **Phase 3a + 3b** — MUST. MetaApi read + trade journal. Foundation for everything lifecycle.
5. **Phase 3c + 3d** — MUST. Reconciliation. Without this, state can silently drift.
6. **Phase 3e** — MUST. Daemon. Without this, reconciliation is manual only.
7. **Phase 3f** — CAN DEFER 1 day. Auto-close is dangerous. Only ship after 3e has run clean for 12+ hours.
8. **Phase 4** — Can slip 1 day. Pure refactor.
9. **Phase 5** — Can slip 1 day. UI prep.
10. **Phase 6** — Requires 3e (minimum) + Phase 5 done. Can happen Day 2.

**Sprint 2 MTF** is a separate week of work, started after Phases 0–6 are in prod.

---

## The fix that unlocks money-printing

After Phase 6, you can:
- Run GFT 10k live with proper JSON policy + crypto spread-aware sizing
- Expand to GFT 5k
- Enroll TTP 10k Swing with equity universe
- Trust auto-close prevents 21-hour positions
- See every approval/rejection reasoning in the trade_journal
- Flip `allow_long: false` in JSON and have crypto longs blocked in 60 seconds

After Sprint 2, you can:
- Trust crypto signals because 5m is anchored by 15m/30m structure
- Stop bleeding on AAVE-style 4× flips per cycle
- Enter the path to full MTF + Volume Profile + Daily conviction integration

---

## Signal hai, stack ho jayega 🚀
