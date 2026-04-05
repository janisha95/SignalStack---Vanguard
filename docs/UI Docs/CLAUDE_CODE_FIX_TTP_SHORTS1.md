# CLAUDE CODE — URGENT: Fix Short Orders for TTP
# TTP via SignalStack does NOT support sell_short / buy_to_cover
# Date: Mar 31, 2026

---

## THE BUG

Short orders fail with 401 AuthenticationError because the adapter sends
`action: "sell_short"` but TTP's SignalStack integration ONLY supports
4 actions: **buy, sell, cancel, close**.

From TTP's SignalStack documentation (https://app.signalstack.com/me/documentation/tradethepool):

```
action: buy, sell, cancel, close
```

There is NO sell_short or buy_to_cover for TTP. Those actions are
Lime/other broker specific.

TTP handles direction implicitly:
- "buy" with no existing position = open LONG
- "sell" with no existing position = open SHORT (TTP treats it as a short)
- "sell" with existing LONG = close LONG
- "buy" with existing SHORT = close SHORT (cover)
- "close" = close whatever position exists for that symbol

## THE FIX

File: `~/SS/Vanguard/vanguard/execution/signalstack_adapter.py`

Find the `_direction_to_action` method and add broker-aware logic:

```python
def _direction_to_action(self, direction: str, operation: str, broker: str = "ttp") -> str:
    """
    Map direction + operation to SignalStack action.
    
    TTP only supports: buy, sell, cancel, close
    Lime supports: buy, sell, sell_short, buy_to_cover, cancel, close
    
    For TTP:
      open LONG  → "buy"
      open SHORT → "sell"     (TTP treats sell without position as short)
      close LONG → "sell"     (or "close")
      close SHORT → "buy"    (or "close")
    """
    direction = direction.upper()
    
    if broker == "ttp":
        # TTP only has buy/sell/cancel/close
        if operation == "open":
            return "buy" if direction == "LONG" else "sell"
        elif operation == "close":
            return "close"  # "close" handles both directions
    elif broker == "lime":
        # Lime supports explicit short actions
        if operation == "open":
            return "buy" if direction == "LONG" else "sell_short"
        elif operation == "close":
            return "sell" if direction == "LONG" else "buy_to_cover"
    
    # Fallback: simple buy/sell
    if operation == "open":
        return "buy" if direction == "LONG" else "sell"
    return "close"
```

Also update `send_order` to accept/detect the broker:

```python
def send_order(self, symbol, direction, quantity, operation="open",
               order_type="market", limit_price=None, stop_price=None,
               broker="ttp"):
    action = self._direction_to_action(direction, operation, broker)
    # ... rest of method
```

## ALSO: Update trade_desk.py execute flow

File: `~/SS/Vanguard/vanguard/api/trade_desk.py`

Find where `adapter.send_order()` is called and ensure it passes broker="ttp":

```python
# In the execute handler:
result = adapter.send_order(
    symbol=trade["symbol"],
    direction=trade["direction"],
    quantity=trade["shares"],
    operation="open",
    broker="ttp",  # ADD THIS
)
```

## ALSO: Update the close endpoint

The `POST /api/v1/execution/{id}/close` endpoint also needs the fix:

```python
# Currently might be using sell_short/buy_to_cover for close
# Fix: use "close" action for TTP (covers both directions)
result = adapter.send_order(
    symbol=symbol,
    direction=direction,
    quantity=shares,
    operation="close",
    broker="ttp",  # TTP uses "close" for all directions
)
```

## VERIFY

```bash
# Restart API
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 2

# Test a 1-share short (NOTE: account may still be DLL-blocked today)
curl -s -X POST http://localhost:8090/api/v1/execute \
  -H 'Content-Type: application/json' \
  -d '{
    "trades": [{
      "symbol": "SPY",
      "direction": "SHORT",
      "shares": 1,
      "tier": "test",
      "tags": ["test"],
      "notes": "Testing sell action for TTP short",
      "entry_price": 560,
      "stop_loss": 570,
      "take_profit": 540,
      "order_type": "market"
    }]
  }' | python3 -m json.tool

# Check the payload that was sent
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, symbol, direction, status, signalstack_response
FROM execution_log
ORDER BY id DESC LIMIT 3
"

# If DLL-blocked, the response will be 400 ValidationError (daily loss limit)
# NOT 401 AuthenticationError. That proves the action mapping is now correct.
# A 400 from TTP risk rules is expected today. A 401 means the action is still wrong.
```

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: TTP shorts use sell not sell_short — TTP only supports buy/sell/cancel/close"
```
