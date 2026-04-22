import unittest
from unittest.mock import MagicMock, patch
import json
import os

# Dummy key for client init
os.environ["GOOGLE_API_KEY"] = "mock_key"

from src.agents.agentic_brain import AgenticBrain

class TestAgenticBrainFallback(unittest.TestCase):
    def setUp(self):
        self.orchestrator = MagicMock()
        # Mock streamer
        self.orchestrator.streamer.get_nifty.return_value = 22000.0
        self.orchestrator.last_nifty_price = 22000.0
        # Mock risk_manager
        self.orchestrator.risk_manager.current_vix = 15.0
        self.orchestrator.risk_manager.is_expiry_day.return_value = False
        self.orchestrator.risk_manager.can_trade.return_value = True
        # Mock data_provider
        self.orchestrator.data_provider.get_multi_timeframe.return_value = {
            "5minute": [], "15minute": [], "60minute": []
        }
        # Mock strategy_selector
        self.orchestrator.strategy_selector.regime_detector.classify.return_value = {
            "regime": "TRENDING", "narrative": "Market is trending"
        }
        self.orchestrator.strategy_selector.strategies = {}
        # Mock context_agent
        self.orchestrator.context_agent.analyze_current_market.return_value = {
            "sentiment": "BULLISH", "confidence_score": 80, "key_drivers": ["FII Buying"]
        }
        self.orchestrator.last_fii_signal = "BUY"
        # Mock options intelligence
        self.orchestrator.last_options_analysis = {
            "pcr": 0.9, "max_pain": 22000, "support_oi": 21800, "resistance_oi": 22200, "bias": "BULLISH"
        }
        
        self.brain = AgenticBrain(self.orchestrator)

    @patch('google.genai.Client')
    def test_gemini_success(self, mock_client_class):
        # Setup mock client
        mock_client = mock_client_class.return_value
        mock_interaction = MagicMock()
        mock_interaction.outputs = [MagicMock(text=json.dumps({
            "thought": "Strong trend and sentiment.",
            "action": "TRADE",
            "strategy": "SuperTrend",
            "side": "BUY_CE",
            "confidence": 85
        }))]
        mock_client.interactions.create.return_value = mock_interaction
        
        # We need to re-init brain to use the mock client
        self.brain.client = mock_client
        
        result = self.brain.run_iteration()
        self.assertIn("DECISION_REACHED", result)
        self.assertIn("TRADE", result)
        self.assertIn("SuperTrend", result)

    @patch('google.genai.Client')
    def test_gemini_429_fallback_to_lmstudio(self, mock_client_class):
        # Setup mock client to fail with 429
        mock_client = mock_client_class.return_value
        mock_client.interactions.create.side_effect = Exception("Error code: 429 - Too Many Requests")
        
        # Setup LMStudio mock
        self.orchestrator.lm_agent._chat.return_value = json.dumps({
            "thought": "Gemini failed, but LMStudio sees a breakout.",
            "action": "TRADE",
            "strategy": "LM_Fallback",
            "side": "BUY_CE",
            "confidence": 70
        })
        
        self.brain.client = mock_client
        
        result = self.brain.run_iteration()
        self.assertIn("DECISION_REACHED", result)
        self.assertIn("TRADE", result)
        self.assertIn("LM_Fallback", result)
        self.orchestrator.lm_agent._chat.assert_called()

if __name__ == '__main__':
    unittest.main()
