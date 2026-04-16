#!/usr/bin/env python3
"""
vanguard_selection_simple.py — V5 simple ranking on regressor predictions.

Reads vanguard_predictions, keeps top longs/shorts by predicted_return, writes:
  - vanguard_shortlist_v2 (new simple shortlist)
  - vanguard_shortlist (legacy V6-compatible mirror)
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.helpers.db import connect_wal
from vanguard.config.runtime_config import get_runtime_config, get_shadow_db_path
from vanguard.helpers.clock import now_utc, iso_utc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vanguard_selection_simple")

DB_PATH = get_shadow_db_path()
PREDICTION_MIN_ABS = 0.0

CREATE_SHORTLIST_V2 = """
CREATE TABLE IF NOT EXISTS vanguard_shortlist_v2 (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    direction TEXT NOT NULL,
    predicted_return REAL,
    edge_score REAL,
    rank INTEGER,
    model_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, symbol, direction)
);
CREATE INDEX IF NOT EXISTS idx_vg_shortlist_v2_cycle
    ON vanguard_shortlist_v2(cycle_ts_utc, asset_class, direction);
"""

CREATE_SHORTLIST_LEGACY = """
CREATE TABLE IF NOT EXISTS vanguard_shortlist (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_rank INTEGER NOT NULL,
    strategy_score REAL NOT NULL,
    ml_prob REAL NOT NULL,
    edge_score REAL,
    consensus_count INTEGER DEFAULT 0,
    strategies_matched TEXT,
    regime TEXT NOT NULL,
    model_family TEXT,
    model_source TEXT,
    model_readiness TEXT,
    feature_profile TEXT,
    tbm_profile TEXT,
    PRIMARY KEY (cycle_ts_utc, symbol, strategy)
);
CREATE INDEX IF NOT EXISTS idx_vg_sl_cycle
    ON vanguard_shortlist(cycle_ts_utc);
"""


def _ensure_schema(con: sqlite3.Connection) -> None:
    for block in (CREATE_SHORTLIST_V2, CREATE_SHORTLIST_LEGACY):
        for stmt in block.strip().split(";"):
            if stmt.strip():
                con.execute(stmt)
    existing_v2 = {
        row[1]
        for row in con.execute("PRAGMA table_info(vanguard_shortlist_v2)").fetchall()
    }
    if "edge_score" not in existing_v2:
        con.execute("ALTER TABLE vanguard_shortlist_v2 ADD COLUMN edge_score REAL")
    con.commit()


def _latest_cycle(con: sqlite3.Connection) -> str | None:
    try:
        row = con.execute("SELECT MAX(cycle_ts_utc) FROM vanguard_predictions").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def _load_predictions(con: sqlite3.Connection, cycle_ts: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM vanguard_predictions WHERE cycle_ts_utc = ? ORDER BY asset_class, symbol",
        con,
        params=(cycle_ts,),
    )


def _select_rows(frame: pd.DataFrame, top_n: int) -> pd.DataFrame:
    frame = frame.copy()
    frame["edge_score"] = frame.groupby("cycle_ts_utc")["predicted_return"].rank(pct=True)
    frame["edge_score"] = frame["edge_score"] * 0.98 + 0.01
    frame["direction"] = np.where(frame["predicted_return"] > 0, "LONG", "SHORT")
    logger.info(
        "[V5] edge_score range: %.4f–%.4f | LONG%% = %.1f%%",
        float(frame["edge_score"].min()),
        float(frame["edge_score"].max()),
        float((frame["direction"] == "LONG").mean() * 100.0),
    )

    selected: list[pd.DataFrame] = []
    for asset_class, grp in frame.groupby("asset_class"):
        longs = grp[grp["direction"] == "LONG"].nlargest(top_n, "edge_score").copy()
        shorts = grp[grp["direction"] == "SHORT"].nsmallest(top_n, "predicted_return").copy()
        if not longs.empty:
            longs["rank"] = range(1, len(longs) + 1)
            selected.append(longs)
        if not shorts.empty:
            shorts["rank"] = range(1, len(shorts) + 1)
            selected.append(shorts)
    if not selected:
        return pd.DataFrame(columns=["cycle_ts_utc", "symbol", "asset_class", "direction", "predicted_return", "rank", "model_id"])
    return pd.concat(selected, ignore_index=True)


def _write_shortlists(con: sqlite3.Connection, shortlist_v2: pd.DataFrame) -> int:
    _ensure_schema(con)
    if shortlist_v2.empty:
        return 0
    cycle_ts = str(shortlist_v2["cycle_ts_utc"].iloc[0])
    now = iso_utc(now_utc())
    con.execute("DELETE FROM vanguard_shortlist_v2 WHERE cycle_ts_utc = ?", (cycle_ts,))
    con.execute("DELETE FROM vanguard_shortlist WHERE cycle_ts_utc = ?", (cycle_ts,))

    v2_records = shortlist_v2.copy()
    v2_records["created_at"] = now
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_shortlist_v2
        (cycle_ts_utc, symbol, asset_class, direction, predicted_return, edge_score, rank, model_id, created_at)
        VALUES (:cycle_ts_utc, :symbol, :asset_class, :direction, :predicted_return, :edge_score, :rank, :model_id, :created_at)
        """,
        v2_records.to_dict("records"),
    )

    legacy = shortlist_v2.copy()
    counts = legacy.groupby(["asset_class", "direction"])["symbol"].transform("count").clip(lower=1)
    legacy["strategy"] = "simple_regressor"
    legacy["strategy_rank"] = legacy["rank"]
    legacy["strategy_score"] = legacy["predicted_return"].abs()
    legacy["ml_prob"] = 1.0 - ((legacy["rank"] - 1) / counts)
    legacy["edge_score"] = legacy["edge_score"].fillna(legacy["strategy_score"]) if "edge_score" in legacy.columns else legacy["strategy_score"]
    legacy["consensus_count"] = 1
    legacy["strategies_matched"] = "simple_regressor"
    legacy["regime"] = "ACTIVE"
    legacy["model_family"] = "regressor"
    legacy["model_source"] = legacy["model_id"]
    legacy["model_readiness"] = "live_native"
    legacy["feature_profile"] = ""
    legacy["tbm_profile"] = ""
    con.executemany(
        """
        INSERT OR REPLACE INTO vanguard_shortlist
        (cycle_ts_utc, symbol, asset_class, direction, strategy, strategy_rank,
         strategy_score, ml_prob, edge_score, consensus_count, strategies_matched,
         regime, model_family, model_source, model_readiness, feature_profile, tbm_profile)
        VALUES (:cycle_ts_utc, :symbol, :asset_class, :direction, :strategy, :strategy_rank,
                :strategy_score, :ml_prob, :edge_score, :consensus_count, :strategies_matched,
                :regime, :model_family, :model_source, :model_readiness, :feature_profile, :tbm_profile)
        """,
        legacy.to_dict("records"),
    )
    con.commit()
    return len(shortlist_v2)


