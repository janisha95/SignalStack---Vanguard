# TTP API Investigation

Date: 2026-03-30

## Summary

- The current SignalStack adapter already uses the correct short-side actions:
  - open short: `sell_short`
  - close short: `buy_to_cover`
- There is no local backend evidence yet of a failed short order from this system:
  - `execution_log` currently contains `33` `LONG|SUBMITTED` rows
  - `0` `SHORT` rows
- Trade The Pool publicly states that its demo/free-trial environment supports **long/short** trading on roughly 12,000 US stocks/ETFs, and explicitly advertises **zero locate fees for shorting penny stocks**.
- DXtrade does have a REST API surface, but access is controlled by the broker/prop firm. Public DXtrade documentation says API docs are available at `https://<platform.address>/specs` when the platform operator exposes them, and that broker/prop firms may restrict allowed IPs.
- I did **not** change production order-routing code because the current local adapter already matches SignalStack’s documented short-action contract, and there is no failed short execution row to prove a payload bug.

## Problem 1 — Short orders rejected by TTP

### Local code truth

File: [signalstack_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py)

- [line 39](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py#L39): `_direction_to_action(self, direction, operation)`
- [lines 43-48](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py#L43): documented supported actions in code
- [line 51](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py#L51): open short maps to `sell_short`
- [line 53](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py#L53): close short maps to `buy_to_cover`
- [lines 68-76](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py#L68): payload builder emits:
  - `symbol`
  - `action`
  - `quantity`
  - optional `limit_price`
  - optional `stop_price`

Current mapping:

```python
if operation == "open":
    return "buy" if direction.upper() == "LONG" else "sell_short"
elif operation == "close":
    return "sell" if direction.upper() == "LONG" else "buy_to_cover"
```

That matches current SignalStack documentation.

### Execution path truth

File: [trade_desk.py](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py)

- [line 327](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py#L327): `adapter.send_order(...)`
- [lines 328-332](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py#L328): execute path passes:
  - `symbol`
  - `direction`
  - `quantity`
  - `operation="open"`

Important limitation:

- The current execute path does **not** pass `limit_price` or `stop_price` into `send_order`, even though the adapter supports them.
- So all opens are effectively sent as plain market-style orders.

This is not automatically the cause of short rejection, but it matters if attempted outside regular hours, because TTP states only limit orders are available pre/after-market.

### Database evidence

Query:

```sql
SELECT direction, status, count(*)
FROM execution_log
GROUP BY direction, status
ORDER BY direction, status;
```

Result:

```text
LONG|SUBMITTED|33
```

Query:

```sql
SELECT id, symbol, direction, status, signalstack_response
FROM execution_log
WHERE direction = 'SHORT'
ORDER BY id DESC
LIMIT 10;
```

Result:

```text
(no rows)
```

Conclusion:

- There is no recorded short execution attempt in `execution_log` to inspect.
- So the statement "all short orders are rejected by TTP" is not proven from the backend DB yet.
- Right now, the local evidence only proves that long orders are being accepted.

### SignalStack contract finding

Official SignalStack docs confirm that short actions use:

- `sell_short`
- `buy_to_cover`

Evidence:

- SignalStack Lime stock docs: [help.signalstack.com/kb/lime/lime-stock](https://help.signalstack.com/kb/lime/lime-stock)
  - lines 45-50 and 80-86 in the fetched page show `Sell-Short` and `Buy-to-Cover`
- SignalStack TradingView alerts docs: [help.signalstack.com/kb/signal-source-integrations/tradingview-alerts-f07455cf99ed63bd](https://help.signalstack.com/kb/signal-source-integrations/tradingview-alerts-f07455cf99ed63bd)
  - lines 83-107 describe separate actions for `buy`, `sell`, `sell short`, and `buy to cover`

So:

- The adapter’s current `sell_short` / `buy_to_cover` mapping is correct.
- Changing it to `sell`, `short`, or `sellshort` would be speculative and likely wrong.

### TTP short-selling support finding

Public TTP materials indicate that shorting is supported on demo/trial and funded environments.

Evidence:

- TTP landing page/article: [tradethepool.com](https://lp.tradethepool.com/trade-stocks-without-a-broker/)
  - fetched lines 357-378 say:
    - you can trade **long/short** any of their 12,000 symbols
    - the free demo/paper account uses real-time market data
  - lines 406-408 mention **zero locate fees for shorting penny stocks**
- TTP program/support content and funded trader case studies also repeatedly reference successful short-selling on TTP.

So:

- There is no evidence that TTP Demo disables short selling globally.
- A blanket “TTP Demo does not support shorts” filter would be wrong.

### Most likely root causes now

Given the local and external evidence, the most likely causes are:

1. **No real failed short attempt has been logged yet**
   - The backend cannot diagnose a rejection reason without an actual `SHORT` row and `signalstack_response`.

2. **Platform-side symbol/risk restriction**
   - TTP may reject specific short entries due to:
     - symbol-level shortability
     - volatility rules
     - price bands
     - internal risk rules
   - TTP program terms mention automatic halts and volatility-based restrictions on new/additional positions.

3. **Order-type mismatch during extended hours**
   - TTP states only **limit orders** are available pre/after-market.
   - The current execute path does not forward `limit_price` into SignalStack.
   - If the rejected shorts were attempted outside regular hours, that is a plausible cause.

### Recommendation for Problem 1

Do **not** change the short action mapping.

Instead:

1. Place one controlled short order through the current backend.
2. Capture the exact `execution_log.signalstack_response`.
3. Compare that response against:
   - symbol
   - time of day
   - order type
   - whether the trade was RTH vs pre/after-hours

Only after that should we change production order-routing behavior.

The most useful next code change, if we decide to act before market open, is **not** to alter `sell_short`. It is to forward `limit_price` when `order_type != "market"` so extended-hours orders can be represented correctly.

## Problem 2 — Can we get P&L from TTP / DXtrade API?

### DXtrade API availability

DXtrade does have REST API support.

Evidence:

- DXtrade release notes: [dx.trade/news/release-notes/dxtrade-xt-updates-automatic-liquidation-api-improvements](https://dx.trade/news/release-notes/dxtrade-xt-updates-automatic-liquidation-api-improvements/)
  - lines 79-84 state:
    - DXtrade officially supports REST API integrations
    - API specs are available online at `https://<platform.address>/specs`
    - example: `https://demo-xt.dx.trade/specs`

### DXtrade access control / auth reality

Public DXtrade FAQ makes clear that access is controlled by the broker/prop firm.

Evidence:

- DXtrade FAQ: [dx.trade/traders-faq](https://dx.trade/traders-faq/)
  - lines 106-113 say:
    - there are no special trader requirements for the DXtrade API
    - **your broker or prop firm controls the IPs permitted**
    - brokers/prop firms may or may not open the APIs externally

So the practical answer is:

- yes, DXtrade has a REST API surface
- but TTP must expose it to us
- and TTP may additionally IP-allowlist access

### TTP-specific API finding

I did not find a public TTP-specific REST API for:

- positions
- account P&L
- fills
- order history

I also checked whether SignalStack exposes a public positions API:

```bash
curl -s https://app.signalstack.com/api/positions
```

Result:

- returns the SignalStack web app HTML, not a documented public positions endpoint

So there is no current evidence of a SignalStack REST endpoint we can call anonymously for broker positions or P&L.

### What is realistically possible

If TTP’s DXtrade deployment exposes `/specs`, then in principle we should be able to get:

- account data
- open positions
- orders / fills
- P&L

But we still need:

1. the exact TTP DXtrade platform base URL
2. proof that `/specs` is exposed on that platform
3. the authentication method TTP expects
4. any IP allowlisting or token issuance by TTP

Until then, the current yfinance-based portfolio P&L remains the only available programmatic path in this codebase.

## Final verdict

### Short-order rejection

- **No local webhook-format bug found**
- Current short-side action mapping is already correct: `sell_short`
- **No short rejection row exists in `execution_log`**, so there is no backend evidence yet to debug from
- TTP publicly supports shorting, including on demo/trial environments

### TTP / DXtrade API for real P&L

- **Yes, DXtrade has REST APIs**
- **Maybe, TTP can expose them**
- **No, we do not currently have confirmed TTP API access details**
- **No, SignalStack did not reveal a public positions/P&L API from the checks performed**

## Recommended next steps

1. Execute one intentionally small short order through the current backend.
2. Capture the resulting `execution_log.signalstack_response`.
3. If the failure happens outside regular hours, prioritize wiring `limit_price` through to SignalStack for non-market orders.
4. Ask TTP support whether their DXtrade environment exposes `/specs` and whether they permit external REST API access for trader accounts.
5. If TTP confirms API access, add a new backend endpoint such as:
   - `/api/v1/portfolio/ttp`
   - backed by DXtrade positions/account endpoints

## Source links

- SignalStack stock action docs: [https://help.signalstack.com/kb/lime/lime-stock](https://help.signalstack.com/kb/lime/lime-stock)
- SignalStack TradingView alert docs: [https://help.signalstack.com/kb/signal-source-integrations/tradingview-alerts-f07455cf99ed63bd](https://help.signalstack.com/kb/signal-source-integrations/tradingview-alerts-f07455cf99ed63bd)
- DXtrade FAQ: [https://dx.trade/traders-faq/](https://dx.trade/traders-faq/)
- DXtrade REST API announcement: [https://dx.trade/news/release-notes/dxtrade-xt-updates-automatic-liquidation-api-improvements/](https://dx.trade/news/release-notes/dxtrade-xt-updates-automatic-liquidation-api-improvements/)
- TTP demo/shorting page: [https://lp.tradethepool.com/trade-stocks-without-a-broker/](https://lp.tradethepool.com/trade-stocks-without-a-broker/)
