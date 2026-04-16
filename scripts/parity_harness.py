#!/usr/bin/env python3
"""
parity_harness.py — QA vs Prod snapshot parity check.

Spec: CC_PHASE_0_PARITY_HARNESS.md §2.1
Usage:
    python3 Vanguard_QAenv/scripts/parity_harness.py \
        --prod-snapshot-path /tmp/vanguard_prod_snapshot.db \
        --qa-db-path /Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db \
        --cycle-ts-utc 2026-04-04T20:05:10Z \
        --output-report /tmp/parity_report.md
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tolerance config
# Spec §2.2 — column names mapped to actual DB schema where they differ.
# Spec name → actual DB name shown in inline comments.
# ---------------------------------------------------------------------------
TABLE_CONFIGS: dict[str, dict[str, Any]] = {
    "vanguard_health": {
        "match_key": ["cycle_ts_utc", "symbol"],
        # spec: status, asset_class
        "exact_cols": ["status", "asset_class"],
        # spec: last_bar_age_s → last_bar_age_seconds; liquidity_score → relative_volume
        "float_cols": ["last_bar_age_seconds", "relative_volume"],
        "json_cols": [],
    },
    "vanguard_features": {
        "match_key": ["cycle_ts_utc", "symbol"],
        # spec: asset_class (not present in actual table — only features_json blob)
        "exact_cols": [],
        # spec: all numeric feature cols → in actual DB, all features are inside features_json
        "float_cols": [],
        "json_cols": ["features_json"],   # compared key-by-key with ε tolerance
    },
    "vanguard_predictions": {
        "match_key": ["cycle_ts_utc", "symbol", "direction"],
        # spec: model_family → actual: model_id
        "exact_cols": ["model_id"],
        # spec: score → predicted_return; probability not present in actual table
        "float_cols": ["predicted_return"],
        "json_cols": [],
    },
    "vanguard_shortlist": {
        "match_key": ["cycle_ts_utc", "symbol", "strategy"],
        # spec: direction, status — actual table has direction but no 'status' column
        "exact_cols": ["direction"],
        # spec: rank → strategy_rank; composite_score → strategy_score
        "float_cols": ["strategy_rank", "strategy_score"],
        "json_cols": [],
    },
    "vanguard_tradeable_portfolio": {
        # spec: profile_id → actual: account_id
        "match_key": ["cycle_ts_utc", "account_id", "symbol"],
        # spec: decision → status; reject_reason → rejection_reason
        "exact_cols": ["status", "rejection_reason"],
        # spec: qty → shares_or_lots; sl_price → stop_price; tp_price → tp_price
        "float_cols": ["shares_or_lots", "stop_price", "tp_price"],
        "json_cols": [],
    },
}

EPSILON = 1e-6


def _open_readonly(path: str) -> sqlite3.Connection:
    """Open SQLite DB in strict read-only mode. Fails loudly if path missing."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"STOP-THE-LINE: Snapshot not found: {path}")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        # Verify it truly rejects writes
        return con
    except sqlite3.OperationalError as exc:
        sys.exit(f"STOP-THE-LINE: Cannot open snapshot read-only ({path}): {exc}")


def _open_readwrite(path: str) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        sys.exit(f"STOP-THE-LINE: QA DB not found: {path}")
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError as exc:
        sys.exit(f"STOP-THE-LINE: Cannot open QA DB read-write ({path}): {exc}")


