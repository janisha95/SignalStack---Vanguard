from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEDUP_WINDOWS = (60, 90, 120)
DEFAULT_SHELLS: tuple[tuple[str, float, float], ...] = (
    ("20/40", 20.0, 40.0),
    ("30/60", 30.0, 60.0),
    ("40/80", 40.0, 80.0),
    ("60/120", 60.0, 120.0),
)
SESSION_TIMEOUT_POLICY: dict[str, dict[str, int]] = {
    "london": {"timeout_minutes": 30, "dedupe_minutes": 120},
    "ny": {"timeout_minutes": 60, "dedupe_minutes": 120},
    "overlap": {"timeout_minutes": 60, "dedupe_minutes": 120},
    "asian": {"timeout_minutes": 120, "dedupe_minutes": 90},
}
ET = ZoneInfo("America/New_York")

FORWARD_CHECKPOINT_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_forward_checkpoints (
    trade_id              TEXT NOT NULL,
    profile_id            TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    approved_cycle_ts_utc TEXT NOT NULL,
    checkpoint_minutes    INTEGER NOT NULL,
    checkpoint_ts_utc     TEXT NOT NULL,
    bars_used             INTEGER NOT NULL DEFAULT 0,
    price_at_checkpoint   REAL,
    close_move_pips       REAL,
    close_move_bps        REAL,
    is_positive_direction INTEGER,
    mfe_pips              REAL,
    mae_pips              REAL,
    status                TEXT NOT NULL,
    source_bar_ts_utc     TEXT,
    created_at_utc        TEXT NOT NULL,
    PRIMARY KEY (trade_id, checkpoint_minutes)
);
CREATE INDEX IF NOT EXISTS idx_vg_forward_checkpoints_symbol
    ON vanguard_forward_checkpoints(symbol, checkpoint_minutes);
"""

SHELL_REPLAY_DDL = """
CREATE TABLE IF NOT EXISTS vanguard_forward_shell_replays (
    trade_id            TEXT NOT NULL,
    checkpoint_minutes  INTEGER NOT NULL,
    dedupe_mode         INTEGER NOT NULL,
    dedupe_is_primary   INTEGER NOT NULL DEFAULT 0,
    session_bucket      TEXT,
    direction           TEXT,
    shell_name          TEXT NOT NULL,
    stop_pips           REAL NOT NULL,
    target_pips         REAL NOT NULL,
    exit_type           TEXT NOT NULL,
    realized_r          REAL,
    mfe_r               REAL,
    mae_r               REAL,
    created_at_utc      TEXT NOT NULL,
    PRIMARY KEY (trade_id, checkpoint_minutes, dedupe_mode, shell_name)
);
CREATE INDEX IF NOT EXISTS idx_vg_forward_shell_replays_session
    ON vanguard_forward_shell_replays(session_bucket, dedupe_mode, checkpoint_minutes, shell_name);

CREATE TABLE IF NOT EXISTS vanguard_timeout_policy_summary (
    session_bucket      TEXT NOT NULL,
    dedupe_mode         INTEGER NOT NULL,
    timeout_minutes     INTEGER NOT NULL,
    n                   INTEGER NOT NULL,
    wins                INTEGER NOT NULL,
    losses              INTEGER NOT NULL,
    win_rate            REAL,
    avg_move_pips       REAL,
    profit_factor       REAL,
    avg_mfe_pips        REAL,
    avg_mae_pips        REAL,
    updated_at_utc      TEXT NOT NULL,
    PRIMARY KEY (session_bucket, dedupe_mode, timeout_minutes)
);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _normalize_direction(value: Any) -> str:
    return str(value or "").upper().strip()


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().strip()


def _pip_size(symbol: str) -> float:
    return 0.01 if symbol.upper().endswith("JPY") else 0.0001


def normalize_session_bucket(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"tokyo", "asian", "asia"}:
        return "asian"
    if raw in {"london"}:
        return "london"
    if raw in {"new york", "new_york", "newyork", "ny"}:
        return "ny"
    if raw in {"london/ny", "london-ny", "overlap"}:
        return "overlap"
    return "unknown"


def get_policy_for_session(session_bucket: str | None) -> dict[str, int] | None:
    return SESSION_TIMEOUT_POLICY.get(normalize_session_bucket(session_bucket))


