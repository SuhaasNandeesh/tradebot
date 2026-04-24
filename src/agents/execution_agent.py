import os
import json
import logging
import uuid
import time
from kiteconnect import KiteConnect
from src.core.risk_manager import RiskManager
from src.core.position_manager import PositionManager, SingleLegPosition, SpreadPosition, PositionLeg
from src.data.audit_logger import audit_logger
from src.core.env_config import config
from src.strategies.spread_builder import SpreadBuilder

logger = logging.getLogger(__name__)

import threading
import time
from typing import Callable, Any

class RateLimiter:
    """
    Leaky bucket rate limiter to respect Zerodha's 10 req/sec limit.
    Ensures max 9 requests per second to be safe.
    """
    def __init__(self, max_requests: int = 9, time_window: float = 1.0):
        self.max_requests = max_requests
        self.time_window = time_window
        self.timestamps = []
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            # Remove timestamps older than the time window
            self.timestamps = [ts for ts in self.timestamps if now - ts < self.time_window]

            if len(self.timestamps) >= self.max_requests:
                # Sleep until the oldest request falls out of the window
                sleep_time = self.time_window - (now - self.timestamps[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
                # Recalculate 'now' after sleeping
                now = time.time()
                self.timestamps = [ts for ts in self.timestamps if now - ts < self.time_window]

            self.timestamps.append(now)

class APIRetryExecutor:
    """
    Wraps API calls with rate limiting and exponential backoff for 429/500 errors.
    """
    def __init__(self, rate_limiter: RateLimiter):
        self.rate_limiter = rate_limiter

    def execute(self, func: Callable, *args, **kwargs) -> Any:
        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries):
            self.rate_limiter.wait()
            try:
                return func(*args, **kwargs)
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "too many requests" in error_str or "network" in error_str:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"⚠️ API Rate Limit/Network Error. Retrying in {delay}s (Attempt {attempt+1}/{max_retries}). Error: {e}")
                    time.sleep(delay)
                else:
                    # Non-retryable error (e.g., validation error, margin shortfall)
                    raise e
        logger.error(f"❌ API call failed after {max_retries} attempts.")
        raise Exception(f"Max retries exceeded for API call: {func.__name__}")

# Global instances
_kite_rate_limiter = RateLimiter(max_requests=9, time_window=1.0)
_api_executor = APIRetryExecutor(_kite_rate_limiter)


