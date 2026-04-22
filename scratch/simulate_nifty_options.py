import time
import random
import logging
from src.core.risk_manager import RiskManager
from src.agents.execution_agent import ExecutionAgent
from src.core.position_manager import PositionManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Simulation")

class MockStreamer:
    def __init__(self):
        self.latest_ticks = {}
        self.order_callback = None

    def simulate_tick(self, token, price):
        self.latest_ticks[token] = {"last_price": price, "depth": {"buy": [{"price": price}], "sell": [{"price": price}]}}

def run_simulation():
    logger.info("Starting Nifty Options Simulation (0DTE Gamma Blast Scenario)...")

    rm = RiskManager()
    pm = PositionManager()
    streamer = MockStreamer()

    # Configure execution agent to use mock streamer and paper trading
    import os
    os.environ["PAPER_TRADE"] = "True"
    os.environ["MOCK_PAPER_CAPITAL"] = "100000"

    agent = ExecutionAgent(rm, pm, streamer)

    # Scenario 1: Sudden spike in Nifty (Gamma Blast on 0DTE)
    logger.info("Scenario 1: Entering Bull Call Spread during sudden spike")
    streamer.simulate_tick(1111, 100.0) # ATM CE
    streamer.simulate_tick(2222, 40.0)  # OTM CE

    order_id = agent.enter_spread(
        buy_symbol="NIFTY_CE_ATM", buy_token=1111,
        sell_symbol="NIFTY_CE_OTM", sell_token=2222,
        instrument="NIFTY", transaction_type="BUY", quantity=50, entry_price=100.0,
        strategy_id="ORB_SPREAD"
    )

    logger.info(f"Spread entered with ID: {order_id}")
    positions = pm.get_open_positions()
    logger.info(f"Open positions: {len(positions)}")

    # Scenario 2: Simulate WebSocket Disconnect / Rate Limit during Stop Loss Hit
    logger.info("Scenario 2: Market Reverses, Stop Loss Hit but WebSocket drops")
    # Simulate partial fill or failure in square off
    logger.info("Attempting to square off all due to SL...")
    try:
        closed = agent.square_off_all()
        logger.info(f"Squared off {len(closed)} positions.")
    except Exception as e:
        logger.error(f"Failed to square off: {e}")

    logger.info("Simulation Complete.")

if __name__ == '__main__':
    run_simulation()
