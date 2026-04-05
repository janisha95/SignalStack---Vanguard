# BUILD PROMPT — Claude Code
# Trade Desk Redesign + Candidates Bug Fixes

Read first:
- ~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx
- ~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx
- ~/SS/Meridian/ui/signalstack-app/lib/unified-api.ts
- ~/SS/Meridian/ui/signalstack-app/lib/types.ts
- ~/SS/Meridian/ui/signalstack-app/lib/trade-desk-store.ts
- /mnt/user-data/outputs/trade_desk_mockup.html (design reference)

## PART 1: Fix Candidates Page Bugs

### Bug 1: Hydration error
In unified-candidates-client.tsx, the source selector buttons have different
className on server vs client render (bg-surface vs bg-amber). Fix by:
- Initialize source state to a consistent default (e.g., "meridian")
- Use useEffect to apply the active styling AFTER mount
- Or add suppressHydrationWarning to the source selector buttons

### Bug 2: working.column_order.filter is not a function (line 297)
column_order is coming from localStorage as a JSON string, not an array.
Fix ALL working state fields that should be arrays:

```typescript
// When loading from localStorage or userviews API:
const parseArray = (val: any, fallback: string[] = []): string[] => {
  if (Array.isArray(val)) return val;
  if (typeof val === 'string') {
    try { const p = JSON.parse(val); return Array.isArray(p) ? p : fallback; }
    catch { return fallback; }
  }
  return fallback;
};

// Apply to:
working.visible_columns = parseArray(working.visible_columns);
working.column_order = parseArray(working.column_order);
working.filters = parseArray(working.filters);
working.sorts = parseArray(working.sorts);
```

### Bug 3: Column picker doesn't work
The Columns popover needs to:
1. Read available columns from field registry for current source
2. Show checkboxes for each field (checked = visible)
3. Toggle updates working.visible_columns
4. Has up/down buttons or drag to reorder
5. "Reset to Default" restores default columns for source

### Bug 4: Filter builder doesn't work
The Filter popover needs to:
1. Show a list of active filters
2. Each row: [Field dropdown] [Operator dropdown] [Value input] [X remove]
3. Field dropdown only shows filterable fields for current source
4. Operators depend on type: number (>, <, >=, <=, =), string (contains, equals), enum (is)
5. "Add Filter" button
6. Filters apply to the candidate query immediately

### Bug 5: Sort builder doesn't work
Same pattern as filter — field dropdown + ASC/DESC toggle.

## PART 2: Trade Desk Redesign

Completely rewrite the Trade Desk section of trades-client.tsx.

### Design Principle
The Trade Desk starts EMPTY. You populate it by clicking "Trade" on rows in
the Candidates page. Each trade becomes a full ORDER CARD with proper risk
sizing, not just a checkbox in a preset list.

### Trade Desk Store (trade-desk-store.ts)
Already exists. Uses localStorage. The store holds an array of TradeOrder
objects. The Candidates page writes to it; the Trade Desk reads from it.

Extend the TradeOrder type:
```typescript
interface TradeOrder {
  symbol: string;
  direction: 'LONG' | 'SHORT';
  source: 'meridian' | 's1' | 'vanguard';
  tier: string;
  price: number;
  scores: Record<string, number>;  // native scores from source
  // NEW fields for order configuration:
  orderType: 'market' | 'limit' | 'stop_limit';
  entryPrice: number;              // default from price, editable for limit
  limitBuffer: number;             // % buffer for limit orders (0.5, 1.0, 2.0)
  stopLoss: number;                // auto-calculated or manual
  stopMethod: 'atr_1.5' | 'atr_2.0' | 'pct_1' | 'pct_2' | 'pct_3' | 'manual';
  takeProfit: number;              // auto-calculated from R:R or manual
  rrRatio: number;                 // 2.0, 2.5, 3.0, or manual
  riskAmount: number;              // $ risk per trade (default 400)
  shares: number;                  // auto-calculated: riskAmount / (entry - stop)
  tags: string[];
  notes: string;
}
```

### Auto-Calculation Logic
When ANY input changes, recalculate everything:

```typescript
function recalculate(order: TradeOrder): TradeOrder {
  const entry = order.entryPrice;
  const isLong = order.direction === 'LONG';

  // Stop Loss
  if (order.stopMethod !== 'manual') {
    const pct = {
      'atr_1.5': 0.02,  // approx 1.5x ATR for avg stock
      'atr_2.0': 0.027,
      'pct_1': 0.01,
      'pct_2': 0.02,
      'pct_3': 0.03,
    }[order.stopMethod] || 0.02;
    order.stopLoss = isLong
      ? Math.round((entry * (1 - pct)) * 100) / 100
      : Math.round((entry * (1 + pct)) * 100) / 100;
  }

  // Take Profit from R:R
  const risk = Math.abs(entry - order.stopLoss);
  if (order.rrRatio > 0) {
    order.takeProfit = isLong
      ? Math.round((entry + risk * order.rrRatio) * 100) / 100
      : Math.round((entry - risk * order.rrRatio) * 100) / 100;
  }

  // Shares from risk amount
  if (risk > 0) {
    order.shares = Math.floor(order.riskAmount / risk);
  }

  return order;
}
```