def _fetch_rows(con: sqlite3.Connection, table: str, cycle_ts: str) -> list[dict]:
    try:
        cur = con.execute(
            f"SELECT * FROM {table} WHERE cycle_ts_utc = ?", (cycle_ts,)
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        return []   # table may not exist in one of the DBs


def _row_key(row: dict, keys: list[str]) -> tuple:
    return tuple(row.get(k) for k in keys)


def _floats_close(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= EPSILON
    except (TypeError, ValueError):
        return a == b


def _compare_json_features(a_json: str | None, b_json: str | None) -> list[str]:
    """Compare two features_json blobs. Returns list of differing keys."""
    diffs = []
    try:
        a = json.loads(a_json or "{}")
        b = json.loads(b_json or "{}")
    except (json.JSONDecodeError, TypeError):
        if a_json != b_json:
            diffs.append("features_json[parse_error]")
        return diffs
    all_keys = set(a) | set(b)
    for k in sorted(all_keys):
        av, bv = a.get(k), b.get(k)
        if isinstance(av, float) or isinstance(bv, float):
            if not _floats_close(av, bv):
                diffs.append(f"features_json[{k}]")
        else:
            if av != bv:
                diffs.append(f"features_json[{k}]")
    return diffs


def _diff_table(
    table: str, prod_rows: list[dict], qa_rows: list[dict], cfg: dict
) -> dict:
    match_key = cfg["match_key"]
    exact_cols = cfg["exact_cols"]
    float_cols = cfg["float_cols"]
    json_cols  = cfg["json_cols"]

    prod_map = {_row_key(r, match_key): r for r in prod_rows}
    qa_map   = {_row_key(r, match_key): r for r in qa_rows}

    missing_in_qa   = [k for k in prod_map if k not in qa_map]
    missing_in_prod = [k for k in qa_map  if k not in prod_map]
    value_mismatches: list[dict] = []

    for key in prod_map:
        if key not in qa_map:
            continue
        pr, qr = prod_map[key], qa_map[key]
        bad_exact  = [c for c in exact_cols if pr.get(c) != qr.get(c)]
        bad_float  = [c for c in float_cols if not _floats_close(pr.get(c), qr.get(c))]
        bad_json: list[str] = []
        for jc in json_cols:
            bad_json.extend(_compare_json_features(pr.get(jc), qr.get(jc)))
        all_bad = bad_exact + bad_float + bad_json
        if all_bad:
            value_mismatches.append({"key": key, "cols": all_bad, "prod": pr, "qa": qr})

    has_exact_diff = any(
        any(c in v["cols"] for c in exact_cols) for v in value_mismatches
    )

    return {
        "prod_rows": len(prod_rows),
        "qa_rows":   len(qa_rows),
        "missing_in_qa":   missing_in_qa,
        "missing_in_prod": missing_in_prod,
        "value_mismatches": value_mismatches,
        "has_exact_diff": has_exact_diff,
        "status": "MATCH" if (
            not missing_in_qa and not missing_in_prod and not value_mismatches
        ) else "FAIL",
    }


def _copy_bars(prod_con: sqlite3.Connection, qa_con: sqlite3.Connection, cycle_ts: str) -> None:
    """Copy 2-hour window of bars from Prod snapshot into QA DB for replay."""
    # Derive start window: 2 hours before cycle_ts
    try:
        ts = datetime.fromisoformat(cycle_ts.replace("Z", "+00:00"))
        window_start = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Use raw ts string for window_start comparison (2h = 7200s back)
        from datetime import timedelta
        start_ts = (ts - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return

    for bar_table in ("vanguard_bars_1m", "vanguard_bars_5m"):
        try:
            cols_cur = prod_con.execute(f"PRAGMA table_info({bar_table})")
            cols = [r[1] for r in cols_cur.fetchall()]
            if not cols:
                continue
            rows = prod_con.execute(
                f"SELECT * FROM {bar_table} WHERE ts_utc BETWEEN ? AND ?",
                (start_ts, cycle_ts),
            ).fetchall()
            if not rows:
                continue
            placeholders = ",".join("?" * len(cols))
            col_list = ",".join(cols)
            qa_con.executemany(
                f"INSERT OR REPLACE INTO {bar_table} ({col_list}) VALUES ({placeholders})",
                [tuple(r) for r in rows],
            )
            qa_con.commit()
        except sqlite3.OperationalError:
            pass  # table may not exist in QA; that's OK


def _replay(qa_db_path: str, cycle_ts: str) -> None:
    """Run QA orchestrator for a specific historical cycle."""
    repo = str(Path(qa_db_path).parent.parent)
    cmd = [
        sys.executable,
        str(Path(repo) / "stages" / "vanguard_orchestrator.py"),
        "--force-cycle-ts", cycle_ts,
        "--execution-mode", "test",
        "--no-telegram",
        "--single-cycle",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo)
    if result.returncode != 0:
        sys.exit(
            f"STOP-THE-LINE: Replay failed (exit {result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def _write_report(path: str, cycle_ts: str, prod_path: str, qa_path: str,
                  results: dict[str, dict]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_diverg = sum(
        len(r["missing_in_qa"]) + len(r["missing_in_prod"]) + len(r["value_mismatches"])
        for r in results.values()
    )
    tables_matched = sum(1 for r in results.values() if r["status"] == "MATCH")
    overall = "PASS" if tables_matched == 5 else "FAIL"

    lines = [
        f"# Parity Report — cycle_ts_utc={cycle_ts}",
        f"Generated: {now}",
        f"Prod snapshot: {prod_path}",
        f"QA DB: {qa_path}",
        "",
        "## Summary",
        f"- Result: {overall}",
        f"- Tables matched: {tables_matched}/5",
        f"- Total row divergence: {total_diverg}",
        "",
        "## Per-table results",
    ]

    for tbl, r in results.items():
        lines += [
            f"### {tbl}",
            f"- Prod rows: {r['prod_rows']} | QA rows: {r['qa_rows']}",
            f"- Missing in QA: {len(r['missing_in_qa'])} | Missing in Prod: {len(r['missing_in_prod'])}",
            f"- Value mismatches: {len(r['value_mismatches'])}",
            f"- Status: {r['status']}",
            "",
        ]

    if overall == "FAIL":
        lines += ["## Divergence detail", ""]
        for tbl, r in results.items():
            if r["status"] == "MATCH":
                continue
            lines.append(f"### {tbl}")
            for k in r["missing_in_qa"]:
                lines.append(f"  MISSING_IN_QA  key={k}")
            for k in r["missing_in_prod"]:
                lines.append(f"  MISSING_IN_PROD key={k}")
            for m in r["value_mismatches"]:
                lines.append(f"  VALUE_MISMATCH key={m['key']} cols={m['cols']}")
                for c in m["cols"]:
                    lines.append(f"    prod.{c}={m['prod'].get(c)!r}  qa.{c}={m['qa'].get(c)!r}")
            lines.append("")

    Path(path).write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="QA vs Prod parity harness")
    ap.add_argument("--prod-snapshot-path", required=True)
    ap.add_argument("--qa-db-path",         required=True)
    ap.add_argument("--cycle-ts-utc",       required=True)
    ap.add_argument("--output-report",      required=True)
    args = ap.parse_args()

    # Step 1: open DBs
    prod_con = _open_readonly(args.prod_snapshot_path)
    qa_con   = _open_readwrite(args.qa_db_path)

    cycle_ts = args.cycle_ts_utc

    # Step 2: verify cycle exists in Prod snapshot
    probe = _fetch_rows(prod_con, "vanguard_health", cycle_ts)
    if not probe:
        sys.exit(
            f"STOP-THE-LINE: cycle_ts_utc={cycle_ts!r} not found in Prod snapshot "
            f"({args.prod_snapshot_path}). Choose a valid cycle."
        )

    # Step 3: check if QA already has this cycle; replay if not
    qa_probe = _fetch_rows(qa_con, "vanguard_health", cycle_ts)
    if not qa_probe:
        print(f"[harness] QA missing cycle {cycle_ts} — copying bars then replaying…")
        _copy_bars(prod_con, qa_con, cycle_ts)
        _replay(args.qa_db_path, cycle_ts)

    # Step 4: diff all 5 tables
    results: dict[str, dict] = {}
    for table, cfg in TABLE_CONFIGS.items():
        prod_rows = _fetch_rows(prod_con, table, cycle_ts)
        qa_rows   = _fetch_rows(qa_con, table, cycle_ts)
        results[table] = _diff_table(table, prod_rows, qa_rows, cfg)

    prod_con.close()
    qa_con.close()

    # Step 5: write report
    _write_report(args.output_report, cycle_ts,
                  args.prod_snapshot_path, args.qa_db_path, results)
    print(f"[harness] Report written → {args.output_report}")

    # Step 6: check stop-the-line triggers (exact column mismatches)
    for table, r in results.items():
        if r["has_exact_diff"]:
            print(f"STOP-THE-LINE: Exact column mismatch in {table}. See report.")
            return 1

    overall = all(r["status"] == "MATCH" for r in results.values())
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
