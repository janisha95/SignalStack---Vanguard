#!/usr/bin/env python3
"""
reconcile_once.py — One-shot reconciliation for Phase 3c.

Usage:
    python3 scripts/reconcile_once.py --profile-id gft_10k_qa

Behavior:
    1. Load runtime config, resolve MetaApi credentials.
    2. Call MetaApiClient.get_open_positions().
    3. Load vanguard_open_positions and vanguard_trade_journal (status=OPEN) for profile.
    4. Call detect_divergences().
    5. Print divergence summary to stdout.
    6. Call log_divergences() to persist to vanguard_reconciliation_log.
    7. Exit 0 (detection always expected to succeed even if divergences found).
    8. Exit 2 on MetaApi outage — no log rows written.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reconcile_once")


# ---------------------------------------------------------------------------
# Env helpers (reuse the same pattern as metaapi_fetch_once.py)
# ---------------------------------------------------------------------------

def _raw_env_extract(var: str) -> str:
    env_path = _REPO_ROOT.parent / ".env"
    if not env_path.exists():
        return ""
    content = env_path.read_text()
    matches = re.findall(rf'{re.escape(var)}=([^\s\n]+)', content)
    return matches[0] if matches else ""


def _resolve_env(value: str) -> str:
    if str(value or "").startswith("ENV:"):
        var = value[4:].strip()
        resolved = os.environ.get(var, "")
        if not resolved or len(resolved) < 20:
            resolved = _raw_env_extract(var)
        if not resolved:
            sys.exit(f"[FATAL] ENV:{var} is not set in environment or .env — cannot proceed.")
        return resolved
    return value


def _load_bridge_config(profile_id: str) -> dict:
    cfg_path = _REPO_ROOT / "config" / "vanguard_runtime.json"
    if not cfg_path.exists():
        sys.exit(f"[FATAL] config not found: {cfg_path}")
    with open(cfg_path) as f:
        cfg = json.load(f)
    bridge_key = f"metaapi_{profile_id}"
    bridges = cfg.get("execution", {}).get("bridges", {})
    bridge = bridges.get(bridge_key)
    if bridge is None:
        sys.exit(
            f"[FATAL] execution.bridges.{bridge_key} not found in runtime config.\n"
            f"Available bridges: {list(bridges.keys())}"
        )
    return bridge


def _db_path() -> str:
    db = os.environ.get("VANGUARD_SOURCE_DB", "")
    if not db:
        db = str(_REPO_ROOT / "data" / "vanguard_universe.db")
    return db


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(profile_id: str, dry_run: bool = False, act: bool = False) -> None:
    from vanguard.execution.metaapi_client import MetaApiClient, MetaApiUnavailable
    from vanguard.execution.reconciler import (
        detect_divergences,
        ensure_table,
        load_journal_open,
        load_open_positions,
        log_divergences,
        resolve_divergences,
    )

    bridge    = _load_bridge_config(profile_id)
    account_id = _resolve_env(bridge["account_id"])
    api_token  = _resolve_env(bridge["api_token"])
    base_url   = _resolve_env(bridge.get("base_url", ""))
    timeout_s  = int(bridge.get("timeout_s", 30))

    db_path = _db_path()
    logger.info("profile=%s db=%s", profile_id, db_path)

    # ── Fetch open positions from broker ──────────────────────────────────
    client = MetaApiClient(
        account_id=account_id, api_token=api_token,
        base_url=base_url, timeout_s=timeout_s,
    )
    logger.info("Fetching open positions from MetaApi…")
    try:
        broker_positions = await client.get_open_positions()
    except MetaApiUnavailable as exc:
        print(f"[ERROR] MetaApi unavailable: {exc}", file=sys.stderr)
        print("[ERROR] Reconciliation ABORTED — no divergence rows written.", file=sys.stderr)
        sys.exit(2)

    logger.info("Broker has %d open position(s)", len(broker_positions))

    # ── Load DB state ─────────────────────────────────────────────────────
    db_open_positions = load_open_positions(db_path, profile_id)
    db_journal_open   = load_journal_open(db_path, profile_id)
    logger.info(
        "DB mirror: %d open_positions, %d journal_open",
        len(db_open_positions), len(db_journal_open),
    )

    # ── Detect divergences ────────────────────────────────────────────────
    divergences = detect_divergences(
        db_path=db_path,
        profile_id=profile_id,
        broker_positions=broker_positions,
        db_open_positions=db_open_positions,
        db_journal_open=db_journal_open,
    )

    # ── Print summary ─────────────────────────────────────────────────────
    detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'='*60}")
    print(f"Reconciliation — {profile_id} — {detected_at}")
    print(f"{'='*60}")
    print(f"Broker positions:    {len(broker_positions)}")
    print(f"DB open_positions:   {len(db_open_positions)}")
    print(f"Journal open rows:   {len(db_journal_open)}")
    print(f"Divergences found:   {len(divergences)}")

    if divergences:
        print(f"\n{'─'*60}")
        for i, d in enumerate(divergences, 1):
            print(f"\n[{i}] {d.divergence_type}")
            print(f"    profile_id:         {d.profile_id}")
            print(f"    broker_position_id: {d.broker_position_id}")
            print(f"    db_trade_id:        {d.db_trade_id}")
            print(f"    symbol:             {d.symbol}")
            print(f"    details:            {json.dumps(d.details, indent=6)}")
    else:
        print("\n  ✓ No divergences detected.")

    # ── Log or act ────────────────────────────────────────────────────────
    if dry_run:
        print("\n  [dry-run] No rows written.")
    elif act:
        ensure_table(db_path)
        # In act mode: resolve divergences (each resolver writes its own recon log row)
        if divergences:
            print(f"\n  [--act] Resolving {len(divergences)} divergence(s)…")
            counts = resolve_divergences(
                db_path=db_path,
                divergences=divergences,
                broker_positions=broker_positions,
            )
            print(f"  Resolved: {counts['resolved']}, Failed: {counts['failed']}")
        else:
            print("\n  [--act] No divergences to resolve.")
    else:
        ensure_table(db_path)
        rows_logged = log_divergences(db_path, divergences, detected_at)
        print(f"\n  {rows_logged} divergence row(s) written to vanguard_reconciliation_log.")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-shot reconciliation.")
    parser.add_argument("--profile-id", required=True,
                        help="Profile ID matching execution.bridges.metaapi_{profile_id}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect divergences but do not write to reconciliation_log")
    parser.add_argument("--act", action="store_true",
                        help="Detect AND resolve divergences (DB-only, no broker calls)")
    args = parser.parse_args()
    asyncio.run(main(args.profile_id, dry_run=args.dry_run, act=args.act))
