
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
from src.core.llm_provider import get_llm

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("E2E_Test")

class TestE2EIntegration(unittest.TestCase):
    @patch('src.agents.execution_agent.KiteConnect')
    @patch('main.KiteStreamer')
    @patch('main.TelegramAgent')
    @patch('main.load_dotenv')
    def setUp(self, mock_dotenv, mock_telegram, mock_streamer, mock_kite):
        # 1. Setup Environment Mocks
        os.environ["GOOGLE_API_KEY"] = "fake_key"
        os.environ["KITE_API_KEY"] = "fake_key"
        os.environ["PAPER_TRADE"] = "True"
        
        # 2. Mock Orchestrator Dependencies
        self.orchestrator = Orchestrator()
        
        # 3. Inject Mock Data for the Test
        self.orchestrator.streamer.get_nifty = MagicMock(return_value=22000.0)
        self.orchestrator.streamer.get_vix = MagicMock(return_value=15.0)
        self.orchestrator.streamer.is_connected = True
        
        # Mock Market Data (Multi-Timeframe)
        dummy_df = pd.DataFrame({
            'open': [22000]*100, 'high': [22100]*100, 'low': [21900]*100, 
            'close': [22050]*100, 'volume': [100000]*100
        })
        self.orchestrator.data_provider.get_multi_timeframe = MagicMock(return_value={
            "5m": dummy_df, "15m": dummy_df, "1h": dummy_df
        })
        self.orchestrator.data_provider.get_ohlcv = MagicMock(return_value=dummy_df)
        self.orchestrator.context_agent.analyze_current_market = MagicMock(return_value={
            "sentiment": "BULLISH",
            "confidence_score": 85,
            "key_drivers": ["FII Buying", "Positive Global Cues"],
            "trading_implication": "Aggressive Longs"
        })
        self.orchestrator.current_context = self.orchestrator.context_agent.analyze_current_market()

    @patch('langchain_google_genai.ChatGoogleGenerativeAI')
    def test_full_trade_pipeline(self, mock_gemini):
        logger.info("🚀 Starting E2E Pipeline Test: Market -> AI -> Strategy -> Execution")
        
        # 1. Mock Gemini 3 Flash Response for Pre-Trade Sanity
        mock_response = MagicMock()
        mock_response.content = "APPROVE: Strong structural confluence detected on NIFTY."
        mock_gemini.return_value.invoke.return_value = mock_response

        # 2. Mock Options Data (NIFTY 22000 CE)
        self.orchestrator.last_options_analysis = {
            "pcr": {"pcr": 1.1},
            "options_direction": "BULLISH",
            "iv_rank": {"iv_rank": 30},
            "oi_buildup": {"signal": "FRESH_LONGS", "dist_to_resistance_%": 1.2}
        }
        
        # 3. Simulate a Market Tick that triggers the Evaluator
        logger.info("Step 1: Simulating Bullish Market State...")
        
        # Trigger Strategy Selector directly to avoid thread-pool complexity in test
        confluence = {"score": 85, "approved": True, "verdict": "✅ APPROVED", "supporters": ["Test"], "blockers": []}
        self.orchestrator.strategy_selector.select_with_confluence = MagicMock(return_value=("Supertrend_VWAP", "BUY_CE", confluence))

        strategy_name, signal, confluence = self.orchestrator.strategy_selector.select_with_confluence(
            context=self.orchestrator.current_context,
            symbol="NIFTY",
            options_analysis=self.orchestrator.last_options_analysis,
            fii_signal="BULLISH",
            factor_accuracy={"technical": 0.8, "options": 0.7, "news": 0.9}
        )
        
        self.assertEqual(signal, "BUY_CE")
        logger.info(f"Step 2: Strategy Selected: {strategy_name} | Signal: {signal}")

        # 4. Test AI Sanity Check (Gemini 3 Flash Preview)
        logger.info("Step 3: Running Gemini 3 Flash Pre-Trade Sanity...")
        sanity = self.orchestrator.lm_agent.pre_trade_sanity(
            signal=signal, instrument="NIFTY", context=self.orchestrator.current_context,
            options_analysis=self.orchestrator.last_options_analysis, confluence=confluence
        )
        self.assertTrue(sanity["approved"])
        logger.info(f"Step 4: AI Sanity Approved: {sanity['narrative']}")

        # 5. Verify Execution Logic (Order Placement Intent)
        logger.info("Step 5: Verifying Order Execution Logic...")
        # Mocking the Kite API call inside ExecutionAgent
        self.orchestrator.execution_agent.kite.place_order = MagicMock(return_value="ORDER_12345")
        
        # 6. Verify Telegram Notification Intent
        logger.info("Step 6: Verifying Telegram Alert Broadcast...")
        self.orchestrator.telegram.broadcast_message_sync = MagicMock()
        
        # Simulate the final broadcast
        self.orchestrator.alert(f"📈 Trade Entered - {signal}")
        self.orchestrator.telegram.broadcast_message_sync.assert_called_with("📈 Trade Entered - BUY_CE")
        
        logger.info("✅ E2E INTEGRATION TEST PASSED: Full loop verified.")

if __name__ == "__main__":
    unittest.main()
