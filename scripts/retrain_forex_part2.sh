#!/bin/bash
# retrain_forex_part2.sh
# Run this AFTER the backfill command has finished:
#   python3 stages/vanguard_training_backfill.py --asset-class forex --horizon-bars 3 --full-rebuild --workers 6
#
# This script: verifies backfill, backs up old models, trains new models, checks IC.

set -euo pipefail
cd ~/SS/Vanguard

echo "=========================================="
echo "  STEP 1: Verify backfill completed"
echo "=========================================="
sqlite3 data/vanguard_universe.db "
  SELECT
    horizon_bars,
    COUNT(*) as rows,
    COUNT(DISTINCT symbol) as symbols,
    MIN(asof_ts_utc) as earliest,
    MAX(asof_ts_utc) as latest
  FROM vanguard_training_data
  WHERE asset_class = 'forex'
  GROUP BY horizon_bars;
"
echo ""
echo "Expected: horizon_bars=3 with 100K+ rows. If 0 rows, backfill failed — stop here."
echo ""
read -p "Does the output look correct? (y/n) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Stopping. Fix backfill first."
    exit 1
fi

echo ""
echo "=========================================="
echo "  STEP 2: Backup old forex model artifacts"
echo "=========================================="
mkdir -p models/vanguard/forex_h6_backup
cp models/vanguard/latest/rf_forex_regressor_v1.pkl models/vanguard/forex_h6_backup/ 2>/dev/null && echo "  Backed up rf_forex_regressor_v1.pkl" || echo "  No rf model found (ok if first train)"
cp models/vanguard/latest/lgbm_forex_regressor_v1.pkl models/vanguard/forex_h6_backup/ 2>/dev/null && echo "  Backed up lgbm_forex_regressor_v1.pkl" || echo "  No lgbm model found"
cp models/vanguard/latest/vanguard_forex_model_meta.json models/vanguard/forex_h6_backup/ 2>/dev/null && echo "  Backed up meta json" || echo "  No meta found"
echo "Old models saved to models/vanguard/forex_h6_backup/"

echo ""
echo "=========================================="
echo "  STEP 3: Train new forex model (h=3, 15min)"
echo "=========================================="
echo "This will take a few minutes. Watch IC per fold."
echo ""
python3 stages/vanguard_model_trainer.py --asset-class forex

echo ""
echo "=========================================="
echo "  STEP 4: Results check"
echo "=========================================="
echo ""
echo "Look at the output above. Key numbers:"
echo "  - IC per fold: want > 0.05 (old model was 0.0437)"
echo "  - Directional accuracy: want > 55%"
echo "  - IC std dev: want < 0.03 (stable across folds)"
echo ""
echo "New model artifacts are already in models/vanguard/latest/"
echo ""
read -p "Is the new IC BETTER than 0.0437? (y/n) " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "=========================================="
    echo "  STEP 5a: Ship it — restart orchestrator"
    echo "=========================================="
    echo "Killing old orchestrator..."
    pkill -f "vanguard_orchestrator.py" 2>/dev/null && echo "  Killed" || echo "  Not running"
    sleep 2
    echo "Starting orchestrator with new model..."
    nohup python3 stages/vanguard_orchestrator.py --loop --interval 120 --execution-mode manual > /tmp/vanguard_orch.log 2>&1 &
    echo "  Started, PID=$!"
    echo "  Log: tail -f /tmp/vanguard_orch.log"
    echo ""
    echo "Let it run 20 cycles (~40 min), then re-run signal analysis:"
    echo "  Give CC: 'Re-run signal horizon analysis on cycles from the last 60 min only. Save to /tmp/signal_horizon_h3.md'"
    echo ""
    echo "DONE. New h3 forex model is live."
else
    echo ""
    echo "=========================================="
    echo "  STEP 5b: Rollback to old model"
    echo "=========================================="
    cp models/vanguard/forex_h6_backup/rf_forex_regressor_v1.pkl models/vanguard/latest/ 2>/dev/null && echo "  Restored rf model"
    cp models/vanguard/forex_h6_backup/lgbm_forex_regressor_v1.pkl models/vanguard/latest/ 2>/dev/null && echo "  Restored lgbm model"
    cp models/vanguard/forex_h6_backup/vanguard_forex_model_meta.json models/vanguard/latest/ 2>/dev/null && echo "  Restored meta"
    echo ""
    echo "Old model restored. Restoring h6 training data..."
    sqlite3 data/vanguard_universe.db "DELETE FROM vanguard_training_data WHERE asset_class = 'forex';"
    sqlite3 data/vanguard_universe.db "INSERT INTO vanguard_training_data SELECT * FROM vanguard_training_data_h6_backup;"
    echo "  Training data restored from backup."
    echo ""
    echo "Try h=2 (10 min) next:"
    echo "  sqlite3 data/vanguard_universe.db \"DELETE FROM vanguard_training_data WHERE asset_class = 'forex';\""
    echo "  python3 stages/vanguard_training_backfill.py --asset-class forex --horizon-bars 2 --full-rebuild --workers 6"
    echo "  bash scripts/retrain_forex_part2.sh"
    echo ""
    echo "ROLLED BACK. Old h6 model is active."
fi
