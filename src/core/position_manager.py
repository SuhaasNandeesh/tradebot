import logging
import threading
from datetime import datetime, date
from typing import Optional, List
import os

from src.memory.state_db import StateDB
from src.core.env_config import config

logger = logging.getLogger(__name__)

# Trailing SL configuration
TRAIL_SL_BREAKEVEN_TRIGGER = config.trail_sl_breakeven_pct
TRAIL_SL_TRAIL_TRIGGER     = config.trail_sl_trail_pct
TRAIL_SL_TRAIL_LOCK_PCT    = config.trail_sl_lock_pct
EXPIRY_ACCELERATE_PCT      = config.expiry_early_exit_pct
MAX_HOLD_MINUTES           = config.max_hold_minutes
STAGNANT_THRESHOLD_PCT     = config.stagnant_threshold_pct

class PositionLeg:
    """A single leg of a multi-leg position."""
    def __init__(self, symbol: str, instrument_token: int, transaction_type: str, quantity: int, entry_price: float):
        self.symbol = symbol
        self.instrument_token = instrument_token
        self.transaction_type = transaction_type
        self.quantity = quantity
        self.entry_price = entry_price
        self.current_price = entry_price

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "instrument_token": self.instrument_token,
            "transaction_type": self.transaction_type,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price
        }

class Position:
    """Base class for Single and Multi-leg positions (Atomic Risk Objects)."""
    def __init__(self, order_id: str, instrument: str, strategy_id: str = None):
        self.order_id = order_id
        self.instrument = instrument
        self.strategy_id = strategy_id
        self.entry_time = datetime.now()
        self.is_open = True
        self.sl_order_id = None
        
    @property
    def unrealized_pnl(self) -> float:
        raise NotImplementedError

    @property
    def pnl_percent(self) -> float:
        raise NotImplementedError

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "instrument": self.instrument,
            "strategy_id": self.strategy_id,
            "entry_time": self.entry_time.isoformat() if isinstance(self.entry_time, datetime) else self.entry_time,
            "sl_order_id": self.sl_order_id
        }

class SingleLegPosition(Position):
    def __init__(self, symbol: str, instrument: str, transaction_type: str,
                 quantity: int, entry_price: float, stop_loss: float,
                 target: float, order_id: str, instrument_token: int,
                 strategy_id: str = None, sl_order_id: str = None):
        super().__init__(order_id, instrument, strategy_id)
        self.symbol = symbol
        self.transaction_type = transaction_type
        self.quantity = quantity
        self.entry_price = entry_price
        self.current_price = entry_price
        self.stop_loss = stop_loss
        self.original_sl = stop_loss
        self.target = target
        self.instrument_token = instrument_token
        self.sl_order_id = sl_order_id
        self.breakeven_set = False
        self.trail_locked = False

    @property
    def unrealized_pnl(self) -> float:
        multiplier = 1 if self.transaction_type == "BUY" else -1
        return multiplier * (self.current_price - self.entry_price) * self.quantity

    @property
    def pnl_percent(self) -> float:
        if self.entry_price == 0: return 0.0
        return (self.unrealized_pnl / (self.entry_price * self.quantity)) * 100

    def to_dict(self):
        d = super().to_dict()
        d.update({
            "type": "SINGLE",
            "symbol": self.symbol,
            "instrument_token": self.instrument_token,
            "transaction_type": self.transaction_type,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "original_sl": self.original_sl,
            "target": self.target,
            "breakeven_set": self.breakeven_set,
            "trail_locked": self.trail_locked
        })
        return d

