"""
TTP Playwright Daemon — Headless browser monitor for TTP web interface.

Logs in once, polls Positions/Working Orders/Filled Orders/Account every
`poll_interval_seconds` (default 30s), writes snapshots to SQLite.

This daemon only writes to the database.
UI/FastAPI integration is handled separately.

Config block in vanguard_runtime.json:
    "ttp_daemon": {
        "enabled": true,
        "username": "ENV:TTP_USERNAME",
        "password": "ENV:TTP_PASSWORD",
        "account_id": "SEV10K2767",
        "poll_interval_seconds": 30,
        "headless": true,
        "login_url": "https://app.tradethepool.com/",
        "db_path": "/Users/sjani008/SS/Vanguard_QAenv/data/ttp_daemon.db"
    }

SELECTOR STATUS (Task 7.3):
  All selectors below are discovered defaults. Run with headless=false first
  and use Chrome DevTools (Cmd+Shift+C) to verify/correct them for the live
  TTP interface. Update SELECTORS dict and re-run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ttp_playwright_daemon")

# ---------------------------------------------------------------------------
# Selector registry — update here after Task 7.3 discovery
# ---------------------------------------------------------------------------

SELECTORS = {
    # Login page
    "email_input":    'input[type="email"], input[name="email"], input[placeholder*="email" i]',
    "password_input": 'input[type="password"]',
    "submit_button":  'button[type="submit"]',

    # Post-login navigation
    "positions_tab":      'text=Positions',
    "working_orders_tab": 'text=Working Orders',
    "filled_orders_tab":  'text=Filled Orders',

    # Tables (broad fallback; TTP uses React so actual selectors may differ)
    "table_body_row": "table tbody tr",
    "table_cell":     "td",

    # Account balance — TTP typically shows this in a header/panel
    # NEEDS VERIFICATION against live site
    "account_balance": '.account-balance, [data-testid="balance"], .balance-display',
}

# Column order for Positions table (0-indexed) — VERIFY against live TTP
_POSITIONS_COL = {
    "symbol":        0,
    "side":          1,
    "quantity":      2,
    "open_price":    3,
    "current_price": 4,
    "fee":           5,
    "net_pnl":       6,
}

# Column order for Working Orders table — VERIFY
_WORKING_COL = {
    "symbol":      0,
    "order_type":  1,
    "side":        2,
    "quantity":    3,
    "price":       4,
    "stop_price":  5,
    "validity":    6,
    "order_id":    7,
}

# Column order for Filled Orders table — VERIFY
_FILLED_COL = {
    "order_id":   0,
    "symbol":     1,
    "side":       2,
    "quantity":   3,
    "fill_price": 4,
    "fill_time":  5,
    "pnl":        6,
}


def _clean_float(text: str) -> float:
    """Strip currency symbols/whitespace and parse as float. Returns 0.0 on failure."""
    try:
        return float(str(text).replace("$", "").replace(",", "").replace(" USD", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def _resolve_env(value: str) -> str:
    if str(value or "").startswith("ENV:"):
        var = value[4:].strip()
        return os.environ.get(var, "")
    return value


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS ttp_positions (
    snapshot_ts  TEXT,
    account_id   TEXT,
    symbol       TEXT,
    side         TEXT,
    quantity     REAL,
    open_price   REAL,
    current_price REAL,
    fee          REAL,
    net_pnl      REAL,
    symbol_type  TEXT DEFAULT 'Equities',
    PRIMARY KEY (snapshot_ts, account_id, symbol)
);
CREATE TABLE IF NOT EXISTS ttp_working_orders (
    snapshot_ts  TEXT,
    account_id   TEXT,
    order_id     TEXT,
    symbol       TEXT,
    order_type   TEXT,
    side         TEXT,
    quantity     REAL,
    price        REAL,
    stop_price   REAL,
    validity     TEXT,
    status       TEXT DEFAULT 'WORKING',
    created_at   TEXT,
    PRIMARY KEY (snapshot_ts, account_id, order_id)
);
CREATE TABLE IF NOT EXISTS ttp_filled_orders (
    snapshot_ts  TEXT,
    account_id   TEXT,
    order_id     TEXT,
    symbol       TEXT,
    side         TEXT,
    quantity     REAL,
    fill_price   REAL,
    fill_time    TEXT,
    pnl          REAL,
    PRIMARY KEY (snapshot_ts, account_id, order_id)
);
CREATE TABLE IF NOT EXISTS ttp_account_state (
    snapshot_ts      TEXT,
    account_id       TEXT,
    balance          REAL,
    equity           REAL,
    buying_power     REAL DEFAULT 0,
    day_pnl          REAL DEFAULT 0,
    total_pnl        REAL DEFAULT 0,
    positions_count  INTEGER DEFAULT 0,
    orders_count     INTEGER DEFAULT 0,
    PRIMARY KEY (snapshot_ts, account_id)
);
CREATE INDEX IF NOT EXISTS idx_ttp_pos_ts  ON ttp_positions(snapshot_ts DESC);
CREATE INDEX IF NOT EXISTS idx_ttp_acct_ts ON ttp_account_state(snapshot_ts DESC);
"""


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class TTPPlaywrightDaemon:
    """Headless TTP browser monitor that writes position/order snapshots to SQLite."""

    def __init__(self, config: dict) -> None:
        self.config        = config
        self.db_path       = config["db_path"]
        self.poll_interval = int(config.get("poll_interval_seconds", 30))
        self.account_id    = config.get("account_id", "")
        self._browser      = None
        self._context      = None
        self._page         = None
        self._playwright   = None
        self._init_db()

    # ── DB ────────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_DDL)
        conn.commit()
        conn.close()
        log.info("[TTP_DAEMON] DB initialised at %s", self.db_path)

    def write_snapshot(
        self,
        snapshot_ts: str,
        positions: list[dict],
        working: list[dict],
        filled: list[dict],
        account_state: dict,
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            for pos in positions:
                conn.execute(
                    "INSERT OR REPLACE INTO ttp_positions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_ts, self.account_id,
                        pos.get("symbol", ""), pos.get("side", ""),
                        pos.get("quantity", 0), pos.get("open_price", 0),
                        pos.get("current_price", 0), pos.get("fee", 0),
                        pos.get("net_pnl", 0), "Equities",
                    ),
                )

            for order in working:
                conn.execute(
                    "INSERT OR REPLACE INTO ttp_working_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_ts, self.account_id,
                        order.get("order_id", ""), order.get("symbol", ""),
                        order.get("order_type", ""), order.get("side", ""),
                        order.get("quantity", 0), order.get("price", 0),
                        order.get("stop_price", 0), order.get("validity", ""),
                        "WORKING", snapshot_ts,
                    ),
                )

            for fill in filled:
                conn.execute(
                    "INSERT OR REPLACE INTO ttp_filled_orders VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_ts, self.account_id,
                        fill.get("order_id", ""), fill.get("symbol", ""),
                        fill.get("side", ""), fill.get("quantity", 0),
                        fill.get("fill_price", 0), fill.get("fill_time", ""),
                        fill.get("pnl", 0),
                    ),
                )

            conn.execute(
                "INSERT OR REPLACE INTO ttp_account_state VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_ts, self.account_id,
                    account_state.get("balance", 0), account_state.get("equity", 0),
                    account_state.get("buying_power", 0), account_state.get("day_pnl", 0),
                    account_state.get("total_pnl", 0),
                    len(positions), len(working),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ── Browser session ───────────────────────────────────────────────────

    async def login(self) -> None:
        """Launch browser, navigate to TTP, fill credentials, wait for dashboard."""
        from playwright.async_api import async_playwright  # noqa: PLC0415

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=self.config.get("headless", True),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
        )
        self._page = await self._context.new_page()

        login_url = self.config.get("login_url", "https://app.tradethepool.com/")
        log.info("[TTP_DAEMON] Navigating to %s", login_url)
        await self._page.goto(login_url, wait_until="networkidle", timeout=30000)

        username = _resolve_env(self.config.get("username", ""))
        password = _resolve_env(self.config.get("password", ""))

        if not username or not password:
            raise RuntimeError(
                "TTP credentials not set. Export TTP_USERNAME and TTP_PASSWORD "
                "environment variables."
            )

        # Fill credentials
        await self._page.wait_for_selector(SELECTORS["email_input"], timeout=15000)
        await self._page.fill(SELECTORS["email_input"], username)
        await self._page.fill(SELECTORS["password_input"], password)
        await self._page.click(SELECTORS["submit_button"])

        # Wait for post-login navigation (broad URL match)
        await self._page.wait_for_url("**/*", wait_until="networkidle", timeout=20000)
        log.info("[TTP_DAEMON] Logged in — current URL: %s", self._page.url)

        # Select account if account_id configured
        if self.account_id:
            try:
                # TTP account selector — selector NEEDS VERIFICATION (Task 7.3)
                acct_selector = (
                    f'[data-testid="account-selector"], '
                    f'.account-selector, '
                    f'text={self.account_id}'
                )
                acct_el = await self._page.query_selector(acct_selector)
                if acct_el:
                    await acct_el.click()
                    await self._page.wait_for_load_state("networkidle", timeout=5000)
                    log.info("[TTP_DAEMON] Selected account %s", self.account_id)
            except Exception as exc:
                log.warning("[TTP_DAEMON] Could not select account %s: %s", self.account_id, exc)

    async def _scrape_table(
        self,
        tab_selector: str,
        col_map: dict[str, int],
    ) -> list[dict[str, Any]]:
        """
        Click a tab, wait for table to render, scrape rows.

        Returns a list of dicts keyed by col_map names.
        Empty list on any failure (non-fatal).
        """
        try:
            await self._page.click(tab_selector)
            await self._page.wait_for_selector("table", timeout=6000)
            rows = await self._page.query_selector_all(SELECTORS["table_body_row"])
            result = []
            for row in rows:
                cells = await row.query_selector_all(SELECTORS["table_cell"])
                texts = [await c.inner_text() for c in cells]
                if not any(t.strip() for t in texts):
                    continue  # skip empty rows
                record: dict[str, Any] = {}
                for field, idx in col_map.items():
                    record[field] = texts[idx].strip() if idx < len(texts) else ""
                result.append(record)
            return result
        except Exception as exc:
            log.warning("[TTP_DAEMON] _scrape_table(%s) failed: %s", tab_selector, exc)
            return []

    async def poll_positions(self) -> list[dict[str, Any]]:
        rows = await self._scrape_table(SELECTORS["positions_tab"], _POSITIONS_COL)
        parsed = []
        for r in rows:
            parsed.append({
                "symbol":        r.get("symbol", ""),
                "side":          r.get("side", ""),
                "quantity":      _clean_float(r.get("quantity", "")),
                "open_price":    _clean_float(r.get("open_price", "")),
                "current_price": _clean_float(r.get("current_price", "")),
                "fee":           _clean_float(r.get("fee", "")),
                "net_pnl":       _clean_float(r.get("net_pnl", "")),
            })
        return parsed

    async def poll_working_orders(self) -> list[dict[str, Any]]:
        rows = await self._scrape_table(SELECTORS["working_orders_tab"], _WORKING_COL)
        parsed = []
        for r in rows:
            parsed.append({
                "order_id":  r.get("order_id", ""),
                "symbol":    r.get("symbol", ""),
                "order_type": r.get("order_type", ""),
                "side":      r.get("side", ""),
                "quantity":  _clean_float(r.get("quantity", "")),
                "price":     _clean_float(r.get("price", "")),
                "stop_price": _clean_float(r.get("stop_price", "")),
                "validity":  r.get("validity", ""),
            })
        return parsed

    async def poll_filled_orders(self) -> list[dict[str, Any]]:
        rows = await self._scrape_table(SELECTORS["filled_orders_tab"], _FILLED_COL)
        parsed = []
        for r in rows:
            parsed.append({
                "order_id":  r.get("order_id", ""),
                "symbol":    r.get("symbol", ""),
                "side":      r.get("side", ""),
                "quantity":  _clean_float(r.get("quantity", "")),
                "fill_price": _clean_float(r.get("fill_price", "")),
                "fill_time": r.get("fill_time", ""),
                "pnl":       _clean_float(r.get("pnl", "")),
            })
        return parsed

    async def poll_account_state(self) -> dict[str, Any]:
        """Scrape balance/equity from the TTP header/panel area."""
        try:
            el = await self._page.query_selector(SELECTORS["account_balance"])
            if el:
                text = await el.inner_text()
                balance = _clean_float(text)
                return {"balance": balance, "equity": balance}
        except Exception as exc:
            log.debug("[TTP_DAEMON] account_state scrape failed: %s", exc)
        return {"balance": 0.0, "equity": 0.0}

    # ── Main loop ─────────────────────────────────────────────────────────

    def _send_failure_alert(self, error: str) -> None:
        try:
            from vanguard.execution.telegram_alerts import send_telegram  # noqa: PLC0415
            send_telegram(f"TTP Daemon Failure: {error}")
        except Exception:
            pass

    async def run_loop(self) -> None:
        """Login once, then poll every poll_interval_seconds until interrupted."""
        await self.login()
        consecutive_failures = 0

        while True:
            try:
                snapshot_ts = datetime.now(timezone.utc).isoformat()

                positions = await self.poll_positions()
                working   = await self.poll_working_orders()
                filled    = await self.poll_filled_orders()
                account   = await self.poll_account_state()

                self.write_snapshot(snapshot_ts, positions, working, filled, account)

                log.info(
                    "[TTP_DAEMON] Snapshot %s: %d positions, balance=%.2f",
                    snapshot_ts, len(positions), account.get("balance", 0),
                )
                consecutive_failures = 0

            except Exception as exc:
                consecutive_failures += 1
                log.error("[TTP_DAEMON] Poll error #%d: %s", consecutive_failures, exc)

                if consecutive_failures >= 3:
                    log.warning("[TTP_DAEMON] 3 consecutive failures — attempting re-login")
                    try:
                        if self._browser:
                            await self._browser.close()
                        self._browser = None
                        self._context = None
                        self._page = None
                        await self.login()
                        consecutive_failures = 0
                        log.info("[TTP_DAEMON] Re-login successful")
                    except Exception as relogin_err:
                        log.error("[TTP_DAEMON] Re-login failed: %s", relogin_err)
                        self._send_failure_alert(str(relogin_err))

            await asyncio.sleep(self.poll_interval)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        stream=sys.stdout,
    )

    _repo_root = Path(__file__).resolve().parent.parent.parent
    cfg_path   = _repo_root / "config" / "vanguard_runtime.json"
    with open(cfg_path) as f:
        cfg = json.load(f)

    daemon_cfg = cfg.get("ttp_daemon", {})
    if not daemon_cfg.get("enabled"):
        print("TTP daemon disabled in config (ttp_daemon.enabled=false)")
        return

    daemon = TTPPlaywrightDaemon(daemon_cfg)
    asyncio.run(daemon.run_loop())


if __name__ == "__main__":
    main()
