"""
db.py — SQLite WAL helper for Vanguard.

All writes go to vanguard_universe.db only.
DB isolation is enforced at construction time.

Location: ~/SS/Vanguard/vanguard/helpers/db.py
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from vanguard.config.runtime_config import (
    resolve_market_data_source_label,
    get_readonly_passthrough_tables,
    get_shadow_db_path,
    get_source_db_path,
    is_replay_from_source_db,
)

logger = logging.getLogger(__name__)

_DB_CONNECT_ATTEMPTS = 3
_DB_CONNECT_RETRY_SLEEP_SEC = 0.3
_DB_WAL_WARN_BYTES = 1 * 1024 * 1024 * 1024
_DB_WRITE_RETRY_ATTEMPTS = 12
_DB_WRITE_RETRY_SLEEP_SEC = 0.5
_CRYPTO_TRAINING_FEATURE_COLUMNS = [
    "btc_relative_momentum",
    "btc_adjusted_return",
    "btc_correlation_z",
    "volatility_regime",
    "range_expansion_5m",
    "momentum_divergence",
    "volume_delta",
    "liquidity_sweep_strength",
    "fvg_size",
]


def _legacy_universe_source(asset_class: str, universe: str, existing_source: str | None = None) -> str:
    """Resolve a bar-source label for migrated universe rows using runtime config first."""
    resolved = resolve_market_data_source_label(asset_class)
    if resolved and resolved != "unknown":
        return resolved
    universe_name = str(universe or "").lower()
    if universe_name.startswith("ttp"):
        return "alpaca"
    if universe_name.startswith("topstep") or str(asset_class or "").lower() == "futures":
        return "ibkr"
    if existing_source:
        return str(existing_source)
    return "unknown"


def _resolved_path(db_path: str) -> str:
    return str(Path(db_path).expanduser().resolve())


def _is_sqlite_write_lock(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg


def _uses_shadow_source_overlay(db_path: str) -> bool:
    return (
        is_replay_from_source_db()
        and _resolved_path(db_path) == _resolved_path(get_shadow_db_path())
        and _resolved_path(get_source_db_path()) != _resolved_path(get_shadow_db_path())
    )


def _attach_source_readonly_views(conn: sqlite3.Connection, db_path: str) -> None:
    """Expose large source tables from the prod DB as temp read-only views."""
    if not _uses_shadow_source_overlay(db_path):
        return

    source_db_path = _resolved_path(get_source_db_path())
    if not Path(source_db_path).exists():
        logger.warning("QA source DB missing; continuing without source overlay: %s", source_db_path)
        return

    conn.execute(f"ATTACH DATABASE 'file:{source_db_path}?mode=ro' AS source_db")
    passthrough_tables = set(get_readonly_passthrough_tables())
    source_rows = conn.execute(
        "SELECT name FROM source_db.sqlite_master WHERE type='table'"
    ).fetchall()
    source_tables = {str(r[0]) for r in source_rows}
    for table_name in sorted(passthrough_tables & source_tables):
        try:
            conn.execute(
                f"CREATE TEMP VIEW IF NOT EXISTS {table_name} AS "
                f"SELECT * FROM source_db.{table_name}"
            )
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Could not expose source read-only view for %s from %s: %s",
                table_name,
                source_db_path,
                exc,
            )


class _AutoCloseConnection(sqlite3.Connection):
    """sqlite3 connection whose context manager commits/rolls back and closes."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


# ---------------------------------------------------------------------------
# Isolation guard
# ---------------------------------------------------------------------------

def assert_db_isolation(db_path: str) -> None:
    """Called by every Vanguard stage at startup."""
    assert "vanguard_universe" in db_path, (
        f"DB isolation violation: '{db_path}' is not the Vanguard DB"
    )
    assert "v2_universe" not in db_path, (
        f"DB isolation violation: attempting to use Meridian DB"
    )


# ---------------------------------------------------------------------------
# Low-level connection
# ---------------------------------------------------------------------------