class ExecutionAgent:
    def __init__(self, risk_manager: RiskManager, position_manager: PositionManager, streamer=None):
        self.api_key = os.getenv("KITE_API_KEY")
        self.access_token = self._load_access_token()
        self.kite = KiteConnect(api_key=self.api_key) if self.api_key and self.api_key != 'your_kite_api_key' else None
        if self.kite and self.access_token:
            self.kite.set_access_token(self.access_token)

        self.paper_trade = os.getenv("PAPER_TRADE", "True").lower() in ("true", "1", "t")
        self.risk_manager = risk_manager
        self.position_manager = position_manager
        self.streamer = streamer 
        self.spread_builder = SpreadBuilder()
        from src.memory.state_db import StateDB
        self.state_db = StateDB()

        if self.streamer:
            self.streamer.order_callback = self._handle_order_update

    def _load_access_token(self):
        try:
            with open("kite_session.json", "r") as f:
                return json.load(f).get("access_token")
        except Exception: return None

    def get_available_capital(self) -> float:
        """
        Fetches available margin for trading. 
        In Live mode: Queries Kite API for 'equity' net margin.
        In Paper mode: Returns value from MOCK_PAPER_CAPITAL env var (default: 100k).
        """
        if self.paper_trade:
            # Fallback to 1L if not set
            return float(os.getenv("MOCK_PAPER_CAPITAL", "100000.0"))
            
        if not self.kite:
            logger.error("Kite API not initialized. Cannot fetch capital.")
            return 0.0
            
        try:
            margins = _api_executor.execute(self.kite.margins, "equity")
            # 'net' is the absolute available cash/margin
            if margins and "net" in margins:
                return float(margins["net"])
            return 0.0
        except Exception as e:
            logger.error(f"Failed to fetch broker margins: {e}")
            return 0.0

    def place_order(self, tradingsymbol: str, transaction_type: str,
                    quantity: int, order_type: str = "MARKET",
                    limit_price: float = None, trigger_price: float = None,
                    exchange: str = "NFO") -> str:
        if not self.risk_manager.can_trade(): return None
        if self.paper_trade:
            order_id = str(uuid.uuid4())
            self.state_db.save_working_order(order_id, tradingsymbol, transaction_type, quantity, order_type, limit_price)
            return order_id
        try:
            kite_order_type = getattr(self.kite, f"ORDER_TYPE_{order_type}")
            order_id = _api_executor.execute(self.kite.place_order,
                tradingsymbol=tradingsymbol, exchange=exchange,
                transaction_type=getattr(self.kite, f"TRANSACTION_TYPE_{transaction_type}"),
                quantity=quantity, variety=self.kite.VARIETY_REGULAR,
                order_type=kite_order_type, product=self.kite.PRODUCT_NRML,
                validity=self.kite.VALIDITY_DAY, price=limit_price, trigger_price=trigger_price
            )
            if order_id:
                self.state_db.save_working_order(order_id, tradingsymbol, transaction_type, quantity, order_type, limit_price)
            return order_id
        except Exception: return None

    def place_smart_order(self, tradingsymbol: str, transaction_type: str,
                          quantity: int, instrument_token: int, exchange: str = "NFO") -> str:
        """
        Institutional Smart Entry: Places a LIMIT order and starts chasing if not filled.
        """
        # We always attempt to get depth for better pricing, even in paper mode
        limit_price = None
        if self.streamer and instrument_token:
            tick = self.streamer.latest_ticks.get(instrument_token)
            if tick and "depth" in tick:
                try:
                    limit_price = tick["depth"]["buy"][0]["price"] if transaction_type == "BUY" else tick["depth"]["sell"][0]["price"]
                except: pass

        if self.paper_trade:
            # In paper mode, we just simulate the entry at the 'limit' price if found
            order_id = self.place_order(tradingsymbol, transaction_type, quantity, "LIMIT" if limit_price else "MARKET", 
                                        limit_price=limit_price, exchange=exchange)
            return order_id
            
        if not limit_price:
            # Fallback if no depth data available in live mode
            return self.place_order(tradingsymbol, transaction_type, quantity, exchange=exchange)
            
        try:
            # Place initial limit order at best bid/ask
            order_id = self.place_order(tradingsymbol, transaction_type, quantity, "LIMIT", limit_price=limit_price, exchange=exchange)
            
            if order_id:
                # Start the background chasing thread
                import threading
                threading.Thread(
                    target=self._chase_order_thread, 
                    args=(order_id, tradingsymbol, transaction_type, instrument_token, exchange),
                    daemon=True
                ).start()
                
            return order_id
        except Exception as e:
            logger.error(f"Smart order placement failed: {e}")
            return self.place_order(tradingsymbol, transaction_type, quantity, exchange=exchange)

    def _chase_order_thread(self, order_id: str, symbol: str, side: str, token: int, exchange: str):
        """
        Background worker that modifies limit price to chase the market until filled.
        """
        max_attempts = 5
        attempts = 0
        wait_time = 5 # seconds
        
        while attempts < max_attempts:
            time.sleep(wait_time)
            
            # Check if order is still open
            try:
                orders = _api_executor.execute(self.kite.orders, )
                order = next((o for o in orders if o["order_id"] == order_id), None)
                
                if not order or order["status"] == "COMPLETE" or order["status"] == "CANCELLED" or order["status"] == "REJECTED":
                    logger.info(f"Chase complete for {order_id} (Status: {order['status'] if order else 'GONE'})")
                    break
                
                # Still open, update price
                tick = self.streamer.latest_ticks.get(token)
                if not tick or "depth" not in tick: continue
                
                new_price = tick["depth"]["buy"][0]["price"] if side == "BUY" else tick["depth"]["sell"][0]["price"]
                
                if new_price != order["price"]:
                    logger.info(f"⚡ [CHASE] Modifying {order_id} to new price {new_price}")
                    _api_executor.execute(self.kite.modify_order,
                        variety=self.kite.VARIETY_REGULAR,
                        order_id=order_id,
                        price=new_price
                    )
                
                attempts += 1
            except Exception as e:
                logger.error(f"Chase attempt {attempts} failed for {order_id}: {e}")
                break
        
        # If still not filled after max attempts, convert to market to ensure entry
        try:
            orders = _api_executor.execute(self.kite.orders, )
            order = next((o for o in orders if o["order_id"] == order_id), None)
            if order and order["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
                logger.warning(f"⏰ [CHASE] Timeout reached for {order_id}. Converting to MARKET.")
                _api_executor.execute(self.kite.modify_order,
                    variety=self.kite.VARIETY_REGULAR,
                    order_id=order_id,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )
        except: pass

    def enter_position(self, tradingsymbol: str, instrument: str,
                       transaction_type: str, quantity: int,
                       entry_price: float, sl_pct: float = None,
                       target_pct: float = None, strategy_id: str = None,
                       exchange: str = None, instrument_token: int = None,
                       atr: float = None) -> str:
        logger.info(f"⚡ [ExecutionAgent] enter_position START: {tradingsymbol}")
        if not self.risk_manager.can_trade():
            logger.error("⚡ [ExecutionAgent] RiskManager blocked enter_position")
            return None
        if exchange is None: exchange = "BFO" if instrument.upper() == "SENSEX" else "NFO"
        
        # 1. Place the primary order
        order_id = self.place_smart_order(tradingsymbol, transaction_type, quantity, instrument_token, exchange)
        logger.info(f"⚡ [ExecutionAgent] Main Order ID: {order_id}")
        if not order_id: return

        # Persist status
        self.state_db.update_working_order_status(order_id, status)

        # 2. Institutional Risk Calculation (ATR-based > %-based)
        if atr:
            sl_dist = atr * config.atr_multiplier_sl
            tgt_dist = atr * config.atr_multiplier_tgt
            
            if transaction_type == "BUY":
                stop_loss = entry_price - sl_dist
                target = entry_price + tgt_dist
            else:
                stop_loss = entry_price + sl_dist
                target = entry_price - tgt_dist
            
            calc_method = f"ATR-based (ATR: {atr:.2f}, Mult: {config.atr_multiplier_sl})"
        else:
            # Fallback to tighter percentage-based risk
            used_sl_pct = sl_pct if sl_pct is not None else config.sl_pct
            used_tgt_pct = target_pct if target_pct is not None else config.target_pct
            
            stop_loss = entry_price * (1 - used_sl_pct if transaction_type == "BUY" else 1 + used_sl_pct)
            target = entry_price * (1 + used_tgt_pct if transaction_type == "BUY" else 1 - used_tgt_pct)
            
            calc_method = f"Percentage-based ({used_sl_pct*100:.1f}%)"

        stop_loss = round(stop_loss, 2)
        target = round(target, 2)
        
        logger.info(f"🛡️ [Risk] {calc_method} | Entry: {entry_price} | SL: {stop_loss} | Tgt: {target}")

        # 3. Place the SL Order (Standard SL-Limit/SL-Market)
        sl_order_id = self.place_order(tradingsymbol, "SELL" if transaction_type == "BUY" else "BUY", 
                                       quantity, "SL", trigger_price=stop_loss, 
                                       limit_price=round(stop_loss * (0.995 if transaction_type == "BUY" else 1.005), 1), exchange=exchange)
        logger.info(f"⚡ [ExecutionAgent] SL Order ID: {sl_order_id}")

        pos = SingleLegPosition(symbol=tradingsymbol, instrument=instrument, transaction_type=transaction_type, 
                       quantity=quantity, entry_price=entry_price, stop_loss=stop_loss, target=target, 
                       order_id=order_id, instrument_token=instrument_token, strategy_id=strategy_id, sl_order_id=sl_order_id)
        
        try:
            self.position_manager.add_position(pos)
            logger.info(f"⚡ [ExecutionAgent] Position added to manager: {tradingsymbol}")
        except Exception as e:
            logger.error(f"⚡ [ExecutionAgent] Failed to add position to manager: {e}")
            return None
            
        return order_id

    def enter_spread(self, buy_symbol: str, sell_symbol: str, instrument: str,
                     transaction_type: str, quantity: int,
                     entry_price: float, strategy_id: str = None,
                     exchange: str = "NFO",
                     buy_token: int = None, sell_token: int = None) -> str:
        """Institutional Atomic Spread Entry."""
        buy_order_id = self.place_order(buy_symbol, "BUY", quantity, exchange=exchange)
        if not buy_order_id: return None
        sell_order_id = self.place_order(sell_symbol, "SELL", quantity, exchange=exchange)
        if not sell_order_id:
            self.place_order(buy_symbol, "SELL", quantity, exchange=exchange)
            return None
        buy_leg  = PositionLeg(buy_symbol, buy_token, "BUY", quantity, entry_price)
        sell_leg = PositionLeg(sell_symbol, sell_token, "SELL", quantity, 0.0)
        pos = SpreadPosition(buy_leg, sell_leg, instrument, round(entry_price*0.7,2), round(entry_price*1.5,2), buy_order_id, strategy_id)
        self.position_manager.add_position(pos)
        return buy_order_id

    def _chase_exit_order(self, symbol: str, side: str, quantity: int, token: int):
        """
        Dynamic Order Chasing for Exits.
        Starts with a LIMIT order at mid-price, then trails it towards market price,
        falling back to MARKET if unfilled to avoid infinite slippage risk.
        """
        if self.paper_trade or not self.kite:
            # Paper trade: Just use a market order approximation
            return self.place_order(symbol, side, quantity, order_type="MARKET")

        mid_price = None
        if self.streamer and token:
            tick = self.streamer.latest_ticks.get(token)
            if tick and "depth" in tick:
                try:
                    bid = tick["depth"]["buy"][0]["price"]
                    ask = tick["depth"]["sell"][0]["price"]
                    mid_price = round((bid + ask) / 2.0, 1)
                except: pass

        if not mid_price:
            logger.warning(f"⚠️ [CHASE EXIT] No depth available for {symbol}. Falling back to MARKET.")
            return self.place_order(symbol, side, quantity, order_type="MARKET")

        logger.info(f"⚡ [CHASE EXIT] Placing initial LIMIT for {symbol} at {mid_price}")
        order_id = self.place_order(symbol, side, quantity, order_type="LIMIT", limit_price=mid_price)
        if not order_id: return

        # Persist status
        self.state_db.update_working_order_status(order_id, status)

        # Chase logic
        max_attempts = 3
        attempts = 0
        while attempts < max_attempts:
            time.sleep(0.5) # Wait 500ms

            try:
                orders = _api_executor.execute(self.kite.orders)
                order = next((o for o in orders if o["order_id"] == order_id), None)
                if not order or order["status"] in ("COMPLETE", "CANCELLED", "REJECTED"):
                    logger.info(f"✅ [CHASE EXIT] Order {order_id} resolved (Status: {order['status'] if order else 'N/A'})")
                    return order_id

                # Not filled. Update price slightly worse
                tick = self.streamer.latest_ticks.get(token)
                if not tick or "depth" not in tick: continue

                # If we are BUYING to cover, we must pay the ASK. If SELLING, hit the BID.
                worse_price = tick["depth"]["sell"][0]["price"] if side == "BUY" else tick["depth"]["buy"][0]["price"]

                if worse_price != order["price"]:
                    logger.info(f"⚡ [CHASE EXIT] Modifying {order_id} to new price {worse_price}")
                    _api_executor.execute(self.kite.modify_order,
                        variety=self.kite.VARIETY_REGULAR,
                        order_id=order_id,
                        price=worse_price
                    )
                attempts += 1
            except Exception as e:
                logger.error(f"❌ [CHASE EXIT] Attempt {attempts} failed: {e}")
                break

        # Final fallback to market
        try:
            orders = _api_executor.execute(self.kite.orders)
            order = next((o for o in orders if o["order_id"] == order_id), None)
            if order and order["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
                logger.warning(f"⏰ [CHASE EXIT] Timeout reached for {order_id}. Converting to MARKET.")
                _api_executor.execute(self.kite.modify_order,
                    variety=self.kite.VARIETY_REGULAR,
                    order_id=order_id,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )
        except: pass
        return order_id

    def close_position(self, order_id: str):
        pos = self.position_manager.close_position(order_id)
        if not pos: return None
        if isinstance(pos, SingleLegPosition):
            if pos.sl_order_id:
                try: _api_executor.execute(self.kite.cancel_order, "regular", pos.sl_order_id)
                except: pass
            self._chase_exit_order(pos.symbol, "SELL" if pos.transaction_type == "BUY" else "BUY", pos.quantity, pos.instrument_token)
            return pos
        elif isinstance(pos, SpreadPosition):
            # For spreads, close concurrently or sequentially via chase
            self._chase_exit_order(pos.buy_leg.symbol, "SELL", pos.buy_leg.quantity, pos.buy_leg.instrument_token)
            self._chase_exit_order(pos.sell_leg.symbol, "BUY", pos.sell_leg.quantity, pos.sell_leg.instrument_token)
            return pos
        return None
        if isinstance(pos, SingleLegPosition):
            if pos.sl_order_id:
                try: _api_executor.execute(self.kite.cancel_order, "regular", pos.sl_order_id)
                except: pass
            self.place_order(pos.symbol, "SELL" if pos.transaction_type == "BUY" else "BUY", pos.quantity)
            return pos
        elif isinstance(pos, SpreadPosition):
            self.place_order(pos.buy_leg.symbol, "SELL", pos.buy_leg.quantity)
            self.place_order(pos.sell_leg.symbol, "BUY", pos.sell_leg.quantity)
            return pos
        return None

    def _handle_order_update(self, data: dict):
        """
        Institutional Order State Machine.
        Reacts instantly to broker callbacks without polling.
        """
        order_id = data.get("order_id")
        status   = data.get("status")
        symbol   = data.get("tradingsymbol")
        
        if not order_id: return

        # Persist status
        self.state_db.update_working_order_status(order_id, status)

        if status == "COMPLETE":
            fill_price = data.get("average_price", 0.0)
            logger.info(f"🎯 ORDER FILL: {symbol} @ {fill_price} (ID: {order_id})")
            # Update local PnL/TCA immediately
            self.risk_manager.release_shadow_margin(order_id)
            
        elif status in ("REJECTED", "CANCELLED"):
            reason = data.get("status_message", "N/A")
            logger.error(f"❌ ORDER {status}: {symbol} - {reason}")
            self.risk_manager.release_shadow_margin(order_id)
            # If a leg of a spread fails, we must trigger emergency rollback
            self._handle_leg_failure(order_id, symbol)

    def _handle_leg_failure(self, order_id: str, symbol: str):
        """Emergency logic for failed multi-leg entries."""
        for pos in self.position_manager.get_open_positions():
            if isinstance(pos, SpreadPosition):
                if pos.order_id == order_id or pos.buy_leg.symbol == symbol:
                    logger.critical(f"☢️ SPREAD LEG FAILED: Nuking entire position {pos.order_id}")
                    self.close_position(pos.order_id)

    def square_off_all(self):
        closed_positions = []
        for pos in self.position_manager.get_open_positions():
            closed_pos = self.close_position(pos.order_id)
            if closed_pos:
                closed_positions.append(closed_pos)
        return closed_positions

    def modify_sl_order(self, sl_order_id: str, new_trigger: float, new_limit: float = None):
        """
        Dynamically modifies an existing SL/SL-M order.
        Used for trailing stop-losses and move-to-breakeven logic.
        """
        if not sl_order_id: return False
        
        if self.paper_trade or not self.kite:
            logger.info(f"📝 [PAPER] Modifying SL {sl_order_id} -> Trigger: {new_trigger} | Limit: {new_limit}")
            return True

        try:
            # Variety is almost always 'regular' for standard retail/broker orders
            _api_executor.execute(self.kite.modify_order,
                variety=self.kite.VARIETY_REGULAR,
                order_id=sl_order_id,
                trigger_price=new_trigger,
                price=new_limit or new_trigger
            )
            logger.info(f"✅ SL Order Modified: {sl_order_id} -> Trigger: {new_trigger}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to modify SL {sl_order_id}: {e}")
            return False

    def get_live_positions_from_broker(self) -> list:
        """
        Fetches 'net' positions from Zerodha Kite.
        Used for institutional state sync at startup and morning routine.
        """
        if self.paper_trade or not self.kite:
            return None
            
        try:
            pos = _api_executor.execute(self.kite.positions, )
            return pos.get("net", []) if pos else []
        except Exception as e:
            logger.error(f"❌ Failed to sync broker positions: {e}")
            return None

    def resume_working_orders(self):
        """Called on startup to resume any interrupted limit chases."""
        working_orders = self.state_db.get_all_working_orders()
        if not working_orders:
            logger.info("✅ No active working orders to resume.")
            return

        logger.warning(f"🔄 Found {len(working_orders)} interrupted working orders. Attempting to resume/cancel.")
        for order in working_orders:
            order_id = order["order_id"]
            logger.info(f"🔄 Resuming tracking for order {order_id} ({order['symbol']})")
            # In a full implementation, you'd re-spawn a thread to continue the `_chase_exit_order` loop here.
            # For safety on startup, we usually just cancel hanging limits and let the position manager re-evaluate.
            if not self.paper_trade and self.kite:
                try:
                    _api_executor.execute(self.kite.cancel_order, "regular", order_id)
                    logger.info(f"✅ Cancelled hanging order {order_id} on startup for safety.")
                    self.state_db.update_working_order_status(order_id, "CANCELLED")
                except Exception as e:
                    logger.error(f"❌ Failed to cancel hanging order {order_id} on startup: {e}")
