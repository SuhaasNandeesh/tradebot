import unittest
import sys
import os
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.core.position_manager import PositionManager, SingleLegPosition, SpreadPosition, PositionLeg

class TestPositionSummary(unittest.TestCase):
    def setUp(self):
        self.pm = PositionManager()
        # Mock DB
        self.pm.db = MagicMock()

    def test_get_summary_empty(self):
        summary = self.pm.get_summary()
        self.assertIn("📭 **No open positions.**", summary)

    def test_get_summary_with_positions(self):
        # Add a position
        pos = SingleLegPosition(
            symbol="NIFTY26MAR22000CE",
            instrument="NIFTY",
            transaction_type="BUY",
            quantity=50,
            entry_price=100.0,
            stop_loss=80.0,
            target=120.0,
            order_id="test_order_123",
            instrument_token=12345
        )
        pos.current_price = 110.0
        self.pm._positions[pos.order_id] = pos
        
        summary = self.pm.get_summary()
        self.assertIn("NIFTY26MAR22000CE", summary)
        self.assertIn("LTP: `110.00`", summary)
        self.assertIn("₹500.00", summary) # 10 pts * 50 units
        self.assertIn("🟢", summary)

if __name__ == '__main__':
    unittest.main()
