
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import json
import logging
import pandas as pd

# Add project root to path
sys.path.append(os.path.join(os.getcwd(), 'Code/ai-app/tradebot'))

from main import Orchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Institutional_Sim")

class TestInstitutionalFeatures(unittest.TestCase):
    @patch('main.KiteStreamer')
    @patch('main.TelegramAgent')
    @patch('main.load_dotenv')
    @patch('src.agents.execution_agent.KiteConnect')
    def setUp(self, mock_kite, mock_dotenv, mock_telegram, mock_streamer):
        os.environ["GOOGLE_API_KEY"] = "fake_key"
        os.environ["KITE_API_KEY"] = "fake_key"
        os.environ["PAPER_TRADE"] = "True"
        
        self.orchestrator = Orchestrator()
        
        # Mocking basic market data
        self.orchestrator.streamer.get_nifty = MagicMock(return_value=22000.0)
        self.orchestrator.streamer.get_vix = MagicMock(return_value=15.0)
        self.orchestrator.streamer.is_connected = True
        
        # Standard Mock MTF Data
        dummy_df = pd.DataFrame({'close': [22000]*100, 'open': [22000]*100, 'high': [22000]*100, 'low': [22000]*100, 'volume': [100000]*100})
        self.orchestrator.data_provider.get_multi_timeframe = MagicMock(return_value={"5m": dummy_df, "15m": dummy_df, "1h": dummy_df})

    @patch('langchain_google_genai.ChatGoogleGenerativeAI')
    def test_scenario_a_order_book_block(self, mock_gemini):
        logger.info("\n--- SCENARIO A: Bullish Signal + Sell Wall ---")
        
        # 1. Force a BUY signal
        signal = {"symbol": "NIFTY22000CE", "side": "BUY", "instrument": "NIFTY", "instrument_token": 12345}
        
        # 2. Mock Order Book: SELL QTY is 5x BUY QTY (Wall)
        self.orchestrator.streamer.latest_ticks = {
            12345: {
                "last_price": 100.0,
                "depth": {
                    "buy": [{"quantity": 100, "price": 99.9}, {"quantity": 100, "price": 99.8}],
                    "sell": [{"quantity": 1000, "price": 100.1}, {"quantity": 1000, "price": 100.2}]
                }
            }
        }
        
        # 3. Try to execute
        success = self.orchestrator.execution_agent.execute_signal(signal)
        self.assertFalse(success)
        logger.info("✅ Result: Trade Blocked by Order Book Wall as expected.")

    @patch('src.agents.context_agent.get_llm')
    def test_scenario_b_black_swan_news(self, mock_get_llm):
        logger.info("\n--- SCENARIO B: Black Swan Detection ---")
        
        # Mock Sentiment Engine to detect CRITICAL alert
        self.orchestrator.context_agent.check_high_priority_alerts = MagicMock(return_value={
            "active": True, "severity": "CRITICAL", "reason": "Flash Crash reported on NSE"
        })
        
        # Try to analyze market
        context = self.orchestrator.context_agent.analyze_current_market()
        
        self.assertEqual(context.get("sentiment"), "NEUTRAL")
        self.assertTrue(context.get("black_swan"))
        logger.info(f"✅ Result: Black Swan detected! Sentiment overridden to Neutral. Reason: {context['error']}")

    def test_scenario_c_gamma_neutralization(self):
        logger.info("\n--- SCENARIO C: Gamma Scalping Trigger ---")
        
        # 1. Setup mocks
        self.orchestrator.alert = MagicMock()
        self.orchestrator.gamma_scalper.evaluate_drift = MagicMock(return_value={
            "side": "SELL", "quantity": 1, "reason": "Test Hedge"
        })
        
        # 2. Run monitor job
        self.orchestrator.position_monitor_job()
        
        # 3. Verify evaluate_drift was called
        self.orchestrator.gamma_scalper.evaluate_drift.assert_called()
        
        # 4. Verify alert was issued
        called = any("Gamma Scalp Triggered" in call[0][0] for call in self.orchestrator.alert.call_args_list)
        self.assertTrue(called)
        logger.info("✅ Result: Gamma Scalper correctly triggered within the monitor loop.")

if __name__ == "__main__":
    unittest.main()
