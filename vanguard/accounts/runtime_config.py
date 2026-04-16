"""
runtime_config.py (accounts) — Phase 2a runtime config loader + validator.

Spec: CC_PHASE_2A_UNIVERSE_RESOLVER.md §2.2

Validates the Phase 2a specific fields:
  config_version, runtime.resolved_universe_mode,
  profiles[].{id, is_active, instrument_scope, policy_id},
  every instrument_scope exists in universes.

This module is separate from vanguard/config/runtime_config.py which handles
the legacy full-system config. Both can coexist.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _REPO_ROOT / "config" / "vanguard_runtime.json"

_REQUIRED_TOP_KEYS = ("config_version", "runtime", "universes", "profiles", "session_windows_utc")
_REQUIRED_PROFILE_KEYS = ("id", "is_active", "instrument_scope", "policy_id")
_VALID_MODES = ("observe", "enforce")


class ConfigValidationError(Exception):
    """Raised when the Phase 2a runtime config fails validation."""


def load_runtime_config(path: str | None = None) -> dict[str, Any]:
    """
    Load and validate the Phase 2a vanguard_runtime.json.

    - Resolves path from env var VANGUARD_RUNTIME_CONFIG or falls back to
      the default QA path.
    - Validates required Phase 2a top-level keys.
    - Validates every profile has id, is_active, instrument_scope, policy_id.
    - Validates every instrument_scope referenced exists in universes.
    - On any validation failure: raises ConfigValidationError with specific
      field path.
    - Logs loaded config_version to stdout.
    """
    resolved = Path(
        path
        or os.environ.get("VANGUARD_RUNTIME_CONFIG", "")
        or _DEFAULT_PATH
    )

    if not resolved.exists():
        raise ConfigValidationError(
            f"Runtime config not found: {resolved}"
        )

    try:
        cfg: dict[str, Any] = json.loads(resolved.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(
            f"Runtime config is not valid JSON ({resolved}): {exc}"
        ) from exc

    if not isinstance(cfg, dict):
        raise ConfigValidationError(
            f"Runtime config must be a JSON object, got {type(cfg).__name__}"
        )

    # Required top-level keys
    for key in _REQUIRED_TOP_KEYS:
        if key not in cfg:
            raise ConfigValidationError(
                f"missing required key '{key}' in {resolved}"
            )

    # runtime.resolved_universe_mode
    mode = cfg.get("runtime", {}).get("resolved_universe_mode")
    if mode not in _VALID_MODES:
        raise ConfigValidationError(
            f"runtime.resolved_universe_mode must be one of {_VALID_MODES}, "
            f"got {mode!r}"
        )

    # profiles validation
    profiles = cfg.get("profiles")
    if not isinstance(profiles, list):
        raise ConfigValidationError("'profiles' must be a JSON array")
    if not profiles:
        raise ConfigValidationError("'profiles' must not be empty")

    universes = cfg.get("universes") or {}

    for i, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            raise ConfigValidationError(
                f"profiles[{i}] must be a JSON object, got {type(profile).__name__}"
            )
        for field in _REQUIRED_PROFILE_KEYS:
            if field not in profile:
                pid = profile.get("id", f"<index {i}>")
                raise ConfigValidationError(
                    f"profiles[id={pid!r}] missing required field '{field}'"
                )
        scope = profile["instrument_scope"]
        if scope not in universes:
            pid = profile["id"]
            raise ConfigValidationError(
                f"profiles[id={pid!r}].instrument_scope={scope!r} not found in "
                f"universes. Available: {sorted(universes)}"
            )

    version = cfg.get("config_version", "unknown")
    print(f"[runtime_config] loaded config_version={version!r} from {resolved}")

    return cfg
