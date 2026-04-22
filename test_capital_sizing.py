import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.core.risk_manager import RiskManager
from src.agents.execution_agent import ExecutionAgent

class TestCapitalSizing(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager()
        self.rm.max_position_size = 10 # 10 lots max
        # Mocking Env
        os.environ["PAPER_TRADE"] = "True"
        os.environ["MOCK_PAPER_CAPITAL"] = "10000.0" # 10k capital
        self.ea = ExecutionAgent(self.rm, MagicMock())

    def test_insufficient_capital(self):
        # 10k capital. Option premium 500. 1 lot (25) costs 12500.
        # Should return 0 lots.
        lots = self.rm.calculate_position_size(
            available_margin = 10000.0,
            option_premium   = 500.0,
            instrument       = "NIFTY"
        )
        self.assertEqual(lots, 0)

    def test_sufficient_capital(self):
        # 50k capital. Option premium 100. 1 lot (25) costs 2500.
        # 90% of 50k is 45k. 45k / 2500 = 18 lots. 
        # But max_position_size is 10.
        # Should return 10 lots.
        lots = self.rm.calculate_position_size(
            available_margin = 50000.0,
            option_premium   = 100.0,
            instrument       = "NIFTY"
        )
        self.assertEqual(lots, 10)

    def test_live_capital_fetch_mock(self):
        self.ea.paper_trade = False
        self.ea.kite = MagicMock()
        self.ea.kite.margins.return_value = {"net": 25000.0}
        
        capital = self.ea.get_available_capital()
        self.assertEqual(capital, 25000.0)
        self.ea.kite.margins.assert_called_with("equity")

if __name__ == '__main__':
    unittest.main()
