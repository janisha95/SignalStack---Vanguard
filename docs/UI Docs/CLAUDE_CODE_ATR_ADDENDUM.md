# ADDENDUM to 3-in-1 Prompt — Fix ATR Stop Method Default + Recalculate

## PROBLEM (confirmed via Chrome MCP)

1. Orders are added to Trade Desk with `stopMethod: "pct_2"` or `"pct_3"` as default.
   ATR is only fetched when stopMethod is an ATR option. So most orders never get
   real ATR values and use fake percentage stops.

2. AAP has `atr: 0` because the sizing API was never called for it — the order
   was added with `pct_3` default.

3. STNG has `atr: 3.1021` cached but `stopMethod: "pct_2"` — so the cached ATR
   is ignored and the percentage-based stop is used instead.

## FIX

File: `~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx`
(and/or `trade-desk-store.ts` or wherever orders are created)

### Fix A: Always fetch ATR when adding an order to Trade Desk

When the "Trade" button is clicked on the Candidates page and an order is
created, ALWAYS fetch ATR regardless of stop method:

```typescript
// When creating a new TradeOrder:
const sizing = await fetchSizing(symbol, direction);
const newOrder: TradeOrder = {
  symbol,
  direction,
  // ...
  atr: sizing?.atr_14 || 0,      // ALWAYS fetch and cache ATR
  stopMethod: 'atr_1.5',          // DEFAULT to ATR 1.5×, not pct_2
  // ... rest of fields
};
```

### Fix B: Default stop method should be `atr_1.5` not `pct_2` or `pct_3`

Find where the default stopMethod is set. Change from:
```typescript
stopMethod: 'pct_2'  // or 'pct_3'
```
To:
```typescript
stopMethod: 'atr_1.5'
```

### Fix C: Recalculate must use ATR when method is ATR-based

The recalculate function must branch on stop method:

```typescript
function recalculate(order: TradeOrder): TradeOrder {
  const entry = order.entryPrice;
  const isLong = order.direction === 'LONG';

  // Stop loss calculation
  if (order.stopMethod === 'atr_1.5' && order.atr > 0) {
    const dist = order.atr * 1.5;
    order.stopLoss = isLong
      ? Math.round((entry - dist) * 100) / 100
      : Math.round((entry + dist) * 100) / 100;
  } else if (order.stopMethod === 'atr_2.0' && order.atr > 0) {
    const dist = order.atr * 2.0;
    order.stopLoss = isLong
      ? Math.round((entry - dist) * 100) / 100
      : Math.round((entry + dist) * 100) / 100;
  } else if (order.stopMethod === 'pct_1') {
    order.stopLoss = isLong
      ? Math.round(entry * 0.99 * 100) / 100
      : Math.round(entry * 1.01 * 100) / 100;
  } else if (order.stopMethod === 'pct_2') {
    order.stopLoss = isLong
      ? Math.round(entry * 0.98 * 100) / 100
      : Math.round(entry * 1.02 * 100) / 100;
  } else if (order.stopMethod === 'pct_3') {
    order.stopLoss = isLong
      ? Math.round(entry * 0.97 * 100) / 100
      : Math.round(entry * 1.03 * 100) / 100;
  }
  // manual = don't touch stopLoss

  // Take profit from R:R
  const riskPerShare = Math.abs(entry - order.stopLoss);
  if (order.rrRatio > 0 && riskPerShare > 0) {
    order.takeProfit = isLong
      ? Math.round((entry + riskPerShare * order.rrRatio) * 100) / 100
      : Math.round((entry - riskPerShare * order.rrRatio) * 100) / 100;
  }

  // Shares from risk amount
  if (riskPerShare > 0) {
    order.shares = Math.floor(order.riskAmount / riskPerShare);
  }

  return order;
}
```

### Fix D: When stop method dropdown changes, re-trigger recalculate

Make sure changing the Method dropdown calls recalculate:

```typescript
const handleStopMethodChange = (orderId: string, newMethod: string) => {
  updateOrder(orderId, { stopMethod: newMethod });
  // If switching TO an ATR method and atr is 0, fetch it now
  if ((newMethod === 'atr_1.5' || newMethod === 'atr_2.0') && order.atr === 0) {
    fetchSizing(order.symbol, order.direction).then(sizing => {
      if (sizing) {
        updateOrder(orderId, { atr: sizing.atr_14 });
        // recalculate will fire from the state update
      }
    });
  }
  recalculateOrder(orderId);
};
```

### Fix E: Stop method dropdown labels

Show clear labels that distinguish ATR from percentage:

```
ATR 1.5×     ← uses real volatility from daily bars
ATR 2.0×     ← uses real volatility from daily bars  
1%           ← fixed percentage of price
2%           ← fixed percentage of price
3%           ← fixed percentage of price
Manual       ← enter your own stop price
```

## EXPECTED BEHAVIOR AFTER FIX

For STNG SHORT at $75.70 with ATR(14) = $3.10, $500 risk:

| Method | Stop | Risk/Share | Shares | Pos Value |
|--------|------|-----------|--------|-----------|
| ATR 1.5× (default) | $80.35 | $4.65 | 107 | $8,100 |
| ATR 2.0× | $81.90 | $6.20 | 80 | $6,056 |
| 2% | $77.21 | $1.51 | 330 | $24,981 |
| 3% | $77.97 | $2.27 | 220 | $16,654 |

Key: tighter stop (ATR 1.5×) = less risk per share = MORE shares.
Wider stop (ATR 2.0×) = more risk per share = FEWER shares.
Dollar risk stays constant at $500.
