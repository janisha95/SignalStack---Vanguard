# CODEX — Fix Short Order Rejection + TTP P&L Investigation
# Short orders via SignalStack → TTP Demo are being rejected

---

## PROBLEM 1: Short orders rejected by TTP

All short orders placed via POST /api/v1/execute are rejected by TTP Demo.
Long orders work fine.

### Diagnose

```bash
# 1. Check the SignalStack adapter — what payload is sent for shorts?
grep -n "short\|SELL\|sell\|action\|direction" \
  ~/SS/Vanguard/vanguard/execution/signalstack_adapter.py

# 2. Check execution log for short orders and their responses
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, symbol, direction, status, signalstack_response
FROM execution_log
WHERE direction = 'SHORT'
ORDER BY id DESC
LIMIT 5
"

# 3. Check what the webhook payload looks like for shorts
grep -n "webhook\|payload\|action\|sell\|short" \
  ~/SS/Vanguard/vanguard/api/trade_desk.py

# 4. Check the BACKEND_CONTRACT.md for the correct webhook format
# Per contract:
#   LONG:  {"symbol": "TRNO", "quantity": 100, "action": "buy"}
#   SHORT: {"symbol": "PAYO", "quantity": 100, "action": "sell"}
```

### Likely root causes:

**A: TTP doesn't support short selling on Demo**
TTP Demo accounts may not have short selling enabled. Check the TTP 
Trader Evolution platform settings — is there a "Short Selling" or 
"Locate" toggle? Some prop firm demos disable shorting entirely.

**B: SignalStack webhook format wrong for shorts**
SignalStack might need a different action for shorts. Possible formats:
- `"action": "sell"` (current — this might be interpreted as "sell existing long")
- `"action": "short"` (explicit short sell)
- `"action": "sell_short"` 
- `"action": "sellshort"`

Check SignalStack docs: https://docs.signalstack.com

**C: TTP requires locate/borrow for shorts**
Some brokers require a stock borrow locate before shorting. TTP's 
platform may enforce this — check if there's a "hard to borrow" list 
or locate requirement.

### Fix based on diagnosis:

If the issue is the webhook format:
```python
# In signalstack_adapter.py — update the payload for shorts
def build_payload(symbol, direction, shares):
    if direction == "LONG":
        return {"symbol": symbol, "quantity": shares, "action": "buy"}
    else:
        # Try "sellshort" or "short" instead of "sell"
        return {"symbol": symbol, "quantity": shares, "action": "sellshort"}
```

If TTP Demo doesn't support shorts at all:
- Log this as a known limitation
- Filter short orders in the execute endpoint with a clear error message:
  "TTP Demo does not support short selling. Use a funded account or switch to FTMO."

---

## PROBLEM 2: Can we get P&L from TTP's API instead of yfinance?

### Investigation

TTP uses Trader Evolution (DXTrade) platform. Check if they have a REST API:

```bash
# Check if there's any TTP/DXTrade API documentation
# The platform was inspected via Chrome MCP on Mar 28, 2026
# Fields captured: Balance, Open P/L, Positions, Daily loss limit, etc.

# Check the platform URL for API endpoints
# TTP typically runs at: https://trade.tradethepool.com or similar
# DXTrade has a REST API documented at: https://api.dxtrade.com/docs

# Check SignalStack for position/account data endpoints
curl -s https://app.signalstack.com/api/positions 2>/dev/null || echo "No SignalStack positions API"
```

### What to look for:

1. **DXTrade REST API** — Trader Evolution is built on DXTrade which has:
   - `GET /accounts` — account balance, P&L
   - `GET /positions` — open positions with live P&L
   - `GET /orders` — order history with fills
   - Auth: typically API key or OAuth

2. **SignalStack API** — SignalStack may expose:
   - Connected account status
   - Position data from connected broker
   - Fill confirmations

3. **TTP-specific API** — Some prop firms have their own dashboard APIs

### If API exists, add to unified backend:

```python
@app.get("/api/v1/portfolio/ttp")
async def get_ttp_portfolio():
    """Fetch real portfolio data from TTP via DXTrade API."""
    # If we find an API, implement here
    # This would replace the yfinance-based P&L computation
    pass
```

### Report findings

