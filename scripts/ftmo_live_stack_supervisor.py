from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
import subprocess
import sys
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from scripts.ftmo_live_stack_status import _classify_summary, _log_status, _pid_alive, _print_verbose, _read_pid, build_status, render_operator_summary
    from vanguard.analytics.authority_audit import audit_active_lane_authority
    from vanguard.config.runtime_config import get_runtime_config
    from vanguard.execution.telegram_alerts import TelegramAlerts


_DEFAULT_MANIFEST = _REPO_ROOT / "config" / "ftmo_live_stack_services.json"
_LIVE_CYCLE_TRACKER = _REPO_ROOT / "scripts" / "live_cycle_tracker.py"
_DEFAULT_POST_START_DELAY_SECONDS = 30
_DEFAULT_POST_START_TIMEOUT_SECONDS = 300


def _resolve_env(value: str) -> str:
    raw = str(value or "")
    if raw.startswith("ENV:"):
        import os
        return os.environ.get(raw[4:], "")
    return raw


def _load_telegram() -> TelegramAlerts | None:
    runtime_cfg = get_runtime_config()
    tg = (runtime_cfg.get("telegram") or {})
    token = _resolve_env(str(tg.get("bot_token") or ""))
    chat_id = _resolve_env(str(tg.get("chat_id") or ""))
    enabled = bool(tg.get("enabled", True))
    if not token or not chat_id:
        return None
    return TelegramAlerts(bot_token=token, chat_id=chat_id, enabled=enabled, source="supervisor")


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _service_runtime_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, service in (manifest.get("services") or {}).items():
        pid_file = Path(str(service["pid_file"]))
        log_file = Path(str(service["log_file"]))
        pid = _read_pid(pid_file)
        out[name] = {
            "pid": pid,
            "alive": _pid_alive(pid),
            "pid_file": str(pid_file),
            "log_file": str(log_file),
            "log": _log_status(log_file),
            "required_for_ready": bool(service.get("required_for_ready")),
        }
    return out


def _status_from_manifest(manifest: dict[str, Any], *, allow_monitor_missing: bool = False, require_recent_logs: bool = False):
    defaults = manifest["status_defaults"]
    return build_status(
        profile_id=str(manifest["profile_id"]),
        db_path=Path(manifest["db_path"]),
        orchestrator_pid_file=Path(defaults["orchestrator_pid_file"]),
        lifecycle_pid_file=Path(defaults["lifecycle_pid_file"]),
        monitor_pid_file=Path(defaults["monitor_pid_file"]),
        datasette_pid_file=Path(defaults["datasette_pid_file"]),
        v1_precompute_pid_file=Path(defaults["v1_precompute_pid_file"]),
        live_ic_tracker_pid_file=Path(defaults["live_ic_tracker_pid_file"]),
        model_truth_snapshot_pid_file=Path(defaults["model_truth_snapshot_pid_file"]),
        metaapi_live_context_pid_file=Path(defaults["metaapi_live_context_pid_file"]),
        metaapi_candle_cache_pid_file=Path(defaults["metaapi_candle_cache_pid_file"]),
        orchestrator_log=Path(defaults["orchestrator_log"]),
        lifecycle_log=Path(defaults["lifecycle_log"]),
        monitor_log=Path(defaults["monitor_log"]),
        v1_precompute_log=Path(defaults["v1_precompute_log"]),
        live_ic_tracker_log=Path(defaults["live_ic_tracker_log"]),
        model_truth_snapshot_log=Path(defaults["model_truth_snapshot_log"]),
        metaapi_live_context_log=Path(defaults["metaapi_live_context_log"]),
        metaapi_candle_cache_log=Path(defaults["metaapi_candle_cache_log"]),
        require_monitor=not allow_monitor_missing,
        require_recent_logs=require_recent_logs,
    )


def _print_status(status, manifest: dict[str, Any], *, as_json: bool, verbose: bool) -> None:
    payload = dict(status.payload)
    payload["manifest_path"] = str(_DEFAULT_MANIFEST)
    payload["services"] = manifest["services"]
    payload["service_runtime"] = _service_runtime_from_manifest(manifest)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not verbose:
        print(render_operator_summary(status))
        return
    print(render_operator_summary(status))
    print()
    _print_verbose(status)
    print()
    print("== services ==")
    for name, service in payload["service_runtime"].items():
        required = bool(service.get("required_for_ready"))
        alive = bool(service.get("alive"))
        pid = service.get("pid")
        print(f"{name}: required={required} alive={alive} pid={pid or '-'} log={service['log_file']}")


def _run_script(script_path: str, *args: str) -> int:
    result = subprocess.run(["bash", script_path, *args], cwd=str(_REPO_ROOT))
    return int(result.returncode)


def _tail_log(path: Path, *, lines: int) -> int:
    if not path.exists():
        print(f"log missing: {path}", file=sys.stderr)
        return 1
    print(f"== log: {path.name} ==", flush=True)
    result = subprocess.run(["tail", "-n", str(lines), str(path)], cwd=str(_REPO_ROOT))
    return int(result.returncode)


def _status_gate_issues(status, *, allow_monitor_missing: bool) -> list[str]:
    stack_state, issues = _classify_summary(status)
    gate_issues = list(issues)
    if allow_monitor_missing:
        gate_issues = [issue for issue in gate_issues if issue != "diagnostic service_drift monitor"]
    if stack_state == "HEALTHY":
        return []
    return gate_issues


