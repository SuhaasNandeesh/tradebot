import unittest
from unittest.mock import MagicMock, patch
import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.agents.context_agent import ContextAgent

class TestContextAnalysis(unittest.TestCase):
    def setUp(self):
        self.agent = ContextAgent()
        # Mock LLM and Fetcher
        self.agent.llm_fast = MagicMock()
        self.agent.fetcher = MagicMock()
        self.agent.check_high_priority_alerts = MagicMock(return_value={"active": False})

    def test_analyze_current_market_success(self):
        # Mock successful news
        self.agent.fetcher.get_global_macro_sentiment.return_value = "Mock news data"
        
        # Mock the chain invocation
        mock_response = MagicMock()
        mock_response.content = '{"sentiment": "BULLISH", "confidence_score": 85, "key_drivers": ["test"], "trading_implication": "buy"}'
        
        # When using LangChain | operator, the executor is the last object in the chain
        # or the prompt itself if it's a Runnable. Here we mock the prompt | llm_fast chain result.
        with patch('langchain_core.prompts.PromptTemplate.__or__', return_value=MagicMock(invoke=MagicMock(return_value=mock_response))):
            result = self.agent.analyze_current_market()
        
        self.assertEqual(result["sentiment"], "BULLISH")
        self.assertEqual(result["confidence_score"], 85)
        self.assertNotIn("error", result)

    def test_analyze_current_market_no_news(self):
        self.agent.fetcher.get_global_macro_sentiment.return_value = None
        
        result = self.agent.analyze_current_market()
        self.assertIn("error", result)
        self.assertEqual(result["sentiment"], "NEUTRAL")

if __name__ == '__main__':
    unittest.main()
