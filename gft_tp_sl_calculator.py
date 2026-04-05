#!/usr/bin/env python3
import sqlite3

db = sqlite3.connect("/Users/sjani008/SS/Vanguard/data/vanguard_universe.db")
db.row_factory = sqlite3.Row

print("🔥 GFT TP/SL + LOT SIZE CALCULATOR (Latest Cycle - 10K Account Focus)\n")
print(f"{'Symbol':<8} {'Dir':<5} {'Edge':<6} {'Entry':<10} {'SL':<10} {'TP':<10} {'Risk $':<8} {'Lot 10K'}")
print("-" * 75)

rows = db.execute("""
    SELECT DISTINCT symbol, direction, edge_score, entry_price, stop_price, tp_price
    FROM vanguard_tradeable_portfolio
    WHERE account_id LIKE 'gft_%'
      AND status = 'APPROVED'
    ORDER BY edge_score DESC
    LIMIT 15
""").fetchall()

for r in rows:
    entry = r["entry_price"]
    sl    = r["stop_price"]
    tp    = r["tp_price"]
    
    if not entry or not sl or not tp:
        continue
        
    risk_price = abs(entry - sl)
    risk_dollars = 50.0                     # 0.5% risk on 10K account
    lot_10k = round(risk_dollars / (risk_price * 100000), 2) if risk_price > 0 else 0.02
    
    # Safe caps
    lot_10k = min(max(lot_10k, 0.02), 0.10)
    
    print(f"{r['symbol']:<8} {r['direction']:<5} {r['edge_score']:.2f}   "
          f"{entry:.5f}   {sl:.5f}   {tp:.5f}   {risk_dollars:<8} {lot_10k}")
