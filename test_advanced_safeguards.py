import unittest
from unittest.mock import patch, MagicMock
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.core.risk_manager import RiskManager
from src.core.gamma_scalper import GammaScalper
from src.agents.execution_agent import ExecutionAgent

class TestAdvancedSafeguards(unittest.TestCase):

    def setUp(self):
        self.rm = RiskManager()
        self.rm.max_position_size = 10
        self.ea = ExecutionAgent(self.rm, MagicMock())
        self.gs = GammaScalper(self.rm, self.ea)

        # Suppress logging in tests
        import logging
        logging.getLogger().setLevel(logging.CRITICAL)

    def test_margin_buffer(self):
        # 100k capital. 20% buffer means 80k usable.
        # 90% of 80k is 72k.
        # Nifty lot size is 65. Premium 100 -> 6500 per lot.
        # 72000 / 6500 = 11 lots.
        # But max_position_size is 10.
        lots = self.rm.calculate_position_size(
            available_margin=100000.0,
            option_premium=100.0,
            instrument="NIFTY"
        )
        self.assertEqual(lots, 10)

    def test_gamma_hysteresis(self):
        # Mock execution agent's place order
        self.ea.place_order = MagicMock(return_value="mock_order_123")

        # Test 1: Drift is 0.30 -> Below trigger threshold (0.40)
        res = self.gs.evaluate_drift(0.30, "NIFTY")
        self.assertIsNone(res)

        # Test 2: Drift hits 0.45 -> Triggers hedge (sell 0.45 rounded? No, it takes int(abs) -> wait, abs(0.45) int is 0. Let's check logic)
        # Ah, int(0.45) is 0, so it will return None if it rounds to 0.
        # Let's test with 1.45 to ensure it actually triggers 1 lot hedge.
        res = self.gs.evaluate_drift(1.45, "NIFTY")
        self.assertIsNotNone(res)
        self.assertEqual(res["side"], "SELL")
        self.assertEqual(res["quantity"], 1)

        # Test 3: We are hedged. Drift drops to 0.20. Should not clear because clear threshold is 0.10.
        res = self.gs.evaluate_drift(0.20, "NIFTY")
        self.assertIsNone(res)

        # Test 4: Drift drops to 0.05. Should clear.
        res = self.gs.evaluate_drift(0.05, "NIFTY")
        self.assertIsNotNone(res)
        self.assertEqual(res["side"], "BUY")
        self.assertEqual(res["quantity"], 1)

        # Ensure tracking is reset
        symbol = "NIFTY26APR"
        self.assertEqual(self.gs.active_hedges[symbol], 0)

if __name__ == '__main__':
    unittest.main()