Even if you can't implement it, document:
1. Does TTP/DXTrade have a REST API? URL?
2. What auth is needed?
3. What endpoints are available?
4. Can we get positions + P&L programmatically?

Write findings to `~/SS/Vanguard/docs/TTP_API_INVESTIGATION.md`

---

## PROBLEM 3: Track Execution Fees in P&L

TTP charges execution fees per trade ($0.75 - $2.49+ depending on share count).
These fees are visible in the TTP Filled Orders tab:

```
PLTZ  Buy   99 shares   $31.05   fee: -$0.75   net P/L: -$0.75
BXP   Sell  259 shares  $51.60   fee: -$1.29   net P/L: -$488.21
CDW   Sell  56 shares   $118.51  fee: -$0.75   net P/L: 204.59
```

### Add execution_fee column to execution_log

```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
  ALTER TABLE execution_log ADD COLUMN execution_fee REAL DEFAULT 0;
  ALTER TABLE execution_log ADD COLUMN fill_price REAL;
  ALTER TABLE execution_log ADD COLUMN filled_at TEXT;
"
```

Only add columns that don't already exist (check with PRAGMA first).

### Update portfolio P&L to include fees

File: wherever `/api/v1/portfolio/live` is defined

When computing unrealized P&L, subtract execution fee:

```python
# Current: pnl = (live - entry) * shares
# Correct: pnl = (live - entry) * shares - execution_fee
fee = row.get("execution_fee") or 0
pnl_dollars = round(pnl_per_share * shares - fee, 2)
```

### Update Trade Log to show fee column

File: `~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx`

Add a "Fee" column to the Trade Log table between "Status" and "Outcome":

```
TIME | SYMBOL | DIR | SHARES | ENTRY | FILL | FEE | P&L | STATUS
```

### If TTP API provides fill data with fees

Add a `PUT /api/v1/execution/{id}/fill` endpoint that updates:
- fill_price (actual fill price from TTP)
- execution_fee (from TTP)
- filled_at (fill timestamp)

This could be called manually or via webhook callback if available.

### TTP Fee Schedule (for estimation when API not available)

From TTP platform observation:
- Base fee appears to be ~$0.75 minimum
- Scales with quantity (ODD 499 shares = $2.49, BXP 259 shares = $1.29)
- Rough estimate: $0.005/share with $0.75 minimum

```python
def estimate_ttp_fee(shares: int) -> float:
    return max(0.75, round(shares * 0.005, 2))
```

---

## PROBLEM 4: Close Position from UI (Trade Log + Portfolio)

The operator needs to close winning positions directly from the UI — either
from the Portfolio tab (where live P&L is visible) or the Trade Log.

### Backend: POST /api/v1/execution/{id}/close

File: `~/SS/Vanguard/vanguard/api/unified_api.py`

```python
@app.post("/api/v1/execution/{execution_id}/close")
async def close_position(execution_id: int):
    """
    Close an open position by sending the reverse order to SignalStack.
    LONG → sends SELL, SHORT → sends BUY (cover).
    Updates execution_log with exit_price, outcome, pnl.
    """
    con = sqlite3.connect(VANGUARD_DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM execution_log WHERE id = ?", (execution_id,)
    ).fetchone()

    if not row:
        con.close()
        raise HTTPException(status_code=404, detail="Trade not found")

    if row["outcome"] and row["outcome"] != "OPEN":
        con.close()
        raise HTTPException(status_code=400, detail=f"Trade already closed: {row['outcome']}")

    symbol = row["symbol"]
    direction = row["direction"]
    shares = row["shares"]
    entry = row["entry_price"]

    # Send close order to SignalStack
    # LONG position → action: "sell" (close long)
    # SHORT position → action: "buy" (cover short)
    close_action = "sell" if direction == "LONG" else "buy"

    webhook_url = os.environ.get("SIGNALSTACK_WEBHOOK_URL")
    if webhook_url:
        import requests
        payload = {
            "symbol": symbol,
            "quantity": shares,
            "action": close_action,
        }
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
            webhook_response = resp.text
        except Exception as e:
            webhook_response = f"ERROR: {e}"
    else:
        webhook_response = "NO_WEBHOOK_CONFIGURED"

    # Get live price for exit
    live_price = _get_live_price(symbol) or entry  # fallback to entry if no price

    # Calculate P&L
    if direction == "LONG":
        pnl_per = live_price - entry
    else:
        pnl_per = entry - live_price
    pnl_dollars = round(pnl_per * shares, 2)
    pnl_pct = round(pnl_per / entry * 100, 2) if entry else 0
    outcome = "WIN" if pnl_dollars > 0 else "LOSE"

    # Update execution_log
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    con.execute("""
        UPDATE execution_log SET
            exit_price = ?,
            exit_date = ?,
            pnl_dollars = ?,
            pnl_pct = ?,
            outcome = ?,
            signalstack_response = COALESCE(signalstack_response, '') || ' | CLOSE: ' || ?
        WHERE id = ?
    """, (live_price, now, pnl_dollars, pnl_pct, outcome, webhook_response, execution_id))
    con.commit()
    con.close()

    return {
        "id": execution_id,
        "symbol": symbol,
        "direction": direction,
        "action": close_action,
        "exit_price": live_price,
        "pnl_dollars": pnl_dollars,
        "pnl_pct": pnl_pct,
        "outcome": outcome,
        "webhook_response": webhook_response,
    }
```