### Order Card Layout (3 columns per card)

```
┌── ORDER CARD ─────────────────────────────────────────────┐
│ HEADER: Symbol, Direction badge, Source badge, Scores, ✕  │
├───────────────┬──────────────────┬────────────────────────┤
│ ORDER CONFIG  │ RISK MANAGEMENT  │ POSITION DETAILS       │
│               │                  │                        │
│ Order Type ▼  │ Stop Loss  $XX   │ ┌──────┐ ┌──────────┐ │
│ Entry Price   │ Method ▼         │ │Shares│ │Pos Value │ │
│ Limit Buffer  │ Take Profit $XX  │ │ 328  │ │ $19,913  │ │
│               │ R:R Ratio ▼      │ └──────┘ └──────────┘ │
│               │ Risk/Trade $     │ ┌──────┐ ┌──────────┐ │
│               │                  │ │MaxLos│ │ Max Gain │ │
│               │                  │ │ -$400│ │ +$797    │ │
│               │                  │ └──────┘ └──────────┘ │
│               │                  │ ┌──────┐ ┌──────────┐ │
│               │                  │ │ R:R  │ │% Account │ │
│               │                  │ │ 2.0  │ │ 39.8%    │ │
│               │                  │ └──────┘ └──────────┘ │
├───────────────┴──────────────────┴────────────────────────┤
│ SIGNAL TAGS: [TCN] [Alpha] [RF] [NN] [Conv] [FR] [LGBM]  │
│ Notes: [_________________________________________________]│
└───────────────────────────────────────────────────────────┘
```

### Risk Summary Bar (sticky top)
Shows: Orders queued, Total Risk, Total Exposure, Positions (vs TTP max 5),
Daily Loss Used (vs TTP 2%), Max Drawdown (vs TTP 4%).
Buttons: [Clear All] [Execute All (N)]

### TTP Compliance Section (bottom)
Checks against TTP Swing Demo rules:
- Daily Loss Limit: 2% of $50K = $1,000
- Max Drawdown: 4% of $50K = $2,000
- Max Positions: 5
- Consistency Rule: No single trade > 30% of total P&L

These values should come from the backend: GET /api/v1/health returns account info.
For now, hardcode TTP Swing $50K defaults.

### Empty State
When no orders in the Trade Desk store:
```
No orders queued.
Browse candidates and click "Trade" to add picks here.
[Go to Candidates →]
```

### Execute Flow
1. Click "Execute All" or "Execute Selected"
2. Confirmation modal shows: trade list, total risk, TTP compliance check
3. Confirm → POST /api/v1/execute with full trade details (symbol, direction,
   shares, tags, notes, entry_price, stop_loss, take_profit, order_type)
4. Toast: "3 trades submitted to TTP Demo"
5. Clear executed orders from Trade Desk store
6. Switch to Trade Log tab automatically

### Colors
- Use the EXACT color scheme from the Meridian dark theme:
  --bg: #0b1326, --green: #3be8a0, --red: #f87171, --amber: #fbbf24
- LONG cards: left border green
- SHORT cards: left border red
- Monospace for all numbers: JetBrains Mono or the app's mono font

## PART 3: Backend Extension

### Extend POST /api/v1/execute to accept full order details:
```json
{
  "trades": [{
    "symbol": "TRNO",
    "direction": "LONG",
    "shares": 328,
    "tier": "tier_meridian_long",
    "tags": ["tcn", "alpha", "factor_rank"],
    "notes": "Strong TCN + FR alignment",
    "entry_price": 60.71,
    "stop_loss": 59.49,
    "take_profit": 63.14,
    "order_type": "limit",
    "limit_buffer": 0.01
  }]
}
```

Store all fields in execution_log. The SignalStack webhook still sends
the simple format: {"symbol": "TRNO", "quantity": 328, "action": "buy"}

### Extend execution_log table (if not already done):
- entry_price, stop_loss, take_profit, order_type columns

## PART 4: Test Every Change

After building, verify:
1. npm run dev starts without errors
2. /candidates loads without hydration error
3. Source selector switches between Meridian/S1/Combined
4. Columns popover opens and columns can be toggled on/off
5. Filter popover opens and a filter can be added (e.g., tcn_score > 0.8)
6. Sort popover opens
7. "Trade" button on a candidate row adds it to Trade Desk store
8. /trades shows Trade Desk tab with the order card
9. Changing SL method or R:R recalculates everything
10. Changing risk amount recalculates shares
11. Execute flow sends to backend (test with curl mock if needed)
12. Trade Log tab shows execution history
13. npm run build passes with zero errors
