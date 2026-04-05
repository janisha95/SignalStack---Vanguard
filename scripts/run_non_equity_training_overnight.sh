#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/sjani008/SS/Vanguard"
DB="$ROOT/data/vanguard_universe.db"
LOG_DIR="$ROOT/logs"
STAMP="$(date +%Y%m%d_%H%M%S)"
WORKERS="${V4A_WORKERS:-4}"

mkdir -p "$LOG_DIR"
cd "$ROOT"

echo "== Non-equity overnight run =="
echo "root: $ROOT"
echo "db:   $DB"
echo "logs: $LOG_DIR"
echo "ts:   $STAMP"
echo

echo "== Before counts =="
sqlite3 "$DB" "
SELECT asset_class, COUNT(*) AS bars_5m, COUNT(DISTINCT symbol) AS symbols
FROM vanguard_bars_5m
WHERE asset_class IN ('forex','crypto','metal','commodity')
GROUP BY asset_class
ORDER BY asset_class;
"
echo

echo "== Step 1: aggregate 1m -> 5m/1h for non-equity =="
python3 -u scripts/aggregate_non_equity_bars.py \
  2>&1 | tee "$LOG_DIR/aggregate_non_equity_${STAMP}.log"
echo

for asset in forex crypto metal; do
  echo "== Step 2: V4A backfill for ${asset} =="
  python3 -u stages/vanguard_training_backfill_fast.py \
    --asset-class "$asset" \
    --workers "$WORKERS" \
    2>&1 | tee "$LOG_DIR/v4a_${asset}_${STAMP}.log"
  echo
done

for asset in forex crypto metal; do
  echo "== Step 3: V4B train for ${asset} =="
  python3 -u stages/vanguard_model_trainer.py \
    --asset-class "$asset" \
    2>&1 | tee "$LOG_DIR/v4b_${asset}_${STAMP}.log"
  echo
done

echo "== After counts =="
sqlite3 "$DB" "
SELECT asset_class, COUNT(*) AS bars_5m, COUNT(DISTINCT symbol) AS symbols
FROM vanguard_bars_5m
WHERE asset_class IN ('forex','crypto','metal','commodity')
GROUP BY asset_class
ORDER BY asset_class;
"
echo

echo "== Training rows by asset class =="
sqlite3 "$DB" "
SELECT asset_class,
       COUNT(*) AS rows,
       COUNT(DISTINCT symbol) AS symbols,
       ROUND(AVG(label_long), 6) AS long_wr,
       ROUND(AVG(label_short), 6) AS short_wr
FROM vanguard_training_data
GROUP BY asset_class
ORDER BY asset_class;
"
echo

echo "== Model registry snapshot =="
sqlite3 "$DB" "
SELECT model_id, asset_class, direction, readiness, training_rows,
       mean_ic_long, mean_ic_short
FROM vanguard_model_registry
WHERE asset_class IN ('forex','crypto','metal','commodity')
ORDER BY asset_class, direction, model_id;
"
echo

echo "Completed. Logs under $LOG_DIR"
