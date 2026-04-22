
import logging
from datetime import datetime
from src.core.position_manager import PositionManager, SingleLegPosition
from src.memory.state_db import StateDB

logging.basicConfig(level=logging.INFO)

def debug():
    pm = PositionManager()
    print("Creating position object...")
    pos = SingleLegPosition(
        symbol="NIFTY2640722750CE",
        instrument="NIFTY",
        transaction_type="BUY",
        quantity=50,
        entry_price=100.0,
        stop_loss=75.0,
        target=140.0,
        order_id="TEST_ORDER_UUID",
        instrument_token=12345,
        strategy_id="DEBUG_TEST"
    )
    
    print("Calling pm.add_position...")
    try:
        pm.add_position(pos)
        print("SUCCESS: Position added")
    except Exception as e:
        import traceback
        print(f"FAILED: {e}")
        print(traceback.format_exc())

if __name__ == "__main__":
    debug()
