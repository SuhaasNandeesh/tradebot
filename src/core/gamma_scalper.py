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

        Uses Hysteresis Banding to prevent churn:
        - Trigger hedge only if abs(drift) > 0.40 lots
        - Clear hedge (un-hedge) only if abs(drift) drops back below 0.10 lots
        """
        lot_size = 65 if instrument.upper() == "NIFTY" else 20
        drift_in_lots = net_delta

        TRIGGER_THRESHOLD = 0.40
        CLEAR_THRESHOLD = 0.10
        
        symbol = f"{instrument}26APR" if instrument == "NIFTY" else f"{instrument}26APR"
        current_hedge_qty = self.active_hedges.get(symbol, 0)
        
        # Currently NOT hedged -> Check if we need to trigger a hedge
        if current_hedge_qty == 0:
            if abs(drift_in_lots) >= TRIGGER_THRESHOLD:
                logger.info(f"🌀 GAMMA SCALPER: Delta Drift {drift_in_lots:.2f} lots breached TRIGGER threshold ({TRIGGER_THRESHOLD}).")
                hedge_order = self._generate_hedge_order(drift_in_lots, instrument)
                if hedge_order:
                    self._execute_hedge(hedge_order)
                return hedge_order

        # Currently HEDGED -> Check if we need to clear the hedge (we are back to neutral natively)
        elif current_hedge_qty != 0:
            # We are hedged. The true underlying drift is the current net_delta + our hedge.
            # Example: We were long 0.5 delta, so we sold 1 lot futures (-1.0). Net is now -0.5.
            # If the market moves and underlying options delta drops to 0.05,
            # our true drift is now just 0.05. We should remove the -1.0 hedge.

            # Since net_delta includes the options, we evaluate the options delta alone:
            options_delta = drift_in_lots

            if abs(options_delta) <= CLEAR_THRESHOLD:
                logger.info(f"🌀 GAMMA SCALPER: Options Delta {options_delta:.2f} dropped below CLEAR threshold ({CLEAR_THRESHOLD}). Removing hedge.")

                # Reverse the active hedge
                side = "BUY" if current_hedge_qty < 0 else "SELL"
                qty = abs(current_hedge_qty)

                clear_order = {
                    "instrument": instrument,
                    "type": "FUTURES",
                    "side": side,
                    "quantity": qty,
                    "reason": f"Gamma Scalping: Clearing hedge as options delta returned to {options_delta:.2f}"
                }
                self._execute_hedge(clear_order)
                # Reset tracking immediately
                self.active_hedges[symbol] = 0
                return clear_order

        return None

    def _execute_hedge(self, hedge_order: dict):
        """
        Automatically executes the futures hedge via the ExecutionAgent.
        """
        instrument = hedge_order["instrument"]
        side = hedge_order["side"]
        qty = hedge_order["quantity"]

        # Determine futures symbol (Assuming front-month future)
        # In a real environment, you'd dynamically fetch the current month's expiry.
        # For this prototype, we use a placeholder that the execution agent parses or overrides if paper trading.
        symbol = f"{instrument}26APR" if instrument == "NIFTY" else f"{instrument}26APR"

        logger.warning(f"🛡️ Executing automated delta-hedge: {side} {qty} lots of {symbol}")

        try:
            # We treat the hedge like a standard market order, as delta risk must be covered instantly
            order_id = self.execution_agent.place_order(
                tradingsymbol=symbol,
                transaction_type=side,
                quantity=qty,
                order_type="MARKET",
                exchange="NFO"
            )

            if order_id:
                logger.info(f"✅ Hedge executed. Order ID: {order_id}")
                # Track the active hedge so we don't double-hedge
                current_qty = self.active_hedges.get(symbol, 0)
                self.active_hedges[symbol] = current_qty + (qty if side == "BUY" else -qty)
            else:
                logger.error("❌ Failed to place hedge order.")
        except Exception as e:
            logger.error(f"❌ Exception executing hedge: {e}")


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
