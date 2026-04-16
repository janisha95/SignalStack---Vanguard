#!/bin/bash
# retrain_forex_short_horizon.sh
# Sprint 2 quick experiment: retrain forex model on 3-bar (15 min) horizon
# instead of 6-bar (30 min). Run this tonight on Mac or Vast.ai.
#
# Hypothesis: the model has signal at 15 min that decays by 30 min.
# If 15-min model IC > 0.06 and directional accuracy > 55%, ship it.

set -euo pipefail
cd ~/SS/Vanguard

echo "=== Step 1: Backup existing forex training data ==="
sqlite3 data/vanguard_universe.db "
  SELECT COUNT(*) as current_forex_rows FROM vanguard_training_data WHERE asset_class = 'forex';
"
# Keep a backup of the h6 training data
sqlite3 data/vanguard_universe.db "
  CREATE TABLE IF NOT EXISTS vanguard_training_data_h6_backup AS
  SELECT * FROM vanguard_training_data WHERE asset_class = 'forex' AND horizon_bars = 6;
"
echo "Backed up h6 forex rows to vanguard_training_data_h6_backup"

echo ""
echo "=== Step 2: Delete existing forex training rows and rebuild with h=3 ==="
echo "This recomputes forward_return for 3 bars (15 min) forward instead of 6 (30 min)."

# Delete only forex rows so we don't mix horizons
sqlite3 data/vanguard_universe.db "
  DELETE FROM vanguard_training_data WHERE asset_class = 'forex';
"

python3 stages/vanguard_training_backfill.py \
  --asset-class forex \
  --horizon-bars 3 \
  --workers 6 \
  --no-resume

echo ""
echo "=== Step 3: Count training rows ==="
sqlite3 data/vanguard_universe.db "
  SELECT horizon_bars, COUNT(*) as rows, COUNT(DISTINCT symbol) as symbols,
         MIN(asof_ts_utc) as earliest, MAX(asof_ts_utc) as latest
  FROM vanguard_training_data
  WHERE asset_class = 'forex'
  GROUP BY horizon_bars;
"

echo ""
echo "=== Step 4: Backup existing forex model artifacts ==="
mkdir -p models/vanguard/forex_h6_backup
cp models/vanguard/latest/rf_forex_regressor_v1.pkl models/vanguard/forex_h6_backup/ 2>/dev/null || echo "No rf model to backup"
cp models/vanguard/latest/lgbm_forex_regressor_v1.pkl models/vanguard/forex_h6_backup/ 2>/dev/null || echo "No lgbm model to backup"
cp models/vanguard/latest/vanguard_forex_model_meta.json models/vanguard/forex_h6_backup/ 2>/dev/null || echo "No meta to backup"
echo "Old models backed up to models/vanguard/forex_h6_backup/"

echo ""
echo "=== Step 5: Train new forex model on 3-bar data ==="
echo "The trainer reads ALL forex rows — now all h=3."
echo "Saves to models/vanguard/latest/ (overwrites old forex models)."

python3 stages/vanguard_model_trainer.py --asset-class forex

echo ""
echo "=== Step 6: Check results ==="
echo "Look at the output above for:"
echo "  - IC per fold (want > 0.05, current h6 model is 0.0437)"
echo "  - Directional accuracy (want > 55%)"
echo "  - Stable across folds (low IC std dev)"
echo ""
echo "If h3 IC beats h6 IC:"
echo "  Models are already saved to models/vanguard/latest/"
echo "  Just restart the orchestrator: kill and relaunch"
echo ""
echo "If h3 IC is WORSE than h6:"
echo "  Rollback: cp models/vanguard/forex_h6_backup/*.pkl models/vanguard/latest/"
echo "  Rollback: cp models/vanguard/forex_h6_backup/*.json models/vanguard/latest/"
echo "  Restore training data:"
echo "    sqlite3 data/vanguard_universe.db \"DELETE FROM vanguard_training_data WHERE asset_class='forex';\""
echo "    sqlite3 data/vanguard_universe.db \"INSERT INTO vanguard_training_data SELECT * FROM vanguard_training_data_h6_backup;\""
echo ""
echo "=== Also try h2 (10 min) if h3 doesn't beat h6 ==="
echo "Same steps but with --horizon-bars 2"