### Frontend: Add "Close" button to Portfolio tab

File: `~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx`

In the Portfolio positions table, add a "Close" column:

```typescript
// In the Portfolio table, add a Close button for each position
<td>
  <button
    onClick={() => handleClosePosition(position.id, position.symbol)}
    className="btn-close-position"
    title={`Close ${position.direction} ${position.symbol}`}
  >
    {position.unrealized_pnl >= 0 ? '💰 Take Profit' : '🛑 Cut Loss'}
  </button>
</td>

const handleClosePosition = async (id: number, symbol: string) => {
  const confirm = window.confirm(
    `Close position in ${symbol}? This will send a ${position.direction === 'LONG' ? 'SELL' : 'BUY'} order to TTP.`
  );
  if (!confirm) return;

  const res = await fetch(`${API}/api/v1/execution/${id}/close`, { method: 'POST' });
  const data = await res.json();

  if (data.pnl_dollars !== undefined) {
    alert(`${symbol} closed: ${data.outcome} · P&L: $${data.pnl_dollars} (${data.pnl_pct}%)`);
  }

  // Refresh portfolio
  fetchPortfolio();
};
```

### Also add "Close" to Trade Log rows

For any trade where `outcome` is null/OPEN, show a Close button in the row:

```typescript
// In Trade Log table, in the last column
{!row.outcome || row.outcome === 'OPEN' ? (
  <button onClick={() => handleClosePosition(row.id, row.symbol)} className="btn-close-sm">
    Close
  </button>
) : (
  <span className={row.outcome === 'WIN' ? 'text-green' : 'text-red'}>
    {row.outcome}
  </span>
)}
```

### Frontend API function

File: `~/SS/Meridian/ui/signalstack-app/lib/unified-api.ts`

```typescript
export async function closePosition(id: number): Promise<any> {
  const base = process.env.NEXT_PUBLIC_UNIFIED_API_URL || 'http://localhost:8090';
  const res = await fetch(`${base}/api/v1/execution/${id}/close`, { method: 'POST' });
  if (!res.ok) throw new Error(`Close failed: ${res.statusText}`);
  return res.json();
}
```

### Close All button in Portfolio summary

Add a "Close All" button to the Portfolio summary bar for flattening:

```typescript
<button
  onClick={handleCloseAll}
  className="btn-close-all"
  disabled={positions.length === 0}
>
  Close All ({positions.length})
</button>

const handleCloseAll = async () => {
  if (!window.confirm(`Close ALL ${positions.length} open positions?`)) return;
  for (const p of positions) {
    await closePosition(p.id);
  }
  fetchPortfolio();
};
```

---

## VERIFY

```bash
# Restart API
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 2

# Test close endpoint (use an actual trade ID from execution_log)
TRADE_ID=$(sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT id FROM execution_log WHERE outcome IS NULL LIMIT 1")
echo "Closing trade $TRADE_ID..."
curl -s -X POST http://localhost:8090/api/v1/execution/$TRADE_ID/close | python3 -m json.tool

# Frontend build
cd ~/SS/Meridian/ui/signalstack-app && npm run build
```
