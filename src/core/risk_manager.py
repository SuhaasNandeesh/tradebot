import logging
from datetime import datetime, time as dtime
from src.core.env_config import config
from src.data.audit_logger import audit_logger
from src.data.market_calendar import MarketCalendar

logger = logging.getLogger(__name__)

# Correct lot sizes as of 2026 (Institutional Standard)
LOT_SIZES = {
    "NIFTY": 65,
    "SENSEX": 20,
    "BANKNIFTY": 15,
}

import threading

class RiskManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.max_daily_loss = config.max_daily_loss
        self.max_position_size = config.max_position_size
        self.vix_threshold = config.vix_threshold
        
        # Portfolio level constraints
        self.max_daily_trades = config.max_daily_trades
        self.daily_trade_count = 0 
        self.consecutive_loss_days = 0
        self.consecutive_loss_halt = config.consecutive_loss_halt
        
        # Institutional Greek Limits (Configurable)
        self.max_delta = 500.0   # Max 500 delta exposure
        self.max_gamma = 50.0    # Max 50 gamma exposure
        self.max_vega  = 1000.0  # Max 1000 vega exposure
        
        # Current Portfolio Greeks
        self.current_delta = 0.0
        self.current_gamma = 0.0
        self.current_vega  = 0.0
        
        self.daily_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.is_circuit_breaker_active = False
        self.circuit_breaker_reason = None
        self.current_vix = None
        
        # Shadow Margin to prevent double-entries before broker API syncs
        self._shadow_margins = {}  # order_id -> reserved_amount
        self._last_warning_times = {} # message -> last_warn_timestamp
        self.market_calendar = MarketCalendar()

    def is_expiry_day(self, instrument: str = "NIFTY") -> bool:
        """Checks if today is the expiry day for the given instrument."""
        return self.market_calendar.is_expiry_day(instrument, datetime.now().date())

    def should_force_spreads(self, instrument: str = "NIFTY") -> bool:
        """For institutional risk: Force spreads on expiry day to avoid Gamma/Theta ruin."""
        if self.is_expiry_day(instrument):
            logger.warning(f"🔔 EXPIRY DETECTED for {instrument}. Forcing Spread-only mode.")
            return True
        return False

    def set_greeks(self, delta: float = 0, gamma: float = 0, vega: float = 0):
        """Institutional Override for testing or manual adjustment."""
        with self._lock:
            self.current_delta = delta
            self.current_gamma = gamma
            self.current_vega  = vega

    def stress_test_portfolio(self, current_spot: float, portfolio_value: float = 1000000.0) -> dict:
        """
        Institutional Shock Simulator (Delta-at-Risk).
        Simulates:
          1. 2% Gap Down / Flash Crash
          2. 5% IV Spike (Vega risk)
        """
        with self._lock:
            c_delta = self.current_delta
            c_vega = self.current_vega

        if c_delta == 0 and c_vega == 0:
            return {"status": "SAFE", "shock_pnl": 0}

        # Scenario: 2% Flash Crash
        price_shock = -0.02 * current_spot
        delta_loss  = c_delta * price_shock
        
        # Scenario: 5% Volatility Spike (Vega is PnL per 1% IV change)
        vega_impact = c_vega * 5.0 
        
        total_shock_pnl = delta_loss + vega_impact # Gamma/Theta omitted for first-order speed
        shock_pct = (total_shock_pnl / portfolio_value) * 100
        
        logger.info(f"🧪 SHOCK TEST: Projected PnL ₹{total_shock_pnl:.2f} ({shock_pct:.2f}%) on 2% crash.")
        
        if shock_pct < -10.0: # Institutional Limit: 10% Shock Loss
            reason = f"SHOCK BREACH: Projected loss {shock_pct:.2f}% exceeds 10% threshold."
            self.activate_circuit_breaker(reason)
            return {"status": "CRITICAL", "shock_pnl": total_shock_pnl}
            
        return {"status": "SAFE", "shock_pnl": total_shock_pnl}

    def update_portfolio_greeks(self, positions: list):
        """
        Updates the net Greeks for the entire portfolio.
        Expects positions list where each item has 'delta', 'gamma', 'vega', 'quantity'.
        """
        with self._lock:
            self.current_delta = sum(p.get('delta', 0) * p.get('quantity', 0) for p in positions)
            self.current_gamma = sum(p.get('gamma', 0) * p.get('quantity', 0) for p in positions)
            self.current_vega  = sum(p.get('vega', 0) * p.get('quantity', 0) for p in positions)
            curr_gamma = self.current_gamma
        
        # Auto-trip circuit breaker if Greeks are extreme
        if abs(curr_gamma) > self.max_gamma * 1.5:
            self.activate_circuit_breaker(f"EXTREME GAMMA: {curr_gamma:.2f}")

    def can_accept_trade_greeks(self, trade_delta: float, trade_gamma: float, trade_vega: float, quantity: int) -> bool:
        """Checks if a new trade would breach portfolio Greek limits."""
        with self._lock:
            new_delta = self.current_delta + (trade_delta * quantity)
            new_gamma = self.current_gamma + (trade_gamma * quantity)
            new_vega  = self.current_vega + (trade_vega * quantity)
        
        if abs(new_delta) > self.max_delta:
            logger.warning(f"Greek breach: Delta {new_delta:.2f} > {self.max_delta}")
            return False
        if abs(new_gamma) > self.max_gamma:
            logger.warning(f"Greek breach: Gamma {new_gamma:.2f} > {self.max_gamma}")
            return False
        return True

    def update_daily_pnl(self, realized_pnl: float, unrealized_pnl: float = 0.0):
        with self._lock:
            self.daily_pnl = realized_pnl
            self.unrealized_pnl = unrealized_pnl
            total_pnl = realized_pnl + unrealized_pnl
            is_active = self.is_circuit_breaker_active
        
        if total_pnl <= self.max_daily_loss:
            if not is_active:
                reason = f"Floating Loss Limit: Total PnL ₹{total_pnl:.2f} (Real: ₹{realized_pnl:.2f}, Float: ₹{unrealized_pnl:.2f}) breached maximum loss limit of ₹{self.max_daily_loss:.2f}"
                logger.critical(f"CIRCUIT BREAKER ACTIVATED: {reason}")
                audit_logger.log_circuit_breaker(reason, realized_pnl, unrealized_pnl)
                with self._lock:
                    self.circuit_breaker_reason = reason
                    self.is_circuit_breaker_active = True

    def update_vix(self, vix: float):
        """Update current India VIX level for gating logic."""
        with self._lock:
            self.current_vix = vix
            is_active = self.is_circuit_breaker_active
        
        if vix > self.vix_threshold:
            if not is_active:
                reason = f"India VIX ({vix:.2f}) exceeded threshold ({self.vix_threshold})"
                if dtime(9, 15) <= datetime.now().time() <= dtime(15, 30):
                    logger.warning(f"VIX GATE ACTIVE: {reason}")
                with self._lock:
                    self.circuit_breaker_reason = reason
                    self.is_circuit_breaker_active = True

    def activate_circuit_breaker(self, reason: str = "Manual override"):
        """Manually activate the circuit breaker (e.g., from Telegram /pause command)."""
        with self._lock:
            self.is_circuit_breaker_active = True
            self.circuit_breaker_reason = reason
        logger.critical(f"CIRCUIT BREAKER MANUALLY ACTIVATED: {reason}")

    def deactivate_circuit_breaker(self):
        """Resume trading (e.g., from Telegram /resume command)."""
        with self._lock:
            self.is_circuit_breaker_active = False
            self.circuit_breaker_reason = None
        logger.info("Circuit breaker deactivated. Trading resumed.")

    def _log_throttled_warning(self, message: str, cooldown_seconds: int = 300):
        """Helper to prevent log-spamming by only printing the same message every N seconds."""
        import time
        now = time.time()
        with self._lock:
            last_time = self._last_warning_times.get(message, 0)
            if now - last_time >= cooldown_seconds:
                self._last_warning_times[message] = now
                should_warn = True
            else:
                should_warn = False
        
        if should_warn:
            logger.warning(message)

    def can_trade(self) -> bool:
        with self._lock:
            is_active = self.is_circuit_breaker_active
            cb_reason = self.circuit_breaker_reason
            trade_count = self.daily_trade_count
            loss_days = self.consecutive_loss_days
            
        if is_active:
            if dtime(9, 15) <= datetime.now().time() <= dtime(15, 30):
                self._log_throttled_warning(f"Trade rejected: Circuit Breaker active. Reason: {cb_reason}")
            return False
            
        if trade_count >= self.max_daily_trades:
            self._log_throttled_warning(f"Trade rejected: Max daily trades ({self.max_daily_trades}) reached.")
            return False
            
        if loss_days >= self.consecutive_loss_halt:
            self._log_throttled_warning(f"Trade rejected: Consecutive Drawdown limit ({self.consecutive_loss_halt} days) hit. Bot paused.")
            return False
            
        return True

    def increment_trade_count(self):
        with self._lock:
            self.daily_trade_count += 1


    def get_lot_size(self, instrument: str) -> int:
        """Returns the correct lot size for a given instrument."""
        return LOT_SIZES.get(instrument.upper(), 65)

    def reserve_shadow_margin(self, order_id: str, amount: float):
        """Immediately reserve margin locally to prevent over-allocation during rapid loops."""
        with self._lock:
            self._shadow_margins[order_id] = amount
        logger.debug(f"[SHADOW MARGIN] Reserved ₹{amount:.2f} for order {order_id}")

    def release_shadow_margin(self, order_id: str):
        """Release shadow margin (e.g. after Kite API fully processes and reflects it)."""
        with self._lock:
            if order_id in self._shadow_margins:
                amount = self._shadow_margins.pop(order_id)
                logger.debug(f"[SHADOW MARGIN] Released ₹{amount:.2f} for order {order_id}")

    def calculate_position_size(self, available_margin: float, option_premium: float, 
                                instrument: str = "NIFTY", custom_margin_per_lot: float = None):
        """
        Calculates how many lots to trade based on available margin minus shadow margin.
        """
        # Deduct any pending trades that the broker hasn't accounted for yet
        with self._lock:
            shadow_total = sum(self._shadow_margins.values())
            eff_max_pos = self.max_position_size
            
        effective_margin = available_margin - shadow_total

        if effective_margin <= 0:
            logger.warning(f"Effective margin ₹{effective_margin:.2f} too low (Shadow: ₹{shadow_total:.2f})")
            return 0
            
        lot_size = self.get_lot_size(instrument)
        
        # Use provided margin or calculate default (premium * lot)
        margin_per_lot = custom_margin_per_lot if custom_margin_per_lot else (option_premium * lot_size)
        
        if margin_per_lot <= 0:
            return 0

        # Institutional Risk Rule: Don't risk more than 5% of effective available margin on a single trade
        # This prevents accidental account blowup during high volatility.
        max_alloc = effective_margin * 0.05
        lots = int(max_alloc // margin_per_lot)
        
        # Ensure we always trade at least 1 lot if we have enough margin, 
        # but bound it by institutional max_position_size
        final_lots = max(0, min(lots, eff_max_pos))
        
        if final_lots == 0 and effective_margin >= margin_per_lot:
             # If our 5% rule results in 0 lots but we could afford 1, just buy 1 lot for entry
             final_lots = 1
             
        return min(final_lots, eff_max_pos)

    def reset_daily(self):
        """Resets daily state. Call this at market open each day."""
        with self._lock:
            # Update consecutive loss tracking BEFORE zeroing daily_pnl
            if self.daily_pnl < 0:
                self.consecutive_loss_days += 1
                if self.consecutive_loss_days >= self.consecutive_loss_halt:
                    logger.critical(f"PORTFOLIO HALT: Hit {self.consecutive_loss_days} consecutive losing days. Trading suspended.")
                    self.is_circuit_breaker_active = True
                    self.circuit_breaker_reason = "Consecutive loss days exhaustion."
            else:
                self.consecutive_loss_days = 0
                
            self.daily_pnl = 0.0
            self.unrealized_pnl = 0.0
            self.daily_trade_count = 0
            self._shadow_margins.clear()
            
            # Only deactivate if VIX is normal; otherwise keep protection
            if self.current_vix is None or self.current_vix <= self.vix_threshold:
                self.is_circuit_breaker_active = False
                self.circuit_breaker_reason = None
        logger.info("Risk Manager daily state & shadow margin reset.")

    def get_dynamic_thresholds(self, regime: str = "TRENDING") -> tuple[int, int]:
        """
        Calculates dynamic APPROVE/WEAK thresholds based on VIX and regime.
        Returns: (APPROVE_THRESHOLD, WEAK_THRESHOLD)
        """

        # Base thresholds
        approve = 65
        weak    = 40
        
        # 1. Volatility Penalty/Bonus
        with self._lock:
            curr_vix = self.current_vix

        if curr_vix:
            if curr_vix > 22: # High Vol
                approve += 10 # Be more selective
                weak    += 5
            elif curr_vix < 14: # Calm/Trending
                approve -= 5  # Allow slightly weaker signals
                weak    -= 5
                
        # 2. Regime adjustment
        if regime == "TRENDING":
            approve -= 5
        elif regime == "VOLATILE":
            approve += 15
            
        return max(50, min(85, approve)), max(30, min(60, weak))

    def get_status(self) -> dict:
        """Returns current risk status for display in Telegram /risk command."""
        with self._lock:
            return {
                "circuit_breaker_active": self.is_circuit_breaker_active,
                "reason": self.circuit_breaker_reason or "None",
                "daily_pnl": self.daily_pnl,
                "unrealized_pnl": self.unrealized_pnl,
                "total_pnl": self.daily_pnl + self.unrealized_pnl,
                "max_daily_loss": self.max_daily_loss,
                "vix": self.current_vix,
                "vix_threshold": self.vix_threshold,
                "max_position_size": self.max_position_size,
            }
