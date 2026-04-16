#!/usr/bin/env python3
"""
vanguard_selection_tradeability.py — V5.1 → V5.2 selection stage.

Config-gated replacement for the legacy simple V5 selector. It writes the
dedicated Model V2 result table and mirrors only selected rows into the legacy
shortlist tables for V6 compatibility.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vanguard.config.runtime_config import get_runtime_config
from vanguard.helpers.db import connect_wal
from vanguard.selection.crypto_symbol_state import build_crypto_symbol_state, load_crypto_context_config
from vanguard.selection.model_v2_outcomes import persist_model_v2_results
from vanguard.selection.shortlisting import evaluate_cycle_selection, select_shortlist
from vanguard.selection.tradeability import evaluate_cycle_tradeability, load_tradeability_config


DEFAULT_DB = ROOT / "data" / "vanguard_universe.db"
DEFAULT_TRADEABILITY_CONFIG = ROOT / "config" / "vanguard_v5_tradeability.json"
DEFAULT_SELECTION_CONFIG = ROOT / "config" / "vanguard_v5_selection.json"
DEFAULT_CRYPTO_CONTEXT_CONFIG = ROOT / "config" / "vanguard_crypto_context.json"


def run(
    *,
    dry_run: bool = False,
    db_path: str | Path = DEFAULT_DB,
    cycle_ts_utc: str | None = None,
    profile_ids: list[str] | None = None,
    mirror_shortlist: bool | None = None,
    persist_outcomes: bool | None = None,
) -> dict[str, Any]:
    runtime_cfg = get_runtime_config()
    v5_cfg = runtime_cfg.get("v5") or {}
    if str(v5_cfg.get("selection_engine") or "tradeability_v1") == "simple_legacy":
        from stages.vanguard_selection_simple import run as legacy_run

        return legacy_run(dry_run=dry_run)

    tradeability_cfg = load_tradeability_config(DEFAULT_TRADEABILITY_CONFIG)
    selection_cfg = json.loads(DEFAULT_SELECTION_CONFIG.read_text())
    defaults = selection_cfg.get("defaults") or {}
    outcome_cfg = defaults.get("outcome_tracking") or {}
    horizon_minutes = int(outcome_cfg.get("model_horizon_minutes") or 120)
    if mirror_shortlist is None:
        mirror_shortlist = bool(v5_cfg.get("mirror_shortlist", True))
    if persist_outcomes is None:
        persist_outcomes = bool(outcome_cfg.get("enabled", True))

    with connect_wal(str(db_path)) as con:
        assets = _prediction_assets(con, cycle_ts_utc)
        if "crypto" in assets and not dry_run:
            build_crypto_symbol_state(
                db_path=db_path,
                runtime_config=runtime_cfg,
                context_config=load_crypto_context_config(DEFAULT_CRYPTO_CONTEXT_CONFIG),
                cycle_ts_utc=cycle_ts_utc or _latest_prediction_cycle(con),
                profile_ids=profile_ids,
                persist=True,
            )
        tradeability_rows = evaluate_cycle_tradeability(
            con,
            tradeability_cfg,
            cycle_ts_utc=cycle_ts_utc,
            profile_ids=profile_ids,
            asset_classes=assets or None,
            persist=not dry_run,
        )
        effective_cycle_ts = (
            str(tradeability_rows[0].get("cycle_ts_utc"))
            if tradeability_rows
            else cycle_ts_utc
        )
        if dry_run:
            selection_rows = select_shortlist(
                tradeability_rows,
                selection_cfg,
                history_rows=[],
            )
            outcome_rows = 0
        else:
            selection_rows = evaluate_cycle_selection(
                con,
                selection_cfg,
                cycle_ts_utc=effective_cycle_ts,
                profile_ids=profile_ids,
                persist=True,
                mirror_shortlist=mirror_shortlist,
            )
            outcome_rows = (
                persist_model_v2_results(con, selection_rows, horizon_minutes=horizon_minutes)
                if persist_outcomes
                else 0
            )

    counts: dict[str, int] = {}
    selected = 0
    for row in selection_rows:
        state = str(row.get("selection_state") or "UNKNOWN")
        counts[state] = counts.get(state, 0) + 1
        selected += int(row.get("selected") or 0)
    if not tradeability_rows:
        status = "no_predictions"
    elif not selection_rows:
        status = "no_candidates"
    else:
        status = "ok"
    return {
        "status": status,
        "rows": len(selection_rows),
        "tradeability_rows": len(tradeability_rows),
        "selected": selected,
        "outcome_rows": outcome_rows,
        "counts": counts,
        "mirrored_shortlist": bool(mirror_shortlist and not dry_run),
    }


def _latest_prediction_cycle(con) -> str | None:
    row = con.execute("SELECT MAX(cycle_ts_utc) FROM vanguard_predictions").fetchone()
    return row[0] if row and row[0] else None


def _prediction_assets(con, cycle_ts_utc: str | None) -> list[str]:
    cycle_ts = cycle_ts_utc or _latest_prediction_cycle(con)
    if not cycle_ts:
        return []
    rows = con.execute(
        "SELECT DISTINCT asset_class FROM vanguard_predictions WHERE cycle_ts_utc = ? ORDER BY asset_class",
        (cycle_ts,),
    ).fetchall()
    return [str(row[0]).lower() for row in rows if row[0]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run V5.1 economics + V5.2 selection.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--cycle-ts-utc", default=None)
    parser.add_argument("--profile-id", action="append", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-mirror-shortlist", action="store_true")
    parser.add_argument("--no-outcomes", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    result = run(
        dry_run=args.dry_run,
        db_path=args.db,
        cycle_ts_utc=args.cycle_ts_utc,
        profile_ids=args.profile_id,
        mirror_shortlist=not args.no_mirror_shortlist,
        persist_outcomes=not args.no_outcomes,
    )
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "V5 tradeability selection: "
            f"status={result['status']} rows={result['rows']} selected={result['selected']} "
            f"outcomes={result['outcome_rows']} counts={result['counts']}"
        )
    return 0 if result["status"] != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
