import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime
import time
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.core.risk_manager import RiskManager

class TestLogThrottling(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager()
        self.rm.is_circuit_breaker_active = True
        self.rm.circuit_breaker_reason = "High VIX"

    @patch('src.core.risk_manager.logger')
    @patch('src.core.risk_manager.datetime')
    def test_throttling_logic(self, mock_datetime, mock_logger):
        # Set time to 10:00 AM (Market hours)
        mock_datetime.now.return_value = datetime(2026, 3, 30, 10, 0)
        
        # Call can_trade 10 times in rapid succession
        for _ in range(10):
            self.rm.can_trade()
            
        # Verify logger.warning was called exactly ONCE
        self.assertEqual(mock_logger.warning.call_count, 1)
        
        # Advance time by 301 seconds (5 minutes + 1s)
        # Note: self._log_throttled_warning uses time.time()
        with patch('time.time', return_value=time.time() + 301):
            self.rm.can_trade()
            
        # Verify logger.warning was called a second time
        self.assertEqual(mock_logger.warning.call_count, 2)

if __name__ == '__main__':
    unittest.main()
