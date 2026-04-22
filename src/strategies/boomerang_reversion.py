"""
Strategy 5: Boomerang (Range Mean Reversion)
Designed for: 11:30–13:30 Midday Slump (Low ADX / Ranging)

Logic:
  1. ADX < 20 (Confirms NO trend)
  2. Price touches/exceeds Bollinger Band (20, 2.5)
  3. RSI < 35 (Oversold) for BUY_CE or RSI > 65 (Overbought) for BUY_PE
  4. Exits on touch of Bollinger Mid-line (Basis)
"""
import logging
import pandas as pd
from src.strategies.base import BaseStrategy
from src.data.market_data import compute_bollinger_bands, compute_rsi, compute_adx

logger = logging.getLogger(__name__)

class BoomerangStrategy(BaseStrategy):
    def __init__(self, name="Boomerang_Reversion"):
        super().__init__(name)

    def _compute_signal(self, df_5m: pd.DataFrame, df_15m: pd.DataFrame = None, df_1hr: pd.DataFrame = None, **kwargs) -> tuple[str, str]:
        if len(df_5m) < 20: return "HOLD", "Insufficient data"
        
        adx_series = compute_adx(df_5m, 14)
        if adx_series.empty: return "HOLD", "ADX calculation failed"
        
        adx = adx_series.iloc[-1]
        if adx > 22: return "HOLD", f"Market too trending (ADX={adx:.1f})"

        bb = compute_bollinger_bands(df_5m["close"], 20, 2.5)
        rsi_series = compute_rsi(df_5m["close"], 14)
        
        if bb.empty or rsi_series.empty:
            return "HOLD", "Indicators failed"

        cur_close = df_5m["close"].iloc[-1]
        rsi = rsi_series.iloc[-1]

        # NaN guard
        if any(v != v for v in [cur_close, rsi, bb["lower"].iloc[-1], bb["upper"].iloc[-1]]):
            return "HOLD", "NaN detected in data/indicators"

        # Oversold Reversion
        if cur_close <= bb["lower"].iloc[-1] and rsi < 35:
            reason = f"Oversold Mean Reversion | RSI={rsi:.1f} | Price={cur_close:.2f}"
            logger.info(f"[Boomerang] {reason}")
            return "BUY_CE", reason
        
        # Overbought Reversion
        if cur_close >= bb["upper"].iloc[-1] and rsi > 65:
            reason = f"Overbought Mean Reversion | RSI={rsi:.1f} | Price={cur_close:.2f}"
            logger.info(f"[Boomerang] {reason}")
            return "BUY_PE", reason

        return "HOLD", "No reversal signal"
