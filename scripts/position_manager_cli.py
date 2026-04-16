#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vanguard.config.runtime_config import get_shadow_db_path
from vanguard.execution.operator_actions import PositionSourceStaleError, get_service

_ET_TZ = ZoneInfo("America/New_York")


def _fmt_money(value) -> str:
    return f"{float(value or 0.0):.2f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def _fmt_num(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _fmt_et_pair(ts_utc: str | None) -> str:
    if not ts_utc:
        return "-"
    try:
        dt = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_et = dt.astimezone(_ET_TZ)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S UTC')} / {dt_et.strftime('%Y-%m-%d %I:%M:%S %p ET')}"
    except Exception:
        return str(ts_utc)


def _print_snapshot(snapshot: dict) -> None:
    account = dict(snapshot.get("account") or {})
    rows = list(snapshot.get("positions") or [])
    print(f"Profile {snapshot.get('profile_id')}")
    print(f"Balance {_fmt_money(account.get('balance'))}")
    print(f"Equity {_fmt_money(account.get('equity'))}")
    print(f"Used Margin {_fmt_money(account.get('used_margin'))}")
    print(f"Free Margin {_fmt_money(account.get('free_margin'))}")
    print(f"Margin Level {_fmt_pct(account.get('margin_level_pct'))}")
    print(f"Open Positions {int(account.get('open_positions') or 0)}")
    print(f"UPL {_fmt_money(account.get('total_upl'))}")
    print("")
    if not rows:
        print("No open positions.")
        return
    for row in rows:
        timeout_minutes = row.get("policy_timeout_minutes")
        timeout_label = f"{timeout_minutes}m" if timeout_minutes is not None else "-"
        print(
            f"{row['symbol']} {row['side']}  ticket={row['broker_position_id']}  lots={_fmt_num(row['lots'])}"
        )
        print(
            f"  entry={_fmt_num(row['broker_fill_entry'], 5)}  "
            f"sl={_fmt_num(row['current_sl'], 5)}  "
            f"tp={_fmt_num(row['current_tp'], 5)}  "
            f"pnl={_fmt_money(row['floating_pnl'])}  "
            f"hold_min={_fmt_num(row['holding_minutes'])}"
        )
        print(
            f"  timeout={timeout_label}  "
            f"timeout_at={_fmt_et_pair(row.get('policy_timeout_at_utc'))}  "
            f"timeout_left={_fmt_num(row.get('minutes_to_timeout'))}m"
        )
        print(
            f"  session={str(row.get('policy_session_bucket') or '-').upper()}  "
            f"dedupe={row.get('policy_dedupe_mode') or '-'}  "
            f"trade_id={row.get('trade_id') or '-'}"
        )
        print("")


def _print_result(result) -> None:
    payload = {
        "request_id": result.request_id,
        "status": result.status,
        "action_type": result.action_type,
        "profile_id": result.profile_id,
        "broker_position_id": result.broker_position_id,
        "trade_id": result.trade_id,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "dry_run": result.dry_run,
        "broker_result": result.broker_result,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Vanguard position manager CLI")
    parser.add_argument("--db-path", default=get_shadow_db_path())
    parser.add_argument("--profile", required=True, help="Runtime profile id, e.g. gft_10k")
    parser.add_argument("--requested-by", default=os.environ.get("USER", "cli"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--execute-signal", action="store_true")
    parser.add_argument("--trade-id")
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--close-at-timeout", action="store_true")
    parser.add_argument("--set-exact-sl-tp", action="store_true")
    parser.add_argument("--move-breakeven", action="store_true")
    parser.add_argument("--flatten", action="store_true")
    parser.add_argument("--position-id")
    parser.add_argument("--sl", type=float)
    parser.add_argument("--tp", type=float)
    parser.add_argument("--override-lot-size", type=float)
    parser.add_argument("--override-sl", type=float)
    parser.add_argument("--override-tp", type=float)
    parser.add_argument("--override-risk-pct", type=float)
    args = parser.parse_args()

    service = get_service(db_path=args.db_path)
    service.ensure_schema()

    if args.list:
        try:
            _print_snapshot(service.get_position_snapshot(args.profile))
            return 0
        except PositionSourceStaleError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    action_flags = [
        bool(args.execute_signal),
        bool(args.close),
        bool(args.close_at_timeout),
        bool(args.set_exact_sl_tp),
        bool(args.move_breakeven),
        bool(args.flatten),
    ]
    if sum(action_flags) != 1:
        parser.error("Choose exactly one action: --execute-signal, --close, --close-at-timeout, --set-exact-sl-tp, --move-breakeven, or --flatten")

    if args.execute_signal:
        if not args.trade_id:
            parser.error("--execute-signal requires --trade-id")
        overrides = {
            key: value
            for key, value in {
                "override_lot_size": args.override_lot_size,
                "override_sl": args.override_sl,
                "override_tp": args.override_tp,
                "override_risk_pct": args.override_risk_pct,
            }.items()
            if value is not None
        }
        request_id = service.queue_execute_signal(
            profile_id=args.profile,
            trade_id=args.trade_id,
            requested_by=args.requested_by,
            overrides=overrides,
        )
    elif args.close:
        if not args.position_id:
            parser.error("--close requires --position-id")
        request_id = service.queue_close_position(
            profile_id=args.profile,
            broker_position_id=args.position_id,
            requested_by=args.requested_by,
        )
    elif args.close_at_timeout:
        if not args.position_id:
            parser.error("--close-at-timeout requires --position-id")
        request_id = service.queue_close_at_timeout(
            profile_id=args.profile,
            broker_position_id=args.position_id,
            requested_by=args.requested_by,
        )
    elif args.set_exact_sl_tp:
        if not args.position_id or args.sl is None or args.tp is None:
            parser.error("--set-exact-sl-tp requires --position-id, --sl, and --tp")
        request_id = service.queue_set_exact_sl_tp(
            profile_id=args.profile,
            broker_position_id=args.position_id,
            sl=args.sl,
            tp=args.tp,
            requested_by=args.requested_by,
        )
    elif args.move_breakeven:
        if not args.position_id:
            parser.error("--move-breakeven requires --position-id")
        request_id = service.queue_move_breakeven(
            profile_id=args.profile,
            broker_position_id=args.position_id,
            requested_by=args.requested_by,
        )
    else:
        request_id = service.queue_flatten_profile(
            profile_id=args.profile,
            requested_by=args.requested_by,
        )

    result = service.process_request(request_id, dry_run=args.dry_run)
    _print_result(result)
    return 0 if result.status not in {"REJECTED", "FAILED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