def _fallback_session_bucket(approved_dt: datetime | None) -> str:
    if approved_dt is None:
        return "unknown"
    et = approved_dt.astimezone(ET)
    hhmm = et.hour * 60 + et.minute
    if hhmm >= 17 * 60 or hhmm < 3 * 60:
        return "asian"
    if hhmm < 8 * 60:
        return "london"
    if hhmm < 12 * 60:
        return "overlap"
    if hhmm < 17 * 60:
        return "ny"
    return "unknown"


def ensure_schema(db_path: str) -> None:
    with sqlite3.connect(db_path, timeout=30) as con:
        for stmt in FORWARD_CHECKPOINT_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        _ensure_forward_checkpoint_columns(con)
        for stmt in SHELL_REPLAY_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        con.commit()


def _ensure_column(con: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_forward_checkpoint_columns(con: sqlite3.Connection) -> None:
    _ensure_column(con, "vanguard_forward_checkpoints", "session_bucket", "TEXT")
    _ensure_column(con, "vanguard_forward_checkpoints", "entry_price", "REAL")
    _ensure_column(con, "vanguard_forward_checkpoints", "direction", "TEXT")
    _ensure_column(con, "vanguard_forward_checkpoints", "dedupe_chain_60", "TEXT")
    _ensure_column(con, "vanguard_forward_checkpoints", "dedupe_chain_90", "TEXT")
    _ensure_column(con, "vanguard_forward_checkpoints", "dedupe_chain_120", "TEXT")
    _ensure_column(con, "vanguard_forward_checkpoints", "dedupe_is_primary_60", "INTEGER")
    _ensure_column(con, "vanguard_forward_checkpoints", "dedupe_is_primary_90", "INTEGER")
    _ensure_column(con, "vanguard_forward_checkpoints", "dedupe_is_primary_120", "INTEGER")
    _ensure_column(con, "vanguard_forward_checkpoints", "policy_timeout_minutes", "INTEGER")
    _ensure_column(con, "vanguard_forward_checkpoints", "policy_dedupe_minutes", "INTEGER")


def refresh_timeout_policy_data(
    db_path: str,
    *,
    trade_ids: Iterable[str] | None = None,
    refresh_shell_replays_flag: bool = True,
) -> None:
    ensure_schema(db_path)
    with sqlite3.connect(db_path, timeout=30) as con:
        con.row_factory = sqlite3.Row
        checkpoint_exists = bool(
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vanguard_forward_checkpoints'"
            ).fetchone()
        )
        if not checkpoint_exists:
            return
        trade_rows = _load_trade_rows(con)
        if not trade_rows:
            return
        if trade_ids:
            requested = {str(trade_id) for trade_id in trade_ids if str(trade_id).strip()}
            target_rows = [row for row in trade_rows if row["trade_id"] in requested]
        else:
            target_rows = trade_rows
        if not target_rows:
            return
        session_map = _load_session_buckets(con, trade_rows)
        chain_map = {
            window: _build_dedupe_chains(trade_rows, chain_gap_minutes=window)
            for window in DEDUP_WINDOWS
        }
        _update_checkpoint_metadata(con, target_rows, session_map=session_map, chain_map=chain_map)
        _refresh_timeout_policy_summary(con)
        if refresh_shell_replays_flag:
            _refresh_shell_replays(con, target_rows)
        con.commit()


