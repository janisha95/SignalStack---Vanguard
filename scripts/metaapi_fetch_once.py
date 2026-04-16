#!/usr/bin/env python3
"""
metaapi_fetch_once.py — One-shot MetaApi open-positions sync for Phase 3a.

Usage:
    python3 scripts/metaapi_fetch_once.py --profile-id gft_10k_qa

Behavior:
    1. Loads runtime config, reads execution.bridges.metaapi_<profile_id> block.
    2. Instantiates MetaApiClient (reads account_id / api_token from ENV: references).
    3. Calls get_open_positions() and get_account_state().
    4. On success: upserts into vanguard_open_positions, prints results as JSON.
    5. On MetaApiUnavailable: prints error to stderr, exits 2, writes NOTHING to DB.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add repo root to path so imports resolve from any CWD
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("metaapi_fetch_once")


def _raw_env_extract(var: str) -> str:
    """
    Fallback: extract VAR=value from .env file using regex.
    Handles cases where python-dotenv fails (e.g., two KEY=VALUE pairs on one line).
    """
    import re
    env_path = _REPO_ROOT.parent / ".env"
    if not env_path.exists():
        return ""
    content = env_path.read_text()
    matches = re.findall(rf'{re.escape(var)}=([^\s\n]+)', content)
    return matches[0] if matches else ""


def _resolve_env(value: str) -> str:
    """Replace ENV:VAR_NAME with os.environ[VAR_NAME], with .env regex fallback."""
    if str(value or "").startswith("ENV:"):
        var = value[4:].strip()
        resolved = os.environ.get(var, "")
        # Fallback: env file may have METAAPI_TOKEN concatenated on same line as another var
        if not resolved or len(resolved) < 20:
            resolved = _raw_env_extract(var)
        if not resolved:
            sys.exit(f"[FATAL] ENV:{var} is not set in environment or .env — cannot proceed.")
        return resolved
    return value


def _load_bridge_config(profile_id: str) -> dict:
    """Load execution.bridges.metaapi_{profile_id} from vanguard_runtime.json."""
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
    """Resolve the DB path from config or env."""
    db = os.environ.get("VANGUARD_SOURCE_DB", "")
    if not db:
        default = _REPO_ROOT / "data" / "vanguard_universe.db"
        db = str(default)
    return db


async def main(profile_id: str) -> None:
    from vanguard.execution.metaapi_client import MetaApiClient, MetaApiUnavailable
    from vanguard.execution.open_positions_writer import upsert_open_positions, ensure_table

    bridge = _load_bridge_config(profile_id)
    account_id = _resolve_env(bridge["account_id"])
    api_token  = _resolve_env(bridge["api_token"])
    base_url   = _resolve_env(bridge.get("base_url", ""))
    timeout_s  = int(bridge.get("timeout_s", 30))

    logger.info("profile=%s account_id=%s timeout=%ds", profile_id, account_id, timeout_s)

    client = MetaApiClient(
        account_id=account_id,
        api_token=api_token,
        base_url=base_url,
        timeout_s=timeout_s,
    )

    db_path = _db_path()
    ensure_table(db_path)

    # ── Fetch account state ────────────────────────────────────────────────────
    logger.info("Fetching account state…")
    try:
        account_state = await client.get_account_state()
        logger.info("Account state: %s", account_state)
    except MetaApiUnavailable as exc:
        print(f"[ERROR] MetaApi unavailable: {exc}", file=sys.stderr)
        print("[ERROR] DB NOT written — stale data preserved.", file=sys.stderr)
        sys.exit(2)

    # ── Fetch open positions ───────────────────────────────────────────────────
    logger.info("Fetching open positions…")
    try:
        positions = await client.get_open_positions()
    except MetaApiUnavailable as exc:
        print(f"[ERROR] MetaApi unavailable: {exc}", file=sys.stderr)
        print("[ERROR] DB NOT written — stale data preserved.", file=sys.stderr)
        sys.exit(2)

    # ── Upsert ────────────────────────────────────────────────────────────────
    synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    upsert_open_positions(
        db_path=db_path,
        profile_id=profile_id,
        positions=positions,
        synced_at_utc=synced_at,
    )

    # ── Print results ─────────────────────────────────────────────────────────
    output = {
        "profile_id":    profile_id,
        "synced_at_utc": synced_at,
        "account_state": account_state,
        "open_positions": positions,
        "position_count": len(positions),
    }
    print(json.dumps(output, indent=2, default=str))
    logger.info("Done. %d position(s) synced to %s", len(positions), db_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-shot MetaApi position sync.")
    parser.add_argument(
        "--profile-id",
        required=True,
        help="Profile ID matching execution.bridges.metaapi_{profile_id} in runtime config.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.profile_id))
