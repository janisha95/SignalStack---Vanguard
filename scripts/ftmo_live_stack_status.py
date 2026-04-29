from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.accounts.runtime_universe import resolve_universe_for_cycle
from vanguard.analytics.authority_audit import audit_active_lane_authority
from vanguard.analytics.live_diagnostics import fetch_diagnostics_snapshot, refresh_diagnostics
from vanguard.config.runtime_config import get_runtime_config
from vanguard.helpers.account_truth import fetch_latest_context_account_row


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    raw = pid_path.read_text().strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _log_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "mtime_utc": None, "age_seconds": None}
    mtime = path.stat().st_mtime
    age = max(int(time.time() - mtime), 0)
    return {
        "exists": True,
        "mtime_utc": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_seconds": age,
    }


def _age_ok(status: dict[str, Any], threshold_seconds: int) -> bool:
    age = status.get("age_seconds")
    return age is not None and int(age) <= threshold_seconds


def _iso_age_seconds(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return max(int((datetime.now(timezone.utc) - dt).total_seconds()), 0)


def _latest_cycle_summary(conn: sqlite3.Connection, profile_id: str) -> dict[str, Any]:
    decision_row = conn.execute(
        """
        select cycle_ts_utc
        from vanguard_signal_decision_log
        where profile_id = ?
        order by cycle_ts_utc desc
        limit 1
        """,
        (profile_id,),
    ).fetchone()
    health_row = conn.execute(
        "select max(cycle_ts_utc) as cycle_ts_utc from vanguard_health"
    ).fetchone()
    decision_ts = str(decision_row["cycle_ts_utc"]) if decision_row and decision_row["cycle_ts_utc"] else ""
    health_ts = str(health_row["cycle_ts_utc"]) if health_row and health_row["cycle_ts_utc"] else ""
    cycle_ts = max(decision_ts, health_ts)
    return {
        "decision_cycle_ts_utc": decision_ts or None,
        "health_cycle_ts_utc": health_ts or None,
        "latest_cycle_ts_utc": cycle_ts or None,
    }


def _latest_cycle_summary_multi(conn: sqlite3.Connection, profile_ids: list[str]) -> dict[str, Any]:
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    if not clean_profile_ids:
        return _latest_cycle_summary(conn, "")
    placeholders = ",".join("?" for _ in clean_profile_ids)
    decision_row = conn.execute(
        """
        select cycle_ts_utc
        from vanguard_signal_decision_log
        where profile_id in (""" + placeholders + """)
        order by cycle_ts_utc desc
        limit 1
        """,
        clean_profile_ids,
    ).fetchone()
    health_row = conn.execute(
        "select max(cycle_ts_utc) as cycle_ts_utc from vanguard_health"
    ).fetchone()
    decision_ts = str(decision_row["cycle_ts_utc"]) if decision_row and decision_row["cycle_ts_utc"] else ""
    health_ts = str(health_row["cycle_ts_utc"]) if health_row and health_row["cycle_ts_utc"] else ""
    cycle_ts = max(decision_ts, health_ts)
    return {
        "decision_cycle_ts_utc": decision_ts or None,
        "health_cycle_ts_utc": health_ts or None,
        "latest_cycle_ts_utc": cycle_ts or None,
    }


def _recent_issue_trace(conn: sqlite3.Connection, profile_id: str, window_minutes: int = 30) -> dict[str, Any]:
    out: dict[str, Any] = {
        "window_minutes": window_minutes,
        "execution_decisions": {},
        "cost_routing_reasons": {},
        "service_errors": {},
    }
    try:
        rows = conn.execute(
            """
            select execution_decision, count(*) as n
            from vanguard_signal_decision_log
            where profile_id = ?
              and created_at_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              and execution_decision in (
                  'SKIPPED_COST_ROUTING',
                  'SKIPPED_TACTICAL_BAN',
                  'SKIPPED_THROUGHPUT_PRIORITY',
                  'SKIPPED_POLICY',
                  'EXECUTED'
              )
            group by execution_decision
            """,
            (profile_id, f"-{int(window_minutes)} minutes"),
        ).fetchall()
        out["execution_decisions"] = {str(r["execution_decision"]): int(r["n"]) for r in rows}
        cost_rows = conn.execute(
            """
            select decision_reason_code, count(*) as n
            from vanguard_signal_decision_log
            where profile_id = ?
              and created_at_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              and execution_decision = 'SKIPPED_COST_ROUTING'
            group by decision_reason_code
            order by n desc, decision_reason_code asc
            """,
            (profile_id, f"-{int(window_minutes)} minutes"),
        ).fetchall()
        out["cost_routing_reasons"] = {str(r["decision_reason_code"]): int(r["n"]) for r in cost_rows}
    except sqlite3.OperationalError:
        pass
    try:
        svc_rows = conn.execute(
            """
            select service_name, last_error_ts_utc
            from vanguard_service_state
            where last_error_ts_utc is not null
              and last_error_ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            order by last_error_ts_utc desc
            """,
            (f"-{int(window_minutes)} minutes",),
        ).fetchall()
        out["service_errors"] = {
            str(r["service_name"]): str(r["last_error_ts_utc"])
            for r in svc_rows
        }
    except sqlite3.OperationalError:
        pass
    return out


def _recent_issue_trace_multi(conn: sqlite3.Connection, profile_ids: list[str], window_minutes: int = 30) -> dict[str, Any]:
    out: dict[str, Any] = {
        "window_minutes": window_minutes,
        "execution_decisions": {},
        "cost_routing_reasons": {},
        "service_errors": {},
    }
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    if not clean_profile_ids:
        return out
    placeholders = ",".join("?" for _ in clean_profile_ids)
    try:
        rows = conn.execute(
            """
            select execution_decision, count(*) as n
            from vanguard_signal_decision_log
            where profile_id in (""" + placeholders + """)
              and created_at_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              and execution_decision in (
                  'SKIPPED_COST_ROUTING',
                  'SKIPPED_TACTICAL_BAN',
                  'SKIPPED_THROUGHPUT_PRIORITY',
                  'SKIPPED_POLICY',
                  'EXECUTED'
              )
            group by execution_decision
            """,
            [*clean_profile_ids, f"-{int(window_minutes)} minutes"],
        ).fetchall()
        out["execution_decisions"] = {str(r["execution_decision"]): int(r["n"]) for r in rows}
        cost_rows = conn.execute(
            """
            select decision_reason_code, count(*) as n
            from vanguard_signal_decision_log
            where profile_id in (""" + placeholders + """)
              and created_at_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              and execution_decision = 'SKIPPED_COST_ROUTING'
            group by decision_reason_code
            order by n desc, decision_reason_code asc
            """,
            [*clean_profile_ids, f"-{int(window_minutes)} minutes"],
        ).fetchall()
        out["cost_routing_reasons"] = {str(r["decision_reason_code"]): int(r["n"]) for r in cost_rows}
    except sqlite3.OperationalError:
        pass
    try:
        svc_rows = conn.execute(
            """
            select service_name, last_error_ts_utc
            from vanguard_service_state
            where last_error_ts_utc is not null
              and last_error_ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            order by last_error_ts_utc desc
            """,
            (f"-{int(window_minutes)} minutes",),
        ).fetchall()
        out["service_errors"] = {
            str(r["service_name"]): str(r["last_error_ts_utc"])
            for r in svc_rows
        }
    except sqlite3.OperationalError:
        pass
    return out


def _active_auto_profile_ids(runtime_cfg: dict[str, Any], asset_class: str = "forex") -> list[str]:
    asset = str(asset_class or "forex").strip().lower() or "forex"
    active_ids: list[str] = []
    for profile in (runtime_cfg.get("profiles") or []):
        profile_id = str((profile or {}).get("id") or "").strip()
        lane = (((profile or {}).get("asset_lanes") or {}).get(asset) or {})
        if (
            not profile_id
            or not bool((profile or {}).get("is_active", False))
            or not bool(lane.get("enabled", False))
        ):
            continue
        if str(lane.get("execution_mode") or "").strip().lower() != "auto":
            continue
        active_ids.append(profile_id)
    return active_ids


def _default_status_profiles(runtime_cfg: dict[str, Any]) -> list[str]:
    active_auto = _active_auto_profile_ids(runtime_cfg, asset_class="forex")
    if active_auto:
        return active_auto
    for profile in (runtime_cfg.get("profiles") or []):
        if isinstance(profile, dict) and bool(profile.get("is_active")):
            profile_id = str(profile.get("id") or "").strip()
            if profile_id:
                return [profile_id]
    return ["ftmo_demo_100k"]


def _runtime_symbols_for_asset(runtime_cfg: dict[str, Any], profile_id: str, asset_class: str) -> list[str]:
    profiles = runtime_cfg.get("profiles") or []
    profile = next((p for p in profiles if str(p.get("id") or "") == profile_id), None)
    if not profile:
        return []
    lane = ((profile.get("asset_lanes") or {}).get(asset_class) or {})
    scope_id = str(lane.get("instrument_scope") or profile.get("instrument_scope") or "")
    universe = ((runtime_cfg.get("universes") or {}).get(scope_id) or {})
    symbols = ((universe.get("symbols") or {}).get(asset_class) or [])
    return [str(symbol).upper() for symbol in symbols if str(symbol).strip()]


def _fallback_symbols_for_asset(
    conn: sqlite3.Connection,
    profile_id: str,
    asset_class: str,
) -> list[str]:
    try:
        rows = conn.execute(
            """
            select distinct symbol
            from vanguard_context_quote_latest
            where profile_id = ?
            order by symbol
            """,
            (profile_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row["symbol"]).upper() for row in rows if str(row["symbol"] or "").strip()]


def _observed_asset_classes(conn: sqlite3.Connection, profile_ids: list[str]) -> list[str]:
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    if not clean_profile_ids:
        return []
    try:
        rows = conn.execute(
            """
            select distinct asset_class
            from vanguard_health
            where asset_class is not null
            order by asset_class
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row["asset_class"]).strip().lower() for row in rows if str(row["asset_class"] or "").strip()]


def _fresh_window_seconds(runtime_cfg: dict[str, Any], asset_class: str) -> int:
    diagnostics = ((runtime_cfg.get("runtime") or {}).get("context_state_diagnostics") or {})
    if asset_class == "crypto":
        return int(diagnostics.get("crypto_fresh_seconds") or 120)
    if asset_class == "forex":
        return int(diagnostics.get("forex_fresh_seconds") or 180)
    return int(diagnostics.get("equity_fresh_seconds") or 180)


def _asset_freshness_state(
    *,
    expected_symbols: int,
    quotes_fresh: int,
    quotes_missing: int,
    latest_quote_ts_utc: str | None,
    latest_state_cycle_ts_utc: str | None,
    state_rows_latest_cycle: int,
    latest_cycle_ts_utc: str | None,
    cycle_interval_seconds: int,
    fresh_window_seconds: int,
) -> str:
    if expected_symbols <= 0:
        return "inactive"
    if quotes_fresh > 0:
        return "fresh"
    if quotes_missing >= expected_symbols:
        return "missing"

    latest_quote_age = _iso_age_seconds(latest_quote_ts_utc)
    latest_cycle_age = _iso_age_seconds(latest_cycle_ts_utc)
    latest_state_age = _iso_age_seconds(latest_state_cycle_ts_utc)
    cadence_budget = cycle_interval_seconds + 60
    quote_budget = cycle_interval_seconds + fresh_window_seconds

    if (
        latest_quote_age is not None
        and latest_quote_age <= quote_budget
        and latest_cycle_age is not None
        and latest_cycle_age <= cadence_budget
        and latest_state_age is not None
        and latest_state_age <= quote_budget
        and state_rows_latest_cycle > 0
    ):
        return "waiting_for_next_cycle"
    return "stale"


def _quote_state_summary(
    conn: sqlite3.Connection,
    profile_id: str,
    asset_class: str,
    symbols: list[str],
    *,
    latest_cycle_ts_utc: str | None,
    cycle_interval_seconds: int,
    fresh_window_seconds: int,
) -> dict[str, Any]:
    if not symbols:
        return {
            "expected_symbols": 0,
            "quotes_fresh": 0,
            "quotes_stale": 0,
            "quotes_missing": 0,
            "latest_quote_ts_utc": None,
            "latest_quote_age_seconds": None,
            "latest_state_cycle_ts_utc": None,
            "latest_state_cycle_age_seconds": None,
            "state_rows_latest_cycle": 0,
            "freshness_state": "inactive",
        }
    placeholders = ",".join("?" for _ in symbols)
    quote_rows = conn.execute(
        f"""
        with ranked_quotes as (
            select symbol,
                   coalesce(received_ts_utc, quote_ts_utc) as effective_quote_ts_utc,
                   source_status,
                   row_number() over (
                       partition by symbol
                       order by coalesce(received_ts_utc, quote_ts_utc) desc
                   ) as rn
            from vanguard_context_quote_latest
            where profile_id = ?
              and symbol in ({placeholders})
        )
        select symbol, effective_quote_ts_utc, source_status
        from ranked_quotes
        where rn = 1
        """,
        [profile_id, *symbols],
    ).fetchall()
    quote_by_symbol = {str(row["symbol"]).upper(): row for row in quote_rows}
    now_dt = datetime.now(timezone.utc)
    fresh = stale = missing = 0
    latest_quote_ts: str | None = None
    for symbol in symbols:
        row = quote_by_symbol.get(symbol)
        if row is None:
            missing += 1
            continue
        quote_ts = str(row["effective_quote_ts_utc"] or "")
        if quote_ts and (latest_quote_ts is None or quote_ts > latest_quote_ts):
            latest_quote_ts = quote_ts
        try:
            age_s = int((now_dt - datetime.fromisoformat(quote_ts.replace("Z", "+00:00"))).total_seconds())
        except Exception:
            age_s = 10**9
        if str(row["source_status"] or "").upper() == "OK" and age_s <= fresh_window_seconds:
            fresh += 1
        else:
            stale += 1

    state_table = {
        "forex": "vanguard_forex_pair_state",
        "crypto": "vanguard_crypto_symbol_state",
        "equity": "vanguard_equity_symbol_state",
    }.get(asset_class)
    latest_state_cycle = None
    state_rows = 0
    if state_table:
        try:
            latest_row = conn.execute(
                f"""
                select max(cycle_ts_utc) as cycle_ts_utc
                from {state_table}
                where profile_id = ?
                  and symbol in ({placeholders})
                """,
                [profile_id, *symbols],
            ).fetchone()
            latest_state_cycle = str(latest_row["cycle_ts_utc"]) if latest_row and latest_row["cycle_ts_utc"] else None
            if latest_state_cycle:
                count_row = conn.execute(
                    f"""
                    select count(*) as n
                    from {state_table}
                    where profile_id = ?
                      and cycle_ts_utc = ?
                      and symbol in ({placeholders})
                    """,
                    [profile_id, latest_state_cycle, *symbols],
                ).fetchone()
                state_rows = int(count_row["n"] or 0)
        except sqlite3.OperationalError:
            latest_state_cycle = None
            state_rows = 0
    freshness_state = _asset_freshness_state(
        expected_symbols=len(symbols),
        quotes_fresh=fresh,
        quotes_missing=missing,
        latest_quote_ts_utc=latest_quote_ts,
        latest_state_cycle_ts_utc=latest_state_cycle,
        state_rows_latest_cycle=state_rows,
        latest_cycle_ts_utc=latest_cycle_ts_utc,
        cycle_interval_seconds=cycle_interval_seconds,
        fresh_window_seconds=fresh_window_seconds,
    )
    return {
        "expected_symbols": len(symbols),
        "quotes_fresh": fresh,
        "quotes_stale": stale,
        "quotes_missing": missing,
        "latest_quote_ts_utc": latest_quote_ts,
        "latest_quote_age_seconds": _iso_age_seconds(latest_quote_ts),
        "latest_state_cycle_ts_utc": latest_state_cycle,
        "latest_state_cycle_age_seconds": _iso_age_seconds(latest_state_cycle),
        "state_rows_latest_cycle": state_rows,
        "freshness_state": freshness_state,
    }


def _row_value(row: sqlite3.Row | None, key: str) -> Any:
    if row is None:
        return None
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        return None
    return None


@dataclass
class StackStatus:
    orchestrator_alive: bool
    lifecycle_alive: bool
    monitor_alive: bool
    datasette_alive: bool
    live_ic_tracker_alive: bool
    model_truth_snapshot_alive: bool
    metaapi_live_context_alive: bool
    metaapi_candle_cache_alive: bool
    startup_ready: bool
    payload: dict[str, Any]


def _fmt_age(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "-"
    if age_seconds < 60:
        return f"{age_seconds}s"
    minutes, seconds = divmod(age_seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _paint(text: str, code: str) -> str:
    if not _color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def _paint_stack_state(state: str) -> str:
    if state == "HEALTHY":
        return _paint(state, "32")
    if state == "DEGRADED":
        return _paint(state, "33")
    return _paint(state, "31")


def _paint_asset_state(state: str) -> str:
    if state == "fresh":
        return _paint(state, "32")
    if state == "waiting_for_next_cycle":
        return _paint(state, "33")
    if state == "inactive":
        return _paint(state, "36")
    return _paint(state, "31")


def _paint_service_state(name: str, alive: bool) -> str:
    if name == "monitor" and not alive:
        return f"{name}={_paint('OFF', '33')}"
    return f"{name}={_paint('UP', '32') if alive else _paint('DOWN', '31')}"


def _section(title: str) -> str:
    return _paint(f"== {title} ==", "36")


def _fetch_service_state(conn: sqlite3.Connection, service_name: str) -> sqlite3.Row | None:
    try:
        return conn.execute(
            """
            select service_name, status, last_started_ts_utc, last_success_ts_utc,
                   last_error_ts_utc, updated_ts_utc, pid, details_json
            from vanguard_service_state
            where service_name = ?
            """,
            (service_name,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _service_payload(
    service_row: sqlite3.Row | None,
    *,
    fresh_seconds: int,
) -> dict[str, Any] | None:
    if not service_row:
        return None
    details = None
    if service_row["details_json"]:
        try:
            details = json.loads(service_row["details_json"])
        except Exception:
            details = None
    last_success_ts = str(service_row["last_success_ts_utc"] or "") or None
    age_s = _iso_age_seconds(last_success_ts)
    return {
        "status": str(service_row["status"] or ""),
        "last_started_ts_utc": str(service_row["last_started_ts_utc"] or "") or None,
        "last_success_ts_utc": last_success_ts,
        "last_error_ts_utc": str(service_row["last_error_ts_utc"] or "") or None,
        "updated_ts_utc": str(service_row["updated_ts_utc"] or "") or None,
        "pid": int(service_row["pid"]) if service_row["pid"] is not None else None,
        "details": details,
        "fresh": bool(age_s is not None and age_s <= max(1, fresh_seconds)),
        "age_seconds": age_s,
    }


def _aggregate_asset_summaries(
    conn: sqlite3.Connection,
    runtime_cfg: dict[str, Any],
    profile_ids: list[str],
    *,
    asset_classes: list[str],
    latest_cycle_ts_utc: str | None,
    cycle_interval_seconds: int,
) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    clean_profile_ids = [str(pid or "").strip() for pid in profile_ids if str(pid or "").strip()]
    for asset_class in asset_classes:
        per_profile: list[dict[str, Any]] = []
        for profile_id in clean_profile_ids:
            symbols = _runtime_symbols_for_asset(runtime_cfg, profile_id, asset_class)
            if not symbols:
                symbols = _fallback_symbols_for_asset(conn, profile_id, asset_class)
            if not symbols:
                continue
            per_profile.append(
                _quote_state_summary(
                    conn,
                    profile_id,
                    asset_class,
                    symbols,
                    latest_cycle_ts_utc=latest_cycle_ts_utc,
                    cycle_interval_seconds=cycle_interval_seconds,
                    fresh_window_seconds=_fresh_window_seconds(runtime_cfg, asset_class),
                )
            )
        if not per_profile:
            continue
        latest_quote_ts = max(
            (str(s.get("latest_quote_ts_utc") or "") for s in per_profile if s.get("latest_quote_ts_utc")),
            default=None,
        ) or None
        latest_state_cycle = max(
            (str(s.get("latest_state_cycle_ts_utc") or "") for s in per_profile if s.get("latest_state_cycle_ts_utc")),
            default=None,
        ) or None
        states = [str(s.get("freshness_state") or "inactive") for s in per_profile]
        if any(state == "stale" for state in states):
            freshness_state = "stale"
        elif any(state == "missing" for state in states):
            freshness_state = "missing"
        elif all(state == "inactive" for state in states):
            freshness_state = "inactive"
        elif any(state == "waiting_for_next_cycle" for state in states):
            freshness_state = "waiting_for_next_cycle"
        else:
            freshness_state = "fresh"
        summaries[asset_class] = {
            "expected_symbols": max(int(s.get("expected_symbols") or 0) for s in per_profile),
            "quotes_fresh": min(int(s.get("quotes_fresh") or 0) for s in per_profile),
            "quotes_stale": max(int(s.get("quotes_stale") or 0) for s in per_profile),
            "quotes_missing": max(int(s.get("quotes_missing") or 0) for s in per_profile),
            "latest_quote_ts_utc": latest_quote_ts,
            "latest_quote_age_seconds": _iso_age_seconds(latest_quote_ts),
            "latest_state_cycle_ts_utc": latest_state_cycle,
            "latest_state_cycle_age_seconds": _iso_age_seconds(latest_state_cycle),
            "state_rows_latest_cycle": min(int(s.get("state_rows_latest_cycle") or 0) for s in per_profile),
            "freshness_state": freshness_state,
            "profile_count": len(per_profile),
        }
    return summaries


def _visible_active_anomalies(payload: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = payload.get("diagnostics") or {}
    active_anomalies = list(diagnostics.get("active_anomalies") or [])
    if bool(payload.get("monitor_required", True)):
        return active_anomalies
    filtered: list[dict[str, Any]] = []
    for anomaly in active_anomalies:
        family = str(anomaly.get("family") or "").strip().lower()
        scope_id = str(anomaly.get("scope_id") or "").strip().lower()
        if family == "service_drift" and scope_id == "monitor":
            continue
        filtered.append(anomaly)
    return filtered


def _classify_summary(status: StackStatus) -> tuple[str, list[str]]:
    payload = status.payload
    issues: list[str] = []
    if not status.startup_ready:
        issues.append("startup checks not ready")
    for name, info in payload["processes"].items():
        if not info.get("configured", True):
            continue
        if name == "monitor" and not info["alive"]:
            continue
        if name == "datasette":
            continue
        if not info["alive"]:
            issues.append(f"{name} down")
    for asset_class, summary in payload["asset_summaries"].items():
        state = str(summary.get("freshness_state") or "inactive")
        if state in {"inactive", "fresh", "waiting_for_next_cycle"}:
            continue
        issues.append(f"{asset_class} quotes {state}")
    account_contexts = payload.get("account_contexts") or [payload["account_context"]]
    stale_accounts = 0
    for account in account_contexts:
        acct_age = _iso_age_seconds(account.get("received_ts_utc"))
        if acct_age is None or acct_age > 300:
            stale_accounts += 1
    if stale_accounts:
        if len(account_contexts) > 1:
            issues.append(f"account context stale ({stale_accounts}/{len(account_contexts)})")
        else:
            issues.append("account context stale")
    v1_precompute = payload.get("v1_precompute") or {}
    if v1_precompute and not v1_precompute.get("fresh", False):
        issues.append("v1 cache stale")
    authority_audit = payload.get("authority_audit") or {}
    if int(authority_audit.get("bindings_with_issues") or 0) > 0:
        issues.append(
            f"authority issues={int(authority_audit.get('bindings_with_issues') or 0)}"
        )
    active_anomalies = _visible_active_anomalies(payload)
    for anomaly in active_anomalies:
        severity = str(anomaly.get("severity") or "").lower()
        if severity in {"critical", "high"}:
            issues.append(
                f"diagnostic {anomaly.get('family')} {anomaly.get('scope_id')}"
            )
            break
    if not issues:
        return "HEALTHY", []
    if status.orchestrator_alive or status.lifecycle_alive:
        return "DEGRADED", issues
    return "DOWN", issues


def render_operator_summary(status: StackStatus) -> str:
    payload = status.payload
    stack_state, issues = _classify_summary(status)
    acct = payload["account_context"]
    cycle_ts = payload["cycles"]["latest_cycle_ts_utc"]
    cycle_age = _fmt_age(_iso_age_seconds(cycle_ts))
    account_age = _fmt_age(_iso_age_seconds(acct["received_ts_utc"]))
    service_state = []
    for name in ("orchestrator", "lifecycle", "monitor", "datasette", "v1_precompute", "live_ic_tracker", "model_truth_snapshot", "metaapi_live_context", "metaapi_candle_cache"):
        info = payload["processes"][name]
        service_state.append(_paint_service_state(name, bool(info["alive"])))

    out = StringIO()
    out.write(f"stack: {_paint_stack_state(stack_state)}\n")
    if payload.get("profile_scope") == "fleet":
        out.write(f"profile: FOREX FLEET ({len(payload.get('active_profile_ids') or [])})\n")
        out.write(f"profiles: {', '.join(payload.get('active_profile_ids') or [])}\n")
    else:
        out.write(f"profile: {payload['profile_id']}\n")
    out.write(f"services: {' '.join(service_state)}\n")
    out.write(f"last_cycle: {cycle_ts or '-'} age={cycle_age}\n")
    if payload.get("profile_scope") == "fleet":
        for account in (payload.get("account_contexts") or []):
            out.write(
                "account: "
                f"profile={account['profile_id']} "
                f"acct={account['account_number'] or '-'} "
                f"ts={account['received_ts_utc'] or '-'} age={_fmt_age(_iso_age_seconds(account['received_ts_utc']))} "
                f"balance={account['balance'] if account['balance'] is not None else '-'} "
                f"equity={account['equity'] if account['equity'] is not None else '-'} "
                f"open_positions={account['open_positions']}\n"
            )
        out.write(f"open_positions_total: {payload['open_positions']['count']}\n")
    else:
        out.write(
            "account: "
            f"acct={acct['account_number'] or '-'} "
            f"ts={acct['received_ts_utc'] or '-'} age={account_age} "
            f"balance={acct['balance'] if acct['balance'] is not None else '-'} "
            f"equity={acct['equity'] if acct['equity'] is not None else '-'} "
            f"open_positions={payload['open_positions']['count']}\n"
        )
    for asset_class, summary in payload["asset_summaries"].items():
        out.write(
            f"{asset_class}: status={_paint_asset_state(summary.get('freshness_state') or 'inactive')} "
            f"quotes={summary['quotes_fresh']}/{summary['expected_symbols']} fresh "
            f"stale={summary['quotes_stale']} missing={summary['quotes_missing']} "
            f"quote_ts={summary['latest_quote_ts_utc'] or '-'} "
            f"state_rows={summary['state_rows_latest_cycle']} "
            f"state_cycle={summary['latest_state_cycle_ts_utc'] or '-'}\n"
        )
    v1_precompute = payload.get("v1_precompute") or {}
    if v1_precompute:
        out.write(
            f"v1_cache: status={v1_precompute.get('status') or '-'} "
            f"fresh={v1_precompute.get('fresh')} "
            f"age={_fmt_age(v1_precompute.get('age_seconds'))} "
            f"last_success={v1_precompute.get('last_success_ts_utc') or '-'}\n"
        )
    live_ic_tracker = payload.get("live_ic_tracker") or {}
    if live_ic_tracker:
        out.write(
            f"live_ic: status={live_ic_tracker.get('status') or '-'} "
            f"fresh={live_ic_tracker.get('fresh')} "
            f"age={_fmt_age(live_ic_tracker.get('age_seconds'))} "
            f"last_success={live_ic_tracker.get('last_success_ts_utc') or '-'}\n"
        )
    model_truth_snapshot = payload.get("model_truth_snapshot") or {}
    if model_truth_snapshot:
        details = model_truth_snapshot.get("details") or {}
        out.write(
            f"model_truth: status={model_truth_snapshot.get('status') or '-'} "
            f"fresh={model_truth_snapshot.get('fresh')} "
            f"age={_fmt_age(model_truth_snapshot.get('age_seconds'))} "
            f"rows={details.get('total_rows_written') if details.get('total_rows_written') is not None else '-'} "
            f"asof={details.get('snapshot_asof_utc') or '-'}\n"
        )
    metaapi_live_context = payload.get("metaapi_live_context") or {}
    if metaapi_live_context:
        details = metaapi_live_context.get("details") or {}
        out.write(
            f"metaapi_context: status={metaapi_live_context.get('status') or '-'} "
            f"fresh={metaapi_live_context.get('fresh')} "
            f"age={_fmt_age(metaapi_live_context.get('age_seconds'))} "
            f"quotes={details.get('quotes_written') if details.get('quotes_written') is not None else '-'} "
            f"req={details.get('symbols_requested') if details.get('symbols_requested') is not None else '-'} "
            f"last_success={metaapi_live_context.get('last_success_ts_utc') or '-'}\n"
        )
    authority_audit = payload.get("authority_audit") or {}
    if authority_audit:
        out.write(
            f"authority: status={authority_audit.get('status') or '-'} "
            f"bindings={authority_audit.get('active_bindings') or 0} "
            f"issues={authority_audit.get('bindings_with_issues') or 0}\n"
        )
    diagnostics = payload.get("diagnostics") or {}
    active_anomalies = _visible_active_anomalies(payload)
    if active_anomalies:
        out.write(f"diagnostics: active_anomalies={len(active_anomalies)}\n")
        for row in active_anomalies[:5]:
            out.write(
                f"  [{row.get('severity')}] {row.get('family')} {row.get('scope_id')}: "
                f"{row.get('summary')}\n"
            )
    elif diagnostics:
        out.write("diagnostics: active_anomalies=0\n")
    metaapi_candle_cache = payload.get("metaapi_candle_cache") or {}
    if metaapi_candle_cache:
        details = metaapi_candle_cache.get("details") or {}
        out.write(
            f"metaapi_cache: status={metaapi_candle_cache.get('status') or '-'} "
            f"fresh={metaapi_candle_cache.get('fresh')} "
            f"age={_fmt_age(metaapi_candle_cache.get('age_seconds'))} "
            f"provider={details.get('provider_id') or '-'} "
            f"rows={details.get('rows_written') if details.get('rows_written') is not None else '-'} "
            f"ok={details.get('symbols_ok') if details.get('symbols_ok') is not None else '-'} "
            f"fail={details.get('symbols_failed') if details.get('symbols_failed') is not None else '-'} "
            f"last_success={metaapi_candle_cache.get('last_success_ts_utc') or '-'}\n"
        )
    ttp_daemon = payload.get("ttp_playwright_daemon") or {}
    if ttp_daemon:
        acct = ((ttp_daemon.get("details") or {}).get("account_id"))
        out.write(
            f"ttp_daemon: status={ttp_daemon.get('status') or '-'} "
            f"fresh={ttp_daemon.get('fresh')} "
            f"age={_fmt_age(ttp_daemon.get('age_seconds'))} "
            f"account={acct or '-'} "
            f"last_success={ttp_daemon.get('last_success_ts_utc') or '-'}\n"
        )
    dwx_reconciliation = payload.get("dwx_reconciliation") or {}
    if dwx_reconciliation:
        unresolved = ((dwx_reconciliation.get("details") or {}).get("unresolved_count"))
        out.write(
            f"reconciliation: status={dwx_reconciliation.get('status') or '-'} "
            f"fresh={dwx_reconciliation.get('fresh')} "
            f"age={_fmt_age(dwx_reconciliation.get('age_seconds'))} "
            f"unresolved={unresolved if unresolved is not None else '-'} "
            f"last_success={dwx_reconciliation.get('last_success_ts_utc') or '-'}\n"
        )
    ttp_reconciliation = payload.get("ttp_reconciliation") or {}
    if ttp_reconciliation:
        unresolved = ((ttp_reconciliation.get("details") or {}).get("unresolved_count"))
        out.write(
            f"ttp_reconciliation: status={ttp_reconciliation.get('status') or '-'} "
            f"fresh={ttp_reconciliation.get('fresh')} "
            f"age={_fmt_age(ttp_reconciliation.get('age_seconds'))} "
            f"unresolved={unresolved if unresolved is not None else '-'} "
            f"last_success={ttp_reconciliation.get('last_success_ts_utc') or '-'}\n"
        )
    if issues:
        out.write(f"notes: {'; '.join(issues)}\n")
    else:
        decision_ts = payload["cycles"]["decision_cycle_ts_utc"]
        if decision_ts and decision_ts < (cycle_ts or ""):
            out.write("notes: stack healthy; no new decision rows on the latest cycle\n")
        else:
            out.write("notes: stack healthy\n")
    return out.getvalue().rstrip()


def build_status(
    *,
    profile_id: str,
    profile_ids: list[str] | None = None,
    asset_class: str = "forex",
    db_path: Path,
    orchestrator_pid_file: Path,
    lifecycle_pid_file: Path,
    monitor_pid_file: Path,
    datasette_pid_file: Path | None,
    v1_precompute_pid_file: Path | None = None,
    live_ic_tracker_pid_file: Path | None = None,
    model_truth_snapshot_pid_file: Path | None = None,
    metaapi_live_context_pid_file: Path | None = None,
    metaapi_candle_cache_pid_file: Path | None = None,
    orchestrator_log: Path | None = None,
    lifecycle_log: Path | None = None,
    monitor_log: Path | None = None,
    v1_precompute_log: Path | None = None,
    live_ic_tracker_log: Path | None = None,
    model_truth_snapshot_log: Path | None = None,
    metaapi_live_context_log: Path | None = None,
    metaapi_candle_cache_log: Path | None = None,
    require_monitor: bool = True,
    require_recent_logs: bool = False,
) -> StackStatus:
    runtime_cfg = get_runtime_config()
    requested_asset = str(asset_class or "forex").strip().lower() or "forex"
    active_profile_ids = [str(pid or "").strip() for pid in (profile_ids or []) if str(pid or "").strip()]
    if not active_profile_ids:
        resolved_active = _active_auto_profile_ids(runtime_cfg, asset_class=requested_asset)
        if resolved_active and (not profile_id or str(profile_id).strip() in resolved_active):
            active_profile_ids = resolved_active
    primary_profile_id = str(profile_id or "").strip() or (active_profile_ids[0] if active_profile_ids else "ftmo_demo_100k")
    if not active_profile_ids:
        active_profile_ids = [primary_profile_id]
    now_utc = datetime.now(timezone.utc)
    resolved = resolve_universe_for_cycle(runtime_cfg, now_utc)
    cycle_interval_seconds = int(((runtime_cfg.get("runtime") or {}).get("cycle_interval_seconds") or 300))
    orch_pid = _read_pid(orchestrator_pid_file)
    lifecycle_pid = _read_pid(lifecycle_pid_file)
    monitor_pid = _read_pid(monitor_pid_file)
    datasette_pid = _read_pid(datasette_pid_file) if datasette_pid_file else None
    v1_precompute_pid = _read_pid(v1_precompute_pid_file) if v1_precompute_pid_file else None
    live_ic_tracker_pid = _read_pid(live_ic_tracker_pid_file) if live_ic_tracker_pid_file else None
    model_truth_snapshot_pid = _read_pid(model_truth_snapshot_pid_file) if model_truth_snapshot_pid_file else None
    metaapi_live_context_pid = _read_pid(metaapi_live_context_pid_file) if metaapi_live_context_pid_file else None
    metaapi_candle_cache_pid = _read_pid(metaapi_candle_cache_pid_file) if metaapi_candle_cache_pid_file else None
    orch_alive = _pid_alive(orch_pid)
    lifecycle_alive = _pid_alive(lifecycle_pid)
    monitor_alive = _pid_alive(monitor_pid)
    datasette_alive = _pid_alive(datasette_pid) if datasette_pid_file else False
    v1_precompute_alive = _pid_alive(v1_precompute_pid) if v1_precompute_pid_file else False
    live_ic_tracker_alive = _pid_alive(live_ic_tracker_pid) if live_ic_tracker_pid_file else False
    model_truth_snapshot_alive = _pid_alive(model_truth_snapshot_pid) if model_truth_snapshot_pid_file else False
    metaapi_live_context_alive = _pid_alive(metaapi_live_context_pid) if metaapi_live_context_pid_file else False
    metaapi_candle_cache_alive = _pid_alive(metaapi_candle_cache_pid) if metaapi_candle_cache_pid_file else False

    with _connect(db_path) as conn:
        cycles = _latest_cycle_summary_multi(conn, active_profile_ids)
        recent_trace = _recent_issue_trace_multi(conn, active_profile_ids, window_minutes=30)
        acct_row = fetch_latest_context_account_row(
            conn,
            profile_id=primary_profile_id,
            runtime_cfg=runtime_cfg,
            require_ok=False,
        )
        if acct_row is None:
            try:
                acct_row = conn.execute(
                    """
                    select *
                    from vanguard_context_account_latest
                    where profile_id = ?
                    order by received_ts_utc desc
                    limit 1
                    """,
                    (primary_profile_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                acct_row = None
        try:
            acct_state_row = conn.execute(
                """
                select equity, starting_equity_today, updated_at_utc
                from vanguard_account_state
                where profile_id = ?
                limit 1
                """,
                (primary_profile_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            acct_state_row = None
        placeholders = ",".join("?" for _ in active_profile_ids)
        open_positions_row = conn.execute(
            f"""
            select count(*) as n
            from vanguard_open_positions
            where profile_id in ({placeholders})
            """,
            active_profile_ids,
        ).fetchone()
        account_contexts: list[dict[str, Any]] = []
        for pid in active_profile_ids:
            row = fetch_latest_context_account_row(
                conn,
                profile_id=pid,
                runtime_cfg=runtime_cfg,
                require_ok=False,
            )
            if row is None:
                try:
                    row = conn.execute(
                        """
                        select *
                        from vanguard_context_account_latest
                        where profile_id = ?
                        order by received_ts_utc desc
                        limit 1
                        """,
                        (pid,),
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = None
            try:
                state_row = conn.execute(
                    """
                    select equity, starting_equity_today, updated_at_utc
                    from vanguard_account_state
                    where profile_id = ?
                    limit 1
                    """,
                    (pid,),
                ).fetchone()
            except sqlite3.OperationalError:
                state_row = None
            balance = float(_row_value(row, "balance")) if _row_value(row, "balance") is not None else None
            equity = float(_row_value(row, "equity")) if _row_value(row, "equity") is not None else None
            received_ts = str(_row_value(row, "received_ts_utc")) if _row_value(row, "received_ts_utc") else None
            source = str(_row_value(row, "source")) if _row_value(row, "source") not in (None, "") else "context_account_latest"
            if balance is None and _row_value(state_row, "starting_equity_today") is not None:
                balance = float(_row_value(state_row, "starting_equity_today"))
                received_ts = str(state_row["updated_at_utc"] or "") or received_ts
                source = "account_state"
            if equity is None and _row_value(state_row, "equity") is not None:
                equity = float(_row_value(state_row, "equity"))
                if not received_ts:
                    received_ts = str(state_row["updated_at_utc"] or "") or None
                    source = "account_state"
            pos_row = conn.execute(
                """
                select count(*) as n
                from vanguard_open_positions
                where profile_id = ?
                """,
                (pid,),
            ).fetchone()
            account_contexts.append(
                {
                    "profile_id": pid,
                    "account_number": str(_row_value(row, "account_number")) if _row_value(row, "account_number") not in (None, "") else None,
                    "received_ts_utc": received_ts,
                    "balance": balance,
                    "equity": equity,
                    "source": source,
                    "open_positions": int(pos_row["n"] or 0) if pos_row else 0,
                }
            )
        observed_assets = _observed_asset_classes(conn, active_profile_ids)
        requested_has_symbols = any(
            _runtime_symbols_for_asset(runtime_cfg, pid, requested_asset)
            for pid in active_profile_ids
        )
        if len(active_profile_ids) == 1 and observed_assets:
            asset_classes = set(observed_assets)
        else:
            asset_classes = set(observed_assets)
            if requested_asset in observed_assets or requested_has_symbols or not asset_classes:
                asset_classes.add(requested_asset)
        asset_summaries = _aggregate_asset_summaries(
            conn,
            runtime_cfg,
            active_profile_ids,
            asset_classes=sorted(asset_classes),
            latest_cycle_ts_utc=cycles["latest_cycle_ts_utc"],
            cycle_interval_seconds=cycle_interval_seconds,
        )
        service_row = _fetch_service_state(conn, "v1_precompute")
        live_ic_service_row = _fetch_service_state(conn, "live_ic_tracker")
        model_truth_snapshot_row = _fetch_service_state(conn, "model_truth_snapshot")
        metaapi_live_context_row = _fetch_service_state(conn, "metaapi_live_context")
        dwx_reconciliation_row = _fetch_service_state(conn, "dwx_reconciliation")
        ttp_daemon_row = _fetch_service_state(conn, "ttp_playwright_daemon")
        ttp_reconciliation_row = _fetch_service_state(conn, "ttp_reconciliation")
        metaapi_candle_cache_row = _fetch_service_state(conn, "metaapi_candle_cache")
        authority_audit = audit_active_lane_authority(runtime_cfg=runtime_cfg)
        diagnostics_result = refresh_diagnostics(conn, runtime_cfg, active_profile_ids)
        diagnostics_snapshot = fetch_diagnostics_snapshot(conn, active_profile_ids, active_only=True, anomaly_limit=20)

    orch_log_status = _log_status(orchestrator_log) if orchestrator_log else {"exists": False, "mtime_utc": None, "age_seconds": None}
    lifecycle_log_status = _log_status(lifecycle_log) if lifecycle_log else {"exists": False, "mtime_utc": None, "age_seconds": None}
    monitor_log_status = _log_status(monitor_log) if monitor_log else {"exists": False, "mtime_utc": None, "age_seconds": None}
    v1_precompute_log_status = _log_status(v1_precompute_log) if v1_precompute_log else {"exists": False, "mtime_utc": None, "age_seconds": None}
    startup_ready = (
        orch_alive
        and lifecycle_alive
        and orch_log_status["exists"]
        and lifecycle_log_status["exists"]
    )
    if require_monitor:
        startup_ready = (
            startup_ready
            and monitor_alive
            and monitor_log_status["exists"]
        )
    if require_recent_logs:
        startup_ready = (
            startup_ready
            and _age_ok(orch_log_status, 180)
            and _age_ok(lifecycle_log_status, 180)
            and ((not require_monitor) or _age_ok(monitor_log_status, 180))
        )
    acct_received_ts = str(_row_value(acct_row, "received_ts_utc")) if _row_value(acct_row, "received_ts_utc") else None
    acct_state_ts = str(_row_value(acct_state_row, "updated_at_utc")) if _row_value(acct_state_row, "updated_at_utc") else None
    context_balance = float(_row_value(acct_row, "balance")) if _row_value(acct_row, "balance") is not None else None
    context_equity = float(_row_value(acct_row, "equity")) if _row_value(acct_row, "equity") is not None else None
    account_balance = context_balance
    account_equity = context_equity
    account_received_ts = acct_received_ts
    account_source = str(_row_value(acct_row, "source")) if _row_value(acct_row, "source") not in (None, "") else "context_account_latest"
    if account_balance is None and _row_value(acct_state_row, "starting_equity_today") is not None:
        account_balance = float(_row_value(acct_state_row, "starting_equity_today"))
        account_received_ts = acct_state_ts
        account_source = "account_state"
    if account_equity is None and _row_value(acct_state_row, "equity") is not None:
        account_equity = float(_row_value(acct_state_row, "equity"))
        if account_received_ts is None:
            account_received_ts = acct_state_ts
            account_source = "account_state"
    payload = {
        "profile_id": primary_profile_id,
        "profile_scope": "fleet" if len(active_profile_ids) > 1 else "single",
        "active_profile_ids": active_profile_ids,
        "monitor_required": require_monitor,
        "resolved_mode": resolved.mode,
        "expected_asset_classes": [requested_asset],
        "in_scope_symbol_count": len(resolved.in_scope_symbols),
        "processes": {
            "orchestrator": {"pid": orch_pid, "alive": orch_alive, "configured": True},
            "lifecycle": {"pid": lifecycle_pid, "alive": lifecycle_alive, "configured": True},
            "monitor": {"pid": monitor_pid, "alive": monitor_alive, "configured": True},
            "datasette": {"pid": datasette_pid, "alive": datasette_alive, "configured": bool(datasette_pid_file)},
            "v1_precompute": {"pid": v1_precompute_pid, "alive": v1_precompute_alive, "configured": bool(v1_precompute_pid_file)},
            "live_ic_tracker": {"pid": live_ic_tracker_pid, "alive": live_ic_tracker_alive, "configured": bool(live_ic_tracker_pid_file)},
            "model_truth_snapshot": {"pid": model_truth_snapshot_pid, "alive": model_truth_snapshot_alive, "configured": bool(model_truth_snapshot_pid_file)},
            "metaapi_live_context": {"pid": metaapi_live_context_pid, "alive": metaapi_live_context_alive, "configured": bool(metaapi_live_context_pid_file)},
            "metaapi_candle_cache": {"pid": metaapi_candle_cache_pid, "alive": metaapi_candle_cache_alive, "configured": bool(metaapi_candle_cache_pid_file)},
        },
        "logs": {
            "orchestrator": orch_log_status,
            "lifecycle": lifecycle_log_status,
            "monitor": monitor_log_status,
            "v1_precompute": v1_precompute_log_status,
            "live_ic_tracker": _log_status(live_ic_tracker_log) if live_ic_tracker_log else {"exists": False, "mtime_utc": None, "age_seconds": None},
            "model_truth_snapshot": _log_status(model_truth_snapshot_log) if model_truth_snapshot_log else {"exists": False, "mtime_utc": None, "age_seconds": None},
            "metaapi_live_context": _log_status(metaapi_live_context_log) if metaapi_live_context_log else {"exists": False, "mtime_utc": None, "age_seconds": None},
            "metaapi_candle_cache": _log_status(metaapi_candle_cache_log) if metaapi_candle_cache_log else {"exists": False, "mtime_utc": None, "age_seconds": None},
        },
        "cycles": cycles,
        "recent_trace": recent_trace,
        "account_context": {
            "account_number": str(_row_value(acct_row, "account_number")) if _row_value(acct_row, "account_number") not in (None, "") else None,
            "received_ts_utc": account_received_ts,
            "balance": account_balance,
            "equity": account_equity,
            "source": account_source,
        },
        "account_contexts": account_contexts,
        "open_positions": {
            "count": int(open_positions_row["n"] or 0) if open_positions_row else 0,
        },
        "asset_summaries": asset_summaries,
        "v1_precompute": None,
        "live_ic_tracker": None,
        "model_truth_snapshot": None,
        "metaapi_live_context": None,
        "metaapi_candle_cache": None,
        "authority_audit": authority_audit,
        "diagnostics": {
            **diagnostics_snapshot,
            "last_refresh_transitions": diagnostics_result.get("transitions") or [],
        },
        "dwx_reconciliation": None,
        "ttp_playwright_daemon": None,
        "ttp_reconciliation": None,
        "startup_ready": startup_ready,
    }
    freshness_seconds = int(((runtime_cfg.get("runtime") or {}).get("v1_precompute") or {}).get("freshness_seconds") or 0)
    payload["v1_precompute"] = _service_payload(service_row, fresh_seconds=freshness_seconds) if service_row else None
    payload["live_ic_tracker"] = _service_payload(live_ic_service_row, fresh_seconds=86400) if live_ic_service_row else None
    payload["model_truth_snapshot"] = _service_payload(model_truth_snapshot_row, fresh_seconds=max(900, cycle_interval_seconds * 3)) if model_truth_snapshot_row else None
    payload["metaapi_live_context"] = _service_payload(metaapi_live_context_row, fresh_seconds=max(60, cycle_interval_seconds)) if metaapi_live_context_row else None
    if metaapi_candle_cache_row:
        service_cfg = (((runtime_cfg.get("market_data") or {}).get("metaapi_candle_cache")) or {})
        interval_seconds = int(service_cfg.get("interval_seconds") or 60)
        payload["metaapi_candle_cache"] = _service_payload(
            metaapi_candle_cache_row,
            fresh_seconds=max(180, interval_seconds * 3),
        )
    if dwx_reconciliation_row:
        payload["dwx_reconciliation"] = _service_payload(dwx_reconciliation_row, fresh_seconds=max(300, cycle_interval_seconds * 3))
    if ttp_daemon_row:
        payload["ttp_playwright_daemon"] = _service_payload(ttp_daemon_row, fresh_seconds=max(300, cycle_interval_seconds * 3))
    if ttp_reconciliation_row:
        payload["ttp_reconciliation"] = _service_payload(ttp_reconciliation_row, fresh_seconds=max(300, cycle_interval_seconds * 3))
    return StackStatus(
        orchestrator_alive=orch_alive,
        lifecycle_alive=lifecycle_alive,
        monitor_alive=monitor_alive,
        datasette_alive=datasette_alive,
        live_ic_tracker_alive=live_ic_tracker_alive,
        model_truth_snapshot_alive=model_truth_snapshot_alive,
        metaapi_live_context_alive=metaapi_live_context_alive,
        metaapi_candle_cache_alive=metaapi_candle_cache_alive,
        startup_ready=startup_ready,
        payload=payload,
    )


def _print_verbose(status: StackStatus) -> None:
    p = status.payload
    stack_state, _issues = _classify_summary(status)
    print(_section("overview"))
    print(f"stack={_paint_stack_state(stack_state)} profile={p['profile_id']} startup_ready={p['startup_ready']}")
    print()
    print(_section("processes"))
    print(f"profile={p['profile_id']} startup_ready={p['startup_ready']}")
    print(
        "processes:",
        " ".join(
            f"{name}={info['alive']}({info['pid'] or '-'})"
            for name, info in p["processes"].items()
        ),
    )
    print()
    print(_section("cycles"))
    print(
        f"cycle latest={p['cycles']['latest_cycle_ts_utc'] or '-'} "
        f"decision={p['cycles']['decision_cycle_ts_utc'] or '-'} "
        f"health={p['cycles']['health_cycle_ts_utc'] or '-'}"
    )
    trace = p.get("recent_trace") or {}
    print()
    print(_section("recent trace"))
    print(
        f"window={trace.get('window_minutes', '-')}m "
        f"decisions={trace.get('execution_decisions') or {}} "
        f"cost_routing={trace.get('cost_routing_reasons') or {}}"
    )
    svc_errors = trace.get("service_errors") or {}
    print(f"service_errors={svc_errors if svc_errors else {}}")
    acct = p["account_context"]
    print()
    print(_section("account"))
    print(
        f"account_context acct={acct['account_number'] or '-'} "
        f"ts={acct['received_ts_utc'] or '-'} "
        f"balance={acct['balance'] if acct['balance'] is not None else '-'} "
        f"equity={acct['equity'] if acct['equity'] is not None else '-'} "
        f"open_positions={p['open_positions']['count']}"
    )
    print()
    print(_section("scope"))
    print(
        f"resolved mode={p['resolved_mode']} assets={','.join(p['expected_asset_classes']) or '-'} "
        f"symbols={p['in_scope_symbol_count']}"
    )
    print()
    print(_section("assets"))
    for asset_class, summary in p["asset_summaries"].items():
        print(
            f"{asset_class}: expected={summary['expected_symbols']} fresh={summary['quotes_fresh']} "
            f"stale={summary['quotes_stale']} missing={summary['quotes_missing']} "
            f"state={summary.get('freshness_state') or '-'} "
            f"quote_ts={summary['latest_quote_ts_utc'] or '-'} "
            f"state_cycle={summary['latest_state_cycle_ts_utc'] or '-'} "
            f"state_rows={summary['state_rows_latest_cycle']}"
        )
    print()
    print(_section("v1 cache"))
    v1_precompute = p.get("v1_precompute") or {}
    if v1_precompute:
        print(
            f"v1_precompute: status={v1_precompute.get('status') or '-'} "
            f"fresh={v1_precompute.get('fresh')} "
            f"age_s={v1_precompute.get('age_seconds') if v1_precompute.get('age_seconds') is not None else '-'} "
            f"last_success={v1_precompute.get('last_success_ts_utc') or '-'}"
        )
    print()
    print(_section("live ic"))
    live_ic_tracker = p.get("live_ic_tracker") or {}
    if live_ic_tracker:
        print(
            f"live_ic_tracker: status={live_ic_tracker.get('status') or '-'} "
            f"fresh={live_ic_tracker.get('fresh')} "
            f"age_s={live_ic_tracker.get('age_seconds') if live_ic_tracker.get('age_seconds') is not None else '-'} "
            f"last_success={live_ic_tracker.get('last_success_ts_utc') or '-'}"
        )
    else:
        print("live_ic_tracker: status=- fresh=- age_s=- last_success=-")
    print()
    print(_section("model truth"))
    model_truth_snapshot = p.get("model_truth_snapshot") or {}
    if model_truth_snapshot:
        details = model_truth_snapshot.get("details") or {}
        print(
            f"model_truth_snapshot: status={model_truth_snapshot.get('status') or '-'} "
            f"fresh={model_truth_snapshot.get('fresh')} "
            f"age_s={model_truth_snapshot.get('age_seconds') if model_truth_snapshot.get('age_seconds') is not None else '-'} "
            f"rows={details.get('total_rows_written') if details.get('total_rows_written') is not None else '-'} "
            f"asof={details.get('snapshot_asof_utc') or '-'}"
        )
    else:
        print("model_truth_snapshot: status=- fresh=- age_s=- rows=- asof=-")
    print()
    print(_section("metaapi context"))
    metaapi_live_context = p.get("metaapi_live_context") or {}
    if metaapi_live_context:
        details = metaapi_live_context.get("details") or {}
        print(
            f"metaapi_live_context: status={metaapi_live_context.get('status') or '-'} "
            f"fresh={metaapi_live_context.get('fresh')} "
            f"age_s={metaapi_live_context.get('age_seconds') if metaapi_live_context.get('age_seconds') is not None else '-'} "
            f"quotes={details.get('quotes_written') if details.get('quotes_written') is not None else '-'} "
            f"req={details.get('symbols_requested') if details.get('symbols_requested') is not None else '-'} "
            f"last_success={metaapi_live_context.get('last_success_ts_utc') or '-'}"
        )
    else:
        print("metaapi_live_context: status=- fresh=- age_s=- quotes=- req=- last_success=-")
    print()
    print(_section("metaapi cache"))
    metaapi_candle_cache = p.get("metaapi_candle_cache") or {}
    if metaapi_candle_cache:
        details = metaapi_candle_cache.get("details") or {}
        print(
            f"metaapi_candle_cache: status={metaapi_candle_cache.get('status') or '-'} "
            f"fresh={metaapi_candle_cache.get('fresh')} "
            f"age_s={metaapi_candle_cache.get('age_seconds') if metaapi_candle_cache.get('age_seconds') is not None else '-'} "
            f"provider={details.get('provider_id') or '-'} "
            f"rows={details.get('rows_written') if details.get('rows_written') is not None else '-'} "
            f"ok={details.get('symbols_ok') if details.get('symbols_ok') is not None else '-'} "
            f"fail={details.get('symbols_failed') if details.get('symbols_failed') is not None else '-'} "
            f"last_success={metaapi_candle_cache.get('last_success_ts_utc') or '-'}"
        )
    else:
        print("metaapi_candle_cache: status=- fresh=- age_s=- provider=- rows=- ok=- fail=- last_success=-")
    print()
    print(_section("ttp broker"))
    ttp_daemon = p.get("ttp_playwright_daemon") or {}
    if ttp_daemon:
        acct = ((ttp_daemon.get("details") or {}).get("account_id"))
        print(
            f"ttp_playwright_daemon: status={ttp_daemon.get('status') or '-'} "
            f"fresh={ttp_daemon.get('fresh')} "
            f"age_s={ttp_daemon.get('age_seconds') if ttp_daemon.get('age_seconds') is not None else '-'} "
            f"account={acct or '-'} "
            f"last_success={ttp_daemon.get('last_success_ts_utc') or '-'}"
        )
    else:
        print("ttp_playwright_daemon: status=- fresh=- age_s=- account=- last_success=-")
    print()
    print(_section("reconciliation"))
    dwx_reconciliation = p.get("dwx_reconciliation") or {}
    if dwx_reconciliation:
        unresolved = ((dwx_reconciliation.get("details") or {}).get("unresolved_count"))
        print(
            f"dwx_reconciliation: status={dwx_reconciliation.get('status') or '-'} "
            f"fresh={dwx_reconciliation.get('fresh')} "
            f"age_s={dwx_reconciliation.get('age_seconds') if dwx_reconciliation.get('age_seconds') is not None else '-'} "
            f"unresolved={unresolved if unresolved is not None else '-'} "
            f"last_success={dwx_reconciliation.get('last_success_ts_utc') or '-'}"
        )
    else:
        print("dwx_reconciliation: status=- fresh=- age_s=- unresolved=- last_success=-")
    ttp_reconciliation = p.get("ttp_reconciliation") or {}
    if ttp_reconciliation:
        unresolved = ((ttp_reconciliation.get("details") or {}).get("unresolved_count"))
        print(
            f"ttp_reconciliation: status={ttp_reconciliation.get('status') or '-'} "
            f"fresh={ttp_reconciliation.get('fresh')} "
            f"age_s={ttp_reconciliation.get('age_seconds') if ttp_reconciliation.get('age_seconds') is not None else '-'} "
            f"unresolved={unresolved if unresolved is not None else '-'} "
            f"last_success={ttp_reconciliation.get('last_success_ts_utc') or '-'}"
        )
    else:
        print("ttp_reconciliation: status=- fresh=- age_s=- unresolved=- last_success=-")


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-serve FTMO PROD stack status.")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--asset-class", default="forex")
    parser.add_argument("--db-path", default=str(_REPO_ROOT / "data" / "vanguard_universe.db"))
    parser.add_argument("--orchestrator-pid-file", default="/tmp/vanguard_prod_ftmo_orchestrator.pid")
    parser.add_argument("--lifecycle-pid-file", default="/tmp/vanguard_prod_lifecycle_daemon.pid")
    parser.add_argument("--monitor-pid-file", default="/tmp/vanguard_prod_ftmo_monitor.pid")
    parser.add_argument("--datasette-pid-file", default="/tmp/vanguard_prod_datasette.pid")
    parser.add_argument("--v1-precompute-pid-file", default="/tmp/vanguard_prod_v1_precompute.pid")
    parser.add_argument("--metaapi-candle-cache-pid-file", default="/tmp/vanguard_prod_metaapi_candles.pid")
    parser.add_argument("--metaapi-live-context-pid-file", default="/tmp/vanguard_prod_metaapi_live_context.pid")
    parser.add_argument("--orchestrator-log", default="/tmp/vanguard_prod_ftmo_orchestrator.log")
    parser.add_argument("--lifecycle-log", default="/tmp/vanguard_prod_lifecycle_daemon.log")
    parser.add_argument("--monitor-log", default="/tmp/vanguard_prod_ftmo_monitor.log")
    parser.add_argument("--v1-precompute-log", default="/tmp/vanguard_prod_v1_precompute.log")
    parser.add_argument("--live-ic-tracker-pid-file", default="/tmp/vanguard_prod_live_ic_tracker.pid")
    parser.add_argument("--live-ic-tracker-log", default="/tmp/vanguard_prod_live_ic_tracker.log")
    parser.add_argument("--model-truth-snapshot-pid-file", default="/tmp/vanguard_prod_model_truth_snapshot.pid")
    parser.add_argument("--model-truth-snapshot-log", default="/tmp/vanguard_prod_model_truth_snapshot.log")
    parser.add_argument("--metaapi-live-context-log", default="/tmp/vanguard_prod_metaapi_live_context.log")
    parser.add_argument("--metaapi-candle-cache-log", default="/tmp/vanguard_prod_metaapi_candles.log")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--allow-monitor-missing", action="store_true")
    parser.add_argument("--require-recent-logs", action="store_true")
    args = parser.parse_args()
    runtime_cfg = get_runtime_config()
    requested_asset = str(args.asset_class or "forex").strip().lower() or "forex"
    active_profile_ids = _active_auto_profile_ids(runtime_cfg, asset_class=requested_asset) or _default_status_profiles(runtime_cfg)
    profile_id = str(args.profile or "").strip() or (active_profile_ids[0] if active_profile_ids else "ftmo_demo_100k")
    if args.profile:
        active_profile_ids = [profile_id]

    status = build_status(
        profile_id=profile_id,
        profile_ids=active_profile_ids,
        asset_class=requested_asset,
        db_path=Path(args.db_path),
        orchestrator_pid_file=Path(args.orchestrator_pid_file),
        lifecycle_pid_file=Path(args.lifecycle_pid_file),
        monitor_pid_file=Path(args.monitor_pid_file),
        datasette_pid_file=Path(args.datasette_pid_file),
        v1_precompute_pid_file=Path(args.v1_precompute_pid_file),
        live_ic_tracker_pid_file=Path(args.live_ic_tracker_pid_file),
        model_truth_snapshot_pid_file=Path(args.model_truth_snapshot_pid_file),
        metaapi_live_context_pid_file=Path(args.metaapi_live_context_pid_file),
        metaapi_candle_cache_pid_file=Path(args.metaapi_candle_cache_pid_file),
        orchestrator_log=Path(args.orchestrator_log),
        lifecycle_log=Path(args.lifecycle_log),
        monitor_log=Path(args.monitor_log),
        v1_precompute_log=Path(args.v1_precompute_log),
        live_ic_tracker_log=Path(args.live_ic_tracker_log),
        model_truth_snapshot_log=Path(args.model_truth_snapshot_log),
        metaapi_live_context_log=Path(args.metaapi_live_context_log),
        metaapi_candle_cache_log=Path(args.metaapi_candle_cache_log),
        require_monitor=not args.allow_monitor_missing,
        require_recent_logs=args.require_recent_logs,
    )
    if args.json:
        print(json.dumps(status.payload, indent=2, sort_keys=True))
    else:
        if args.verbose:
            _print_verbose(status)
        else:
            print(render_operator_summary(status))
    if args.require_ready and not status.startup_ready:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
