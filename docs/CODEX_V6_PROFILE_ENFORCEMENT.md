# CODEX — V6 Profile Enforcement Pass
# Wire dormant account_profiles fields into the live execution gate
# Based on GPT Stage 6 Gap Analysis
# Date: Mar 31, 2026

---

## READ FIRST

```bash
# 1. Current account_profiles schema
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(account_profiles)"

# 2. Current profile data
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT * FROM account_profiles" | head -20

# 3. Current gate checks in trade_desk.py
grep -n "def.*gate\|def.*check\|def.*validate\|APPROVED\|REJECTED\|reject\|block" \
  ~/SS/Vanguard/vanguard/api/trade_desk.py | head -30

# 4. Current pre-execution gate function
grep -n "pre_execution\|risk_gate\|v6_gate" ~/SS/Vanguard/vanguard/api/trade_desk.py | head -10

# 5. Existing sizing helpers
cat ~/SS/Vanguard/vanguard/helpers/position_sizer.py | head -50

# 6. EOD flatten helpers
cat ~/SS/Vanguard/vanguard/helpers/eod_flatten.py | head -30

# 7. TTP rules helpers
cat ~/SS/Vanguard/vanguard/helpers/ttp_rules.py | head -30
```

Report ALL output.

---

## WHAT TO FIX — 8 ITEMS IN ORDER

### 1. Add environment/mode column to account_profiles

```sql
ALTER TABLE account_profiles ADD COLUMN environment TEXT DEFAULT 'demo';
-- Values: demo, eval, funded, live

-- Update existing profiles
UPDATE account_profiles SET environment = 'demo' WHERE id LIKE 'ttp_%';
UPDATE account_profiles SET environment = 'demo' WHERE id = 'ftmo_100k';
UPDATE account_profiles SET environment = 'demo' WHERE id = 'topstep_100k';
```

### 2. Add instrument_scope column for asset class restriction

```sql
ALTER TABLE account_profiles ADD COLUMN instrument_scope TEXT DEFAULT 'us_equities';
-- Values: us_equities, forex_cfd, futures, all
```

Update profiles:
```sql
UPDATE account_profiles SET instrument_scope = 'us_equities' WHERE id LIKE 'ttp_%';
UPDATE account_profiles SET instrument_scope = 'forex_cfd' WHERE id = 'ftmo_100k';
UPDATE account_profiles SET instrument_scope = 'futures' WHERE id = 'topstep_100k';
```

### 3. Add holding_style column

```sql
ALTER TABLE account_profiles ADD COLUMN holding_style TEXT DEFAULT 'swing';
-- Values: intraday, swing
```

Update profiles:
```sql
UPDATE account_profiles SET holding_style = 'intraday' WHERE id IN ('ttp_50k_intraday', 'ttp_100k_intraday', 'ftmo_100k', 'topstep_100k');
UPDATE account_profiles SET holding_style = 'swing' WHERE id IN ('ttp_20k_swing', 'ttp_40k_swing');
```

### 4. Wire instrument_scope into pre-execution gate

In the gate function in trade_desk.py, add this check BEFORE other checks:

```python
# Check: instrument scope
profile_scope = profile.get("instrument_scope", "all")
if profile_scope == "us_equities" and trade.get("asset_class") not in (None, "equity", "us_equity"):
    reject(f"Profile {profile_id} only allows US equities, got {trade.get('asset_class')}")
elif profile_scope == "forex_cfd" and trade.get("asset_class") not in ("forex", "index", "metal", "energy", "crypto", "commodity"):
    reject(f"Profile {profile_id} only allows forex/CFD instruments")
elif profile_scope == "futures" and trade.get("asset_class") != "futures":
    reject(f"Profile {profile_id} only allows futures instruments")
```

### 5. Wire dormant risk fields into the gate

Check which of these fields exist in account_profiles but are NOT enforced
in the gate. For each one that exists in the schema, add a gate check:

