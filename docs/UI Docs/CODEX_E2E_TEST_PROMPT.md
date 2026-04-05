# CODEX TASK: End-to-End Playwright Tests for SignalStack UI

Read first:
- ~/SS/Meridian/ui/signalstack-app/package.json (check if playwright is installed)
- ~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx
- ~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx

## Setup

If Playwright is not installed:
```bash
cd ~/SS/Meridian/ui/signalstack-app
npm install -D @playwright/test
npx playwright install chromium
```

Create: ~/SS/Meridian/ui/signalstack-app/e2e/

## Prerequisites for tests

Both servers must be running:
- Unified API: http://localhost:8090 (cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --port 8090)
- Next.js dev: http://localhost:3000 (cd ~/SS/Meridian/ui/signalstack-app && npm run dev)

## Test 1: Candidates Page Loads

File: e2e/candidates.spec.ts

```typescript
test('candidates page loads without errors', async ({ page }) => {
  await page.goto('http://localhost:3000/candidates');
  // No console errors
  const errors: string[] = [];
  page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  // Wait for data to load
  await page.waitForSelector('table', { timeout: 10000 });
  // Should show candidates count
  await expect(page.locator('text=/\\d+ candidates/')).toBeVisible();
  expect(errors.filter(e => !e.includes('hydration'))).toHaveLength(0);
});
```

## Test 2: Source Selector Works

```typescript
test('source selector switches data', async ({ page }) => {
  await page.goto('http://localhost:3000/candidates');
  await page.waitForSelector('table');

  // Click S1 source
  await page.click('text=S1');
  await page.waitForTimeout(1000);
  // Should show S1 badges
  await expect(page.locator('text=s1').first()).toBeVisible();

  // Click Meridian source
  await page.click('text=Meridian');
  await page.waitForTimeout(1000);
  await expect(page.locator('text=meridian').first()).toBeVisible();

  // Click Combined
  await page.click('text=Combined');
  await page.waitForTimeout(1000);
  // Should show both
  const text = await page.textContent('body');
  expect(text).toContain('meridian');
});
```

## Test 3: Create Custom Userview

```typescript
test('create and load custom userview', async ({ page }) => {
  await page.goto('http://localhost:3000/candidates');
  await page.waitForSelector('table');

  // Click "New" to create view
  await page.click('text=New');
  // Fill name
  await page.fill('input[placeholder*="view" i], input[placeholder*="name" i]', 'My High TCN View');
  // Save
  await page.click('text=Save');
  await page.waitForTimeout(500);

  // Verify view appears in dropdown
  await expect(page.locator('text=My High TCN View')).toBeVisible();
});
```

## Test 4: Column Picker Works

```typescript
test('column picker toggles columns', async ({ page }) => {
  await page.goto('http://localhost:3000/candidates');
  await page.waitForSelector('table');

  // Open columns popover
  await page.click('text=Columns');
  await page.waitForTimeout(500);

  // Should see field checkboxes
  await expect(page.locator('text=TCN Score')).toBeVisible();

  // Toggle a column off
  const tcnCheckbox = page.locator('text=TCN Score').locator('..').locator('input[type="checkbox"]');
  await tcnCheckbox.click();
  await page.waitForTimeout(300);

  // Close popover
  await page.keyboard.press('Escape');

  // TCN column should be hidden from table
  // (verify header doesn't contain TCN_SCORE)
});
```

## Test 5: Send Candidate to Trade Desk

```typescript
test('trade button sends candidate to trade desk', async ({ page }) => {
  await page.goto('http://localhost:3000/candidates');
  await page.waitForSelector('table');

  // Click Trade on first row
  const tradeButton = page.locator('text=TRADE').first();
  await tradeButton.click();
  await page.waitForTimeout(500);

  // Navigate to Trade Desk
  await page.goto('http://localhost:3000/trades');
  await page.waitForTimeout(1000);

  // Should see at least one order card (not the empty state)
  await expect(page.locator('text=/No orders queued/').first()).not.toBeVisible({ timeout: 3000 }).catch(() => {
    // If empty state is visible, the store didn't persist — report but don't fail
    console.warn('Trade desk store may not have persisted');
  });
});
```

## Test 6: Trade Desk Auto-Calculation

```typescript
test('trade desk recalculates on input change', async ({ page }) => {
  // First add a pick via localStorage directly
  await page.goto('http://localhost:3000/trades');
  await page.evaluate(() => {
    const order = {
      symbol: 'TEST',
      direction: 'LONG',
      source: 'meridian',
      tier: 'tier_meridian_long',
      price: 100,
      scores: { tcn_score: 0.85 },
      orderType: 'market',
      entryPrice: 100,
      stopLoss: 98,
      stopMethod: 'pct_2',
      takeProfit: 104,
      rrRatio: 2.0,
      riskAmount: 400,
      shares: 200,
      tags: [],
      notes: ''
    };
    localStorage.setItem('signalstack_trade_desk', JSON.stringify([order]));
  });
  await page.reload();
  await page.waitForTimeout(1000);

  // Should show the order card
  await expect(page.locator('text=TEST')).toBeVisible();

  // Verify shares = 200 (400 risk / $2 stop distance)
  await expect(page.locator('text=200')).toBeVisible();
});
```

## Test 7: Execute Trade (1 share test)

```typescript
test('execute single trade end to end', async ({ page }) => {
  await page.goto('http://localhost:3000/trades');

  // Inject a minimal test order
  await page.evaluate(() => {
    const order = {
      symbol: 'SPY',
      direction: 'LONG',
      source: 'meridian',
      tier: 'test_e2e',
      price: 500,
      scores: {},
      orderType: 'market',
      entryPrice: 500,
      stopLoss: 490,
      stopMethod: 'manual',
      takeProfit: 520,
      rrRatio: 2.0,
      riskAmount: 10,
      shares: 1,
      tags: ['e2e_test'],
      notes: 'Playwright E2E test — 1 share'
    };
    localStorage.setItem('signalstack_trade_desk', JSON.stringify([order]));
  });
  await page.reload();
  await page.waitForTimeout(1000);

  // Should see SPY order card
  await expect(page.locator('text=SPY')).toBeVisible();

  // Click Execute
  await page.click('text=/Execute/');
  await page.waitForTimeout(500);

  // Confirm in modal
  await page.click('text=/Confirm/');
  await page.waitForTimeout(2000);

  // Check execution log
  await page.click('text=Trade Log');
  await page.waitForTimeout(1000);

  // Should see SPY in the log
  await expect(page.locator('text=SPY')).toBeVisible();
  await expect(page.locator('text=SUBMITTED')).toBeVisible();
});
```

## Running Tests

```bash
cd ~/SS/Meridian/ui/signalstack-app

# Make sure both servers are running first:
# Terminal 1: cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --port 8090
# Terminal 2: cd ~/SS/Meridian/ui/signalstack-app && npm run dev

# Run all E2E tests
npx playwright test e2e/ --headed

# Run single test
npx playwright test e2e/candidates.spec.ts --headed
```

Report: which tests pass, which fail, and what the failure looks like.
DO NOT modify the frontend code — only write tests. Report bugs.
