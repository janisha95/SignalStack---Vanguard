#!/usr/bin/env python3
"""
Test: Can Alpaca free IEX WebSocket handle 2000+ symbol minute bar subscriptions from Canada?

This script:
1. Connects to Alpaca IEX WebSocket
2. Subscribes to minute bars for 2000+ symbols
3. Waits 2 minutes to collect bars
4. Reports how many unique symbols received bars

Run during market hours (9:30-16:00 ET) for best results.
Outside market hours, you'll get 0 bars but the subscription should still succeed.

Usage:
    export APCA_API_KEY_ID=your_key
    export APCA_API_SECRET_KEY=your_secret
    python3 test_alpaca_scale.py
"""

import os
import sys
import json
import time
import asyncio
import signal
from datetime import datetime

# Try websockets library
try:
    import websockets
except ImportError:
    print("Installing websockets...")
    os.system(f"{sys.executable} -m pip install websockets --break-system-packages -q")
    import websockets


ALPACA_KEY = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_KEY")
ALPACA_SECRET = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET")

if not ALPACA_KEY or not ALPACA_SECRET:
    # Try reading from config files
    config_paths = [
        os.path.expanduser("~/SS/Vanguard/config/vanguard_config.json"),
        os.path.expanduser("~/SS/Meridian/config/config.json"),
        os.path.expanduser("~/.alpaca"),
    ]
    for p in config_paths:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                ALPACA_KEY = cfg.get("alpaca_key") or cfg.get("APCA_API_KEY_ID") or cfg.get("key")
                ALPACA_SECRET = cfg.get("alpaca_secret") or cfg.get("APCA_API_SECRET_KEY") or cfg.get("secret")
                if ALPACA_KEY:
                    print(f"[config] Loaded keys from {p}")
                    break
            except Exception:
                pass

if not ALPACA_KEY:
    print("ERROR: No Alpaca API keys found.")
    print("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY environment variables.")
    sys.exit(1)


# Get a large symbol list
def get_test_symbols(n=2500):
    """Get N symbols to test with. Uses Alpaca assets API."""
    import urllib.request

    url = "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    })

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            assets = json.loads(resp.read())

        # Filter to tradeable, non-OTC symbols
        tradeable = [
            a["symbol"] for a in assets
            if a.get("tradable") and a.get("exchange") in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")
            and not a.get("symbol", "").endswith(".U")
            and len(a.get("symbol", "")) <= 5  # skip long/weird symbols
        ]

        print(f"[assets] {len(tradeable)} tradeable symbols found from Alpaca")
        return tradeable[:n]

    except Exception as e:
        print(f"[assets] Failed to fetch assets: {e}")
        # Fallback: generate a basic list
        print("[assets] Using fallback symbol list (top 100 only)")
        return [
            "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK.B", "UNH", "XOM",
            "JPM", "JNJ", "V", "PG", "MA", "HD", "CVX", "MRK", "ABBV", "LLY",
            "PEP", "KO", "COST", "AVGO", "WMT", "MCD", "CSCO", "ACN", "TMO", "ABT",
            "DHR", "CRM", "NEE", "LIN", "TXN", "AMD", "PM", "UPS", "BMY", "RTX",
            "LOW", "AMGN", "HON", "UNP", "INTC", "QCOM", "ELV", "SPGI", "GS", "BA",
            "CAT", "AMAT", "BLK", "MDLZ", "GILD", "ADP", "ISRG", "BKNG", "VRTX", "ADI",
            "SYK", "MMC", "TJX", "REGN", "CVS", "CI", "PGR", "SCHW", "LRCX", "ZTS",
            "SNPS", "CDNS", "KLAC", "EOG", "ITW", "SLB", "BDX", "CME", "FISV", "AON",
            "MO", "SO", "DUK", "HUM", "CL", "ICE", "CSX", "WM", "NOC", "SHW",
            "PXD", "FCX", "NSC", "APD", "MCK", "EMR", "PH", "ORLY", "MSI", "GM",
        ]