```python
# Weekly loss limit
weekly_limit = profile.get("weekly_loss_limit")
if weekly_limit and weekly_limit > 0:
    weekly_pnl = get_weekly_pnl(profile_id)
    if abs(weekly_pnl) >= weekly_limit * 0.70:
        reject(f"Weekly loss at {abs(weekly_pnl)/weekly_limit*100:.0f}% of ${weekly_limit}")

# Max trades per day
max_trades = profile.get("max_trades_per_day")
if max_trades and max_trades > 0:
    trades_today = get_trades_today(profile_id)
    if trades_today >= max_trades:
        reject(f"Max trades/day: {trades_today} >= {max_trades}")

# Volume limit
volume_limit = profile.get("volume_limit")
if volume_limit and volume_limit > 0:
    volume_today = get_volume_today(profile_id)
    if volume_today >= volume_limit:
        reject(f"Volume limit: {volume_today} >= {volume_limit}")
```

Only add checks for fields that actually exist in the schema.
Do NOT add schema columns that don't already exist — just wire existing ones.

### 6. Wire profile-based sizing

The gate currently checks position size but doesn't resize.
Add sizing enforcement:

```python
# After all checks pass, apply profile-based sizing cap
max_single_pct = profile.get("max_single_position_pct", 0.10)
max_position_value = profile["account_size"] * max_single_pct
trade_value = trade["shares"] * trade["entry_price"]

if trade_value > max_position_value:
    # Resize to fit, don't reject
    new_shares = int(max_position_value / trade["entry_price"])
    trade["original_shares"] = trade["shares"]
    trade["shares"] = new_shares
    trade["resized"] = True
    trade["resize_reason"] = f"Capped from {trade['original_shares']} to {new_shares} shares (max {max_single_pct*100}% of account)"
```

### 7. Add audit metadata to gate response

Every approved/rejected trade should include:

```python
{
    "profile_id": profile_id,
    "profile_name": profile["name"],
    "environment": profile.get("environment", "unknown"),
    "instrument_scope": profile.get("instrument_scope", "all"),
    "holding_style": profile.get("holding_style", "unknown"),
    "rules_fired": rules_fired,  # list of check names that triggered
    "resized": trade.get("resized", False),
    "resize_reason": trade.get("resize_reason"),
}
```

Add these to both the `approved[]` and `rejected[]` entries in the response.

### 8. Block execute without valid active profile

In the execute endpoint, BEFORE any gate logic:

```python
if not profile_id or profile_id == "default":
    return {"error": "No active profile selected. Select a profile before executing.", "approved": [], "rejected": all_trades}

profile = load_profile(profile_id)
if not profile:
    return {"error": f"Profile {profile_id} not found", "approved": [], "rejected": all_trades}
if not profile.get("is_active"):
    return {"error": f"Profile {profile_id} is inactive", "approved": [], "rejected": all_trades}
```

---

## VERIFY

```bash
# Compile check
python3 -m py_compile ~/SS/Vanguard/vanguard/api/trade_desk.py

# Check new columns exist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, name, environment, instrument_scope, holding_style FROM account_profiles
"

# Test: TopStep profile should reject equity trade
curl -s -X POST http://localhost:8090/api/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"profile_id":"topstep_100k","preview_only":true,"trades":[{"symbol":"AAPL","direction":"LONG","shares":100,"tier":"test","entry_price":185,"stop_loss":180,"take_profit":195}]}' | python3 -m json.tool
# Should show REJECTED with "only allows futures instruments"

# Test: TTP profile should accept equity trade
curl -s -X POST http://localhost:8090/api/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"profile_id":"ttp_40k_swing","preview_only":true,"trades":[{"symbol":"AAPL","direction":"LONG","shares":100,"tier":"test","entry_price":185,"stop_loss":180,"take_profit":195}]}' | python3 -m json.tool
# Should show APPROVED

# Test: No profile should block
curl -s -X POST http://localhost:8090/api/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{"trades":[{"symbol":"AAPL","direction":"LONG","shares":100}]}' | python3 -m json.tool
# Should show error "No active profile selected"
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: V6 profile enforcement — environment, instrument scope, holding style, dormant field wiring"
```
