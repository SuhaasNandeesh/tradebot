"""
Strategy 5: Mid-Day Range Mean Reversion (The Boomerang)

Market regime: NEUTRAL or RANGING days. 
Targeting: 11:30 AM – 1:30 PM (Mid-Day Slump).

Logic:
  - Price stretches beyond 2 Standard Deviations (Bollinger Bands).
  - RSI confirms overbought (>70) or oversold (<30).
  - Target: Return to VWAP (the "Mean").
  - Time filter: Only active when volume is low (mid-day).
"""
import logging
import pandas as pd
from datetime import time as dtime, datetime
from src.strategies.base import BaseStrategy
from src.data.market_data import compute_vwap, compute_rsi, compute_adx

logger = logging.getLogger(__name__)

SLUMP_START = dtime(11, 30)
SLUMP_END   = dtime(13, 30)
ADX_MAX_FOR_RANGE = 20.0  # If ADX > 20, the market is trending; don't mean-revert.

class RangeMeanReversionStrategy(BaseStrategy):
    def __init__(self, name="Range_Mean_Reversion"):
        super().__init__(name)

    def _compute_bb(self, series: pd.Series, period: int = 20, std: float = 2.0):
        """Bollinger Bands Upper/Lower extraction."""
        sma = series.rolling(window=period).mean()
        std_dev = series.rolling(window=period).std()
        upper = sma + (std_dev * std)
        lower = sma - (std_dev * std)
        return upper, lower

    def _compute_signal(self, df_5m: pd.DataFrame,
                         df_15m: pd.DataFrame = None,
                         df_1hr: pd.DataFrame = None,
                         **kwargs) -> tuple[str, str]:
        
        current_time = datetime.now().time()
        if not (SLUMP_START <= current_time <= SLUMP_END):
            return "HOLD", f"Outside mid-day slump hours ({SLUMP_START}-{SLUMP_END})"

        if len(df_5m) < 30:
            return "HOLD", "Insufficient 5m data (need 30+ bars)"
            
        if df_15m is None or len(df_15m) < 20:
            return "HOLD", "Insufficient 15m data (need 20+ bars)"

        # ── MTF Filter: 15m Trend Strength ───────────────────────────────────
        adx_15m = compute_adx(df_15m, 14).iloc[-1]
        if adx_15m > ADX_MAX_FOR_RANGE:
            return "HOLD", f"Market is trending on 15m (ADX={adx_15m:.1f} > {ADX_MAX_FOR_RANGE})"

        # ── MTF Filter: 1hr Volatility check ─────────────────────────────────
        if df_1hr is not None and len(df_1hr) >= 20:
            upper_1h, lower_1h = self._compute_bb(df_1hr["close"])
            cur_1h = df_1hr["close"].iloc[-1]
            if cur_1h > upper_1h.iloc[-1]:
                return "HOLD", "1hr Breakout detected (Bullish)"
            elif cur_1h < lower_1h.iloc[-1]:
                return "HOLD", "1hr Breakout detected (Bearish)"

        close = df_5m["close"]
        upper, lower = self._compute_bb(close)
        vwap = compute_vwap(df_5m)
        rsi = compute_rsi(close, 14)

        cur_close = close.iloc[-1]
        cur_upper = upper.iloc[-1]
        cur_lower = lower.iloc[-1]
        cur_vwap  = vwap.iloc[-1]
        cur_rsi   = rsi.iloc[-1]

        # ── BULLISH: Oversold + Below Lower BB ────────────────────────────────
        if cur_close < cur_lower and cur_rsi < 30:
            reason = f"Mean Reversion BULLISH | close={cur_close:.2f} < LowerBB={cur_lower:.2f}, RSI={cur_rsi:.1f}"
            return "BUY_CE", reason

        # ── BEARISH: Overbought + Above Upper BB ──────────────────────────────
        if cur_close > cur_upper and cur_rsi > 70:
            reason = f"Mean Reversion BEARISH | close={cur_close:.2f} > UpperBB={cur_upper:.2f}, RSI={cur_rsi:.1f}"
            return "BUY_PE", reason

        # Detailed HOLD reason
        if cur_close < cur_lower:
            fail_reason = f"Price below Lower BB but RSI ({cur_rsi:.1f}) not oversold"
        elif cur_close > cur_upper:
            fail_reason = f"Price above Upper BB but RSI ({cur_rsi:.1f}) not overbought"
        else:
            fail_reason = "Price within Bollinger Bands"

        return "HOLD", fail_reason
