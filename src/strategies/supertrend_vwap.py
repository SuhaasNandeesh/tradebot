"""
Strategy 4: Supertrend + VWAP Confluence (Primary Institutional Strategy)

Market regime: Works on both trending AND mildly volatile days.
Most robust of the 4 strategies — lowest false signal rate.

Logic (2 primary signals, both must agree):
  BULLISH:
    1. Supertrend(10,3) is BULLISH (trend = +1, price above lower band)
    2. Price > Session VWAP (institutional flow is bullish)
    Bonus filter: ADX > 20 (trend has enough strength — avoids whipsaw in ranging markets)
    → BUY_CE

  BEARISH:
    1. Supertrend(10,3) is BEARISH (trend = -1, price below upper band)
    2. Price < Session VWAP
    Bonus filter: ADX > 20
    → BUY_PE

  Multi-TF: 15m Supertrend must agree directionally with 5m signal.
  1hr EMA21 used as macro direction sanity check.

Why this is the "primary" strategy:
  Supertrend is ATR-based — it adapts to market volatility.
  In low-VIX environments it stays closer to price (tighter), in high-VIX it widens.
  Combined with VWAP (institutional consensus level), this is as close as you get
  to reading institutional flow without an order-flow tool.

  ADX > 20 filter: prevents chasing signals in choppy markets where
  Supertrend flips rapidly. Keeps the bot out of death-by-a-thousand-cuts situations.

R:R: SL at price level where Supertrend would flip (dynamic, ATR-based).
     Target: 1.5x SL distance (structured R:R built into indicator geometry).
"""
import logging
import pandas as pd
import numpy as np
from src.strategies.base import BaseStrategy
from src.data.market_data import (compute_ema, compute_vwap,
                                   compute_supertrend, compute_adx)

logger = logging.getLogger(__name__)

ADX_TREND_THRESHOLD = 25.0   # Institutional 'True Momentum' threshold

class SupertrendVWAPStrategy(BaseStrategy):
    # ... (rest of class) ...

    def _compute_signal(self, df_5m: pd.DataFrame,
                         df_15m: pd.DataFrame = None,
                         df_1hr: pd.DataFrame = None,
                         futures_vwap: pd.Series = None,
                         aggression: dict = None,
                         **kwargs) -> tuple[str, str]:
        if len(df_5m) < 35:
            return "HOLD", "Insufficient data (need 35+ bars)"

        # ── Compute indicators on 5m ──────────────────────────────────────────
        st_5m   = compute_supertrend(df_5m, period=10, multiplier=3.0)
        # Institutional Upgrade: Use Futures VWAP if provided and not empty
        vwap    = futures_vwap if (futures_vwap is not None and not futures_vwap.empty) else compute_vwap(df_5m)
        adx     = compute_adx(df_5m, period=14)
        ema20   = compute_ema(df_5m["close"], 20)

        cur_close     = df_5m["close"].iloc[-1]
        cur_trend_5m  = st_5m["trend"].iloc[-1]
        cur_vwap      = vwap.iloc[-1]
        cur_adx       = adx.iloc[-1]
        
        # EMA Slope for momentum confirmation
        slope = (ema20.iloc[-1] - ema20.iloc[-5]) / ema20.iloc[-5] * 100

        # ── Intelligence Layer: ADX Threshold ────────────────────────────────
        current_adx_threshold = ADX_TREND_THRESHOLD
        if aggression and (aggression.get("ignore_macro_trend") or aggression.get("wider_rsi")):
            current_adx_threshold = 10.0 # Relaxed for aggression

        # NaN guard
        if any(v != v for v in [cur_close, cur_vwap, cur_adx]):
            return "HOLD", "NaN data detected"

        # ── ADX Quality Filter (avoid whipsaw in ranging markets) ────────────
        if cur_adx < current_adx_threshold:
            return "HOLD", f"ADX too low ({cur_adx:.1f} < {current_adx_threshold})"


        # ── 15m Supertrend Direction Filter ──────────────────────────────────
        trend_15m = 0
        if df_15m is not None and len(df_15m) >= 35:
            st_15m = compute_supertrend(df_15m, period=10, multiplier=3.0)
            trend_15m = st_15m["trend"].iloc[-1]  # +1 or -1

        # ── 1hr EMA21 Macro Direction (Relaxed by LLM if aggressive) ──────────
        macro_dir = 0
        ignore_macro = aggression.get("ignore_macro_trend", False) if aggression else False
        
        if df_1hr is not None and len(df_1hr) >= 21:
            ema21_1hr = compute_ema(df_1hr["close"], 21)
            macro_dir = 1 if df_1hr["close"].iloc[-1] > ema21_1hr.iloc[-1] else -1

        # ── BULLISH: Supertrend UP + Price above VWAP + Positive Slope ────────
        bullish_macro = (macro_dir >= 0 or ignore_macro)
        if (cur_trend_5m == 1
                and cur_close > cur_vwap
                and slope > 0.05
                and trend_15m >= 0
                and bullish_macro):

            reason = f"Supertrend UP + VWAP Bounce + ADX={cur_adx:.1f}"
            return "BUY_CE", reason

        # ── BEARISH: Supertrend DOWN + Price below VWAP + Negative Slope ──────
        bearish_macro = (macro_dir <= 0 or ignore_macro)
        if (cur_trend_5m == -1
                and cur_close < cur_vwap
                and slope < -0.05
                and trend_15m <= 0
                and bearish_macro):

            reason = f"Supertrend DOWN + VWAP Rejection + ADX={cur_adx:.1f}"
            return "BUY_PE", reason

        # Identify why it failed
        if cur_trend_5m == 1 and cur_close <= cur_vwap:
            fail_reason = "Price below VWAP despite Bullish Supertrend"
        elif cur_trend_5m == -1 and cur_close >= cur_vwap:
            fail_reason = "Price above VWAP despite Bearish Supertrend"
        elif trend_15m != 0 and cur_trend_5m != trend_15m:
            fail_reason = f"TF Mismatch: 5m ST={cur_trend_5m}, 15m ST={trend_15m}"
        elif abs(slope) <= 0.05:
            fail_reason = f"Insufficient EMA Slope ({slope:.3f}%)"
        elif not bullish_macro and cur_trend_5m == 1:
            fail_reason = "Bullish trade blocked by Macro Trend"
        elif not bearish_macro and cur_trend_5m == -1:
            fail_reason = "Bearish trade blocked by Macro Trend"
        else:
            fail_reason = "No confluence of ST and VWAP"

        return "HOLD", fail_reason
