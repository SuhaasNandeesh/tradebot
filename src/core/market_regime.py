"""
Market Regime Detector

Classifies the current market into one of 4 regimes using multiple factors.
Each regime recommends different strategy sets to avoid applying trend-following
logic in sideways markets (a common cause of losses in retail systems).

Regimes:
  TRENDING   — Strong directional move. ADX > 25, BB narrow before breakout.
               Use: ORB, Supertrend+VWAP, EMA Pullback
  
  RANGING    — Price oscillating. ADX < 20, BB contracted.
               Use: VWAP mean-reversion (not yet implemented). Default: HOLD.
  
  VOLATILE   — High VIX, wide price swings. VIX > 18 or BB very wide.
               Use: Sell spreads or HOLD. Avoid buying naked options.
  
  EVENT      — Major pre-market gap (>0.5%) or scheduled macro events.
               Use: Wait 30 min after open for gap-fill/gap-go confirmation.

Input sources:
  - ADX(14) on 15m chart
  - Bollinger Band Width (BB Width) = (Upper-Lower)/Middle on 15m
  - VIX level (from streamer)
  - Pre-market gap % (today open vs yesterday close)
  - News event flag (from ContextAgent — if major event scheduled)
"""
import logging
import pandas as pd
import polars as pl
import numpy as np
from typing import Optional

from src.core.env_config import config

logger = logging.getLogger(__name__)

# ── Regime Thresholds ───────────────────────────────────────────────────────
VIX_HIGH           = config.vix_threshold - 5.0  # Elevated risk
VIX_EXTREME        = config.vix_threshold        # High risk — block option buys
GAP_THRESHOLD      = 0.005                       # 0.5% gap is significant
ADX_TREND_STRONG   = 25.0
ADX_TREND_MODERATE = 20.0
BB_NARROW          = 0.015                       # 1.5% width is compressed
BB_WIDE            = 0.040                       # 4% width is volatile

