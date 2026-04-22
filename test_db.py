
import sqlite3
import json
from datetime import datetime
from src.memory.journal import TradeJournal

def test_journal():
    j = TradeJournal()
    print("Testing log_trade_start...")
    tid = j.log_trade_start(
        symbol="TEST_NIFTY",
        signal="BUY_CE",
        context_str=json.dumps({"test": True}),
        strategy_code="DRY_RUN",
        decision_price=22000.0
    )
    print(f"Inserted trade ID: {tid}")
    
    with sqlite3.connect(j.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE id = ?", (tid,))
        row = cursor.fetchone()
        print(f"Row found: {row}")

if __name__ == "__main__":
    test_journal()
