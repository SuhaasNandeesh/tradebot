"""
Strategy 2: VWAP Momentum

Market regime: Works best in trending markets with clear institutional participation.
Especially effective on NIFTY which is heavily institutional.
"""
import logging
import pandas as pd
from src.strategies.base import BaseStrategy
from src.data.market_data import compute_ema, compute_rsi, compute_vwap, compute_adx

logger = logging.getLogger(__name__)

RSI_BULL_MIN = 52;  RSI_BULL_MAX = 70
RSI_BEAR_MIN = 30;  RSI_BEAR_MAX = 48
ADX_MIN_FOR_TREND = 20.0  # Must be trending to take momentum trade

class VWAPMomentumStrategy(BaseStrategy):
    def __init__(self, name="VWAP_Momentum"):
        super().__init__(name)

    def _compute_signal(self, df_5m: pd.DataFrame,
                         df_15m: pd.DataFrame = None,
                         df_1hr: pd.DataFrame = None,
                         futures_vwap: pd.Series = None,
                         aggression: dict = None,
                         **kwargs) -> tuple[str, str]:
        if len(df_5m) < 30:
            return "HOLD", "Insufficient data (need 30+ bars)"

        # ── Intelligence Layer: Aggression ───────────────────────────────────
        ignore_macro = aggression.get("ignore_macro_trend", False) if aggression else False

        # ── MTF Filter: 1hr Macro Slope ──────────────────────────────────────
        trend_1hr = 0
        if df_1hr is not None and len(df_1hr) >= 21:
            ema21_1hr = compute_ema(df_1hr["close"], 21)
            slope_1hr = (ema21_1hr.iloc[-1] - ema21_1hr.iloc[-5])
            if slope_1hr > 0:
                trend_1hr = 1
            elif slope_1hr < 0:
                trend_1hr = -1

        # ── MTF Filter: 15m Trend & ADX Quality ──────────────────────────────
        trend_15m = 0
        adx_15m = 0
        if df_15m is not None and len(df_15m) >= 21:
            ema9_15m  = compute_ema(df_15m["close"], 9).iloc[-1]
            ema21_15m = compute_ema(df_15m["close"], 21).iloc[-1]
            adx_15m   = compute_adx(df_15m, 14).iloc[-1]
            if ema9_15m > ema21_15m: trend_15m = 1
            elif ema9_15m < ema21_15m: trend_15m = -1
        
        if adx_15m < ADX_MIN_FOR_TREND:
            return "HOLD", f"ADX too low ({adx_15m:.1f} < {ADX_MIN_FOR_TREND})"

        # ── 5m Momentum Core ──
        cur_close = df_5m["close"].iloc[-1]
        vwap      = futures_vwap if (futures_vwap is not None and not futures_vwap.empty) else compute_vwap(df_5m)
        cur_vwap  = vwap.iloc[-1]
        cur_rsi   = compute_rsi(df_5m["close"]).iloc[-1]
        cur_ema9  = compute_ema(df_5m["close"], 9).iloc[-1]
        cur_ema21 = compute_ema(df_5m["close"], 21).iloc[-1]

        # ── BULLISH: all 3 factors + 15m not bearish ─────────────────────────
        bullish_macro = (trend_1hr >= 0 or ignore_macro)
        if (cur_close > cur_vwap and cur_ema9 > cur_ema21 and 
            RSI_BULL_MIN <= cur_rsi <= RSI_BULL_MAX and trend_15m >= 0 and bullish_macro):
            
            reason = f"VWAP Momentum BULLISH | RSI={cur_rsi:.1f}, 15m_trend={trend_15m}"
            return "BUY_CE", reason

        # ── BEARISH: all 3 factors + 15m not bullish ─────────────────────────
        bearish_macro = (trend_1hr <= 0 or ignore_macro)
        if (cur_close < cur_vwap and cur_ema9 < cur_ema21 and 
            RSI_BEAR_MIN <= cur_rsi <= RSI_BEAR_MAX and trend_15m <= 0 and bearish_macro):
            
            reason = f"VWAP Momentum BEARISH | RSI={cur_rsi:.1f}, 15m_trend={trend_15m}"
            return "BUY_PE", reason

        # Detailed HOLD reason
        if cur_close > cur_vwap and cur_ema9 > cur_ema21:
            if not (RSI_BULL_MIN <= cur_rsi <= RSI_BULL_MAX):
                fail_reason = f"RSI {cur_rsi:.1f} out of Bullish range ({RSI_BULL_MIN}-{RSI_BULL_MAX})"
            elif trend_15m < 0:
                fail_reason = "15m Trend is Bearish"
            elif not bullish_macro:
                fail_reason = "1hr Macro Trend is Bearish"
            else:
                fail_reason = "Unknown momentum stall"
        elif cur_close < cur_vwap and cur_ema9 < cur_ema21:
            if not (RSI_BEAR_MIN <= cur_rsi <= RSI_BEAR_MAX):
                fail_reason = f"RSI {cur_rsi:.1f} out of Bearish range ({RSI_BEAR_MIN}-{RSI_BEAR_MAX})"
            elif trend_15m > 0:
                fail_reason = "15m Trend is Bullish"
            elif not bearish_macro:
                fail_reason = "1hr Macro Trend is Bullish"
            else:
                fail_reason = "Unknown momentum stall"
        else:
            fail_reason = "Price/EMA/VWAP misalignment"

        return "HOLD", fail_reason
