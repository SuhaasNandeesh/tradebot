import sqlite3
import threading
import logging
import json
from datetime import datetime

logger = logging.getLogger(__name__)

class StateDB:
    """
    Thread-safe SQLite database to store open positions across crashes and restarts.
    Institutional bots require absolute state persistence to prevent orphaned trades.
    """
    def __init__(self, db_path="trade_state.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS open_positions (
                        order_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        instrument TEXT NOT NULL,
                        transaction_type TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        entry_price REAL NOT NULL,
                        stop_loss REAL NOT NULL,
                        original_sl REAL NOT NULL,
                        target REAL NOT NULL,
                        strategy_id TEXT,
                        entry_time TIMESTAMP,
                        breakeven_set BOOLEAN DEFAULT 0,
                        trail_locked BOOLEAN DEFAULT 0,
                        sl_order_id TEXT,
                        type TEXT DEFAULT 'SINGLE',
                        data_json TEXT
                    )
                """)
                
                # Dynamic migration
                columns = [c[1] for c in cursor.execute("PRAGMA table_info(open_positions)").fetchall()]
                if "type" not in columns:
                    cursor.execute("ALTER TABLE open_positions ADD COLUMN type TEXT DEFAULT 'SINGLE'")
                if "data_json" not in columns:
                    cursor.execute("ALTER TABLE open_positions ADD COLUMN data_json TEXT")
                if "sl_order_id" not in columns:
                    cursor.execute("ALTER TABLE open_positions ADD COLUMN sl_order_id TEXT")
                    
                conn.commit()
            except Exception as e:
                logger.critical(f"Failed to initialize StateDB: {e}")
            finally:
                conn.close()

    def upsert_position(self, pos_dict: dict):
        """Insert or update a live position in SQLite."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cursor = conn.cursor()
                
                # Safe extraction for both Single and Spread types
                pos_type = pos_dict.get('type', 'SINGLE')
                is_spread = pos_type == 'SPREAD'
                
                if is_spread:
                    symbol = f"{pos_dict['buy_leg']['symbol']}/{pos_dict['sell_leg']['symbol']}"
                    tx_type = "SPREAD"
                    qty = pos_dict['buy_leg']['quantity']
                    entry_px = pos_dict['buy_leg']['entry_price'] - pos_dict['sell_leg']['entry_price']
                else:
                    symbol = pos_dict.get('symbol', 'N/A')
                    tx_type = pos_dict.get('transaction_type', 'N/A')
                    qty = pos_dict.get('quantity', 0)
                    entry_px = pos_dict.get('entry_price', 0.0)

                cursor.execute("""
                    INSERT INTO open_positions (
                        order_id, symbol, instrument, transaction_type, quantity,
                        entry_price, stop_loss, original_sl, target, strategy_id,
                        entry_time, breakeven_set, trail_locked, sl_order_id,
                        type, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        stop_loss=excluded.stop_loss,
                        breakeven_set=excluded.breakeven_set,
                        trail_locked=excluded.trail_locked,
                        sl_order_id=excluded.sl_order_id,
                        data_json=excluded.data_json
                """, (
                    pos_dict['order_id'], symbol, pos_dict['instrument'],
                    tx_type, qty, entry_px,
                    pos_dict['stop_loss'], pos_dict.get('original_sl', pos_dict['stop_loss']), 
                    pos_dict['target'], pos_dict.get('strategy_id'), pos_dict['entry_time'],
                    1 if pos_dict.get('breakeven_set') else 0,
                    1 if pos_dict.get('trail_locked') else 0,
                    pos_dict.get('sl_order_id'),
                    pos_type, json.dumps(pos_dict)
                ))
                conn.commit()
            except Exception as e:
                logger.error(f"DB Upsert failed for {pos_dict.get('order_id')}: {e}")
            finally:
                conn.close()

    def delete_position(self, order_id: str):
        """Removes a position from state (after successful closure)."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM open_positions WHERE order_id = ?", (order_id,))
                conn.commit()
            except Exception as e:
                logger.error(f"DB Delete failed for {order_id}: {e}")
            finally:
                conn.close()

    def load_all_positions(self) -> list:
        """Loads all open positions from state on boot."""
        with self._lock:
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM open_positions")
                
                # Get column names to handle dynamic schemas safely
                cols = [c[0] for c in cursor.description]
                rows = cursor.fetchall()
                
                positions = []
                for r in rows:
                    row_dict = dict(zip(cols, r))
                    # Use data_json if available for high-fidelity reconstruction
                    if row_dict.get("data_json"):
                        try:
                            positions.append(json.loads(row_dict["data_json"]))
                            continue
                        except:
                            pass
                    
                    # Fallback to legacy flat columns
                    positions.append({
                        "order_id": row_dict["order_id"], "symbol": row_dict["symbol"], 
                        "instrument": row_dict["instrument"], "transaction_type": row_dict["transaction_type"], 
                        "quantity": row_dict["quantity"], "entry_price": row_dict["entry_price"],
                        "stop_loss": row_dict["stop_loss"], "original_sl": row_dict["original_sl"], 
                        "target": row_dict["target"], "strategy_id": row_dict["strategy_id"], 
                        "entry_time": row_dict["entry_time"], "breakeven_set": bool(row_dict["breakeven_set"]), 
                        "trail_locked": bool(row_dict["trail_locked"]), 
                        "sl_order_id": row_dict.get("sl_order_id"),
                        "type": row_dict.get("type", "SINGLE")
                    })
                return positions
            except Exception as e:
                logger.error(f"DB Load failed: {e}")
                return []
            finally:
                conn.close()
