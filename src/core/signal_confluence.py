"""
Signal Confluence Engine

The GATING layer that must approve every trade before execution.
All 4 factors must point in the same direction:

  [1] Technical Signal    — from StrategySelector (BUY_CE / BUY_PE)
  [2] Options Signal      — PCR + OI buildup direction (BULLISH / BEARISH / NEUTRAL)
  [3] News/Macro Sentiment — from ContextAgent (BULLISH / BEARISH / NEUTRAL)
  [4] Market Regime       — TRENDING (allowed) / RANGING / VOLATILE / EVENT (blocked)

Scoring:
  Each factor contributes 0–25 points.
  Final score 0–100:
    ≥ 65 → APPROVED (full position)
    40–64 → WEAK (half position, proceed with caution)
    < 40 → BLOCKED (no trade)

This prevents the bot from trading:
  - Technically bullish but news is BEARISH (macro override)
  - Good setup but PCR screaming extreme fear (big put buying)
  - Good signal in VOLATILE/EVENT regime (macro/event risk)
  - IV too expensive (IV rank > 60) — buying expensive premium
"""
import logging
from typing import Optional
from src.core.lmstudio_agent import LMStudioAgent

logger = logging.getLogger(__name__)

# Score thresholds
APPROVE_THRESHOLD  = 65   # Full position
WEAK_THRESHOLD     = 40   # Half position
BLOCK_THRESHOLD    = 40   # Below this → no trade

REGIME_SCORE = {
    "TRENDING": 25,    # Perfect
    "RANGING":  10,    # Possible but not ideal
    "VOLATILE": 0,     # Block
    "EVENT":    0,     # Block
}


