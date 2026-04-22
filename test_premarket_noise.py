import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, time as dtime
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.core.risk_manager import RiskManager

class TestPreMarketNoise(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager()
        self.rm.vix_threshold = 20.0

    @patch('src.core.risk_manager.datetime')
    @patch('src.core.risk_manager.logger')
    def test_can_trade_pre_market_silent(self, mock_logger, mock_datetime):
        # Set time to 09:05 AM (Pre-market)
        mock_datetime.now.return_value = datetime(2026, 3, 30, 9, 5)
        mock_datetime.time = dtime # Ensure dtime works
        
        # Trip circuit breaker
        self.rm.is_circuit_breaker_active = True
        self.rm.circuit_breaker_reason = "Test VIX High"
        
        # Call can_trade
        result = self.rm.can_trade()
        
        # Should be False but NO warning log
        self.assertFalse(result)
        mock_logger.warning.assert_not_called()

    @patch('src.core.risk_manager.datetime')
    @patch('src.core.risk_manager.logger')
    def test_can_trade_market_hours_noisy(self, mock_logger, mock_datetime):
        # Set time to 09:20 AM (Market hours)
        mock_datetime.now.return_value = datetime(2026, 3, 30, 9, 20)
        
        # Trip circuit breaker
        self.rm.is_circuit_breaker_active = True
        self.rm.circuit_breaker_reason = "Test VIX High"
        
        # Call can_trade
        result = self.rm.can_trade()
        
        # Should be False AND have a warning log
        self.assertFalse(result)
        self.assertTrue(mock_logger.warning.called)

if __name__ == '__main__':
    unittest.main()
