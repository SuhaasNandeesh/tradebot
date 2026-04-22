"""
Strategy 3: EMA Pullback + RSI Entry

Market regime: Trending markets where we want to enter on a retracement (better R:R).
Distinctly different from VWAP Momentum — this is a PULLBACK strategy, not a breakout.

Logic:
  - 1hr chart defines the macro trend (EMA21 slope)
  - 15m chart confirms medium-term direction (EMA9 > EMA21)
  - 5m chart: wait for price to PULL BACK to the EMA21, then bounce/reject

  BULLISH (enter on pullback in uptrend):
    1. 1hr: EMA21 slope positive (trending up)
    2. 15m: close > EMA21 (uptrend intact)
    3. 5m: close approaches EMA21 (within 0.15%), then BOUNCES
       (previous bar touched/crossed EMA21, current bar closes ABOVE)
    4. RSI on 5m: 40–58 (coming off a dip, not overbought)
    → BUY_CE

  BEARISH (enter on pullback in downtrend):
    1. 1hr: EMA21 slope negative (trending down)
    2. 15m: close < EMA21 (downtrend intact)
    3. 5m: close approaches EMA21 from below, then REVERSES BACK DOWN
    4. RSI on 5m: 42–60 (bounced from resistance, heading down again)
    → BUY_PE

Why institutional:
  Rarely trade breakouts — expensive. Smart money waits for "the pullback",
  buying at the EMA21 zone in the direction of the trend.
  This gives a tighter stop-loss (price should NOT go through EMA21 if trend is valid)
  and a better risk-reward than chasing breakouts.

R:R target: SL just below EMA21 (tight) → Target 2x that distance (1:2 R:R).
"""
import logging
import pandas as pd
import numpy as np
from src.strategies.base import BaseStrategy
from src.data.market_data import compute_ema, compute_rsi

logger = logging.getLogger(__name__)

PULLBACK_TOLERANCE = 0.0015   # 0.15% proximity to EMA21 to count as "touching"
EMA_SLOPE_WINDOW   = 5        # Bars to measure EMA slope


class EMAPullbackStrategy(BaseStrategy):
    """
    EMA21 Pullback — enter in the direction of the higher-timeframe trend on retracements.
    Best on: Clean trending days. Avoid on: Range/sideways, high VIX days.
    Advantage: Tighter SL = better R:R than breakout entries.
    """
    name = "EMA_Pullback"
    description = "EMA21 pullback entry in 1hr trend direction, RSI 40–58 zone, tight SL at EMA21"

    def _ema_slope_positive(self, series: pd.Series, window: int = 5) -> bool:
        """True if the last 'window' bars of EMA are rising."""
        if len(series) < window + 1:
            return False
        return series.iloc[-1] > series.iloc[-(window + 1)]

    def _compute_signal(self, df_5m: pd.DataFrame,
                         df_15m: pd.DataFrame = None,
                         df_1hr: pd.DataFrame = None,
                         **kwargs) -> tuple[str, str]:
        if len(df_5m) < 30:
            return "HOLD", "Insufficient data (need 30+ bars)"

        close_5m  = df_5m["close"]
        ema21_5m  = compute_ema(close_5m, 21)
        rsi_5m    = compute_rsi(close_5m, 14)

        cur_close = close_5m.iloc[-1]
        prev_close = close_5m.iloc[-2]
        cur_ema21 = ema21_5m.iloc[-1]
        prev_ema21 = ema21_5m.iloc[-2]
        cur_rsi   = rsi_5m.iloc[-1]

        # ── 1hr Trend: EMA21 slope ────────────────────────────────────────────
        trend_1hr = 0
        if df_1hr is not None and len(df_1hr) >= 25:
            ema21_1hr = compute_ema(df_1hr["close"], 21)
            if self._ema_slope_positive(ema21_1hr, EMA_SLOPE_WINDOW):
                trend_1hr = 1
            else:
                trend_1hr = -1

        # ── 15m Trend: Price relative to EMA21 ───────────────────────────────
        trend_15m = 0
        if df_15m is not None and len(df_15m) >= 21:
            ema21_15m = compute_ema(df_15m["close"], 21)
            if df_15m["close"].iloc[-1] > ema21_15m.iloc[-1]:
                trend_15m = 1
            else:
                trend_15m = -1

        # ── BULLISH PULLBACK SETUP ────────────────────────────────────────────
        if trend_1hr == 1 and trend_15m == 1:
            prev_touched_ema = prev_close <= prev_ema21 * (1 + PULLBACK_TOLERANCE)
            bounced_up = cur_close > cur_ema21 * (1 + PULLBACK_TOLERANCE * 0.5)
            rsi_zone   = 40 <= cur_rsi <= 58

            if prev_touched_ema and bounced_up and rsi_zone:
                reason = f"EMA21 Pullback BULLISH | RSI={cur_rsi:.1f}, Bounced from EMA21"
                return "BUY_CE", reason

        # ── BEARISH PULLBACK SETUP ────────────────────────────────────────────
        if trend_1hr == -1 and trend_15m == -1:
            prev_touched_ema = prev_close >= prev_ema21 * (1 - PULLBACK_TOLERANCE)
            rejected_down = cur_close < cur_ema21 * (1 - PULLBACK_TOLERANCE * 0.5)
            rsi_zone       = 42 <= cur_rsi <= 60

            if prev_touched_ema and rejected_down and rsi_zone:
                reason = f"EMA21 Pullback BEARISH | RSI={cur_rsi:.1f}, Rejected from EMA21"
                return "BUY_PE", reason

        # Detailed HOLD reason
        if trend_1hr == 1 and trend_15m == 1:
            if not (40 <= cur_rsi <= 58):
                fail_reason = f"RSI {cur_rsi:.1f} out of Pullback range (40-58)"
            else:
                fail_reason = "Waiting for EMA21 touch/bounce"
        elif trend_1hr == -1 and trend_15m == -1:
            if not (42 <= cur_rsi <= 60):
                fail_reason = f"RSI {cur_rsi:.1f} out of Pullback range (42-60)"
            else:
                fail_reason = "Waiting for EMA21 touch/reversal"
        elif trend_1hr != trend_15m:
            fail_reason = f"TF Mismatch: 1hr={trend_1hr}, 15m={trend_15m}"
        else:
            fail_reason = "No clear trend for pullback"

        return "HOLD", fail_reason
