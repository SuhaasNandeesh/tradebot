"""
Statistical Arbitrage (Pairs Trading) Strategy

Trades the mean-reverting spread between NIFTY and BANKNIFTY.
Institutional edge: Instead of relying on single-asset retail indicators, 
it calculates the Ratio (BANKNIFTY / NIFTY) and tracks its Bollinger Bands (Z-Score).
"""
import logging
import pandas as pd
from src.strategies.base import BaseStrategy
from src.data.market_data import compute_rsi

logger = logging.getLogger(__name__)

class StatArbPairsStrategy(BaseStrategy):
    name = "StatArb_Pairs"
    description = "Trades NIFTY/BANKNIFTY ratio mean-reversion via 2-SD Z-Score divergence"

    def __init__(self, data_provider=None):
        super().__init__()
        self.data = data_provider
        self.lookback = 20
        self.z_threshold = 2.0

    def _compute_signal(self, df_5m: pd.DataFrame, df_15m: pd.DataFrame, df_1hr: pd.DataFrame, **kwargs) -> tuple[str, str]:
        if not self.data:
            return "HOLD", "Data provider not initialized"
            
        bn_df = self.data.get_ohlcv("BANKNIFTY", "5minute", days=1)
        if bn_df.empty or len(bn_df) < self.lookback:
            return "HOLD", "Insufficient BANKNIFTY data"
            
        aligned = pd.DataFrame({
            "NIFTY": df_5m["close"],
            "BANKNIFTY": bn_df["close"]
        }).dropna()
        
        if len(aligned) < self.lookback:
            return "HOLD", "Insufficient aligned NIFTY/BANKNIFTY data"
            
        aligned["ratio"] = aligned["BANKNIFTY"] / aligned["NIFTY"]
        ratio_ma = aligned["ratio"].rolling(window=self.lookback).mean()
        ratio_std = aligned["ratio"].rolling(window=self.lookback).std()
        aligned["z_score"] = (aligned["ratio"] - ratio_ma) / (ratio_std.replace(0, 1e-9))
        
        current_z = aligned["z_score"].iloc[-1]
        
        # ── MTF Filter: 15m RSI Confirmation ──────────────────────────────────
        if df_15m is not None and len(df_15m) >= 14:
            rsi_15m = compute_rsi(df_15m["close"], 14).iloc[-1]
            
            if current_z > self.z_threshold and rsi_15m < 70:
                reason = f"StatArb BULLISH | Z-Score={current_z:.2f}, 15m RSI={rsi_15m:.1f}"
                return "BUY_CE", reason
            elif current_z < -self.z_threshold and rsi_15m > 30:
                reason = f"StatArb BEARISH | Z-Score={current_z:.2f}, 15m RSI={rsi_15m:.1f}"
                return "BUY_PE", reason

            if abs(current_z) > self.z_threshold:
                fail_reason = f"Z-Score ({current_z:.2f}) extreme but 15m RSI ({rsi_15m:.1f}) overextended"
            else:
                fail_reason = f"Z-Score ({current_z:.2f}) within threshold (+/-{self.z_threshold})"
        else:
            fail_reason = "Waiting for Z-Score divergence or 15m data"
                
        return "HOLD", fail_reason
