"""
Strategy 1: Opening Range Breakout (ORB)

Market regime: Works best on high-conviction trending days (BULLISH/BEARISH with confidence > 65).
Avoid: Choppy/NEUTRAL days, major data release days.

Logic:
  - Define the "opening range" as the High/Low of the first 15 minutes of the session (9:15–9:30).
  - After 9:30 AM, wait for a clear break *with volume confirmation*.
  - Break above ORB High + volume > 1.5x avg  → BUY_CE (Breakout entry)
  - Break below ORB Low  + volume > 1.5x avg  → BUY_PE (Breakdown entry)
  - SL: re-entry back into range (i.e., price reverses below ORB High / above ORB Low)
  - Target: 1.5x the ORB range width from entry
  - Only fires ONCE per session (no re-entries on failed breakouts)

Multi-TF role:
  - 15m trend direction used to filter: only take CE breaks in 15m uptrend, PE breaks in 15m downtrend.
  - 5m bars used for ORB formation and break detection.

Why institutional:
  The first 15 minutes define where smart money placed their orders overnight.
  A break with volume confirms institutional participation, not retail noise.
"""
import logging
import pandas as pd
import numpy as np
from datetime import datetime, time as dtime
from src.strategies.base import BaseStrategy
from src.data.market_data import compute_ema

logger = logging.getLogger(__name__)

ORB_START  = dtime(9, 15)
ORB_END    = dtime(9, 30)
ORB_TRADE_START = dtime(9, 35) # Wait for initial volatility to settle
ORB_TRADE_END   = dtime(11, 00) # Only trade morning breakouts
VOLUME_MULTIPLIER = 2.0   # Institutional confirmation: 2x average volume

class ORBStrategy(BaseStrategy):
    def __init__(self, name="ORB"):
        super().__init__(name)
        self.orb_high = 0
        self.orb_low = 0
        self.orb_formed = False
        self.fired_today = False
        self.last_reset_date = None

    def _reset_if_new_session(self):
        today = datetime.now().date()
        if self.last_reset_date != today:
            self.orb_high = 0
            self.orb_low = 0
            self.orb_formed = False
            self.fired_today = False
            self.last_reset_date = today
            logger.info(f"[{self.name}] Session reset for {today}")

    def _build_orb(self, df_5m: pd.DataFrame):
        """Extract high/low from 9:15 to 9:30 AM."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        start_time = datetime.strptime(f"{today_str} 09:15", "%Y-%m-%d %H:%M")
        end_time = datetime.strptime(f"{today_str} 09:30", "%Y-%m-%d %H:%M")
        
        # Filter rows within ORB window
        mask = (df_5m.index >= start_time) & (df_5m.index <= end_time)
        orb_df = df_5m.loc[mask]
        
        if not orb_df.empty and len(orb_df) >= 3: # 3 bars of 5m = 15m
            self.orb_high = orb_df["high"].max()
            self.orb_low  = orb_df["low"].min()
            self.orb_formed = True
            logger.info(f"[ORB] Range Formed: {self.orb_low:.2f} - {self.orb_high:.2f}")

    def _get_15m_trend(self, df_15m: pd.DataFrame) -> int:
        if df_15m is None or len(df_15m) < 20:
            return 0
        ema9 = compute_ema(df_15m["close"], 9).iloc[-1]
        ema21 = compute_ema(df_15m["close"], 21).iloc[-1]
        if ema9 > ema21: return 1
        if ema9 < ema21: return -1
        return 0

    def _compute_signal(self, df_5m: pd.DataFrame,
                         df_15m: pd.DataFrame,
                         df_1hr: pd.DataFrame,
                         **kwargs) -> tuple[str, str]:
        self._reset_if_new_session()
        
        # Institutional Filter: Don't trade the very first candle break
        current_time = datetime.now().time()
        if current_time < ORB_TRADE_START:
            return "HOLD", f"Waiting for ORB trading window ({ORB_TRADE_START})"
        if current_time > ORB_TRADE_END:
            return "HOLD", f"ORB window expired ({ORB_TRADE_END})"

        # Only fires once per session
        if self.fired_today:
            return "HOLD", "ORB signal already fired today"

        # Build ORB if not yet formed
        if not self.orb_formed:
            self._build_orb(df_5m)
            if not self.orb_formed:
                return "HOLD", "ORB range not yet formed"

        current_close  = df_5m["close"].iloc[-1]
        
        # Volume confirmation: compare last bar vs 20-bar rolling average
        vol_avg = df_5m["volume"].rolling(20).mean().iloc[-1]
        vol_now = df_5m["volume"].iloc[-1]
        volume_confirmed = vol_now >= vol_avg * VOLUME_MULTIPLIER

        # 15m trend filter (avoid counter-trend breakouts)
        trend_15m = self._get_15m_trend(df_15m)

        orb_range = self.orb_high - self.orb_low
        if orb_range <= 0:
            return "HOLD", "Invalid ORB range (<= 0)"

        # Breakout: candle closes above ORB High (+ aligned with 15m uptrend or neutral)
        if current_close > self.orb_high:
            if trend_15m < 0:
                return "HOLD", "ORB Breakout blocked by Bearish 15m trend"
            if not volume_confirmed:
                return "HOLD", f"ORB Breakout lacks volume ({vol_now/vol_avg:.1f}x < {VOLUME_MULTIPLIER}x)"
            
            self.fired_today = True
            reason = f"ORB Breakout BULLISH | Close={current_close:.2f} > ORB_H={self.orb_high:.2f}, vol={vol_now/vol_avg:.1f}x"
            return "BUY_CE", reason

        # Breakdown: candle closes below ORB Low (+ aligned with 15m downtrend or neutral)
        if current_close < self.orb_low:
            if trend_15m > 0:
                return "HOLD", "ORB Breakdown blocked by Bullish 15m trend"
            if not volume_confirmed:
                return "HOLD", f"ORB Breakdown lacks volume ({vol_now/vol_avg:.1f}x < {VOLUME_MULTIPLIER}x)"
            
            self.fired_today = True
            reason = f"ORB Breakdown BEARISH | Close={current_close:.2f} < ORB_L={self.orb_low:.2f}, vol={vol_now/vol_avg:.1f}x"
            return "BUY_PE", reason

        return "HOLD", "Price within ORB range"
