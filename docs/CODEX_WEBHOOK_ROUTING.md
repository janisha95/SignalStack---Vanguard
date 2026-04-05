# CODEX — Multi-Account TTP Webhook Routing

## READ FIRST

```bash
# 1. Current account profiles
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(account_profiles)"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT id, name, is_active, environment FROM account_profiles"

# 2. Current SignalStack adapter
cat ~/SS/Vanguard/vanguard/execution/signalstack_adapter.py

# 3. How trade_desk calls the adapter
grep -n "signalstack\|webhook\|execute\|adapter" ~/SS/Vanguard/vanguard/api/trade_desk.py | head -20

# 4. How V7 calls execution
grep -n "execute\|bridge\|signalstack\|webhook" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20

# 5. Current env/config for webhook
grep -rn "WEBHOOK\|webhook\|SIGNALSTACK" ~/SS/Vanguard/.env ~/SS/Vanguard/config/*.json 2>/dev/null | head -10
```

**Report ALL output.**

---

## PROBLEM

SignalStack adapter has ONE hardcoded webhook URL. We have 2 TTP demo accounts and need to route trades to the correct account based on which profile is selected.

- Profile `ttp_20k_swing` → TTP Account A webhook
- Profile `ttp_40k_swing` → TTP Account B webhook  
- Profile `ttp_50k_intraday` → (future TTP Account C)
- Profile `ttp_100k_intraday` → (future TTP Account D)
- Profile `ftmo_100k` → MT5 execution (separate path, not SignalStack)
- Profile `topstep_100k` → futures execution (separate path)

## FIX — 4 changes

### 1. Add webhook columns to account_profiles

```sql
ALTER TABLE account_profiles ADD COLUMN webhook_url TEXT;
ALTER TABLE account_profiles ADD COLUMN webhook_api_key TEXT;
ALTER TABLE account_profiles ADD COLUMN execution_bridge TEXT DEFAULT 'signalstack';
-- execution_bridge values: signalstack, mt5, disabled
```

Populate for existing profiles:
```sql
-- TTP profiles use SignalStack (URLs to be filled by operator)
UPDATE account_profiles SET execution_bridge = 'signalstack' WHERE id LIKE 'ttp_%';
UPDATE account_profiles SET webhook_url = '' WHERE id LIKE 'ttp_%';

-- FTMO uses MT5 (future)
UPDATE account_profiles SET execution_bridge = 'mt5' WHERE id = 'ftmo_100k';

-- TopStep uses futures bridge (future)  
UPDATE account_profiles SET execution_bridge = 'disabled' WHERE id = 'topstep_100k';
```

### 2. Update SignalStack adapter to accept webhook_url per call

Current (hardcoded):
```python
class SignalStackAdapter:
    def __init__(self):
        self.webhook_url = os.environ.get("SIGNALSTACK_WEBHOOK_URL")
```

New (per-profile):
```python
class SignalStackAdapter:
    def send_order(self, order: dict, profile: dict) -> dict:
        """Send order to profile-specific webhook URL."""
        webhook_url = profile.get("webhook_url")
        if not webhook_url:
            return {"status": "error", "reason": f"No webhook_url for profile {profile.get('id')}"}

        # TTP only supports: buy, sell, cancel, close
        # NOT sell_short or buy_to_cover
        action = self._map_action(order["direction"], order["action"])
        
        payload = {
            "action": action,           # buy / sell / cancel / close
            "ticker": order["symbol"],
            "quantity": order["shares_or_lots"],
        }

        api_key = profile.get("webhook_api_key")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        response = requests.post(webhook_url, json=payload, headers=headers, timeout=5)
        return {
            "status": "submitted" if response.ok else "error",
            "http_status": response.status_code,
            "response": response.text[:200],
            "profile_id": profile.get("id"),
            "webhook_used": webhook_url[:30] + "...",
        }

    def _map_action(self, direction: str, action: str) -> str:
        """Map to TTP's 4 supported actions: buy, sell, cancel, close."""
        if action == "close":
            return "close"
        if action == "cancel":
            return "cancel"
        if direction.upper() == "LONG":
            return "buy"
        return "sell"  # SHORT → sell (NOT sell_short)
```

### 3. Update trade_desk.py to pass profile to adapter

Where the trade desk calls execution, pass the full profile dict:

```python
# Before: adapter.send_order(order)
# After:  adapter.send_order(order, profile=active_profile)
```

### 4. Update V7 orchestrator execution bridge

Where V7 calls execution for approved trades, look up the profile and route:

```python
def execute(self, portfolios, cycle_ts):
    for account_id, trades in portfolios.items():
        profile = self.get_profile(account_id)
        bridge = profile.get("execution_bridge", "disabled")
        
        if bridge == "signalstack" and profile.get("webhook_url"):
            for trade in trades:
                result = self.signalstack.send_order(trade, profile=profile)
                self.log_execution(trade, result, account_id)
        elif bridge == "mt5":
            # Future: MT5 execution for FTMO
            logger.info(f"[V7] MT5 execution not yet implemented for {account_id}")
        else:
            # Forward-track only
            for trade in trades:
                self.log_forward_track(trade, account_id)
```

## VERIFY

```bash
# Schema updated
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, name, execution_bridge, webhook_url IS NOT NULL as has_webhook
FROM account_profiles
"

# Adapter accepts profile param
grep -n "def send_order\|profile\|webhook_url" ~/SS/Vanguard/vanguard/execution/signalstack_adapter.py

# Compile
python3 -m py_compile ~/SS/Vanguard/vanguard/execution/signalstack_adapter.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/trade_desk.py
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_orchestrator.py
```

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: multi-account webhook routing — per-profile webhook_url for TTP accounts"
```

## After Implementation — Operator Steps

When you have the TTP demo webhook URLs:
```sql
UPDATE account_profiles SET webhook_url = 'https://signalstack.com/hook/ABC123' WHERE id = 'ttp_20k_swing';
UPDATE account_profiles SET webhook_url = 'https://signalstack.com/hook/XYZ789' WHERE id = 'ttp_40k_swing';
```