def _load_trade_rows(con: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = {row[1] for row in con.execute("PRAGMA table_info(vanguard_trade_journal)").fetchall()}
    entry_expr = "COALESCE(fill_price, expected_entry)" if "fill_price" in cols else "expected_entry"
    rows = con.execute(
        f"""
        SELECT trade_id,
               profile_id,
               symbol,
               side,
               approved_cycle_ts_utc,
               {entry_expr} AS entry_price
        FROM vanguard_trade_journal
        WHERE approved_cycle_ts_utc IS NOT NULL
          AND symbol IS NOT NULL
          AND side IS NOT NULL
        ORDER BY profile_id, symbol, approved_cycle_ts_utc, trade_id
        """
    ).fetchall()
    return [
        {
            "trade_id": str(row["trade_id"]),
            "profile_id": str(row["profile_id"] or ""),
            "symbol": _normalize_symbol(row["symbol"]),
            "direction": _normalize_direction(row["side"]),
            "approved_cycle_ts_utc": str(row["approved_cycle_ts_utc"] or ""),
            "approved_dt": _parse_utc(row["approved_cycle_ts_utc"]),
            "entry_price": float(row["entry_price"] or 0.0),
        }
        for row in rows
        if _parse_utc(row["approved_cycle_ts_utc"]) is not None and str(row["trade_id"] or "").strip()
    ]


def _load_session_buckets(
    con: sqlite3.Connection,
    trade_rows: list[dict[str, Any]],
) -> dict[tuple[str, str], list[tuple[datetime, str]]]:
    profile_ids = sorted({row["profile_id"] for row in trade_rows if row["profile_id"]})
    if not profile_ids:
        return {}
    start_dt = min(row["approved_dt"] for row in trade_rows if row["approved_dt"] is not None)
    end_dt = max(row["approved_dt"] for row in trade_rows if row["approved_dt"] is not None)
    params: list[Any] = [start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), *profile_ids]
    placeholders = ",".join("?" for _ in profile_ids)
    session_map: dict[tuple[str, str], list[tuple[datetime, str]]] = defaultdict(list)
    try:
        rows = con.execute(
            f"""
            SELECT cycle_ts_utc, symbol, profile_id, session_bucket
            FROM vanguard_forex_pair_state
            WHERE cycle_ts_utc >= ?
              AND cycle_ts_utc <= ?
              AND profile_id IN ({placeholders})
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        ts = _parse_utc(row["cycle_ts_utc"])
        if ts is None:
            continue
        session_map[(str(row["profile_id"] or ""), _normalize_symbol(row["symbol"]))].append(
            (ts, normalize_session_bucket(row["session_bucket"]))
        )
    for key in session_map:
        session_map[key].sort(key=lambda item: item[0])
    return session_map


def _resolve_session_bucket(
    session_map: dict[tuple[str, str], list[tuple[datetime, str]]],
    *,
    profile_id: str,
    symbol: str,
    approved_dt: datetime | None,
) -> str:
    if approved_dt is None:
        return "unknown"
    series = session_map.get((profile_id, symbol))
    if not series:
        return _fallback_session_bucket(approved_dt)
    nearest: tuple[float, str] | None = None
    for ts, bucket in series:
        diff = abs((ts - approved_dt).total_seconds())
        if nearest is None or diff < nearest[0]:
            nearest = (diff, bucket)
        if ts > approved_dt and diff > 60:
            break
    if nearest is None or nearest[0] > 60:
        return _fallback_session_bucket(approved_dt)
    bucket = normalize_session_bucket(nearest[1])
    if bucket == "unknown":
        return _fallback_session_bucket(approved_dt)
    return bucket


def _build_dedupe_chains(
    trade_rows: list[dict[str, Any]],
    *,
    chain_gap_minutes: int,
) -> dict[str, tuple[str, int]]:
    state: dict[tuple[str, str], dict[str, Any]] = {}
    out: dict[str, tuple[str, int]] = {}
    for row in trade_rows:
        approved_dt = row["approved_dt"]
        if approved_dt is None:
            continue
        key = (row["profile_id"], row["symbol"])
        previous = state.get(key)
        keep_existing = (
            previous is not None
            and previous["direction"] == row["direction"]
            and (approved_dt - previous["last_dt"]).total_seconds() <= chain_gap_minutes * 60
        )
        if keep_existing:
            chain_id = previous["chain_id"]
            is_primary = 0
            previous["last_dt"] = approved_dt
        else:
            seed = f"{row['profile_id']}|{row['symbol']}|{row['direction']}|{approved_dt.isoformat()}|{chain_gap_minutes}"
            chain_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
            is_primary = 1
            state[key] = {
                "direction": row["direction"],
                "last_dt": approved_dt,
                "chain_id": chain_id,
            }
        out[row["trade_id"]] = (chain_id, is_primary)
    return out


def _update_checkpoint_metadata(
    con: sqlite3.Connection,
    trade_rows: list[dict[str, Any]],
    *,
    session_map: dict[tuple[str, str], list[tuple[datetime, str]]],
    chain_map: dict[int, dict[str, tuple[str, int]]],
) -> None:
    for row in trade_rows:
        session_bucket = _resolve_session_bucket(
            session_map,
            profile_id=row["profile_id"],
            symbol=row["symbol"],
            approved_dt=row["approved_dt"],
        )
        policy = get_policy_for_session(session_bucket) or {}
        con.execute(
            """
            UPDATE vanguard_forward_checkpoints
               SET session_bucket = ?,
                   entry_price = ?,
                   direction = ?,
                   dedupe_chain_60 = ?,
                   dedupe_chain_90 = ?,
                   dedupe_chain_120 = ?,
                   dedupe_is_primary_60 = ?,
                   dedupe_is_primary_90 = ?,
                   dedupe_is_primary_120 = ?,
                   policy_timeout_minutes = ?,
                   policy_dedupe_minutes = ?
             WHERE trade_id = ?
            """,
            (
                session_bucket,
                float(row["entry_price"] or 0.0),
                row["direction"],
                chain_map[60].get(row["trade_id"], ("", 0))[0],
                chain_map[90].get(row["trade_id"], ("", 0))[0],
                chain_map[120].get(row["trade_id"], ("", 0))[0],
                chain_map[60].get(row["trade_id"], ("", 0))[1],
                chain_map[90].get(row["trade_id"], ("", 0))[1],
                chain_map[120].get(row["trade_id"], ("", 0))[1],
                policy.get("timeout_minutes"),
                policy.get("dedupe_minutes"),
                row["trade_id"],
            ),
        )


def _refresh_timeout_policy_summary(con: sqlite3.Connection) -> None:
    con.execute("DELETE FROM vanguard_timeout_policy_summary")
    updated_at = _now_utc()
    for dedupe_mode in DEDUP_WINDOWS:
        primary_col = f"dedupe_is_primary_{dedupe_mode}"
        rows = con.execute(
            f"""
            SELECT session_bucket,
                   checkpoint_minutes,
                   close_move_pips,
                   mfe_pips,
                   mae_pips,
                   is_positive_direction
            FROM vanguard_forward_checkpoints
            WHERE status = 'READY'
              AND COALESCE({primary_col}, 0) = 1
              AND close_move_pips IS NOT NULL
              AND session_bucket IS NOT NULL
            """
        ).fetchall()
        groups: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            groups[(normalize_session_bucket(row["session_bucket"]), int(row["checkpoint_minutes"]))].append(row)
        for (session_bucket, timeout_minutes), group in groups.items():
            moves = [float(r["close_move_pips"] or 0.0) for r in group]
            gross_wins = sum(value for value in moves if value > 0)
            gross_losses = abs(sum(value for value in moves if value < 0))
            wins = sum(1 for value in moves if value > 0)
            losses = sum(1 for value in moves if value <= 0)
            profit_factor = None if gross_losses <= 0 else gross_wins / gross_losses
            con.execute(
                """
                INSERT INTO vanguard_timeout_policy_summary (
                    session_bucket, dedupe_mode, timeout_minutes, n, wins, losses,
                    win_rate, avg_move_pips, profit_factor, avg_mfe_pips, avg_mae_pips,
                    updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_bucket,
                    dedupe_mode,
                    timeout_minutes,
                    len(group),
                    wins,
                    losses,
                    (wins / len(group)) if group else None,
                    (sum(moves) / len(group)) if group else None,
                    profit_factor,
                    (sum(float(r["mfe_pips"] or 0.0) for r in group) / len(group)) if group else None,
                    (sum(float(r["mae_pips"] or 0.0) for r in group) / len(group)) if group else None,
                    updated_at,
                ),
            )