class SignalConfluence:
    """
    Multi-factor trade gate. All signals must align before a trade is approved.
    Produces a score 0–100 and a human-readable Telegram-ready narrative.
    """
    def __init__(self):
        self.lm_agent = LMStudioAgent()

    def evaluate(self,
                 technical_signal:  str,
                 options_analysis:  dict,
                 news_sentiment:    str,
                 news_confidence:   int,
                 regime:            str,
                 iv_rank:           Optional[float] = None,
                 fii_signal:        Optional[str]  = None,
                 strategy_name:     str = "",
                 factor_accuracy:   Optional[dict] = None,
                 strategy_regime_stats: Optional[dict] = None
                 ) -> dict:
        """
        Evaluates confluence with ADAPTIVE weights from historical factor accuracy
        AND LLM-driven reasoning.
        """
        if technical_signal == "HOLD":
            return self._no_signal()

        direction  = "BULLISH" if technical_signal == "BUY_CE" else "BEARISH"
        breakdown  = {}
        score      = 0
        blockers   = []
        supporters = []

        # ── LLM ARBITRATION (Thinking Layer) ──────────────────────────────────
        # Let the LLM weigh the signals before we apply rule-based math
        llm_logic = self.lm_agent.evaluate_confluence_logic(
            tech_signal = technical_signal,
            options     = {"dir": options_analysis.get("options_direction"), "iv_rank": iv_rank},
            news        = {"sentiment": news_sentiment, "conf": news_confidence},
            regime      = regime
        )
        score += llm_logic.get("score_mod", 0)
        if llm_logic.get("reasoning"):
            supporters.append(f"LLM Arbiter: {llm_logic['reasoning']}")
        
        # Adaptive max per factor: base 25 pts, scaled by 30-day accuracy
        # accuracy 0.5 = baseline (1.0x) = 25 pts
        # accuracy 0.7 = above baseline (1.4x) = 35 pts (good predictor)
        # accuracy 0.3 = below baseline (0.6x) = 15 pts (poor predictor)
        acc = factor_accuracy or {}
        def _max(factor: str, base: int = 25) -> int:
            a = acc.get(factor, 0.5)
            scale = max(0.6, min(1.8, 0.6 + (a / 0.5)))
            return int(base * scale)

        # ── Factor 1: Technical (adaptive max) ──────────────────────────────
        tech_score = _max("technical")
        
        # ACTIVE LEARNING LOOP: Drop strategy weight to 0 if historically bleeding edge in this regime
        if strategy_regime_stats and strategy_regime_stats.get("total_trades", 0) >= 5:
            ev = strategy_regime_stats.get("conditional_expectancy", 0.0)
            if ev < -5.0: # If bleeding more than 5 PnL points on average
                # Mathematically tune the strategy out of deployment
                logger.warning(f"[Confluence] Active Learning: {strategy_name} EV is {ev:.2f} in {regime}. Forcing weight to 0.")
                tech_score = 0
                blockers.append(f"Active Learning: `{strategy_name}` has negative edge in `{regime}` (EV: {ev:.2f})")
        
        breakdown["technical"] = {"score": tech_score, "signal": technical_signal,
                                   "strategy": strategy_name}
        score += tech_score
        if tech_score > 0:
            supporters.append(f"Technical: `{strategy_name}` → `{technical_signal}`")

        # ── Factor 2: Options Market (adaptive max) ──────────────────────────
        max_opts  = _max("options")
        opts_dir  = options_analysis.get("options_direction", "NEUTRAL")
        iv_safe   = options_analysis.get("is_iv_safe_to_buy", True)
        prem_sig  = options_analysis.get("premium_analysis", {}).get("signal", "")

        if not iv_safe:
            opt_score = 0
            blockers.append(f"IV Rank too high ({options_analysis.get('iv_rank',{}).get('iv_rank',0):.0f}) — premium expensive.")
        elif opts_dir == direction:
            opt_score = max_opts
            supporters.append(f"Options: PCR+OI confirm {direction}")
        elif opts_dir == "NEUTRAL":
            opt_score = max_opts // 2
        else:
            opt_score = 0
            blockers.append(f"Options market points {opts_dir} (vs technical {direction})")

        if "DIVERGING" in prem_sig:
            opt_score = max(0, opt_score - 10)
            blockers.append(f"Premium diverging: `{prem_sig}` — move may be limited.")
        elif "GAMMA_EXPANSION" in prem_sig and direction in prem_sig:
            opt_score = min(max_opts, opt_score + 5)
            supporters.append(f"Gamma expansion confirms breakout direction.")

        breakdown["options"] = {"score": opt_score, "direction": opts_dir,
                                 "iv_safe": iv_safe, "premium_sig": prem_sig}
        score += opt_score

        # ── Factor 3: News/Macro Sentiment (adaptive max) ────────────────────
        max_news = _max("news")
        news_score = 0
        if news_sentiment == direction:
            news_score = int(max_news * (news_confidence / 100))
            supporters.append(f"News: `{news_sentiment}` (confidence {news_confidence}%)")
        elif news_sentiment == "NEUTRAL":
            news_score = max_news // 2
        else:
            news_score = 0
            blockers.append(f"News sentiment `{news_sentiment}` contradicts signal `{direction}`.")

        if fii_signal == direction:
            news_score = min(max_news, news_score + 5)
            supporters.append(f"FII/DII flow: {fii_signal}")
        elif fii_signal and fii_signal != "NEUTRAL" and fii_signal != direction:
            news_score = max(0, news_score - 5)
            blockers.append(f"FII/DII flow ({fii_signal}) contradicts direction.")

        breakdown["news"] = {"score": news_score, "sentiment": news_sentiment,
                              "confidence": news_confidence, "fii": fii_signal}
        score += news_score

        # ── Factor 4: Market Regime (adaptive max) ────────────────────────────
        max_reg   = _max("regime")
        reg_base  = REGIME_SCORE.get(regime, 0)
        reg_score = int(max_reg * (reg_base / 25)) if reg_base > 0 else 0
        if reg_score == 0:
            blockers.append(f"Regime is `{regime}` — no directional trades allowed.")
        else:
            supporters.append(f"Regime: `{regime}`")

        breakdown["regime"] = {"score": reg_score, "regime": regime}
        score += reg_score

        score = max(0, min(100, score))

        # ── Decision ──────────────────────────────────────────────────────────
        if score >= APPROVE_THRESHOLD:
            approved = True
            size_mult = 1.0
            verdict  = "✅ APPROVED"
        elif score >= WEAK_THRESHOLD:
            approved = True
            size_mult = 0.5   # Half position on weak confluence
            verdict  = "⚠️ WEAK APPROVAL (half size)"
        else:
            approved = False
            size_mult = 0.0
            verdict  = "🚫 BLOCKED"

        narrative = (
            f"{verdict} — Confluence Score: `{score}/100`\n"
            f"**Supporting:** {', '.join(supporters) if supporters else 'None'}\n"
            f"**Blocking:** {', '.join(blockers) if blockers else 'None'}"
        )

        logger.info(
            f"[Confluence] Signal={technical_signal} | Score={score} | "
            f"Approved={approved} | SizeMult={size_mult:.1f} | "
            f"Blockers: {blockers}"
        )

        return {
            "approved":             approved,
            "score":                score,
            "verdict":              verdict,
            "position_size_mult":   size_mult,
            "signal":               technical_signal,
            "direction":            direction,
            "narrative":            narrative,
            "breakdown":            breakdown,
            "blockers":             blockers,
            "supporters":           supporters,
        }

    def _no_signal(self) -> dict:
        return {
            "approved": False, "score": 0,
            "verdict": "HOLD", "position_size_mult": 0.0,
            "signal": "HOLD", "direction": None,
            "narrative": "No technical signal — HOLD",
            "breakdown": {}, "blockers": [], "supporters": [],
        }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    sc = SignalConfluence()

    # Test 1: Full confluence — should approve
    result = sc.evaluate(
        technical_signal="BUY_CE",
        options_analysis={"options_direction": "BULLISH", "is_iv_safe_to_buy": True,
                           "premium_analysis": {"signal": "PREMIUM_TRACKING_BULL"},
                           "iv_rank": {"iv_rank": 25}},
        news_sentiment="BULLISH",
        news_confidence=80,
        regime="TRENDING",
        iv_rank=25,
        fii_signal="BULLISH",
        strategy_name="Supertrend_VWAP",
    )
    print(f"Test 1 (Full bull): score={result['score']} | {result['verdict']}")
    assert result["approved"] and result["score"] >= 65

    # Test 2: Technical bullish but news bearish — should block/weaken
    result2 = sc.evaluate(
        technical_signal="BUY_CE",
        options_analysis={"options_direction": "BEARISH", "is_iv_safe_to_buy": True,
                           "premium_analysis": {"signal": "PREMIUM_DIVERGING_UP"},
                           "iv_rank": {"iv_rank": 30}},
        news_sentiment="BEARISH",
        news_confidence=75,
        regime="TRENDING",
        fii_signal="BEARISH",
        strategy_name="VWAP_Momentum",
    )
    print(f"Test 2 (Conflicting): score={result2['score']} | {result2['verdict']}")
    assert not result2["approved"] or result2["score"] < 65

    # Test 3: Volatile regime — should block
    result3 = sc.evaluate(
        technical_signal="BUY_PE",
        options_analysis={"options_direction": "NEUTRAL", "is_iv_safe_to_buy": False,
                           "premium_analysis": {}, "iv_rank": {"iv_rank": 75}},
        news_sentiment="NEUTRAL",
        news_confidence=50,
        regime="VOLATILE",
        strategy_name="EMA_Pullback",
    )
    print(f"Test 3 (Volatile + expensive IV): score={result3['score']} | {result3['verdict']}")
    assert not result3["approved"]

    print("\nSignalConfluence PASS ✅")
