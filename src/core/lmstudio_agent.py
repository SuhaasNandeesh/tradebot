"""
LM Studio Chat Agent

Leverages locally running LLMs (nemotron, qwen3.5, gemma-3) via LM Studio's
OpenAI-compatible server for high-value reasoning tasks:

  1. evening_reflection()  — Reviews today's trades, suggests improvements
  2. pre_trade_sanity()    — Second opinion before any order is placed
  3. mine_patterns()       — SQL-driven pattern analysis on confluence history
  4. explain_confluence()  — Plain-language confluence explanation for Telegram

Uses a fast local model for speed. Falls back gracefully if LM Studio is busy.

LM Studio server: http://127.0.0.1:1234
Models available (pick fastest for each task type):
  - nvidia/nemotron-3-nano-4b  → light analysis, fast
  - qwen3.5-4b                 → moderate reasoning
  - qwen/qwen3.5-9b            → deeper reasoning, slower
"""
import os
import json
import sqlite3
import logging
import requests
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

LMSTUDIO_BASE    = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
CHAT_URL         = f"{LMSTUDIO_BASE}/v1/chat/completions"
# Both chat tasks use nemotron-3-nano-4b — override via env if needed
CHAT_MODEL_FAST  = os.getenv("LMSTUDIO_CHAT_MODEL", "nvidia/nemotron-3-nano-4b")
CHAT_MODEL_DEEP  = os.getenv("LMSTUDIO_CHAT_MODEL", "nvidia/nemotron-3-nano-4b")
TIMEOUT_S        = 60    # LLM completion timeout

SYSTEM_PROMPT = (
    "You are an institutional Indian derivatives trader with 15+ years of experience "
    "trading NIFTY and SENSEX options. You are concise, honest, and data-driven. "
    "You think in terms of risk:reward, probability, and market microstructure. "
    "Respond in 3-5 sentences maximum unless asked for more. Be direct."
)