class MarketRegime:
    """Classifies current market into TRENDING/RANGING/VOLATILE/EVENT."""

    TRENDING  = "TRENDING"
    RANGING   = "RANGING"
    VOLATILE  = "VOLATILE"
    EVENT     = "EVENT"

    # Which strategies are appropriate per regime
    STRATEGY_MAP = {
        TRENDING: ["Supertrend_VWAP", "ORB", "EMA_Pullback"],
        RANGING:  ["Range_Mean_Reversion", "StatArb_Pairs"],
        VOLATILE: [],                               # No naked option buys
        EVENT:    [],                               # Wait 30 min → HOLD until confirmed
    }

    def _compute_bb_width(self, series: pd.Series, period: int = 20) -> float:
        """Bollinger Band Width = (Upper - Lower) / Middle using Polars."""
        if len(series) < period:
            return 0.02
            
        # Hardware acceleration via Polars
        pl_series = pl.Series("close", series)
        
        # Calculate MA and STD
        ma = pl_series.rolling_mean(window_size=period)
        std = pl_series.rolling_std(window_size=period)
        
        # Get last values
        last_ma = ma.tail(1)[0]
        last_std = std.tail(1)[0]
        
        if last_ma is None or last_ma <= 0:
            return 0.02
            
        upper = last_ma + 2 * last_std
        lower = last_ma - 2 * last_std
        return (upper - lower) / last_ma

    def _compute_adx(self, df: pd.DataFrame, period: int = 14) -> float:
        """Compute ADX from 15m OHLCV df."""
        if len(df) < period + 1:
            return 20.0  # Default moderate ADX
        from src.data.market_data import compute_adx
        return compute_adx(df, period).dropna().iloc[-1] if not compute_adx(df, period).dropna().empty else 20.0

    def classify(self, df_15m: pd.DataFrame,
                  vix: float = None,
                  pre_market_gap_pct: float = None,
                  major_event: bool = False) -> dict:
        """
        Classifies market regime.
        
        Args:
            df_15m: 15m OHLCV DataFrame (at least 30 bars)
            vix: Current India VIX level
            pre_market_gap_pct: (today_open - yesterday_close) / yesterday_close
            major_event: True if scheduled macro event (RBI, CPI, expiry)
        
        Returns dict with: regime, confidence, recommended_strategies, narrative
        """
        scores = {self.TRENDING: 0, self.RANGING: 0,
                  self.VOLATILE: 0, self.EVENT: 0}
        reasons = []

        # ── EVENT Check (highest priority) ───────────────────────────────────
        if major_event:
            scores[self.EVENT] += 3
            reasons.append("Major event scheduled.")

        if pre_market_gap_pct is not None and abs(pre_market_gap_pct) > GAP_THRESHOLD:
            scores[self.EVENT] += 2
            reasons.append(f"Large pre-market gap ({pre_market_gap_pct:.2%}).")

        # ── VIX Check (VOLATILE gate) ─────────────────────────────────────────
        if vix:
            if vix > VIX_EXTREME:
                scores[self.VOLATILE] += 4
                reasons.append(f"VIX={vix:.1f} (extreme — no option buys).")
            elif vix > VIX_HIGH:
                scores[self.VOLATILE] += 2
                reasons.append(f"VIX={vix:.1f} (elevated).")
            elif vix < 13.0:
                scores[self.TRENDING] += 1
                reasons.append(f"VIX={vix:.1f} (low — calm trending environment).")

        # ── ADX Check ─────────────────────────────────────────────────────────
        adx = None
        if df_15m is not None and not df_15m.empty and len(df_15m) >= 20:
            try:
                adx = self._compute_adx(df_15m)
                if adx >= ADX_TREND_STRONG:
                    scores[self.TRENDING] += 3
                    reasons.append(f"ADX={adx:.1f} (strong trend).")
                elif adx >= ADX_TREND_MODERATE:
                    scores[self.TRENDING] += 1
                    reasons.append(f"ADX={adx:.1f} (moderate trend).")
                else:
                    scores[self.RANGING] += 2
                    reasons.append(f"ADX={adx:.1f} (ranging).")
            except Exception:
                pass

        # ── Bollinger Band Width ───────────────────────────────────────────────
        bb_width = None
        if df_15m is not None and len(df_15m) >= 20:
            try:
                bb_width = self._compute_bb_width(df_15m["close"])
                if bb_width < BB_NARROW:
                    scores[self.TRENDING] += 2  # Compression before breakout
                    reasons.append(f"BB Width={bb_width:.3f} (compressed — breakout pending).")
                elif bb_width > BB_WIDE:
                    scores[self.VOLATILE] += 2
                    reasons.append(f"BB Width={bb_width:.3f} (wide — volatile).")
                else:
                    scores[self.RANGING] += 1
                    reasons.append(f"BB Width={bb_width:.3f} (normal).")
            except Exception:
                pass

        # ── Determine Regime ──────────────────────────────────────────────────
        regime    = max(scores, key=scores.get)
        max_score = scores[regime]
        total_score = sum(scores.values()) or 1
        confidence = int((max_score / total_score) * 100)

        # Override: if VOLATILE or EVENT score even tied, prefer safer regime
        if scores[self.VOLATILE] >= 3:
            regime = self.VOLATILE
        if scores[self.EVENT] >= 4:
            regime = self.EVENT

        recommended = self.STRATEGY_MAP.get(regime, [])

        self.current_regime    = regime
        self.regime_confidence = confidence

        narrative = (
            f"Market Regime: **{regime}** (confidence: {confidence}%)\n"
            f"Factors: {', '.join(reasons) or 'No strong signals.'}\n"
            f"Recommended strategies: {', '.join(recommended) or 'HOLD (no action)'}"
        )

        logger.info(f"[Regime] {regime} | confidence={confidence}% | ADX={adx} | BB={bb_width} | VIX={vix}")

        return {
            "regime":           regime,
            "confidence":       confidence,
            "recommended":      recommended,
            "narrative":        narrative,
            "scores":           scores,
            "adx":              round(adx, 1) if adx else None,
            "bb_width":         round(bb_width, 4) if bb_width else None,
            "vix":              vix,
        }

    def is_strategy_approved(self, strategy_name: str) -> bool:
        """Returns True if given strategy is appropriate for current regime."""
        return strategy_name in self.STRATEGY_MAP.get(self.current_regime, [])

    def regime_allows_trading(self) -> bool:
        """Returns False if regime is VOLATILE or EVENT (stay flat)."""
        return self.current_regime in (self.TRENDING, self.RANGING)


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
    logging.basicConfig(level=logging.INFO)
    import pandas as pd, numpy as np

    # Generate a trending test dataset
    n = 50
    closes = [22000 + i * 15 for i in range(n)]
    df_15m = pd.DataFrame({
        "open":  [c - 5 for c in closes],
        "high":  [c + 20 for c in closes],
        "low":   [c - 20 for c in closes],
        "close": closes,
        "volume": [100000] * n,
    })

    regime = MarketRegime()
    result = regime.classify(df_15m, vix=13.5, pre_market_gap_pct=0.002)
    print(f"\nTest 1 (Trending): {result['regime']} (conf={result['confidence']}%)")
    print(f"  {result['narrative']}")
    assert result["regime"] == MarketRegime.TRENDING

    result2 = regime.classify(df_15m, vix=23.0)
    print(f"\nTest 2 (Volatile VIX): {result2['regime']}")
    assert result2["regime"] == MarketRegime.VOLATILE

    result3 = regime.classify(df_15m, vix=14.0, major_event=True, pre_market_gap_pct=0.008)
    print(f"\nTest 3 (Event gap): {result3['regime']}")
    assert result3["regime"] == MarketRegime.EVENT

    print("\nMarketRegime PASS ✅")