def _wait_for_post_start_ready(
    manifest: dict[str, Any],
    *,
    allow_monitor_missing: bool,
    require_recent_logs: bool,
    initial_delay_seconds: int,
    timeout_seconds: int,
) -> tuple[bool, Any]:
    if initial_delay_seconds > 0:
        time.sleep(initial_delay_seconds)
    deadline = time.time() + max(1, timeout_seconds)
    last_status = None
    while time.time() <= deadline:
        status = _status_from_manifest(
            manifest,
            allow_monitor_missing=allow_monitor_missing,
            require_recent_logs=require_recent_logs,
        )
        last_status = status
        if status.startup_ready and not _status_gate_issues(status, allow_monitor_missing=allow_monitor_missing):
            return True, status
        time.sleep(5)
    return False, last_status


def _notify_post_start_failure(
    status,
    *,
    command: str,
    no_telegram: bool,
) -> None:
    if no_telegram:
        return
    telegram = _load_telegram()
    if telegram is None:
        return
    summary = render_operator_summary(status) if status is not None else "status unavailable"
    telegram.send(
        (
            f"⚠️ <b>STACK {command.upper()} SELF-CHECK FAILED</b>\n"
            f"{summary}"
        ),
        message_type="ERROR",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FTMO PROD stack supervisor.")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "logs", "audit", "diagnostics"])
    parser.add_argument("--manifest", default=str(_DEFAULT_MANIFEST))
    parser.add_argument(
        "--service",
        choices=[
            "datasette",
            "lifecycle",
            "v1_precompute",
            "live_ic_tracker",
            "model_truth_snapshot",
            "metaapi_live_context",
            "metaapi_candle_cache",
            "orchestrator",
            "monitor",
        ],
    )
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--allow-monitor-missing", action="store_true")
    parser.add_argument("--require-recent-logs", action="store_true")
    parser.add_argument("--emergency", action="store_true")
    parser.add_argument("--skip-post-start-check", action="store_true")
    parser.add_argument("--post-start-delay-seconds", type=int, default=_DEFAULT_POST_START_DELAY_SECONDS)
    parser.add_argument("--post-start-timeout-seconds", type=int, default=_DEFAULT_POST_START_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    manifest = _load_manifest(Path(args.manifest))

    if args.command == "status":
        status = _status_from_manifest(
            manifest,
            allow_monitor_missing=args.allow_monitor_missing,
            require_recent_logs=args.require_recent_logs,
        )
        _print_status(status, manifest, as_json=args.json, verbose=args.verbose)
        return

    if args.command == "audit":
        payload = audit_active_lane_authority()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        print(
            "[active_lane_authority_audit] "
            f"status={payload.get('status')} "
            f"profiles={payload.get('active_profiles')} "
            f"bindings={payload.get('active_bindings')} "
            f"bindings_with_issues={payload.get('bindings_with_issues')}"
        )
        for row in payload.get("rows") or []:
            issues = ",".join(row.get("issues") or []) or "none"
            print(
                "  "
                f"{row.get('profile_id')} {row.get('binding_id')} "
                f"mode={row.get('execution_mode')} route={row.get('route_authority')} "
                f"submit={row.get('submit_authority')} issues={issues}"
            )
        raise SystemExit(0 if payload.get("status") == "ok" else 1)

    if args.command == "logs":
        if not args.service:
            raise SystemExit("--service is required for logs")
        log_file = Path(manifest["services"][args.service]["log_file"])
        raise SystemExit(_tail_log(log_file, lines=args.lines))

    if args.command == "diagnostics":
        result = subprocess.run(
            ["python3", str(_LIVE_CYCLE_TRACKER), "--db-path", str(manifest["db_path"])],
            cwd=str(_REPO_ROOT),
        )
        raise SystemExit(int(result.returncode))

    if args.command == "start":
        start_args: list[str] = []
        if args.no_telegram:
            start_args.append("--no-telegram")
        start_code = _run_script(manifest["startup_script"], *start_args)
        if start_code != 0:
            raise SystemExit(start_code)
        if args.skip_post_start_check:
            raise SystemExit(0)
        ok, status = _wait_for_post_start_ready(
            manifest,
            allow_monitor_missing=args.allow_monitor_missing,
            require_recent_logs=args.require_recent_logs,
            initial_delay_seconds=int(args.post_start_delay_seconds),
            timeout_seconds=int(args.post_start_timeout_seconds),
        )
        _print_status(status, manifest, as_json=args.json, verbose=args.verbose)
        if not ok:
            _notify_post_start_failure(status, command="start", no_telegram=args.no_telegram)
            raise SystemExit(1)
        raise SystemExit(0)

    if args.command == "stop":
        script = manifest["emergency_stop_script"] if args.emergency else manifest["stop_script"]
        raise SystemExit(_run_script(script))

    if args.command == "restart":
        stop_script = manifest["emergency_stop_script"] if args.emergency else manifest["stop_script"]
        stop_code = _run_script(stop_script)
        if stop_code != 0:
            raise SystemExit(stop_code)
        start_args = ["--no-telegram"] if args.no_telegram else []
        start_code = _run_script(manifest["startup_script"], *start_args)
        if start_code != 0:
            raise SystemExit(start_code)
        if args.skip_post_start_check:
            raise SystemExit(0)
        ok, status = _wait_for_post_start_ready(
            manifest,
            allow_monitor_missing=args.allow_monitor_missing,
            require_recent_logs=args.require_recent_logs,
            initial_delay_seconds=int(args.post_start_delay_seconds),
            timeout_seconds=int(args.post_start_timeout_seconds),
        )
        _print_status(status, manifest, as_json=args.json, verbose=args.verbose)
        if not ok:
            _notify_post_start_failure(status, command="restart", no_telegram=args.no_telegram)
            raise SystemExit(1)
        raise SystemExit(0)


if __name__ == "__main__":
    main()