def connect_wal(db_path: str) -> sqlite3.Connection:
    """
    Open a WAL-mode SQLite connection.
    Applies: journal_mode=WAL, busy_timeout=30s, synchronous=NORMAL.
    """
    assert_db_isolation(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    warn_if_large_wal(db_path)
    last_exc: Exception | None = None
    for attempt in range(1, _DB_CONNECT_ATTEMPTS + 1):
        try:
            conn = sqlite3.connect(
                db_path,
                timeout=30,
                factory=_AutoCloseConnection,
            )
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.row_factory = sqlite3.Row
            _attach_source_readonly_views(conn, db_path)
            return conn
        except sqlite3.Error as exc:
            last_exc = exc
            logger.warning(
                "connect_wal failed for db_path=%s attempt=%d/%d: %s",
                db_path,
                attempt,
                _DB_CONNECT_ATTEMPTS,
                exc,
            )
            if attempt < _DB_CONNECT_ATTEMPTS:
                time.sleep(_DB_CONNECT_RETRY_SLEEP_SEC)
    raise sqlite3.OperationalError(
        f"connect_wal exhausted retries for db_path={db_path}: {last_exc}"
    ) from last_exc


@contextmanager
def sqlite_conn(db_path: str):
    """Yield a WAL connection and always close it on context exit."""
    conn = connect_wal(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def warn_if_large_wal(db_path: str, threshold_bytes: int = _DB_WAL_WARN_BYTES) -> int:
    """Log when the WAL grows beyond the maintenance threshold."""
    wal_path = Path(f"{db_path}-wal")
    try:
        wal_bytes = wal_path.stat().st_size
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.warning("Could not stat WAL for db_path=%s wal_path=%s: %s", db_path, wal_path, exc)
        return 0

    if threshold_bytes > 0 and wal_bytes >= threshold_bytes:
        logger.warning(
            "Large SQLite WAL detected for db_path=%s wal_path=%s size=%.2f GiB",
            db_path,
            wal_path,
            wal_bytes / (1024 ** 3),
        )
    return wal_bytes


def checkpoint_wal_truncate(
    db_path: str,
    *,
    min_wal_bytes: int = _DB_WAL_WARN_BYTES,
    timeout_seconds: float = 5.0,
) -> bool:
    """
    Run a controlled WAL checkpoint outside the hot connection path.

    Returns True when a TRUNCATE checkpoint was attempted successfully, False
    when no checkpoint was needed or lock/contention prevented maintenance.
    """
    assert_db_isolation(db_path)
    wal_bytes = warn_if_large_wal(db_path, threshold_bytes=min_wal_bytes)
    if wal_bytes < min_wal_bytes:
        return False

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path, timeout=timeout_seconds)
        conn.execute("PRAGMA busy_timeout=1000;")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.close()
        logger.info(
            "SQLite WAL checkpoint(TRUNCATE) completed for db_path=%s previous_wal_size=%.2f GiB",
            db_path,
            wal_bytes / (1024 ** 3),
        )
        return True
    except sqlite3.Error as exc:
        logger.warning(
            "SQLite WAL checkpoint(TRUNCATE) skipped for db_path=%s wal_size=%.2f GiB: %s",
            db_path,
            wal_bytes / (1024 ** 3),
            exc,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# VanguardDB — table management and upsert helpers
# ---------------------------------------------------------------------------

class VanguardDB:
    """
    Thin wrapper around vanguard_universe.db.
    Handles table creation, batch upserts, and metadata key-value store.
    """

    def __init__(self, db_path: str):
        assert_db_isolation(db_path)
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def connect(self) -> sqlite3.Connection:
        last_exc: Exception | None = None
        for attempt in range(1, _DB_CONNECT_ATTEMPTS + 1):
            try:
                return connect_wal(self.db_path)
            except sqlite3.Error as exc:
                last_exc = exc
                logger.warning(
                    "VanguardDB.connect failed for db_path=%s attempt=%d/%d: %s",
                    self.db_path,
                    attempt,
                    _DB_CONNECT_ATTEMPTS,
                    exc,
                )
                if attempt < _DB_CONNECT_ATTEMPTS:
                    time.sleep(_DB_CONNECT_RETRY_SLEEP_SEC)
        raise sqlite3.OperationalError(
            f"VanguardDB.connect exhausted retries for db_path={self.db_path}: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _init_tables(self) -> None:
        with self.connect() as conn:
            def _safe_execute(sql: str) -> None:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    logger.warning("Skipping DB DDL due to lock/contention: %s", exc)

            def _ensure_column(table: str, column: str, ddl: str) -> None:
                cols = {
                    row[1]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in cols:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                    except sqlite3.OperationalError as exc:
                        logger.warning(
                            "Skipping column migration %s.%s due to lock/contention: %s",
                            table,
                            column,
                            exc,
                        )

            def _table_columns(table: str) -> set[str]:
                return {
                    row[1]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }

            owns_source_tables = not _uses_shadow_source_overlay(self.db_path)

            if owns_source_tables:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vanguard_bars_5m (
                        symbol      TEXT NOT NULL,
                        bar_ts_utc  TEXT NOT NULL,
                        open        REAL,
                        high        REAL,
                        low         REAL,
                        close       REAL,
                        volume      INTEGER,
                        asset_class TEXT DEFAULT 'equity',
                        data_source TEXT DEFAULT 'alpaca',
                        PRIMARY KEY (symbol, bar_ts_utc)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vanguard_bars_1h (
                        symbol     TEXT NOT NULL,
                        bar_ts_utc TEXT NOT NULL,
                        open       REAL,
                        high       REAL,
                        low        REAL,
                        close      REAL,
                        volume     INTEGER,
                        PRIMARY KEY (symbol, bar_ts_utc)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vanguard_bars_1m (
                        symbol        TEXT    NOT NULL,
                        bar_ts_utc    TEXT    NOT NULL,
                        open          REAL,
                        high          REAL,
                        low           REAL,
                        close         REAL,
                        volume        INTEGER,
                        tick_volume   INTEGER DEFAULT 0,
                        asset_class   TEXT    DEFAULT 'unknown',
                        data_source   TEXT    DEFAULT 'unknown',
                        ingest_ts_utc TEXT,
                        PRIMARY KEY (symbol, bar_ts_utc)
                    )
                """)
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_bars_1m_ts
                        ON vanguard_bars_1m(bar_ts_utc)
                """)
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_bars_1m_source_asset_ts
                        ON vanguard_bars_1m(data_source, asset_class, bar_ts_utc)
                """)
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_bars_1m_symbol_ts
                        ON vanguard_bars_1m(symbol, bar_ts_utc)
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_cache_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            if owns_source_tables:
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_bars_5m_ts
                        ON vanguard_bars_5m(bar_ts_utc)
                """)
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_bars_5m_symbol_ts
                        ON vanguard_bars_5m(symbol, bar_ts_utc)
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_health (
                    symbol          TEXT NOT NULL,
                    cycle_ts_utc    TEXT NOT NULL,
                    status          TEXT,
                    asset_class     TEXT DEFAULT 'equity',
                    data_source     TEXT,
                    session_active  INTEGER DEFAULT 1,
                    last_bar_age_seconds INTEGER,
                    relative_volume REAL,
                    spread_bps      REAL,
                    bars_available  INTEGER,
                    last_bar_ts_utc TEXT,
                    PRIMARY KEY (symbol, cycle_ts_utc)
                )
            """)
            _safe_execute("""
                CREATE INDEX IF NOT EXISTS idx_vg_health_cycle
                    ON vanguard_health(cycle_ts_utc, status)
            """)
            _ensure_column("vanguard_health", "asset_class", "asset_class TEXT DEFAULT 'equity'")
            _ensure_column("vanguard_health", "data_source", "data_source TEXT")
            _ensure_column("vanguard_health", "session_active", "session_active INTEGER DEFAULT 1")
            _ensure_column("vanguard_health", "last_bar_age_seconds", "last_bar_age_seconds INTEGER")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_features (
                    symbol        TEXT NOT NULL,
                    cycle_ts_utc  TEXT NOT NULL,
                    features_json TEXT,
                    PRIMARY KEY (symbol, cycle_ts_utc)
                )
            """)
            if owns_source_tables:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vanguard_universe_members (
                        symbol        TEXT NOT NULL,
                        asset_class   TEXT NOT NULL,
                        data_source   TEXT NOT NULL,
                        universe      TEXT NOT NULL,
                        exchange      TEXT,
                        tick_size     REAL,
                        tick_value    REAL,
                        point_value   REAL,
                        margin        REAL,
                        session_start TEXT,
                        session_end   TEXT,
                        session_tz    TEXT DEFAULT 'America/New_York',
                        is_active     INTEGER DEFAULT 1,
                        added_at      TEXT,
                        last_seen_at  TEXT,
                        PRIMARY KEY (symbol, data_source)
                    )
                """)
                cols = _table_columns("vanguard_universe_members")
                expected_cols = {
                    "symbol", "asset_class", "data_source", "universe", "exchange",
                    "tick_size", "tick_value", "point_value", "margin",
                    "session_start", "session_end", "session_tz",
                    "is_active", "added_at", "last_seen_at",
                }
                if cols != expected_cols:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS vanguard_universe_members_v2 (
                            symbol        TEXT NOT NULL,
                            asset_class   TEXT NOT NULL,
                            data_source   TEXT NOT NULL,
                            universe      TEXT NOT NULL,
                            exchange      TEXT,
                            tick_size     REAL,
                            tick_value    REAL,
                            point_value   REAL,
                            margin        REAL,
                            session_start TEXT,
                            session_end   TEXT,
                            session_tz    TEXT DEFAULT 'America/New_York',
                            is_active     INTEGER DEFAULT 1,
                            added_at      TEXT,
                            last_seen_at  TEXT,
                            PRIMARY KEY (symbol, data_source)
                        )
                    """)
                    if cols:
                        legacy_rows = conn.execute(
                            "SELECT * FROM vanguard_universe_members"
                        ).fetchall()
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO vanguard_universe_members_v2 (
                                symbol, asset_class, data_source, universe, exchange,
                                tick_size, tick_value, point_value, margin,
                                session_start, session_end, session_tz,
                                is_active, added_at, last_seen_at
                            )
                            VALUES (
                                :symbol, :asset_class, :data_source, :universe, :exchange,
                                :tick_size, :tick_value, :point_value, :margin,
                                :session_start, :session_end, :session_tz,
                                :is_active, :added_at, :last_seen_at
                            )
                            """,
                            [
                                {
                                    "symbol": row["symbol"],
                                    "asset_class": row["asset_class"],
                                    "data_source": _legacy_universe_source(
                                        str(row["asset_class"] or ""),
                                        str(row["universe"] or ""),
                                        str(row["data_source"]) if "data_source" in cols else None,
                                    ),
                                    "universe": row["universe"],
                                    "exchange": row["exchange"] if "exchange" in cols else None,
                                    "tick_size": row["tick_size"] if "tick_size" in cols else None,
                                    "tick_value": row["tick_value"] if "tick_value" in cols else None,
                                    "point_value": row["point_value"] if "point_value" in cols else None,
                                    "margin": row["margin"] if "margin" in cols else None,
                                    "session_start": (
                                        row["session_start"]
                                        if "session_start" in cols
                                        else ("09:30" if row["asset_class"] == "equity" else None)
                                    ),
                                    "session_end": (
                                        row["session_end"]
                                        if "session_end" in cols
                                        else ("16:00" if row["asset_class"] == "equity" else None)
                                    ),
                                    "session_tz": (
                                        row["session_tz"] if "session_tz" in cols else "America/New_York"
                                    ),
                                    "is_active": row["is_active"] if "is_active" in cols else 1,
                                    "added_at": (
                                        row["added_at"]
                                        if "added_at" in cols
                                        else row["last_refreshed_utc"] if "last_refreshed_utc" in cols else None
                                    ) or datetime.utcnow().isoformat(),
                                    "last_seen_at": (
                                        row["last_seen_at"]
                                        if "last_seen_at" in cols
                                        else row["last_refreshed_utc"] if "last_refreshed_utc" in cols else None
                                    ) or datetime.utcnow().isoformat(),
                                }
                                for row in legacy_rows
                            ],
                        )
                        _safe_execute("DROP TABLE vanguard_universe_members")
                        _safe_execute(
                            "ALTER TABLE vanguard_universe_members_v2 RENAME TO vanguard_universe_members"
                        )
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_universe_members_universe
                        ON vanguard_universe_members(universe)
                """)
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_universe_members_asset
                        ON vanguard_universe_members(asset_class)
                """)
                _safe_execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_universe_members_source
                        ON vanguard_universe_members(data_source)
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_source_health (
                    data_source              TEXT NOT NULL,
                    asset_class              TEXT NOT NULL,
                    last_bar_ts              TEXT,
                    last_poll_ts             TEXT,
                    symbols_active           INTEGER,
                    symbols_with_recent_bar  INTEGER,
                    bars_last_5min           INTEGER,
                    status                   TEXT DEFAULT 'unknown',
                    last_updated             TEXT,
                    PRIMARY KEY (data_source, asset_class)
                )
            """)
            # ------------------------------------------------------------------
            # vanguard_training_data (V4A)
            # ------------------------------------------------------------------
            if owns_source_tables:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vanguard_training_data (
                    asof_ts_utc                     TEXT NOT NULL,
                    symbol                          TEXT NOT NULL,
                    asset_class                     TEXT NOT NULL,
                    path                            TEXT NOT NULL,
                    entry_price                     REAL NOT NULL,
                    label_long                      INTEGER NOT NULL,
                    label_short                     INTEGER NOT NULL,
                    forward_return                  REAL,
                    max_favorable_excursion         REAL,
                    max_adverse_excursion           REAL,
                    exit_bar                        INTEGER,
                    exit_type_long                  TEXT,
                    exit_type_short                 TEXT,
                    truncated                       INTEGER DEFAULT 0,
                    warm_up                         INTEGER DEFAULT 0,
                    session_vwap_distance           REAL,
                    premium_discount_zone           REAL,
                    gap_pct                         REAL,
                    session_opening_range_position  REAL,
                    daily_drawdown_from_high        REAL,
                    momentum_3bar                   REAL,
                    momentum_12bar                  REAL,
                    momentum_acceleration           REAL,
                    atr_expansion                   REAL,
                    daily_adx                       REAL,
                    relative_volume                 REAL,
                    volume_burst_z                  REAL,
                    down_volume_ratio               REAL,
                    effort_vs_result                REAL,
                    spread_proxy                    REAL,
                    rs_vs_benchmark_intraday        REAL,
                    daily_rs_vs_benchmark           REAL,
                    benchmark_momentum_12bar        REAL,
                    cross_asset_correlation         REAL,
                    daily_conviction                REAL,
                    session_phase                   REAL,
                    time_in_session_pct             REAL,
                    bars_since_session_open         REAL,
                    ob_proximity_5m                 REAL,
                    fvg_bullish_nearest             REAL,
                    fvg_bearish_nearest             REAL,
                    structure_break                 REAL,
                    liquidity_sweep                 REAL,
                    smc_premium_discount            REAL,
                    htf_trend_direction             REAL,
                    htf_structure_break             REAL,
                    htf_fvg_nearest                 REAL,
                    htf_ob_proximity                REAL,
                    btc_relative_momentum           REAL,
                    btc_adjusted_return             REAL,
                    btc_correlation_z               REAL,
                    volatility_regime               REAL,
                    range_expansion_5m              REAL,
                    momentum_divergence             REAL,
                    volume_delta                    REAL,
                    liquidity_sweep_strength        REAL,
                    fvg_size                        REAL,
                    bars_available                  REAL,
                    nan_ratio                       REAL,
                    horizon_bars                    INTEGER NOT NULL,
                    tp_pct                          REAL NOT NULL,
                    sl_pct                          REAL NOT NULL,
                    tbm_profile                     TEXT,
                    PRIMARY KEY (asof_ts_utc, symbol, horizon_bars)
                )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_train_asset
                        ON vanguard_training_data(asset_class)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_train_date
                        ON vanguard_training_data(asof_ts_utc)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_train_long
                        ON vanguard_training_data(label_long)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vg_train_short
                        ON vanguard_training_data(label_short)
                """)
            # ------------------------------------------------------------------
            # vanguard_model_registry (V4B)
            # ------------------------------------------------------------------
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_model_registry (
                    model_id            TEXT PRIMARY KEY,
                    model_family        TEXT NOT NULL,
                    track               TEXT NOT NULL,
                    target_label        TEXT NOT NULL,
                    feature_count       INTEGER,
                    train_rows          INTEGER,
                    test_rows           INTEGER,
                    walk_forward_windows INTEGER,
                    mean_ic_long        REAL,
                    mean_ic_short       REAL,
                    auc_long            REAL,
                    auc_short           REAL,
                    long_wr_at_55       REAL,
                    short_wr_at_55      REAL,
                    artifact_path       TEXT,
                    created_at_utc      TEXT,
                    status              TEXT DEFAULT 'trained',
                    notes               TEXT,
                    asset_class         TEXT,
                    direction           TEXT,
                    feature_profile     TEXT,
                    tbm_profile         TEXT,
                    training_rows       INTEGER,
                    readiness           TEXT DEFAULT 'not_trained',
                    fallback_family     TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vanguard_walkforward_results (
                    model_id        TEXT NOT NULL,
                    window_id       INTEGER NOT NULL,
                    train_start     TEXT,
                    train_end       TEXT,
                    test_start      TEXT,
                    test_end        TEXT,
                    test_rows       INTEGER,
                    ic_long         REAL,
                    ic_short        REAL,
                    auc             REAL,
                    accuracy        REAL,
                    wr_at_55        REAL,
                    asset_class     TEXT,
                    PRIMARY KEY (model_id, window_id)
                )
            """)
            if owns_source_tables:
                _ensure_column("vanguard_training_data", "tbm_profile", "tbm_profile TEXT")
                for column in _CRYPTO_TRAINING_FEATURE_COLUMNS:
                    _ensure_column("vanguard_training_data", column, f"{column} REAL")
            _ensure_column("vanguard_model_registry", "asset_class", "asset_class TEXT")
            _ensure_column("vanguard_model_registry", "direction", "direction TEXT")
            _ensure_column("vanguard_model_registry", "feature_profile", "feature_profile TEXT")
            _ensure_column("vanguard_model_registry", "tbm_profile", "tbm_profile TEXT")
            _ensure_column("vanguard_model_registry", "training_rows", "training_rows INTEGER")
            _ensure_column(
                "vanguard_model_registry",
                "readiness",
                "readiness TEXT DEFAULT 'not_trained'",
            )
            _ensure_column(
                "vanguard_model_registry",
                "fallback_family",
                "fallback_family TEXT",
            )
            _ensure_column("vanguard_walkforward_results", "asset_class", "asset_class TEXT")
            conn.commit()

    # ------------------------------------------------------------------
    # Batch upserts
    # ------------------------------------------------------------------

    def upsert_bars_5m(self, rows: list[dict]) -> int:
        """
        Batch upsert 5m bars.
        Each row must have keys: symbol, bar_ts_utc, open, high, low,
        close, volume, asset_class, data_source.
        Returns number of rows written.
        """
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_bars_5m
                    (symbol, bar_ts_utc, open, high, low, close, volume,
                     asset_class, data_source)
                VALUES
                    (:symbol, :bar_ts_utc, :open, :high, :low, :close, :volume,
                     :asset_class, :data_source)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def upsert_bars_1h(self, rows: list[dict]) -> int:
        """
        Batch upsert 1h bars.
        Each row must have keys: symbol, bar_ts_utc, open, high, low,
        close, volume.
        Returns number of rows written.
        """
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_bars_1h
                    (symbol, bar_ts_utc, open, high, low, close, volume)
                VALUES
                    (:symbol, :bar_ts_utc, :open, :high, :low, :close, :volume)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def upsert_bars_1m(self, rows: list[dict]) -> int:
        """
        Batch upsert 1m bars.
        Each row must have keys: symbol, bar_ts_utc, open, high, low,
        close, volume, asset_class, data_source.
        Optional: tick_volume, ingest_ts_utc.
        Returns number of rows written.
        """
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_bars_1m
                    (symbol, bar_ts_utc, open, high, low, close, volume,
                     tick_volume, asset_class, data_source, ingest_ts_utc)
                VALUES
                    (:symbol, :bar_ts_utc, :open, :high, :low, :close, :volume,
                     :tick_volume, :asset_class, :data_source, :ingest_ts_utc)
                """,
                [
                    {
                        **r,
                        "tick_volume":   r.get("tick_volume", 0),
                        "ingest_ts_utc": r.get("ingest_ts_utc"),
                    }
                    for r in rows
                ],
            )
            conn.commit()
        return len(rows)

    def upsert_bars_mtf(self, rows: list[dict], timeframe: str) -> int:
        """
        Batch upsert aggregated intraday bars (10m/15m/30m).
        Each row must have keys: symbol, bar_ts_utc, open, high, low,
        close, volume, asset_class, data_source.
        """
        if not rows:
            return 0
        table = f"vanguard_bars_{timeframe}"
        if timeframe not in {"10m", "15m", "30m"}:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        with self.connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    symbol TEXT NOT NULL,
                    bar_ts_utc TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    asset_class TEXT,
                    source TEXT DEFAULT 'aggregated_from_5m',
                    PRIMARY KEY (symbol, bar_ts_utc)
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_symbol_ts
                    ON {table}(symbol, bar_ts_utc)
                """
            )
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {table}
                    (symbol, bar_ts_utc, open, high, low, close, volume,
                     asset_class, source)
                VALUES
                    (:symbol, :bar_ts_utc, :open, :high, :low, :close, :volume,
                     :asset_class, :data_source)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Metadata key-value store
    # ------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vanguard_cache_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM vanguard_cache_meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Diagnostic queries (used by validate())
    # ------------------------------------------------------------------

    def count_symbols_5m(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM vanguard_bars_5m"
            ).fetchone()
        return row[0] if row else 0

    def count_bars_5m(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM vanguard_bars_5m").fetchone()
        return row[0] if row else 0

    def count_symbols_1h(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM vanguard_bars_1h"
            ).fetchone()
        return row[0] if row else 0

    def count_bars_1h(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM vanguard_bars_1h").fetchone()
        return row[0] if row else 0

    def latest_bar_ts_utc(self, table: str = "vanguard_bars_5m") -> str | None:
        """Most recent bar_ts_utc across all symbols in the given table."""
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT MAX(bar_ts_utc) FROM {table}"
            ).fetchone()
        return row[0] if row else None

    def db_size_mb(self) -> float:
        """Current DB file size in MB."""
        path = Path(self.db_path)
        if not path.exists():
            return 0.0
        return path.stat().st_size / (1024 * 1024)

    # ------------------------------------------------------------------
    # vanguard_health (V2)
    # ------------------------------------------------------------------

    def upsert_health(self, rows: list[dict]) -> int:
        """
        Batch upsert health results.
        Each row must have: symbol, cycle_ts_utc, status.
        """
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_health
                    (symbol, cycle_ts_utc, status, asset_class, data_source,
                     session_active, last_bar_age_seconds, relative_volume,
                     spread_bps, bars_available, last_bar_ts_utc)
                VALUES
                    (:symbol, :cycle_ts_utc, :status, :asset_class, :data_source,
                     :session_active, :last_bar_age_seconds, :relative_volume,
                     :spread_bps, :bars_available, :last_bar_ts_utc)
                """,
                [
                    {
                        "asset_class": "equity",
                        "data_source": None,
                        "session_active": 1,
                        "last_bar_age_seconds": None,
                        "relative_volume": None,
                        "spread_bps": None,
                        "bars_available": 0,
                        "last_bar_ts_utc": None,
                        **row,
                    }
                    for row in rows
                ],
            )
            conn.commit()
        return len(rows)

    def get_active_symbols(self, cycle_ts: str | None = None) -> list[str]:
        """Return ACTIVE symbols from the most recent (or given) health cycle."""
        with self.connect() as conn:
            if cycle_ts is None:
                row = conn.execute(
                    "SELECT MAX(cycle_ts_utc) FROM vanguard_health"
                ).fetchone()
                if not row or not row[0]:
                    return []
                cycle_ts = row[0]
            rows = conn.execute(
                "SELECT symbol FROM vanguard_health "
                "WHERE cycle_ts_utc = ? AND status = 'ACTIVE' ORDER BY symbol",
                (cycle_ts,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_active_health_rows(self, cycle_ts: str | None = None) -> list[dict]:
        """Return ACTIVE health rows from the most recent (or given) health cycle."""
        with self.connect() as conn:
            if cycle_ts is None:
                row = conn.execute(
                    "SELECT MAX(cycle_ts_utc) FROM vanguard_health"
                ).fetchone()
                if not row or not row[0]:
                    return []
                cycle_ts = row[0]
            rows = conn.execute(
                """
                SELECT symbol, cycle_ts_utc, asset_class, data_source,
                       session_active, last_bar_age_seconds, last_bar_ts_utc,
                       relative_volume, spread_bps, bars_available, status
                FROM vanguard_health
                WHERE cycle_ts_utc = ? AND status = 'ACTIVE'
                ORDER BY asset_class, symbol
                """,
                (cycle_ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_health_cycle(self) -> str | None:
        """Most recent cycle_ts_utc in vanguard_health."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(cycle_ts_utc) FROM vanguard_health"
            ).fetchone()
        return row[0] if row else None

    def count_health_by_status(self, cycle_ts: str) -> dict[str, int]:
        """Returns {status: count} for a given cycle."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM vanguard_health "
                "WHERE cycle_ts_utc = ? GROUP BY status",
                (cycle_ts,),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------
    # vanguard_features (V3)
    # ------------------------------------------------------------------

    def upsert_features(self, rows: list[dict]) -> int:
        """
        Batch upsert feature results.
        Each row must have: symbol, cycle_ts_utc, features_json.
        """
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_features
                    (symbol, cycle_ts_utc, features_json)
                VALUES
                    (:symbol, :cycle_ts_utc, :features_json)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Generic bar reads (used by V3)
    # ------------------------------------------------------------------

    def get_bars_for_symbol(
        self,
        symbol: str,
        table: str = "vanguard_bars_5m",
        limit: int | None = 200,
        data_source: str | None = None,
        start_ts_utc: str | None = None,
        end_ts_utc: str | None = None,
    ) -> list[dict]:
        """
        Fetch bars for a symbol in chronological order.
        When `limit` is provided, returns the most recent `limit` bars.
        Table must be vanguard_bars_5m or vanguard_bars_1h.
        """
        with self.connect() as conn:
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            where = ["symbol = ?"]
            params: list[object] = [symbol]
            if data_source and "data_source" in columns:
                where.append("data_source = ?")
                params.append(data_source)
            if start_ts_utc:
                where.append("bar_ts_utc >= ?")
                params.append(start_ts_utc)
            if end_ts_utc:
                where.append("bar_ts_utc <= ?")
                params.append(end_ts_utc)
            query = f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY bar_ts_utc DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_symbols_with_bars(self, table: str = "vanguard_bars_5m") -> list[str]:
        """All distinct symbols in the given bar table."""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT symbol FROM {table} ORDER BY symbol"
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # vanguard_universe_members
    # ------------------------------------------------------------------

    def upsert_universe_members(self, rows: list[dict]) -> int:
        """
        Batch upsert universe membership records.
        Each row must have: symbol, universe, asset_class, data_source.
        Returns number of rows written.
        """
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_universe_members
                    (symbol, asset_class, data_source, universe, exchange,
                     tick_size, tick_value, point_value, margin,
                     session_start, session_end, session_tz,
                     is_active, added_at, last_seen_at)
                VALUES
                    (:symbol, :asset_class, :data_source, :universe, :exchange,
                     :tick_size, :tick_value, :point_value, :margin,
                     :session_start, :session_end, :session_tz,
                     :is_active, :added_at, :last_seen_at)
                """,
                [
                    {
                        "data_source": _legacy_universe_source(
                            str(row.get("asset_class") or ""),
                            str(row.get("universe") or ""),
                            row.get("data_source"),
                        ),
                        "exchange": None,
                        "tick_size": None,
                        "tick_value": None,
                        "point_value": None,
                        "margin": None,
                        "session_start": None,
                        "session_end": None,
                        "session_tz": "America/New_York",
                        "is_active": 1,
                        "added_at": (
                            row.get("added_at")
                            or row.get("last_seen_at")
                            or row.get("last_refreshed_utc")
                        ),
                        "last_seen_at": (
                            row.get("last_seen_at")
                            or row.get("added_at")
                            or row.get("last_refreshed_utc")
                        ),
                        **row,
                    }
                    for row in rows
                ],
            )
            conn.commit()
        return len(rows)

    def get_universe_members(
        self,
        universe: str | None = None,
    ) -> list[dict]:
        """
        Return all rows from vanguard_universe_members.
        If `universe` is given, filter to that universe only.
        """
        with self.connect() as conn:
            if universe:
                rows = conn.execute(
                    "SELECT * FROM vanguard_universe_members "
                    "WHERE universe = ? ORDER BY symbol, data_source",
                    (universe,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM vanguard_universe_members "
                    "ORDER BY universe, data_source, symbol"
                ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.setdefault("last_refreshed_utc", item.get("last_seen_at"))
            out.append(item)
        return out

    def count_universe_members(self) -> dict[str, int]:
        """Return {universe: count} for all universes in the DB."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT universe, COUNT(*) FROM vanguard_universe_members GROUP BY universe"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_active_symbols_map(self, data_source: str) -> dict[str, str]:
        """Return {symbol: asset_class} for active rows in a given source."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, asset_class
                FROM vanguard_universe_members
                WHERE data_source = ? AND is_active = 1
                ORDER BY symbol
                """,
                (data_source,),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def upsert_source_health(self, rows: list[dict]) -> int:
        """Batch upsert source freshness rows."""
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_source_health
                    (data_source, asset_class, last_bar_ts, last_poll_ts,
                     symbols_active, symbols_with_recent_bar, bars_last_5min,
                     status, last_updated)
                VALUES
                    (:data_source, :asset_class, :last_bar_ts, :last_poll_ts,
                     :symbols_active, :symbols_with_recent_bar, :bars_last_5min,
                     :status, :last_updated)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def get_source_health(self) -> list[dict]:
        """Return source freshness rows ordered by source and asset class."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vanguard_source_health ORDER BY data_source, asset_class"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # vanguard_training_data (V4A)
    # ------------------------------------------------------------------

    def upsert_training_data(self, rows: list[dict]) -> int:
        """
        Batch upsert training rows.
        Each row must have all columns of vanguard_training_data.
        Returns number of rows written.
        """
        if not rows:
            return 0
        payload = [{**row, "tbm_profile": row.get("tbm_profile")} for row in rows]
        for attempt in range(1, _DB_WRITE_RETRY_ATTEMPTS + 1):
            try:
                with self.connect() as conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO vanguard_training_data (
                            asof_ts_utc, symbol, asset_class, path, entry_price,
                            label_long, label_short,
                            forward_return, max_favorable_excursion, max_adverse_excursion,
                            exit_bar, exit_type_long, exit_type_short,
                            truncated, warm_up,
                            session_vwap_distance, premium_discount_zone, gap_pct,
                            session_opening_range_position, daily_drawdown_from_high,
                            momentum_3bar, momentum_12bar, momentum_acceleration,
                            atr_expansion, daily_adx,
                            relative_volume, volume_burst_z, down_volume_ratio,
                            effort_vs_result, spread_proxy,
                            rs_vs_benchmark_intraday, daily_rs_vs_benchmark,
                            benchmark_momentum_12bar, cross_asset_correlation, daily_conviction,
                            session_phase, time_in_session_pct, bars_since_session_open,
                            ob_proximity_5m, fvg_bullish_nearest, fvg_bearish_nearest,
                            structure_break, liquidity_sweep, smc_premium_discount,
                            htf_trend_direction, htf_structure_break, htf_fvg_nearest,
                            htf_ob_proximity,
                            btc_relative_momentum, btc_adjusted_return,
                            btc_correlation_z, volatility_regime, range_expansion_5m,
                            momentum_divergence, volume_delta,
                            liquidity_sweep_strength, fvg_size,
                            bars_available, nan_ratio,
                            horizon_bars, tp_pct, sl_pct, tbm_profile
                        ) VALUES (
                            :asof_ts_utc, :symbol, :asset_class, :path, :entry_price,
                            :label_long, :label_short,
                            :forward_return, :max_favorable_excursion, :max_adverse_excursion,
                            :exit_bar, :exit_type_long, :exit_type_short,
                            :truncated, :warm_up,
                            :session_vwap_distance, :premium_discount_zone, :gap_pct,
                            :session_opening_range_position, :daily_drawdown_from_high,
                            :momentum_3bar, :momentum_12bar, :momentum_acceleration,
                            :atr_expansion, :daily_adx,
                            :relative_volume, :volume_burst_z, :down_volume_ratio,
                            :effort_vs_result, :spread_proxy,
                            :rs_vs_benchmark_intraday, :daily_rs_vs_benchmark,
                            :benchmark_momentum_12bar, :cross_asset_correlation, :daily_conviction,
                            :session_phase, :time_in_session_pct, :bars_since_session_open,
                            :ob_proximity_5m, :fvg_bullish_nearest, :fvg_bearish_nearest,
                            :structure_break, :liquidity_sweep, :smc_premium_discount,
                            :htf_trend_direction, :htf_structure_break, :htf_fvg_nearest,
                            :htf_ob_proximity,
                            :btc_relative_momentum, :btc_adjusted_return,
                            :btc_correlation_z, :volatility_regime, :range_expansion_5m,
                            :momentum_divergence, :volume_delta,
                            :liquidity_sweep_strength, :fvg_size,
                            :bars_available, :nan_ratio,
                            :horizon_bars, :tp_pct, :sl_pct, :tbm_profile
                        )
                        """,
                        payload,
                    )
                    conn.commit()
                return len(rows)
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_write_lock(exc) or attempt == _DB_WRITE_RETRY_ATTEMPTS:
                    raise
                sleep_s = min(5.0, _DB_WRITE_RETRY_SLEEP_SEC * attempt)
                logger.warning(
                    "SQLite write contention in upsert_training_data (attempt %d/%d, rows=%d): %s; sleeping %.1fs",
                    attempt,
                    _DB_WRITE_RETRY_ATTEMPTS,
                    len(rows),
                    exc,
                    sleep_s,
                )
                time.sleep(sleep_s)
        return len(rows)

    def count_training_rows(
        self,
        symbol: str | None = None,
        horizon_bars: int | None = None,
    ) -> int:
        """Count rows in vanguard_training_data, optionally filtered."""
        with self.connect() as conn:
            if symbol and horizon_bars is not None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM vanguard_training_data "
                    "WHERE symbol = ? AND horizon_bars = ?",
                    (symbol, horizon_bars),
                ).fetchone()
            elif symbol:
                row = conn.execute(
                    "SELECT COUNT(*) FROM vanguard_training_data WHERE symbol = ?",
                    (symbol,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM vanguard_training_data"
                ).fetchone()
        return row[0] if row else 0

    def get_training_label_rates(self) -> dict:
        """Return positive label rates and row count from training data."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), SUM(label_long), SUM(label_short) "
                "FROM vanguard_training_data"
            ).fetchone()
        if not row or not row[0]:
            return {"total": 0, "label_long_rate": 0.0, "label_short_rate": 0.0}
        total = row[0]
        return {
            "total": total,
            "label_long_rate": round(row[1] / total, 4) if row[1] else 0.0,
            "label_short_rate": round(row[2] / total, 4) if row[2] else 0.0,
        }

    def get_training_symbols(self) -> list[str]:
        """All distinct symbols in vanguard_training_data."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM vanguard_training_data ORDER BY symbol"
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # vanguard_model_registry + vanguard_walkforward_results (V4B)
    # ------------------------------------------------------------------

    def upsert_model_registry(self, row: dict) -> None:
        """Insert or replace a model registry entry."""
        payload = {
            "asset_class": None,
            "direction": None,
            "feature_profile": None,
            "tbm_profile": None,
            "training_rows": row.get("train_rows"),
            "readiness": row.get("status", "not_trained"),
            "fallback_family": None,
            **row,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO vanguard_model_registry (
                    model_id, model_family, track, target_label,
                    feature_count, train_rows, test_rows, walk_forward_windows,
                    mean_ic_long, mean_ic_short, auc_long, auc_short,
                    long_wr_at_55, short_wr_at_55,
                    artifact_path, created_at_utc, status, notes,
                    asset_class, direction, feature_profile, tbm_profile,
                    training_rows, readiness, fallback_family
                ) VALUES (
                    :model_id, :model_family, :track, :target_label,
                    :feature_count, :train_rows, :test_rows, :walk_forward_windows,
                    :mean_ic_long, :mean_ic_short, :auc_long, :auc_short,
                    :long_wr_at_55, :short_wr_at_55,
                    :artifact_path, :created_at_utc, :status, :notes,
                    :asset_class, :direction, :feature_profile, :tbm_profile,
                    :training_rows, :readiness, :fallback_family
                )
                """,
                payload,
            )
            conn.commit()

    def upsert_walkforward_results(self, rows: list[dict]) -> int:
        """Batch upsert walk-forward window results."""
        if not rows:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO vanguard_walkforward_results (
                    model_id, window_id, train_start, train_end,
                    test_start, test_end, test_rows,
                    ic_long, ic_short, auc, accuracy, wr_at_55, asset_class
                ) VALUES (
                    :model_id, :window_id, :train_start, :train_end,
                    :test_start, :test_end, :test_rows,
                    :ic_long, :ic_short, :auc, :accuracy, :wr_at_55, :asset_class
                )
                """,
                [{**row, "asset_class": row.get("asset_class")} for row in rows],
            )
            conn.commit()
        return len(rows)
