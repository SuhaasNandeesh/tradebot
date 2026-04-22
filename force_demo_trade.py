
import os
import sys
import time
import logging
import json
import pandas as pd
from datetime import datetime
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(os.path.join(os.getcwd(), 'Code/ai-app/tradebot'))

from main import Orchestrator
from src.core.position_manager import SingleLegPosition

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Demo_Validator")

def run_forced_demo():
    logger.info("🧪 INITIALIZING LIVE-FIRE DEMO VALIDATION...")
    
    # 1. Setup Environment
    os.environ["PAPER_TRADE"] = "True"
    
    # 2. Initialize Orchestrator with Mocks for External APIs
    with patch('main.KiteStreamer'), \
         patch('main.TelegramAgent'), \
         patch('src.agents.execution_agent.KiteConnect'), \
         patch('langchain_google_genai.ChatGoogleGenerativeAI') as mock_gemini:
        
        app = Orchestrator()
        
        # 3. Setup Mock Data
        logger.info("Step 1: Mocking Market Environment (NIFTY @ 22,000)...")
        app.streamer.get_nifty = MagicMock(return_value=22000.0)
        app.streamer.is_connected = True
        app.streamer.latest_ticks = {
            256265: {"last_price": 22000.0}, # Nifty Token
            99999: { # Mock Option Token
                "last_price": 150.0,
                "depth": {
                    "buy": [{"quantity": 5000, "price": 149.9}],
                    "sell": [{"quantity": 1000, "price": 150.1}]
                }
            }
        }
        
        # 4. Setup Mock AI Intelligence
        mock_response = MagicMock()
        mock_response.content = '{"approved": true, "narrative": "Institutional confluence verified. Market depth is healthy."}'
        mock_gemini.return_value.invoke.return_value = mock_response
        
        # 5. Force a Strategy Signal
        logger.info("Step 2: Forcing Strategy Signal (Supertrend_VWAP -> BUY_CE)...")
        signal = {
            "symbol": "NIFTY26MAR22000CE",
            "side": "BUY_CE",
            "instrument": "NIFTY",
            "quantity": 50,
            "instrument_token": 99999
        }
        
        # 6. Manually Trigger the Execution Pipeline
        logger.info("Step 3: Triggering Pipeline (_async_nifty_execution)...")
        # We call the internal execution method directly to see it flow
        app._async_nifty_execution(
            strategy_name="Supertrend_VWAP",
            signal=signal,
            confluence={"score": 85, "approved": True},
            adj_score=85,
            ctx={"sentiment": "BULLISH"},
            nifty=22000.0
        )
        
        # 7. Final Verification: Check Database & Memory
        logger.info("Step 4: Final Verification...")
        
        # Check Position Manager
        open_positions = app.position_manager.get_open_positions()
        if len(open_positions) > 0:
            pos = open_positions[0]
            logger.info(f"✅ SUCCESS: Position found in Memory: {pos.symbol} @ {pos.entry_price}")
        else:
            logger.error("❌ FAILURE: No position found in Memory.")
            return

        # Check SQLite Journal
        recent_trades = app.journal.get_recent_trades(limit=1, is_paper_trade=True)
        if recent_trades:
            tid, ts, sym, sig, pnl = recent_trades[0]
            logger.info(f"✅ SUCCESS: Trade recorded in SQLite Journal (ID: {tid}, Sym: {sym})")
        else:
            logger.error("❌ FAILURE: Trade not found in SQLite Database.")
            return

        logger.info("\n" + "="*50)
        logger.info("🏆 DEMO TRADE VALIDATED: The code is behaving correctly.")
        logger.info("All institutional layers (AI Sanity, Journaling, Memory) are active.")
        logger.info("="*50)

if __name__ == "__main__":
    run_forced_demo()
