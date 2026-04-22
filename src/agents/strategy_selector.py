"""
Agentic Strategy Selector

Institutional-grade strategy arbiter that re-evaluates and re-ranks all 
strategies every minute based on live market context and RAG memory.
"""
import logging
import os
import pandas as pd
from datetime import datetime, time as dtime
from typing import Optional, Tuple, List, Dict

from src.core.risk_manager import RiskManager
from src.memory.journal import TradeJournal
from src.data.market_data import MarketDataProvider
from src.strategies.base import BaseStrategy
from src.strategies.orb import ORBStrategy
from src.strategies.supertrend_vwap import SupertrendVWAPStrategy
from src.strategies.vwap_momentum import VWAPMomentumStrategy
from src.strategies.ema_pullback import EMAPullbackStrategy
from src.strategies.stat_arb_pairs import StatArbPairsStrategy
from src.strategies.range_mean_reversion import RangeMeanReversionStrategy

from src.core.signal_confluence import SignalConfluence
from src.core.market_regime import MarketRegime

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_LOSSES  = 2
CONFIDENCE_HIGH         = 70
CONFIDENCE_MODERATE     = 50

class StrategySelector:
    def __init__(self, risk_manager: RiskManager, journal: TradeJournal, data_provider: MarketDataProvider):
        self.risk_manager  = risk_manager
        self.journal       = journal
        self.data_provider = data_provider
        self.confluence    = SignalConfluence()
        self.regime_detector = MarketRegime()
        
        self.strategies: Dict[str, BaseStrategy] = {
            "ORB":                  ORBStrategy(),
            "Supertrend_VWAP":      SupertrendVWAPStrategy(),
            "VWAP_Momentum":        VWAPMomentumStrategy(),
            "EMA_Pullback":         EMAPullbackStrategy(),
            "StatArb_Pairs":        StatArbPairsStrategy(data_provider=self.data_provider),
            "Range_Mean_Reversion": RangeMeanReversionStrategy(),
        }
        
        self.session_date = None
        self.session_total_losses = 0
        self.session_total_wins = 0
        self.session_total_consecutive = 0
        self.active_strategy_name = None

    def select_with_confluence(self, context: dict, symbol: str = "NIFTY", **kwargs) -> Tuple[str, str, dict]:
        """Full institutional pipeline: Data -> Regime -> Technicals -> Confluence."""
        # 1. Data Fetching
        mtf = self._fetch_mtf_data(symbol)
        if not mtf: return ("NONE", "HOLD", {})

        # 2. Regime Classification
        regime_data = self.regime_detector.classify(mtf["15minute"], vix=self.risk_manager.current_vix)
        regime = regime_data.get("regime", "RANGING")
        if not self.regime_detector.regime_allows_trading():
            return ("NONE", "HOLD", {"regime": regime, "narrative": regime_data.get("narrative")})

        # 3. Global Risk Gate
        if self.session_total_consecutive >= MAX_CONSECUTIVE_LOSSES or not self.risk_manager.can_trade():
            return ("NONE", "HOLD", {"regime": regime})

        # 4. Intelligence Layer: Re-Ranking & Aggression
        ranked = self._rank_strategies(context)
        aggression = self._get_intelligence_profile(context)
        
        # 5. Technical Sweep (Evaluate ALL strategies in priority order)
        strategy_name, signal, hold_reasons = self._run_technical_sweep(ranked, mtf, aggression, regime)

        # 6. Confluence Gating
        if signal == "HOLD":
            return (ranked[0] if ranked else "NONE", "HOLD", {"narrative": "; ".join(hold_reasons[:3]), "blockers": hold_reasons, "regime": regime})

        confluence_result = self.confluence.evaluate(
            technical_signal = signal, strategy_name = strategy_name, regime = regime,
            options_analysis = kwargs.get("options_analysis", {}),
            news_sentiment   = context.get("sentiment", "NEUTRAL"),
            news_confidence  = int(context.get("confidence_score", 50)),
            factor_accuracy  = kwargs.get("factor_accuracy"),
            strategy_regime_stats = self.journal.get_strategy_regime_stats(strategy_name, regime)
        )

        # 7. Dynamic Decision
        return self._apply_dynamic_thresholds(strategy_name, signal, confluence_result, regime)

    # ── Private Helper Methods (Refactored) ───────────────────────────────────

    def _fetch_mtf_data(self, symbol: str) -> Optional[dict]:
        try:
            mtf = self.data_provider.get_multi_timeframe(symbol)
            if mtf.get("5minute") is not None and not mtf["5minute"].empty: return mtf
        except Exception as e: logger.error(f"[Selector] Data error: {e}")
        return None

    def _get_intelligence_profile(self, context: dict) -> dict:
        profile = self.confluence.lm_agent.get_aggression_profile(context)
        if os.getenv("FORCE_ONE_TRADE", "false").lower() == "true":
            profile.update({"ignore_macro_trend": True, "wider_rsi": True})
            logger.info("[Selector] 🚀 FORCE_ONE_TRADE: Bypassing filters.")
        return profile

    def _run_technical_sweep(self, ranked: list, mtf: dict, aggression: dict, regime: str) -> Tuple[str, str, List[str]]:
        hold_reasons = []
        futures_vwap = self.data_provider.get_futures_vwap("NIFTY")
        
        for name in ranked:
            if self._is_cooling_off(name):
                hold_reasons.append(f"{name}: Cooling off")
                continue
            if not self.regime_detector.is_strategy_approved(name):
                hold_reasons.append(f"{name}: Not approved for {regime}")
                continue

            # ── Bayesian Strategy Gating (Self-Learning) ──────────────────
            stats = self.journal.get_strategy_regime_stats(name, regime, days=14)
            if stats["total_trades"] >= 3 and stats["conditional_expectancy"] < -500:
                logger.warning(
                    f"🚫 BAYESIAN GATE: {name} disabled for {regime} regime. "
                    f"14-day EV: ₹{stats['conditional_expectancy']}"
                )
                hold_reasons.append(f"{name}: Bayesian Gate (Negative EV)")
                continue

            strat = self.strategies[name]
            sig, reason = strat.generate_signal(mtf["5minute"], mtf["15minute"], mtf["60minute"], 
                                               futures_vwap=futures_vwap, aggression=aggression)
            
            if sig != "HOLD": return name, sig, []
            hold_reasons.append(f"{name}: {reason}")
            
        return "NONE", "HOLD", hold_reasons

    def _apply_dynamic_thresholds(self, name, sig, conf, regime) -> Tuple[str, str, dict]:
        approve, weak = self.risk_manager.get_dynamic_thresholds(regime)
        score = conf["score"]

        if score >= approve:
            conf.update({"approved": True, "position_size_mult": 1.0, "verdict": "✅ APPROVED"})
        elif score >= weak:
            conf.update({"approved": True, "position_size_mult": 0.5, "verdict": "⚠️ WEAK"})
        else:
            conf.update({"approved": False, "verdict": "🚫 BLOCKED"})
            return name, "HOLD", conf

        return name, sig, conf

    def _rank_strategies(self, context: dict) -> list[str]:
        sentiment = context.get("sentiment", "NEUTRAL")
        confidence = context.get("confidence_score", 50)
        
        # RAG-based re-ranking
        context_str = f"Sentiment: {sentiment}, Confidence: {confidence}%"
        past_wins = self.journal.query_past_experience(context_str, n_results=5)
        rag_scores = {n: sum(1 for m in past_wins if n in m and "PROFITABLE" in m) for n in self.strategies}

        # Contextual base order
        if sentiment == "BULLISH" and confidence >= CONFIDENCE_HIGH:
            base = ["StatArb_Pairs", "Supertrend_VWAP", "VWAP_Momentum", "EMA_Pullback"]
        elif sentiment == "BEARISH" and confidence >= CONFIDENCE_HIGH:
            base = ["StatArb_Pairs", "Supertrend_VWAP", "EMA_Pullback", "VWAP_Momentum"]
        else:
            base = ["StatArb_Pairs", "EMA_Pullback", "Supertrend_VWAP", "VWAP_Momentum"]

        return sorted(self.strategies.keys(), key=lambda n: (base.index(n) if n in base else 99) - rag_scores.get(n, 0))

    def _is_cooling_off(self, name: str) -> bool:
        s = self.strategies.get(name)
        return s.consecutive_losses >= MAX_CONSECUTIVE_LOSSES if s else False

    def reset_session(self):
        today = datetime.now().date()
        if self.session_date == today: return
        self.session_date = today
        for s in self.strategies.values(): s.reset_session()
        self.session_total_losses = 0
        self.session_total_wins = 0
        self.session_total_consecutive = 0
        logger.info("[StrategySelector] Session reset.")

    def record_outcome(self, strategy_name: str, pnl: float):
        if strategy_name in self.strategies: self.strategies[strategy_name].on_trade_closed(pnl)
        if pnl > 0:
            self.session_total_wins += 1
            self.session_total_consecutive = 0
        else:
            self.session_total_losses += 1
            self.session_total_consecutive += 1
        self.active_strategy_name = None

    def get_session_summary(self) -> str:
        lines = ["📊 **Strategy Performance**\n"]
        for name, s in self.strategies.items():
            stats = s.get_stats()
            lines.append(f"`{name}`: {stats['wins']}W/{stats['losses']}L | WR: {stats['win_rate']:.0%}")
        return "\n".join(lines)
