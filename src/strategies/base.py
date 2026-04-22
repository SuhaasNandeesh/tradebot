"""
BaseStrategy — Abstract contract all institutional strategies must implement.

Every strategy has:
  - generate_signal(df_5m, df_15m, df_1hr) → 'BUY_CE' | 'BUY_PE' | 'HOLD'
  - name and description for logging/Telegram
  - internal session state tracking (last signal, entry time)
  - time gate: no trades before 9:30 AM or after 14:30 PM IST
  - VIX gate: no option buying when VIX > 20
"""
import logging
import pandas as pd
from datetime import datetime
from abc import ABC, abstractmethod

from src.core.env_config import config

logger = logging.getLogger(__name__)

MARKET_OPEN_GRACE  = 9 * 60 + 30   # 9:30 AM (skip first 15 min whipsaw)
MARKET_CUTOFF      = 14 * 60 + 30  # 2:30 PM (theta decay / EOD risk)
VIX_BLOCK_LEVEL    = config.vix_threshold


class BaseStrategy(ABC):
    """Abstract base for all institutional intraday strategies."""

    name: str = "BaseStrategy"
    description: str = ""

    def __init__(self, name: str = None, description: str = None):
        if name: self.name = name
        if description: self.description = description
        self.last_signal   = "HOLD"    # Last emitted signal
        self.entry_time    = None      # Timestamp of last entry
        self.trade_count   = 0         # Total signals fired this session
        self.session_wins  = 0
        self.session_losses = 0
        self.consecutive_losses = 0    # Anti-revenge tracker

    # ── Time & VIX Gates ──────────────────────────────────────────────────────

    def _is_valid_trading_time(self) -> bool:
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        return MARKET_OPEN_GRACE <= current_minutes <= MARKET_CUTOFF

    def _is_vix_safe(self, df: pd.DataFrame) -> bool:
        """Return False if VIX column exists and is above threshold."""
        if "vix" in df.columns:
            vix = df["vix"].dropna().iloc[-1] if not df["vix"].dropna().empty else None
            if vix and vix > VIX_BLOCK_LEVEL:
                return False
        return True

    # ── Warmup Guard ─────────────────────────────────────────────────────────

    def _has_enough_data(self, df: pd.DataFrame, min_bars: int = 30) -> bool:
        return df is not None and len(df) >= min_bars

    # ── Public Entry Point ────────────────────────────────────────────────────

    def generate_signal(self, df_5m: pd.DataFrame,
                        df_15m: pd.DataFrame = None,
                        df_1hr: pd.DataFrame = None,
                        **kwargs) -> tuple[str, str]:
        """
        Multi-timeframe signal generation entry point.
        Returns: ('BUY_CE' | 'BUY_PE' | 'HOLD', 'Reason message')
        """
        # ── Gate 1: Time check ────────────────────────────────────────────────
        if not self._is_valid_trading_time():
            return "HOLD", "Outside market hours"

        # ── Gate 2: Warmup check ──────────────────────────────────────────────
        if not self._has_enough_data(df_5m, min_bars=30):
            return "HOLD", "Warming up: not enough data"

        # ── Gate 3: VIX check ────────────────────────────────────────────────
        if not self._is_vix_safe(df_5m):
            return "HOLD", f"VIX above {VIX_BLOCK_LEVEL}"

        # ── Gate 4: Already in a trade — wait for exit before new entry ───────
        if self.last_signal != "HOLD":
            return "HOLD", "Strategy already has active signal"

        # ── Delegate to strategy logic ────────────────────────────────────────
        try:
            signal, reason = self._compute_signal(df_5m, df_15m, df_1hr, **kwargs)
        except Exception as e:
            logger.error(f"[{self.name}] compute_signal error: {e}")
            signal, reason = "HOLD", f"Error: {str(e)}"

        if signal != "HOLD":
            self.last_signal  = signal
            self.entry_time   = datetime.now()
            self.trade_count += 1
            logger.info(f"[{self.name}] Signal: {signal} | Reason: {reason}")

        return signal, reason

    def on_trade_closed(self, pnl: float):
        """
        Call this after a trade is closed to update session stats.
        Anti-revenge: strategy reports consecutive_losses for the selector.
        """
        self.last_signal = "HOLD"  # Ready for next trade
        self.entry_time  = None

        if pnl > 0:
            self.session_wins += 1
            self.consecutive_losses = 0
        else:
            self.session_losses += 1
            self.consecutive_losses += 1

        win_rate = self.session_wins / max(self.trade_count, 1)
        logger.info(
            f"[{self.name}] Trade closed. PnL=₹{pnl:.2f} | "
            f"Session: {self.session_wins}W/{self.session_losses}L | "
            f"Consecutive losses: {self.consecutive_losses}"
        )

    def reset_session(self):
        """Reset all session state at market open."""
        self.last_signal         = "HOLD"
        self.entry_time          = None
        self.trade_count         = 0
        self.session_wins        = 0
        self.session_losses      = 0
        self.consecutive_losses  = 0

    def get_stats(self) -> dict:
        return {
            "name": self.name,
            "trade_count": self.trade_count,
            "wins": self.session_wins,
            "losses": self.session_losses,
            "consecutive_losses": self.consecutive_losses,
            "win_rate": round(self.session_wins / max(self.trade_count, 1), 2),
        }

    # ── To Implement ─────────────────────────────────────────────────────────

    def _compute_signal(self, df_5m: pd.DataFrame,
                         df_15m: pd.DataFrame,
                         df_1hr: pd.DataFrame,
                         **kwargs) -> tuple[str, str]:
        """Core strategy logic. Must return ('BUY_CE' | 'BUY_PE' | 'HOLD', 'Reason message')."""
        ...
