# CODEX — Add Shortlist Picks to Telegram Notifications

## Context
The orchestrator already sends Telegram messages for execution runs.
Add the shortlist picks (equity + forex sections) with scores to each cycle's
Telegram notification so the operator can see picks without querying SQL.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-telegram-shortlist"
```

---

## What to Add

After each cycle completes, send a Telegram message showing the top picks
grouped by asset class and direction.

### Find where Telegram messages are sent:
```bash
grep -n "telegram\|send_message\|notify\|tg_bot\|TELEGRAM" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20
```

### Add a shortlist summary to the Telegram notification:

After V5 selection and before V6, build a shortlist message:

```python
def _build_shortlist_telegram(self, cycle_ts):
    """Build Telegram message showing top picks by asset class."""
    import sqlite3
    con = sqlite3.connect(self.db_path)
    
    rows = con.execute("""
        SELECT symbol, asset_class, direction, predicted_return, edge_score, rank
        FROM vanguard_shortlist_v2
        WHERE cycle_ts_utc = ?
        ORDER BY asset_class, direction, rank
    """, (cycle_ts,)).fetchall()
    con.close()
    
    if not rows:
        return None
    
    # Group by asset class
    from collections import defaultdict
    groups = defaultdict(lambda: defaultdict(list))
    for sym, ac, dir, pred, edge, rank in rows:
        groups[ac][dir].append((sym, pred, edge, rank))
    
    lines = [f"📊 VANGUARD SHORTLIST — {cycle_ts[:16]}"]
    lines.append("")
    
    for ac in sorted(groups.keys()):
        lines.append(f"━━ {ac.upper()} ━━")
        
        # LONGs
        longs = groups[ac].get('LONG', [])
        if longs:
            top5 = longs[:5]
            symbols = ", ".join(f"{s} ({e:.2f})" for s, p, e, r in top5)
            lines.append(f"🟢 LONG ({len(longs)}): {symbols}")
        
        # SHORTs
        shorts = groups[ac].get('SHORT', [])
        if shorts:
            top5 = shorts[:5]
            symbols = ", ".join(f"{s} ({e:.2f})" for s, p, e, r in top5)
            lines.append(f"🔴 SHORT ({len(shorts)}): {symbols}")
        
        lines.append("")
    
    # Add V6 approved summary
    con = sqlite3.connect(self.db_path)
    approved = con.execute("""
        SELECT account_id, COUNT(*)
        FROM vanguard_tradeable_portfolio
        WHERE cycle_ts_utc = ? AND status = 'APPROVED'
        GROUP BY account_id
    """, (cycle_ts,)).fetchall()
    con.close()
    
    if approved:
        lines.append("✅ V6 APPROVED:")
        for acc, count in approved:
            lines.append(f"  {acc}: {count} trades")
    else:
        lines.append("⚠️ V6: 0 approved")
    
    return "\n".join(lines)
```

### Wire it into the cycle:

After V5 completes and before V6, call `_build_shortlist_telegram()` and
send via the existing Telegram bot.

```python
# After V5 selection:
shortlist_msg = self._build_shortlist_telegram(cycle_ts)
if shortlist_msg:
    self._send_telegram(shortlist_msg)
```

### Expected Telegram output:

```
📊 VANGUARD SHORTLIST — 2026-04-02 14:17

━━ EQUITY ━━
🟢 LONG (30): GGLS (0.99), PLTD (0.98), GGB (0.97), MOS (0.96), KGC (0.95)
🔴 SHORT (30): NVD (0.02), CRWG (0.03), TSLG (0.04), IREN (0.05), CRCG (0.06)

━━ FOREX ━━
🟢 LONG (15): EURUSD (0.92), GBPUSD (0.88), AUDUSD (0.85)
🔴 SHORT (15): USDJPY (0.08), USDCHF (0.12), USDCAD (0.15)

✅ V6 APPROVED:
  TTP_20K_SWING: 12 trades
  ftmo_100k: 20 trades
```

---

## Verification

```bash
# Run single cycle and check Telegram
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -20
# Check Telegram for the shortlist message
```

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: Telegram shortlist notifications with scores"
```