class SpreadPosition(Position):
    """Institutional Spread Object: SL/Target based on Net Premium."""
    def __init__(self, buy_leg: PositionLeg, sell_leg: PositionLeg, 
                 instrument: str, stop_loss: float, target: float, 
                 order_id: str, strategy_id: str = None, sl_order_id: str = None):
        super().__init__(order_id, instrument, strategy_id)
        self.buy_leg = buy_leg
        self.sell_leg = sell_leg
        self.stop_loss = stop_loss
        self.target = target
        self.sl_order_id = sl_order_id

    @property
    def net_premium(self) -> float:
        return self.buy_leg.current_price - self.sell_leg.current_price

    @property
    def entry_premium(self) -> float:
        return self.buy_leg.entry_price - self.sell_leg.entry_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.net_premium - self.entry_premium) * self.buy_leg.quantity

    @property
    def pnl_percent(self) -> float:
        if self.entry_premium == 0: return 0.0
        return (self.unrealized_pnl / (self.entry_premium * self.buy_leg.quantity)) * 100

    def to_dict(self):
        return {
            "type": "SPREAD",
            "order_id": self.order_id,
            "buy_leg": self.buy_leg.to_dict(),
            "sell_leg": self.sell_leg.to_dict(),
            "instrument": self.instrument,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "strategy_id": self.strategy_id,
            "sl_order_id": self.sl_order_id,
            "entry_time": self.entry_time.isoformat() if isinstance(self.entry_time, datetime) else self.entry_time
        }

import queue

