"""
field_registry.py — Unified field metadata for the SignalStack Unified API.

Defines every available column across all sources (meridian, s1, vanguard).
The React frontend fetches this at startup and builds filter/sort/column pickers
dynamically.

Location: ~/SS/Vanguard/vanguard/api/field_registry.py
"""
from __future__ import annotations

FIELD_REGISTRY: list[dict] = [
    # === COMMON (all sources) ===
    {
        "key": "symbol", "label": "Symbol", "type": "string",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "ticker",
    },
    {
        "key": "source", "label": "Source", "type": "enum",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "badge",
        "options": ["meridian", "s1", "vanguard"],
    },
    {
        "key": "side", "label": "Direction", "type": "enum",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "direction",
        "options": ["LONG", "SHORT"],
    },
    {
        "key": "price", "label": "Price", "type": "number",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": True, "groupable": False, "format": "currency",
    },
    {
        "key": "price_unavailable_reason", "label": "Price Unavailable", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "text",
        "description": "Explains why price is null for a Vanguard candidate when no latest bar is available",
    },
    {
        "key": "as_of", "label": "As Of", "type": "datetime",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "datetime",
    },
    {
        "key": "as_of_display", "label": "As Of (ET)", "type": "datetime",
        "sources": ["vanguard"],
        "filterable": False, "sortable": True, "groupable": False, "format": "datetime",
    },
    {
        "key": "tier", "label": "Tier", "type": "string",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "badge",
    },
    {
        "key": "sector", "label": "Sector", "type": "string",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "text",
    },
    {
        "key": "regime", "label": "Regime", "type": "string",
        "sources": ["meridian", "s1", "vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "badge",
    },
    {
        "key": "lane_status", "label": "Lane Status", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "badge",
    },
    {
        "key": "readiness", "label": "Readiness", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "badge",
    },
    {
        "key": "partial_data", "label": "Partial Data", "type": "boolean",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "boolean",
    },

    # === MERIDIAN-NATIVE ===
    {
        "key": "tcn_long_score", "label": "TCN Long", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False,
        "format": "score_0_1", "description": "Meridian long-side TCN score",
    },
    {
        "key": "tcn_short_score", "label": "TCN Short", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False,
        "format": "score_0_1", "description": "Meridian short-side TCN score",
    },
    {
        "key": "tcn_score", "label": "TCN Score", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False,
        "format": "score_0_1", "description": "Directional TCN score used by the shortlist row",
    },
    {
        "key": "factor_rank", "label": "Factor Rank", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
        "description": "Deprecated after the TCN-only shortlist rebuild; retained for fallback compatibility",
    },
    {
        "key": "final_score", "label": "Final Score", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
    },
    {
        "key": "residual_alpha", "label": "Residual Alpha", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False, "format": "percent",
        "description": "Deprecated after the TCN-only shortlist rebuild; retained as a zeroed legacy field",
    },
    {
        "key": "predicted_return", "label": "Predicted Return", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False, "format": "percent",
    },
    {
        "key": "beta", "label": "Beta", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False, "format": "decimal_2",
        "description": "Deprecated after the TCN-only shortlist rebuild; retained as a zeroed legacy field",
    },
    {
        "key": "m_lgbm_long_prob", "label": "LGBM Long (M)", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False,
        "format": "score_0_1", "description": "Deprecated Meridian LGBM long classifier field",
    },
    {
        "key": "m_lgbm_short_prob", "label": "LGBM Short (M)", "type": "number",
        "sources": ["meridian"],
        "filterable": True, "sortable": True, "groupable": False,
        "format": "score_0_1", "description": "Deprecated Meridian LGBM short classifier field",
    },

    # === S1-NATIVE ===
    {
        "key": "p_tp", "label": "RF Prob", "type": "number",
        "sources": ["s1"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
    },
    {
        "key": "nn_p_tp", "label": "NN Prob", "type": "number",
        "sources": ["s1"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
    },
    {
        "key": "scorer_prob", "label": "Scorer Prob", "type": "number",
        "sources": ["s1"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
    },
    {
        "key": "convergence_score", "label": "Convergence", "type": "number",
        "sources": ["s1"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
    },
    {
        "key": "n_strategies_agree", "label": "Strategy Count", "type": "number",
        "sources": ["s1"],
        "filterable": True, "sortable": True, "groupable": False, "format": "integer",
    },
    {
        "key": "strategy", "label": "Strategy", "type": "string",
        "sources": ["s1", "vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "text",
    },
    {
        "key": "volume_ratio", "label": "Volume Ratio", "type": "number",
        "sources": ["s1"],
        "filterable": True, "sortable": True, "groupable": False, "format": "decimal_1",
    },

    # === VANGUARD-NATIVE ===
    {
        "key": "strategy_rank", "label": "Strategy Rank", "type": "number",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": False, "format": "integer",
    },
    {
        "key": "strategy_score", "label": "Strategy Score", "type": "number",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
    },
    {
        "key": "ml_prob", "label": "ML Prob", "type": "number",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
    },
    {
        "key": "edge_score", "label": "Edge Score", "type": "number",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": False, "format": "score_0_1",
        "description": "One-sided percentile score: LONG higher is stronger, SHORT lower is stronger",
    },
    {
        "key": "consensus_count", "label": "Consensus Count", "type": "number",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": False, "format": "integer",
    },
    {
        "key": "strategies_matched", "label": "Strategies Matched", "type": "list",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": False, "format": "tags",
    },
    {
        "key": "asset_class", "label": "Asset Class", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "badge",
    },
    {
        "key": "model_family", "label": "Model Family", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "text",
    },
    {
        "key": "model_source", "label": "Model Source", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "badge",
    },
    {
        "key": "model_readiness", "label": "Model Readiness", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "badge",
    },
    {
        "key": "feature_profile", "label": "Feature Profile", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "text",
    },
    {
        "key": "tbm_profile", "label": "TBM Profile", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "text",
    },
    {
        "key": "price_source", "label": "Price Source", "type": "string",
        "sources": ["vanguard"],
        "filterable": True, "sortable": False, "groupable": True, "format": "badge",
    },
    {
        "key": "price_bar_ts_utc", "label": "Price Bar Time", "type": "datetime",
        "sources": ["vanguard"],
        "filterable": True, "sortable": True, "groupable": True, "format": "datetime",
    },
]

# Lookup by key for fast access
FIELD_REGISTRY_BY_KEY: dict[str, dict] = {f["key"]: f for f in FIELD_REGISTRY}