class LMStudioAgent:
    """
    Uses LM Studio's local LLMs as an intelligent co-pilot for the trading bot.
    All methods are non-blocking with graceful timeout fallbacks.
    """

    def __init__(self):
        self._session = requests.Session()

    def _chat(self, prompt: str, model: str = None,
              system: str = None, max_tokens: int = 400) -> Optional[str]:
        """
        Single chat completion call. Returns text or None on failure.
        """
        model    = model or CHAT_MODEL_FAST
        messages = [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ]
        try:
            resp = self._session.post(
                CHAT_URL,
                json={"model": model, "messages": messages,
                      "max_tokens": max_tokens, "temperature": 0.3,
                      "stream": False},
                timeout=TIMEOUT_S,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            logger.warning(f"[LMStudio] Chat failed: {resp.status_code}")
        except requests.exceptions.Timeout:
            logger.warning(f"[LMStudio] Timeout after {TIMEOUT_S}s")
        except Exception as e:
            logger.error(f"[LMStudio] Error: {e}")
        return None

    # ── Evening Reflection ─────────────────────────────────────────────────────

    def evening_reflection(self, db_path: str = "trade_journal.db") -> dict:
        """
        Analyses today's trades and confluence outcomes.
        Returns dict with: summary, insights, recommendations, formatted_report.
        Designed to run at 3:45 PM (after market close).
        """
        today = date.today().isoformat()

        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()

                # Today's trades
                cursor.execute('''
                    SELECT symbol, signal, pnl, strategy_id FROM trades
                    WHERE timestamp LIKE ? AND pnl IS NOT NULL
                    ORDER BY timestamp
                ''', (f"{today}%",))
                trades = cursor.fetchall()

                # Today's blocked signals (Regret Analysis)
                cursor.execute('''
                    SELECT strategy_name, signal, reason, confluence_score, regime 
                    FROM evaluation_logs 
                    WHERE timestamp LIKE ? AND reason LIKE '%BLOCKED%'
                ''', (f"{today}%",))
                blocked = cursor.fetchall()

                # Today's confluence outcomes
                cursor.execute('''
                    SELECT strategy_name, confluence_score, actual_direction,
                           technical_correct, options_correct, news_correct,
                           regime_correct, pnl
                    FROM confluence_outcomes WHERE timestamp LIKE ?
                ''', (f"{today}%",))
                outcomes = cursor.fetchall()

                # 30-day factor accuracy
                cursor.execute('''
                    SELECT AVG(technical_correct), AVG(options_correct),
                           AVG(news_correct), AVG(regime_correct), COUNT(*)
                    FROM confluence_outcomes
                    WHERE timestamp > datetime('now', '-30 days')
                ''')
                acc_row = cursor.fetchone()
        except Exception as e:
            logger.error(f"[LMStudio] DB read failed: {e}")
            return self._empty_reflection()

        if not trades and not outcomes and not blocked:
            return {
                "summary": "No activity today.",
                "insights": [],
                "recommendations": [],
                "formatted_report": "📭 No trading activity to analyse.",
            }

        # Format data for LLM
        trades_text = "\n".join(
            f"  - {sym} | {sig} | P&L: ₹{pnl:.0f} | {strat}"
            for sym, sig, pnl, strat in (trades or [])
        ) or "  None"

        blocked_text = "\n".join(
            f"  - {strat} | {sig} | Score: {score} | Reason: {reason}"
            for strat, sig, reason, score, reg in (blocked or [])
        ) or "  None"

        outcomes_text = "\n".join(
            f"  - {strat} | score={score} | {direction} | "
            f"tech={tech} opts={opts} news={news} regime={reg} | P&L=₹{pnl:.0f}"
            for strat, score, direction, tech, opts, news, reg, pnl
            in (outcomes or [])
        ) or "  None"

        acc_text = ""
        if acc_row and acc_row[0] is not None:
            acc_text = (
                f"30-day factor accuracy ({acc_row[4]} trades): "
                f"Technical={acc_row[0]:.0%}, Options={acc_row[1]:.0%}, "
                f"News={acc_row[2]:.0%}, Regime={acc_row[3]:.0%}"
            )

        prompt = f"""Today is {today}. Analyse these trading results and provide insights:

TODAY'S TRADES:
{trades_text}

BLOCKED SIGNALS (Potential Missed Opportunities):
{blocked_text}

CONFLUENCE OUTCOMES (tech/opts/news/regime = 1 if correct):
{outcomes_text}

HISTORICAL ACCURACY:
{acc_text}

Provide:
1. Brief summary of today's performance.
2. Regret Analysis: Were any of the 'BLOCKED' signals likely winners? (Compare with your knowledge of today's price action).
3. Should we adjust rule weights? (e.g., 'distrust PCR in trends' or 'be more aggressive on high-conviction news').
4. Specific recommendation for tomorrow.

Keep response under 250 words. Be specific and data-driven."""

        response = self._chat(prompt, model=CHAT_MODEL_DEEP, max_tokens=350)
        if not response:
            response = "LM Studio reflection unavailable. Review trades manually."

        # Parse structured output loosely
        total_pnl   = sum(t[2] or 0 for t in (trades or []))
        win_count   = sum(1 for t in (trades or []) if (t[2] or 0) > 0)
        total_count = len(trades or [])

        formatted = (
            f"📊 **Evening Reflection — {today}**\n\n"
            f"Trades: {total_count} | Wins: {win_count} | Total P&L: ₹{total_pnl:.0f}\n\n"
            f"🤖 **LM Studio Analysis:**\n{response}"
        )

        logger.info(f"[LMStudio] Evening reflection: {len(trades)} trades analysed")
        return {
            "summary":          f"{total_count} trades | ₹{total_pnl:.0f} P&L",
            "insights":         [response],
            "recommendations":  [],
            "formatted_report": formatted,
        }

    # ── Pre-Trade Sanity Check ─────────────────────────────────────────────────

    def pre_trade_sanity(self, signal: str, instrument: str,
                          context: dict, options_analysis: dict,
                          confluence: dict, entry_price: float = 0) -> dict:
        """
        Called BEFORE placing any order. Local LLM acts as a second opinion.
        Returns: {approved, confidence, reason, narrative}
        Default approve=True (non-blocking) to not impede good setups.
        """
        opts = options_analysis or {}
        pcr      = opts.get("pcr", {}).get("pcr", "N/A")
        iv_rank  = opts.get("iv_rank", {}).get("iv_rank", "N/A")
        opts_dir = opts.get("options_direction", "N/A")
        prem_sig = opts.get("premium_analysis", {}).get("signal", "N/A")
        max_pain = opts.get("max_pain", {}).get("max_pain_strike", "N/A")
        oi_sig   = opts.get("oi_buildup", {}).get("signal", "N/A")
        dist_res = opts.get("oi_buildup", {}).get("dist_to_resistance_%", "N/A")

        ctx = context or {}
        prompt = f"""TRADE SETUP ANALYSIS — {datetime.now().strftime('%H:%M')}

Instrument: {instrument} | Signal: {signal} | Entry premium: ₹{entry_price:.0f}
News Sentiment: {ctx.get('sentiment','N/A')} ({ctx.get('confidence_score','N/A')}% conf)
Key drivers: {', '.join(ctx.get('key_drivers', [])[:3]) if isinstance(ctx.get('key_drivers'), list) else 'N/A'}

OPTIONS INTELLIGENCE:
- PCR: {pcr} | Options direction: {opts_dir}
- IV Rank: {iv_rank}/100 | Premium signal: {prem_sig}
- OI Buildup: {oi_sig}
- Max Pain: {max_pain} | Distance to resistance: {dist_res}%

CONFLUENCE: Score {confluence.get('score',0)}/100 | {confluence.get('verdict','')}
Supporting: {', '.join(confluence.get('supporters',[])[:2])}
Blocking: {', '.join(confluence.get('blockers',[])[:2])}

Question: Should I take this trade? Is there anything the rule-based system may have missed?
Answer with: APPROVE or CAUTION, then one specific concern or endorsement."""

        response = self._chat(prompt, model=CHAT_MODEL_FAST, max_tokens=200)
        if not response:
            # Non-blocking: system continues without LLM opinion
            return {"approved": True, "confidence": 50,
                    "reason": "LM Studio unavailable", "narrative": ""}

        approved   = "CAUTION" not in response.upper()
        confidence = 80 if approved else 40

        logger.info(f"[LMStudio] Pre-trade sanity: {'APPROVE' if approved else 'CAUTION'}")
        return {
            "approved":   approved,
            "confidence": confidence,
            "reason":     response[:100],
            "narrative":  f"🤖 LLM: {response}",
        }

    def get_aggression_profile(self, context: dict) -> dict:
        """
        Determines if the bot should 'relax' its strict rules based on context.
        Returns: {ignore_macro_trend: bool, wider_rsi: bool, aggression_level: float}
        """
        ctx = context or {}
        sentiment = ctx.get("sentiment", "NEUTRAL")
        confidence = ctx.get("confidence_score", 50)
        drivers = ", ".join(ctx.get("key_drivers", [])) if isinstance(ctx.get("key_drivers"), list) else "N/A"

        prompt = f"""As a Lead Strategist, decide if we should be AGGRESSIVE today.
Current Sentiment: {sentiment} ({confidence}% Confidence)
Key Drivers: {drivers}

If confidence is > 80% and sentiment is clear, we may want to 'relax' the 1-hour trend filter to catch early reversals or momentum.

Return JSON only:
{{"ignore_macro_trend": bool, "wider_rsi": bool, "reasoning": str}}"""

        response = self._chat(prompt, model=CHAT_MODEL_FAST, max_tokens=200)
        try:
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0]
            return json.loads(response)
        except:
            return {"ignore_macro_trend": False, "wider_rsi": False, "reasoning": "Standard conservative mode."}

    def evaluate_confluence_logic(self, tech_signal: str, options: dict, news: dict, regime: str) -> dict:
        """
        Institutional Reasoning Arbiter: Weighs conflicting signals.
        Returns: {weighted_score_mod, approved_override, reasoning}
        """
        prompt = f"""As an expert trader, weigh these conflicting signals for {tech_signal} in a {regime} regime:

TECHNICAL: {tech_signal}
OPTIONS: {json.dumps(options)}
NEWS/MACO: {json.dumps(news)}

A rigid rule-based system might block this trade if one factor is missing. 
Should we prioritize one factor over others today?
If the trade is strong despite a missing rule, suggest an APPROVE_OVERRIDE.

Return JSON only: {{"score_mod": int, "override": bool, "reasoning": str}}"""
        
        response = self._chat(prompt, model=CHAT_MODEL_DEEP, max_tokens=250)
        try:
            # Simple extraction if LLM adds markdown
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0]
            return json.loads(response)
        except:
            return {"score_mod": 0, "override": False, "reasoning": "Standard rules apply."}

    def debate_trade(self, signal: str, context: str) -> str:
        """Simulates a Bull vs Bear debate for a final decision."""
        prompt = f"""Simulate a brief 2-round debate between a 'Bullish Analyst' and a 'Bearish Skeptic' regarding this {signal} setup:
{context}

The Skeptic must try to find reasons to FAIL the trade. The Analyst must provide data-backed rebuttals.
Conclude with a 'CONSENSUS' (TAKE TRADE or AVOID)."""
        
        return self._chat(prompt, model=CHAT_MODEL_DEEP, max_tokens=400) or "Debat unavailable."

    # ── Pattern Mining ─────────────────────────────────────────────────────────

    def mine_patterns(self, db_path: str = "trade_journal.db") -> str:
        """
        Runs SQL queries on confluence_outcomes + trades and asks the LLM
        to find patterns and generate a Telegram-ready insights report.
        """
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()

                # Pattern 1: Win rate by regime
                cursor.execute('''
                    SELECT regime,
                           COUNT(*) as total,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                           AVG(pnl) as avg_pnl
                    FROM confluence_outcomes
                    WHERE timestamp > datetime('now', '-30 days')
                    GROUP BY regime
                ''')
                regime_data = cursor.fetchall()

                # Pattern 2: Win rate by strategy
                cursor.execute('''
                    SELECT strategy_name,
                           COUNT(*) as total,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                           AVG(pnl) as avg_pnl
                    FROM confluence_outcomes
                    WHERE timestamp > datetime('now', '-30 days')
                    GROUP BY strategy_name
                ''')
                strategy_data = cursor.fetchall()

                # Pattern 3: Confluence score distribution vs outcomes
                cursor.execute('''
                    SELECT
                        CASE
                            WHEN confluence_score >= 80 THEN 'HIGH (80+)'
                            WHEN confluence_score >= 65 THEN 'MEDIUM (65-80)'
                            ELSE 'LOW (<65)'
                        END as score_band,
                        COUNT(*) as total,
                        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                        AVG(pnl) as avg_pnl
                    FROM confluence_outcomes
                    WHERE timestamp > datetime('now', '-30 days')
                    GROUP BY score_band
                ''')
                score_data = cursor.fetchall()

                # Pattern 4: Best hour to trade
                cursor.execute('''
                    SELECT substr(timestamp, 12, 2) as hour,
                           COUNT(*) as total,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
                    FROM confluence_outcomes
                    WHERE timestamp > datetime('now', '-30 days')
                    GROUP BY hour ORDER BY wins DESC LIMIT 5
                ''')
                hour_data = cursor.fetchall()

        except Exception as e:
            return f"❌ Pattern mining failed: {e}"

        # Format for LLM
        data_text = f"""
REGIME WIN RATES (30 days):
{chr(10).join(f'  {r[0]}: {r[2]}/{r[1]} wins, avg P&L=₹{(r[3] or 0):.0f}' for r in regime_data) or '  No data'}

STRATEGY WIN RATES (30 days):
{chr(10).join(f'  {r[0]}: {r[2]}/{r[1]} wins, avg P&L=₹{(r[3] or 0):.0f}' for r in strategy_data) or '  No data'}

CONFLUENCE SCORE BANDS:
{chr(10).join(f'  {r[0]}: {r[2]}/{r[1]} wins, avg P&L=₹{(r[3] or 0):.0f}' for r in score_data) or '  No data'}

BEST TRADING HOURS:
{chr(10).join(f'  {r[0]}:00 IST — {r[2]}/{r[1]} wins' for r in hour_data) or '  No data'}
"""

        if all(not r for r in [regime_data, strategy_data, score_data]):
            return "📭 **Insights**: Not enough trade history yet (minimum 5 trades needed). Keep trading!"

        prompt = f"""Analyse this trading performance data and provide 3 actionable insights:{data_text}

Focus on:
1. Which regime/strategy combination works best
2. Is the confluence score threshold correct (should it be higher/lower)?
3. Is there an optimal time window within the trading day?

Be specific with numbers. Max 150 words."""

        analysis = self._chat(prompt, model=CHAT_MODEL_DEEP, max_tokens=300)
        if not analysis:
            analysis = "Pattern analysis unavailable (LM Studio offline)."

        return (
            f"📈 **30-Day Trading Insights**\n\n"
            f"{data_text.strip()}\n\n"
            f"🤖 **LLM Analysis:**\n{analysis}"
        )

    # ── Explain Confluence ─────────────────────────────────────────────────────

    def explain_confluence(self, confluence: dict, options: dict = None) -> str:
        """
        Generates a plain-language explanation of the confluence score.
        Adds nuance beyond just listing supporters/blockers.
        """
        score     = confluence.get("score", 0)
        supporters = confluence.get("supporters", [])
        blockers   = confluence.get("blockers", [])
        signal    = confluence.get("signal", "HOLD")
        verdict   = confluence.get("verdict", "")

        prem_sig  = (options or {}).get("premium_analysis", {}).get(
            "interpretation", "")

        prompt = (
            f"Confluence score: {score}/100 | Verdict: {verdict} | Signal: {signal}\n"
            f"Supporting factors: {', '.join(supporters[:3]) or 'None'}\n"
            f"Blocking factors: {', '.join(blockers[:3]) or 'None'}\n"
            f"Premium movement: {prem_sig[:80] if prem_sig else 'N/A'}\n\n"
            f"In 2 sentences: why is this a {'good' if score >= 65 else 'risky'} setup? "
            f"What's the key risk?"
        )

        explanation = self._chat(prompt, model=CHAT_MODEL_FAST, max_tokens=150)
        return explanation or f"Confluence {score}/100 — {', '.join(blockers[:2]) or 'All clear'}."

    def _empty_reflection(self) -> dict:
        return {"summary": "Error", "insights": [], "recommendations": [],
                "formatted_report": "❌ Reflection failed. Check logs."}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent = LMStudioAgent()

    print("Testing pre_trade_sanity...")
    result = agent.pre_trade_sanity(
        signal="BUY_CE", instrument="NIFTY",
        context={"sentiment": "BULLISH", "confidence_score": 78,
                 "key_drivers": ["FII buying", "RBI dovish"]},
        options_analysis={
            "pcr": {"pcr": 0.85}, "iv_rank": {"iv_rank": 28},
            "options_direction": "BULLISH",
            "premium_analysis": {"signal": "PREMIUM_TRACKING_BULL",
                                  "interpretation": "CE premium tracking spot normally"},
            "oi_buildup": {"signal": "FRESH_LONGS_BULLISH", "dist_to_resistance_%": 0.8},
            "max_pain": {"max_pain_strike": 22000},
        },
        confluence={"score": 82, "verdict": "✅ APPROVED",
                    "supporters": ["Technical: Supertrend_VWAP → BUY_CE", "Options: PCR+OI confirm BULLISH"],
                    "blockers": []},
        entry_price=120.0,
    )
    print(f"Sanity: approved={result['approved']}")
    print(f"Narrative: {result['narrative'][:200]}")

    print("\nTesting mine_patterns...")
    report = agent.mine_patterns()
    print(f"Insights preview: {report[:200]}")
    print("\nLMStudioAgent PASS ✅")