def _load_bars_for_trade(
    con: sqlite3.Connection,
    *,
    symbol: str,
    approved_cycle_ts_utc: str,
    checkpoint_ts_utc: str,
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT bar_ts_utc, high, low, close
        FROM vanguard_bars_1m
        WHERE symbol = ?
          AND bar_ts_utc > ?
          AND bar_ts_utc <= ?
        ORDER BY bar_ts_utc
        """,
        (symbol, approved_cycle_ts_utc, checkpoint_ts_utc),
    ).fetchall()
    return [
        {
            "ts": _parse_utc(row["bar_ts_utc"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for row in rows
        if row["high"] is not None and row["low"] is not None and row["close"] is not None
    ]


def _compute_contract(entry_price: float, direction: str, pip_size: float, stop_pips: float, target_pips: float) -> tuple[float, float]:
    if direction == "LONG":
        return entry_price - stop_pips * pip_size, entry_price + target_pips * pip_size
    return entry_price + stop_pips * pip_size, entry_price - target_pips * pip_size


def _classify_bar_hit(direction: str, high: float, low: float, sl: float, tp: float) -> str | None:
    if direction == "LONG":
        sl_hit = low <= sl
        tp_hit = high >= tp
    else:
        sl_hit = high >= sl
        tp_hit = low <= tp
    if sl_hit and tp_hit:
        return "SL"
    if sl_hit:
        return "SL"
    if tp_hit:
        return "TP"
    return None


def _signed_move_pips(entry_price: float, exit_price: float, direction: str, pip_size: float) -> float:
    raw = (exit_price - entry_price) / pip_size
    return raw if direction == "LONG" else -raw


def _excursion_r(entry_price: float, direction: str, bars: list[dict[str, Any]], pip_size: float, stop_pips: float) -> tuple[float, float]:
    if not bars:
        return 0.0, 0.0
    if direction == "LONG":
        mfe_pips = max((bar["high"] - entry_price) / pip_size for bar in bars)
        mae_pips = min((bar["low"] - entry_price) / pip_size for bar in bars)
    else:
        mfe_pips = max((entry_price - bar["low"]) / pip_size for bar in bars)
        mae_pips = min((entry_price - bar["high"]) / pip_size for bar in bars)
    return mfe_pips / stop_pips, mae_pips / stop_pips


def _refresh_shell_replays(con: sqlite3.Connection, trade_rows: list[dict[str, Any]]) -> None:
    if not trade_rows:
        return
    trade_ids = [row["trade_id"] for row in trade_rows]
    placeholders = ",".join("?" for _ in trade_ids)
    checkpoints = con.execute(
        f"""
        SELECT trade_id,
               checkpoint_minutes,
               checkpoint_ts_utc,
               approved_cycle_ts_utc,
               symbol,
               direction,
               entry_price,
               session_bucket,
               dedupe_is_primary_60,
               dedupe_is_primary_90,
               dedupe_is_primary_120,
               status
        FROM vanguard_forward_checkpoints
        WHERE trade_id IN ({placeholders})
          AND status = 'READY'
        """,
        trade_ids,
    ).fetchall()
    now = _now_utc()
    for row in checkpoints:
        symbol = _normalize_symbol(row["symbol"])
        direction = _normalize_direction(row["direction"])
        entry_price = float(row["entry_price"] or 0.0)
        if not symbol or direction not in {"LONG", "SHORT"} or entry_price <= 0:
            continue
        bars = _load_bars_for_trade(
            con,
            symbol=symbol,
            approved_cycle_ts_utc=str(row["approved_cycle_ts_utc"] or ""),
            checkpoint_ts_utc=str(row["checkpoint_ts_utc"] or ""),
        )
        if not bars:
            continue
        pip_size = _pip_size(symbol)
        for dedupe_mode in DEDUP_WINDOWS:
            is_primary = int(row[f"dedupe_is_primary_{dedupe_mode}"] or 0)
            for shell_name, stop_pips, target_pips in DEFAULT_SHELLS:
                sl, tp = _compute_contract(entry_price, direction, pip_size, stop_pips, target_pips)
                exit_type = "TIME"
                exit_price = bars[-1]["close"]
                for bar in bars:
                    hit = _classify_bar_hit(direction, float(bar["high"]), float(bar["low"]), sl, tp)
                    if hit:
                        exit_type = hit
                        exit_price = sl if hit == "SL" else tp
                        break
                realized_r = _signed_move_pips(entry_price, float(exit_price), direction, pip_size) / stop_pips
                mfe_r, mae_r = _excursion_r(entry_price, direction, bars, pip_size, stop_pips)
                con.execute(
                    """
                    INSERT INTO vanguard_forward_shell_replays (
                        trade_id, checkpoint_minutes, dedupe_mode, dedupe_is_primary,
                        session_bucket, direction, shell_name, stop_pips, target_pips,
                        exit_type, realized_r, mfe_r, mae_r, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_id, checkpoint_minutes, dedupe_mode, shell_name)
                    DO UPDATE SET
                        dedupe_is_primary = excluded.dedupe_is_primary,
                        session_bucket = excluded.session_bucket,
                        direction = excluded.direction,
                        stop_pips = excluded.stop_pips,
                        target_pips = excluded.target_pips,
                        exit_type = excluded.exit_type,
                        realized_r = excluded.realized_r,
                        mfe_r = excluded.mfe_r,
                        mae_r = excluded.mae_r,
                        created_at_utc = excluded.created_at_utc
                    """,
                    (
                        str(row["trade_id"]),
                        int(row["checkpoint_minutes"]),
                        dedupe_mode,
                        is_primary,
                        normalize_session_bucket(row["session_bucket"]),
                        direction,
                        shell_name,
                        stop_pips,
                        target_pips,
                        exit_type,
                        realized_r,
                        mfe_r,
                        mae_r,
                        now,
                    ),
                )
