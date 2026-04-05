#!/bin/bash
# Create Vanguard repo structure
# Usage: bash create_vanguard_structure.sh
# Run from anywhere — creates structure at ~/SS/Vanguard/

BASE="$HOME/SS/Vanguard"

echo "Creating Vanguard repo structure at $BASE..."

# Stage entry points
mkdir -p "$BASE/stages"

# Vanguard package
mkdir -p "$BASE/vanguard/strategies"
mkdir -p "$BASE/vanguard/factors"
mkdir -p "$BASE/vanguard/helpers"
mkdir -p "$BASE/vanguard/execution"
mkdir -p "$BASE/vanguard/data_adapters"
mkdir -p "$BASE/vanguard/models"

# Scripts
mkdir -p "$BASE/scripts"

# Config
mkdir -p "$BASE/config"

# Data
mkdir -p "$BASE/data/reports"
mkdir -p "$BASE/data/runtime"

# Models
mkdir -p "$BASE/models/vanguard/latest"
mkdir -p "$BASE/models/vanguard/archive"

# Tests
mkdir -p "$BASE/tests"

# Docs
mkdir -p "$BASE/docs"

# Create __init__.py files
touch "$BASE/vanguard/__init__.py"
touch "$BASE/vanguard/strategies/__init__.py"
touch "$BASE/vanguard/factors/__init__.py"
touch "$BASE/vanguard/helpers/__init__.py"
touch "$BASE/vanguard/execution/__init__.py"
touch "$BASE/vanguard/data_adapters/__init__.py"
touch "$BASE/vanguard/models/__init__.py"

# Create placeholder stage files
for stage in cache prefilter factor_engine training_backfill model_trainer selection risk_filters orchestrator; do
    touch "$BASE/stages/vanguard_${stage}.py"
done

# Create placeholder strategy files
for strat in base smc_confluence momentum mean_reversion risk_reward_edge relative_strength breakdown session_timing htf_alignment liquidity_grab_reversal session_open cross_asset momentum_breakout llm_strategy; do
    touch "$BASE/vanguard/strategies/${strat}.py"
done

# Create placeholder factor files
for factor in price_location momentum volume market_context daily_bridge smc_5m smc_htf_1h session_time quality; do
    touch "$BASE/vanguard/factors/${factor}.py"
done

# Create placeholder helper files
for helper in clock market_sessions db bars quality normalize regime_detector strategy_router consensus_counter position_sizer portfolio_state correlation_checker eod_flatten ttp_rules; do
    touch "$BASE/vanguard/helpers/${helper}.py"
done

# Create placeholder execution files
for exec_file in bridge signalstack_adapter mt5_adapter telegram_alerts; do
    touch "$BASE/vanguard/execution/${exec_file}.py"
done

# Create placeholder data adapter files
for adapter in alpaca_adapter ibkr_adapter mt5_data_adapter; do
    touch "$BASE/vanguard/data_adapters/${adapter}.py"
done

# Create feature contract
touch "$BASE/vanguard/models/feature_contract.py"

# Create script files
touch "$BASE/scripts/execute_daily_picks.py"
touch "$BASE/scripts/eod_flatten.py"

# Create test files
for test in signalstack_adapter mt5_adapter vanguard_selection vanguard_risk vanguard_orchestrator telegram_alerts; do
    touch "$BASE/tests/test_${test}.py"
done

# Move existing spec files to docs/ if they're in root
for spec in "$BASE"/VANGUARD_STAGE_*.md "$BASE"/PROJECT_VANGUARD_HANDOFF.md "$BASE"/VANGUARD_SUPPORTING_SPECS.md "$BASE"/VANGUARD_BUILD_PLAN.md "$BASE"/ftmo_universe*.json; do
    if [ -f "$spec" ]; then
        mv "$spec" "$BASE/docs/" 2>/dev/null
        echo "  Moved $(basename $spec) → docs/"
    fi
done

# Move AGENTS.md and ROADMAP.md to root (they stay at root)
# These should already be there

echo ""
echo "✅ Vanguard repo structure created!"
echo ""
echo "Directory structure:"
find "$BASE" -type d | sort | sed "s|$BASE|~/SS/Vanguard|"
echo ""
echo "Files:"
find "$BASE" -type f | wc -l
echo "total files created"
