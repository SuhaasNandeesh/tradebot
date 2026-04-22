"""
Dynamic Gamma Scalper (Institutional Module)

Monitors the Delta drift of existing option positions. 
If Delta drift exceeds the hedging threshold (Institutional norm: 0.15 to 0.25 net delta),
it signals for a delta-neutralizing trade in the underlying Futures.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

HEDGE_THRESHOLD = 0.20  # Hedge if net delta drift > 20% of a lot

class GammaScalper:
    def __init__(self, risk_manager, execution_agent):
        self.risk_manager = risk_manager
        self.execution_agent = execution_agent
        self.active_hedges = {} # symbol -> current_futures_qty

    def evaluate_drift(self, net_delta: float, instrument: str = "NIFTY"):
        """
        net_delta: The current portfolio delta for the instrument.
        If net_delta is +0.5, we are 'over-long' by 0.5 lots. 
        We need to SELL 0.5 lots of futures to remain neutral.
        """
        # Institutional Lot Sizes (2026 Standards)
        lot_size = 65 if instrument.upper() == "NIFTY" else 20
        drift_in_lots = net_delta # Delta 1.0 = 1 lot of underlying
        
        if abs(drift_in_lots) >= HEDGE_THRESHOLD:
            logger.info(f"🌀 GAMMA SCALPER: Delta Drift {drift_in_lots:.2f} lots detected in {instrument}.")
            return self._generate_hedge_order(drift_in_lots, instrument)
        
        return None

    def _generate_hedge_order(self, drift_lots: float, instrument: str):
        """
        Creates a futures order to neutralize delta.
        Positive drift (Long) -> Sell Futures
        Negative drift (Short) -> Buy Futures
        """
        side = "SELL" if drift_lots > 0 else "BUY"
        qty = int(abs(drift_lots)) # Minimal hedge: round to nearest whole lot
        
        if qty == 0:
            return None # Drift not enough for a full futures lot hedge yet
            
        return {
            "instrument": instrument,
            "type": "FUTURES",
            "side": side,
            "quantity": qty,
            "reason": f"Gamma Scalping: Neutralizing {drift_lots:.2f} delta drift"
        }