class PositionManager:
    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._lock = threading.Lock()
        self.sl_modify_callback = None
        self.db = StateDB()
        self._db_queue = queue.Queue()
        self._db_worker_thread = threading.Thread(target=self._db_worker, daemon=True)
        self._db_worker_thread.start()
        self.load_from_db()
        self.current_vix = 15.0

    def _db_worker(self):
        """Background thread to handle database I/O without blocking ticker."""
        while True:
            try:
                task = self._db_queue.get()
                if task is None: break
                
                action, data = task
                if action == "UPSERT":
                    self.db.upsert_position(data)
                elif action == "DELETE":
                    self.db.delete_position(data)
                
                self._db_queue.task_done()
            except Exception as e:
                logger.error(f"Error in PositionManager DB Worker: {e}")

    def update_volatility_regime(self, vix: float):
        self.current_vix = vix

    def _get_vol_multiplier(self) -> float:
        if self.current_vix > 25: return 1.5
        if self.current_vix > 20: return 1.2
        if self.current_vix < 12: return 0.8
        return 1.0

    def load_from_db(self):
        with self._lock:
            rows = self.db.load_all_positions()
            for r in rows:
                try:
                    pos_type = r.get("type", "SINGLE")
                    if pos_type == "SPREAD":
                        buy_leg_data = r["buy_leg"]
                        sell_leg_data = r["sell_leg"]
                        
                        buy_leg = PositionLeg(
                            symbol=buy_leg_data["symbol"],
                            instrument_token=buy_leg_data["instrument_token"],
                            transaction_type=buy_leg_data["transaction_type"],
                            quantity=buy_leg_data["quantity"],
                            entry_price=buy_leg_data["entry_price"]
                        )
                        buy_leg.current_price = buy_leg_data.get("current_price", buy_leg.entry_price)
                        
                        sell_leg = PositionLeg(
                            symbol=sell_leg_data["symbol"],
                            instrument_token=sell_leg_data["instrument_token"],
                            transaction_type=sell_leg_data["transaction_type"],
                            quantity=sell_leg_data["quantity"],
                            entry_price=sell_leg_data["entry_price"]
                        )
                        sell_leg.current_price = sell_leg_data.get("current_price", sell_leg.entry_price)
                        
                        pos = SpreadPosition(
                            buy_leg=buy_leg, sell_leg=sell_leg,
                            instrument=r["instrument"], stop_loss=r["stop_loss"],
                            target=r["target"], order_id=r["order_id"],
                            strategy_id=r.get("strategy_id"), sl_order_id=r.get("sl_order_id")
                        )
                        pos.entry_time = datetime.fromisoformat(r["entry_time"]) if isinstance(r["entry_time"], str) else r["entry_time"]
                        self._positions[pos.order_id] = pos
                        
                    else:
                        pos = SingleLegPosition(
                            symbol=r["symbol"], instrument=r["instrument"],
                            transaction_type=r["transaction_type"], quantity=r["quantity"],
                            entry_price=r["entry_price"], stop_loss=r["stop_loss"],
                            target=r["target"], order_id=r["order_id"],
                            instrument_token=r.get("instrument_token"),
                            strategy_id=r["strategy_id"], sl_order_id=r.get("sl_order_id")
                        )
                        pos.breakeven_set = r.get("breakeven_set", False)
                        pos.trail_locked = r.get("trail_locked", False)
                        pos.entry_time = datetime.fromisoformat(r["entry_time"]) if isinstance(r["entry_time"], str) else r["entry_time"]
                        self._positions[pos.order_id] = pos
                except Exception as e:
                    logger.error(f"Failed to reconstruct position {r.get('order_id')}: {e}")

    def sync_with_broker(self, kite_positions: list):
        # Remove early return: empty kite_positions is a valid state (0 positions)
        broker_qty = {kp["tradingsymbol"]: kp["quantity"] for kp in (kite_positions or [])}
        with self._lock:
            for order_id, pos in list(self._positions.items()):
                if isinstance(pos, SingleLegPosition):
                    if broker_qty.get(pos.symbol, 0) == 0:
                        logger.warning(f"⚠️ Position {pos.symbol} not found on broker. Removing from state.")
                        self._db_queue.put(("DELETE", order_id))
                        del self._positions[order_id]
                elif isinstance(pos, SpreadPosition):
                    # For spreads, both legs must exist with correct quantity
                    b_qty = broker_qty.get(pos.buy_leg.symbol, 0)
                    s_qty = broker_qty.get(pos.sell_leg.symbol, 0)
                    if b_qty == 0 or s_qty == 0:
                        logger.warning(f"⚠️ Spread leg for {pos.order_id} missing on broker. Removing from state.")
                        self._db_queue.put(("DELETE", order_id))
                        del self._positions[order_id]

    def add_position(self, position: Position):
        logger.info(f"⚡ [PositionManager] Adding position {position.order_id}")
        with self._lock:
            self._positions[position.order_id] = position
        self._db_queue.put(("UPSERT", position.to_dict()))

    def update_prices(self, live_ticks: dict) -> list[dict]:
        exit_signals = []
        # Create a snapshot of positions to minimize lock time
        with self._lock:
            positions_snapshot = list(self._positions.values())
        
        for pos in positions_snapshot:
            if isinstance(pos, SingleLegPosition):
                tick = live_ticks.get(pos.instrument_token)
                if tick:
                    pos.current_price = tick["last_price"]
                    self._update_trailing_sl(pos)
                    reason = self._check_exit(pos)
                    if reason:
                        exit_signals.append({"order_id": pos.order_id, "symbol": pos.symbol, "reason": reason, "quantity": pos.quantity})
            elif isinstance(pos, SpreadPosition):
                t_buy = live_ticks.get(pos.buy_leg.instrument_token)
                t_sell = live_ticks.get(pos.sell_leg.instrument_token)
                if t_buy and t_sell:
                    pos.buy_leg.current_price = t_buy["last_price"]
                    pos.sell_leg.current_price = t_sell["last_price"]
                    reason = self._check_exit(pos)
                    if reason:
                        exit_signals.append({"order_id": pos.order_id, "symbol": pos.buy_leg.symbol, "reason": reason, "is_spread": True})
        return exit_signals

    def _update_trailing_sl(self, pos: SingleLegPosition):
        if pos.transaction_type != "BUY": return
        profit_pct = (pos.current_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
        # 1. Breakeven
        if profit_pct >= TRAIL_SL_BREAKEVEN_TRIGGER and not pos.breakeven_set:
            new_sl = pos.entry_price
            pos.stop_loss = new_sl
            pos.breakeven_set = True
            logger.info(f"🔄 Positioning Moving to Breakeven for {pos.symbol} @ {new_sl}")
            self._db_queue.put(("UPSERT", pos.to_dict()))
            if self.sl_modify_callback and pos.sl_order_id:
                # 1% buffer for SELL limit (assuming BUY position SL is a SELL order)
                limit = round(new_sl * 0.99, 1)
                # Note: sl_modify_callback might still be synchronous network I/O
                threading.Thread(target=self.sl_modify_callback, args=(pos.sl_order_id, new_sl, limit), daemon=True).start()

        # 2. Dynamic Trailing
        if profit_pct >= TRAIL_SL_TRAIL_TRIGGER:
            # Lock in profits: SL = Entry + (Profit * Lock%)
            locked_profit = (pos.current_price - pos.entry_price) * TRAIL_SL_TRAIL_LOCK_PCT
            new_sl = round(pos.entry_price + locked_profit, 2)
            
            if new_sl > pos.stop_loss:
                pos.stop_loss = new_sl
                pos.trail_locked = True
                logger.info(f"📈 Trailing SL Locked for {pos.symbol} @ {new_sl} (Profit: {profit_pct*100:.1f}%)")
                self._db_queue.put(("UPSERT", pos.to_dict()))
                if self.sl_modify_callback and pos.sl_order_id:
                    limit = round(new_sl * 0.99, 1)
                    threading.Thread(target=self.sl_modify_callback, args=(pos.sl_order_id, new_sl, limit), daemon=True).start()

    def _check_exit(self, pos: Position) -> Optional[str]:
        vol_mult = self._get_vol_multiplier()
        if isinstance(pos, SingleLegPosition):
            if pos.transaction_type == "BUY":
                if pos.current_price <= pos.stop_loss: return "STOP_LOSS"
                if pos.current_price >= pos.target: return "TARGET"
            else:
                if pos.current_price >= pos.stop_loss: return "STOP_LOSS"
                if pos.current_price <= pos.target: return "TARGET"
        elif isinstance(pos, SpreadPosition):
            if pos.net_premium <= pos.stop_loss: return "SPREAD_SL"
            if pos.net_premium >= pos.target: return "SPREAD_TARGET"
        return None

    def close_position(self, order_id: str) -> Optional[Position]:
        with self._lock:
            pos = self._positions.pop(order_id, None)
            if pos:
                self._db_queue.put(("DELETE", order_id))
            return pos

    def has_open_positions(self) -> bool:
        with self._lock: return len(self._positions) > 0

    def get_total_unrealized_pnl(self) -> float:
        with self._lock: return sum(p.unrealized_pnl for p in self._positions.values())

    def get_open_positions(self) -> List[Position]:
        with self._lock: return list(self._positions.values())

    def get_summary(self) -> str:
        """Returns a Markdown-formatted summary of all open positions for Telegram."""
        with self._lock:
            if not self._positions:
                return "📭 **No open positions.**"
            
            lines = ["📦 **Active Institutional Book**\n"]
            total_unrealized = 0
            
            for pos in self._positions.values():
                pnl = pos.unrealized_pnl
                total_unrealized += pnl
                pnl_icon = "🟢" if pnl >= 0 else "🔴"
                
                if isinstance(pos, SingleLegPosition):
                    sym_escaped = pos.symbol.replace("_", "\\_")
                    lines.append(
                        f"{pnl_icon} **{sym_escaped}** ({pos.transaction_type})\n"
                        f"  LTP: `{pos.current_price:.2f}` | Entry: `{pos.entry_price:.2f}`\n"
                        f"  PnL: **₹{pnl:.2f}** ({pos.pnl_percent:.2f}%)\n"
                        f"  SL: `{pos.stop_loss:.2f}` | Tgt: `{pos.target:.2f}`\n"
                    )
                elif isinstance(pos, SpreadPosition):
                    sym_escaped = pos.buy_leg.symbol.replace("_", "\\_")
                    lines.append(
                        f"{pnl_icon} **{sym_escaped} SPREAD**\n"
                        f"  Net Prem: `{pos.net_premium:.2f}` | Entry: `{pos.entry_premium:.2f}`\n"
                        f"  PnL: **₹{pnl:.2f}** ({pos.pnl_percent:.2f}%)\n"
                    )
            
            summary_icon = "💰" if total_unrealized >= 0 else "📉"
            lines.append(f"\n{summary_icon} **Total Unrealized PnL: ₹{total_unrealized:.2f}**")
            return "\n".join(lines)