def run(*, dry_run: bool = False, top_n: int = 30, show_n: int = 5, db_path: str = DB_PATH) -> dict[str, Any]:
    with connect_wal(db_path) as con:
        cycle_ts = _latest_cycle(con)
        if not cycle_ts:
            logger.warning("No vanguard_predictions available")
            return {"status": "no_predictions", "rows": 0, "cycle_ts_utc": None}
        preds = _load_predictions(con, cycle_ts)

    if preds.empty:
        return {"status": "no_predictions", "rows": 0, "cycle_ts_utc": cycle_ts}

    shortlist = _select_rows(preds, top_n=top_n)
    if shortlist.empty:
        logger.warning("No candidates exceeded predicted_return threshold")
        return {"status": "no_candidates", "rows": 0, "cycle_ts_utc": cycle_ts}

    if dry_run:
        print(f"[V5] cycle={cycle_ts} rows={len(shortlist)}")
        for (asset_class, direction), grp in shortlist.groupby(["asset_class", "direction"]):
            print(f"\n{asset_class.upper()} {direction} top {min(show_n, len(grp))}:")
            print(grp[["symbol", "predicted_return", "rank", "model_id"]].head(show_n).to_string(index=False))
        return {"status": "dry_run", "rows": len(shortlist), "cycle_ts_utc": cycle_ts}

    with connect_wal(db_path) as con:
        written = _write_shortlists(con, shortlist)
    logger.info("Wrote %d shortlist rows for cycle=%s", written, cycle_ts)
    return {"status": "ok", "rows": written, "cycle_ts_utc": cycle_ts}


def main() -> int:
    runtime_cfg = get_runtime_config().get("runtime") or {}
    parser = argparse.ArgumentParser(description="Vanguard V5 simple selection")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--top-n",
        type=int,
        default=int(runtime_cfg.get("shortlist_top_n_per_asset_class", 30)),
    )
    parser.add_argument(
        "--show-n",
        type=int,
        default=int(runtime_cfg.get("shortlist_display_top_n", 5)),
    )
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, top_n=args.top_n, show_n=args.show_n)
    return 0 if result.get("status") not in {"no_predictions"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