async def test_websocket_scale():
    symbols = get_test_symbols(2500)
    print(f"\n[test] Will attempt to subscribe to {len(symbols)} symbols for minute bars")
    print(f"[test] Using IEX feed (free plan)")
    print(f"[test] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    ws_url = "wss://stream.data.alpaca.markets/v2/iex"

    bars_received = {}
    subscription_confirmed = False
    errors = []

    try:
        async with websockets.connect(ws_url, ping_interval=30) as ws:
            # Wait for connection message
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            print(f"[ws] Connected: {data}")

            # Authenticate
            auth_msg = json.dumps({
                "action": "auth",
                "key": ALPACA_KEY,
                "secret": ALPACA_SECRET,
            })
            await ws.send(auth_msg)

            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            auth_status = data[0].get("msg") if data else "unknown"
            print(f"[ws] Auth: {auth_status}")

            if auth_status != "authenticated":
                print(f"[ws] AUTH FAILED: {data}")
                return

            # Subscribe to minute bars for ALL symbols
            # Send in batches to avoid message size limits
            batch_size = 500
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                sub_msg = json.dumps({
                    "action": "subscribe",
                    "bars": batch,
                })
                await ws.send(sub_msg)
                print(f"[ws] Sent subscription batch {i // batch_size + 1}: {len(batch)} symbols ({i + len(batch)}/{len(symbols)})")

                # Read confirmation
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(msg)
                if data and data[0].get("T") == "subscription":
                    subscribed_bars = len(data[0].get("bars", []))
                    print(f"[ws] Confirmed: {subscribed_bars} bars subscribed")
                    subscription_confirmed = True
                elif data and data[0].get("T") == "error":
                    error_msg = data[0].get("msg", "unknown error")
                    errors.append(error_msg)
                    print(f"[ws] ERROR: {error_msg}")

                await asyncio.sleep(0.5)  # Small delay between batches

            if not subscription_confirmed:
                print("[ws] WARNING: No subscription confirmation received")

            # Listen for bars for 2 minutes
            print(f"\n[ws] Listening for minute bars for 120 seconds...")
            print(f"[ws] (If outside market hours, expect 0 bars)")
            print()

            start = time.time()
            timeout = 120  # 2 minutes

            while time.time() - start < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)

                    for item in data:
                        if item.get("T") == "b":  # minute bar
                            sym = item.get("S")
                            bars_received[sym] = bars_received.get(sym, 0) + 1

                    # Progress update every 30 seconds
                    elapsed = int(time.time() - start)
                    if elapsed > 0 and elapsed % 30 == 0:
                        print(f"[ws] {elapsed}s elapsed: {len(bars_received)} unique symbols received bars")

                except asyncio.TimeoutError:
                    elapsed = int(time.time() - start)
                    if elapsed % 30 == 0:
                        print(f"[ws] {elapsed}s elapsed: {len(bars_received)} unique symbols (waiting...)")

    except Exception as e:
        print(f"\n[ws] CONNECTION ERROR: {e}")
        errors.append(str(e))

    # Report results
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Symbols requested:     {len(symbols)}")
    print(f"Subscription confirmed: {'YES' if subscription_confirmed else 'NO'}")
    print(f"Unique symbols w/bars: {len(bars_received)}")
    print(f"Total bars received:   {sum(bars_received.values())}")
    print(f"Errors:                {len(errors)}")

    if errors:
        print(f"\nErrors:")
        for e in errors:
            print(f"  - {e}")

    if bars_received:
        top = sorted(bars_received.items(), key=lambda x: -x[1])[:10]
        print(f"\nTop 10 symbols by bar count:")
        for sym, count in top:
            print(f"  {sym}: {count} bars")

    print("\n" + "=" * 60)
    if subscription_confirmed and not errors:
        print("VERDICT: Alpaca IEX WebSocket ACCEPTS {}-symbol subscription from Canada".format(len(symbols)))
        if bars_received:
            print("         AND delivers real-time minute bars.")
        else:
            print("         (No bars received — likely outside market hours. Retry during RTH.)")
        print("         USE THIS for US equity data. $0/mo.")
    elif errors:
        print("VERDICT: FAILED — errors during subscription.")
        print("         Need alternative provider for US equities.")
    else:
        print("VERDICT: UNCLEAR — subscription sent but no confirmation.")
        print("         Retry during market hours for definitive test.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_websocket_scale())
