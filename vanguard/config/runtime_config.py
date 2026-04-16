"""
runtime_config.py — Canonical QA/shadow config loader.

This module is the single runtime-config entrypoint for Vanguard_QAenv.
Legacy JSON files remain as fallback sources only.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Load .env from repo root before any ENV: resolution so that subprocesses
# started without an explicit `source .env` still pick up credentials.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_REPO_ROOT / ".env", override=False)
except Exception:
    pass  # dotenv not installed or .env missing — rely on OS environment

_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "vanguard_runtime.json"
_DEFAULT_SOURCE_DB = Path(os.environ.get("VANGUARD_SOURCE_DB", ""))
_DEFAULT_SHADOW_DB = _REPO_ROOT / "data" / "vanguard_universe.db"
_LEGACY_EXECUTION_CONFIG = _REPO_ROOT / "config" / "vanguard_execution_config.json"
_LEGACY_UNIVERSE_CONFIG = _REPO_ROOT / "config" / "vanguard_universes.json"
_LEGACY_TWELVEDATA_CONFIG = _REPO_ROOT / "config" / "twelvedata_symbols.json"
_LEGACY_GFT_UNIVERSE = _REPO_ROOT / "config" / "gft_universe.json"
_LEGACY_ML_THRESHOLDS = _REPO_ROOT / "config" / "vanguard_ml_thresholds.json"

_RUNTIME_CONFIG: dict[str, Any] | None = None
_RUNTIME_CONFIG_PATH = _DEFAULT_CONFIG_PATH

_DEFAULT_SOURCE_TABLES = [
    "vanguard_bars_1m",
    "vanguard_bars_5m",
    "vanguard_bars_1h",
    "vanguard_universe_members",
    "vanguard_training_data",
]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    return json.loads(path.read_text())


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("ENV:"):
        return os.environ.get(value[4:], "")
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def _base_runtime_config() -> dict[str, Any]:
    execution_cfg = _read_json(_LEGACY_EXECUTION_CONFIG, {})
    universe_cfg = _read_json(_LEGACY_UNIVERSE_CONFIG, {})
    td_symbols = _read_json(_LEGACY_TWELVEDATA_CONFIG, {})
    gft_symbols = _read_json(_LEGACY_GFT_UNIVERSE, {"gft_universe": []})
    ml_thresholds = _read_json(_LEGACY_ML_THRESHOLDS, {})

    return {
        "runtime": {
            "environment": "qa-shadow",
            "execution_mode": "test",
            "cycle_interval_seconds": 300,
            "single_cycle_default": True,
            "replay_from_source_db": True,
            "force_assets": [],
            "shortlist_top_n_per_asset_class": 30,
            "shortlist_display_top_n": 5,
        },
        "database": {
            "source_db_path": str(_DEFAULT_SOURCE_DB),
            "shadow_db_path": str(_DEFAULT_SHADOW_DB),
            "readonly_passthrough_tables": list(_DEFAULT_SOURCE_TABLES),
        },
        "market_data": {
            "sessions": {
                "equity": {
                    "days": [0, 1, 2, 3, 4],
                    "start": "09:30",
                    "end": "16:00",
                },
                "index": {
                    "days": [0, 1, 2, 3, 4],
                    "start": "09:30",
                    "end": "16:00",
                },
                "forex": {
                    "days": [6, 0, 1, 2, 3, 4],
                    "start": "17:00",
                    "end": "17:00",
                },
                "crypto": {
                    "days": [0, 1, 2, 3, 4, 5, 6],
                    "start": None,
                    "end": None,
                },
            },
            "sources": {
                "equity": ["ibkr", "alpaca"],
                "forex": ["ibkr", "twelvedata"],
                "crypto": ["twelvedata"],
            },
            "twelvedata_symbols": {
                key: value
                for key, value in td_symbols.items()
                if not str(key).startswith("_")
            },
        },
        "universes": {
            **dict(universe_cfg.get("universes") or {}),
            "gft_universe": {
                "mode": "static_explicit",
                "symbols": list(gft_symbols.get("gft_universe") or []),
            },
        },
        "risk_defaults": {
            "ml_thresholds": ml_thresholds,
            "gft": {
                "max_positions": 3,
                "risk_per_trade_pct": 0.005,
                "daily_loss_limit_pct": 0.04,
                "max_portfolio_heat_pct": 0.02,
                "min_sl_pips": 20.0,
                "min_tp_pips": 40.0,
                "min_crypto_qty": 0.000001,
                "min_crypto_sl_pct": 0.08,
                "min_crypto_tp_pct": 0.16,
            },
        },
        "profiles": [],
        "execution": {
            "mode": "test",
            "signalstack": dict(execution_cfg.get("signalstack") or {}),
            "telegram": {
                **dict(execution_cfg.get("telegram") or {}),
                "enabled": False,
            },
            "defaults": dict(execution_cfg.get("defaults") or {}),
        },
        "telegram": {
            "enabled": False,
            "max_message_chars": 3500,
            "bot_token": "ENV:TELEGRAM_BOT_TOKEN",
            "chat_id": "ENV:TELEGRAM_CHAT_ID",
        },
        "models": {
            "equity": {
                "model_dir": str(_REPO_ROOT / "models" / "equity_regressors"),
                "meta_file": "meta.json",
            },
            "forex": {
                "model_dir": str(_REPO_ROOT / "models" / "vanguard" / "latest"),
                "meta_file": "vanguard_forex_model_meta.json",
            },
            "crypto": {
                "model_dir": str(_REPO_ROOT / "models" / "vanguard" / "latest"),
                "meta_file": "vanguard_crypto_model_meta.json",
            },
            "metal": {
                "model_dir": str(_REPO_ROOT / "models" / "lgbm_metals_energy_v1"),
                "meta_file": "meta.json",
            },
            "energy": {
                "model_dir": str(_REPO_ROOT / "models" / "lgbm_metals_energy_v1"),
                "meta_file": "meta.json",
            },
            "index": {
                "model_dir": str(_REPO_ROOT / "models" / "lgbm_index_v1"),
                "meta_file": "meta.json",
            },
        },
    }


def _validate_runtime_config(cfg: dict[str, Any]) -> None:
    required_top = ["runtime", "database", "market_data", "universes", "risk_defaults", "profiles", "execution", "telegram", "models"]
    missing = [key for key in required_top if key not in cfg]
    if missing:
        raise ValueError(f"Missing runtime config sections: {missing}")

    db_cfg = cfg.get("database") or {}
    if not db_cfg.get("source_db_path"):
        raise ValueError("database.source_db_path is required")
    if not db_cfg.get("shadow_db_path"):
        raise ValueError("database.shadow_db_path is required")
    if not isinstance(cfg.get("profiles"), list):
        raise ValueError("profiles must be a list")


def load_runtime_config(config_path: str | Path | None = None, *, refresh: bool = False) -> dict[str, Any]:
    global _RUNTIME_CONFIG, _RUNTIME_CONFIG_PATH

    path = Path(config_path or _DEFAULT_CONFIG_PATH)
    if _RUNTIME_CONFIG is not None and not refresh and path == _RUNTIME_CONFIG_PATH:
        return deepcopy(_RUNTIME_CONFIG)

    if path.exists():
        payload = json.loads(path.read_text())
    else:
        payload = {}

    cfg = _base_runtime_config()
    for key, value in payload.items():
        cfg[key] = value
    cfg = _resolve_env(cfg)
    _validate_runtime_config(cfg)

    _RUNTIME_CONFIG = cfg
    _RUNTIME_CONFIG_PATH = path
    return deepcopy(_RUNTIME_CONFIG)


def get_runtime_config(*, refresh: bool = False) -> dict[str, Any]:
    return load_runtime_config(_DEFAULT_CONFIG_PATH, refresh=refresh)


def save_runtime_config(payload: dict[str, Any], config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path or _DEFAULT_CONFIG_PATH)
    merged = _base_runtime_config()
    for key, value in dict(payload or {}).items():
        merged[key] = value
    _validate_runtime_config(merged)
    path.write_text(json.dumps(merged, indent=2, sort_keys=False) + "\n")
    return load_runtime_config(path, refresh=True)


def get_source_db_path() -> str:
    return str(get_runtime_config().get("database", {}).get("source_db_path") or _DEFAULT_SOURCE_DB)


def get_shadow_db_path() -> str:
    return str(get_runtime_config().get("database", {}).get("shadow_db_path") or _DEFAULT_SHADOW_DB)


def get_readonly_passthrough_tables() -> list[str]:
    tables = get_runtime_config().get("database", {}).get("readonly_passthrough_tables") or []
    return [str(t) for t in tables if str(t).strip()]


def is_replay_from_source_db() -> bool:
    return bool(get_runtime_config().get("runtime", {}).get("replay_from_source_db", True))


def get_execution_config() -> dict[str, Any]:
    return dict(get_runtime_config().get("execution") or {})


def get_profiles_config() -> list[dict[str, Any]]:
    return [dict(p) for p in (get_runtime_config().get("profiles") or [])]


def get_universes_config() -> dict[str, Any]:
    return dict(get_runtime_config().get("universes") or {})


def get_market_data_config() -> dict[str, Any]:
    return dict(get_runtime_config().get("market_data") or {})


_MARKET_DATA_SOURCE_LABELS = {
    "mt5_local": "mt5_dwx",
}

_ASSET_CLASS_SOURCE_FALLBACKS = {
    "metal": "commodity",
    "energy": "commodity",
    "agriculture": "commodity",
}


def _candidate_asset_classes(asset_class: str) -> list[str]:
    normalized = str(asset_class or "").strip().lower()
    if not normalized:
        return []
    alias = _ASSET_CLASS_SOURCE_FALLBACKS.get(normalized)
    return [normalized, alias] if alias and alias != normalized else [normalized]


def get_data_sources_config() -> dict[str, Any]:
    return dict(get_runtime_config().get("data_sources") or {})


def get_context_sources_config() -> dict[str, Any]:
    return dict(get_runtime_config().get("context_sources") or {})


def resolve_market_data_source_id(
    asset_class: str,
    *,
    runtime_config: dict[str, Any] | None = None,
) -> str:
    cfg = runtime_config or get_runtime_config()
    md_cfg = cfg.get("market_data") or {}
    data_sources = cfg.get("data_sources") or {}

    for candidate in _candidate_asset_classes(asset_class):
        explicit = (md_cfg.get("primary_source_by_asset_class") or {}).get(candidate)
        if explicit:
            return str(explicit)

    for source_id, source_cfg in data_sources.items():
        if not bool(source_cfg.get("enabled", False)):
            continue
        primary_for = {str(v).lower() for v in (source_cfg.get("primary_for") or [])}
        if any(candidate in primary_for for candidate in _candidate_asset_classes(asset_class)):
            return str(source_id)

    market_sources = md_cfg.get("sources") or {}
    for candidate in _candidate_asset_classes(asset_class):
        configured = market_sources.get(candidate) or []
        if configured:
            return str(configured[0])

    return ""


def data_source_label_for_source_id(
    source_id: str,
    *,
    runtime_config: dict[str, Any] | None = None,
) -> str:
    normalized = str(source_id or "").strip()
    if not normalized:
        return "unknown"
    cfg = runtime_config or get_runtime_config()
    source_cfg = (cfg.get("data_sources") or {}).get(normalized) or {}
    explicit = source_cfg.get("data_source_label")
    if explicit:
        return str(explicit)
    return _MARKET_DATA_SOURCE_LABELS.get(normalized, normalized)


def resolve_market_data_source_label(
    asset_class: str,
    *,
    runtime_config: dict[str, Any] | None = None,
) -> str:
    source_id = resolve_market_data_source_id(asset_class, runtime_config=runtime_config)
    return data_source_label_for_source_id(source_id, runtime_config=runtime_config)


def get_twelvedata_symbols_config() -> dict[str, list[str]]:
    td = (get_market_data_config().get("twelvedata_symbols") or {})
    return {
        str(asset_class): [str(sym) for sym in symbols]
        for asset_class, symbols in td.items()
        if isinstance(symbols, list)
    }


def get_model_config(asset_class: str) -> dict[str, Any]:
    return dict((get_runtime_config().get("models") or {}).get(asset_class) or {})


def get_risk_defaults() -> dict[str, Any]:
    return dict(get_runtime_config().get("risk_defaults") or {})


def get_policy_templates_config() -> dict[str, Any]:
    return dict(get_runtime_config().get("policy_templates") or {})
