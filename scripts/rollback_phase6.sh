#!/bin/bash
# rollback_phase6.sh — Restore prod DB from pre-Phase 6 backup.
# Usage: ./rollback_phase6.sh <backup_path>
#
# SAFE: only restores the DB file. Does NOT touch Python code or config.
# After running this script, restart the prod orchestrator manually.
set -euo pipefail

BACKUP="${1:-}"
PROD_DB="/Users/sjani008/SS/Vanguard/data/vanguard_universe.db"

if [ -z "$BACKUP" ]; then
    echo "ERROR: must pass backup path as first argument"
    echo "Usage: $0 /tmp/prod_pre_phase6_backup_YYYYMMDD_HHMMSS.db"
    exit 1
fi

if [ ! -f "$BACKUP" ]; then
    echo "ERROR: backup file not found: $BACKUP"
    exit 1
fi

# Verify backup is readable before overwriting
ROWS=$(sqlite3 "$BACKUP" "SELECT COUNT(*) FROM vanguard_tradeable_portfolio;" 2>&1)
if ! echo "$ROWS" | grep -qE '^[0-9]+$'; then
    echo "ERROR: backup not readable or corrupt (got: $ROWS)"
    exit 1
fi
echo "Backup verified: $ROWS rows in vanguard_tradeable_portfolio"

# Stop orchestrator + lifecycle daemon
echo "Stopping prod orchestrator and lifecycle daemon..."
pkill -f "vanguard_orchestrator.py" || true
pkill -f "lifecycle_daemon" || true
sleep 2

# Restore
echo "Restoring $BACKUP -> $PROD_DB"
cp "$BACKUP" "$PROD_DB"
echo "Rollback complete. Restart prod orchestrator manually."
echo ""
echo "  Verify: sqlite3 $PROD_DB 'SELECT COUNT(*) FROM vanguard_tradeable_portfolio;'"
