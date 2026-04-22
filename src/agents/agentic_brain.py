import logging
import json
import time
from datetime import datetime
from typing import List, Dict, Any

from google import genai
from google.genai import types
from src.core.llm_provider import get_llm

logger = logging.getLogger(__name__)

class AgenticBrain:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        # Configure the native SDK with robust retries for 429/5xx errors
        retry_config = types.HttpRetryOptions(
            initial_delay=1.0,
            attempts=3,
            exp_base=2.0,
            http_status_codes=[429, 500, 502, 503, 504]
        )
        http_config = types.HttpOptions(
            retry_options=retry_config,
            timeout=30 * 1000 # 30s
        )
        
        self.client = genai.Client(http_options=http_config)
        self.primary_model = "gemini-3-flash-preview"
        self.fallback_model = "gemini-2.5-flash"

    def _get_market_context(self) -> Dict[str, Any]:
        """Get current market price, volatility (VIX), trend, and market regime."""
        nifty = self.orchestrator.streamer.get_nifty() or self.orchestrator.last_nifty_price or 0.0
        vix = self.orchestrator.risk_manager.current_vix
        mtf = self.orchestrator.data_provider.get_multi_timeframe("NIFTY")
        regime_data = self.orchestrator.strategy_selector.regime_detector.classify(mtf["15minute"], vix=vix)
        
        return {
            "nifty_price": nifty,
            "vix": vix,
            "regime": regime_data.get("regime"),
            "regime_narrative": regime_data.get("narrative"),
            "is_expiry_day": self.orchestrator.risk_manager.is_expiry_day("NIFTY"),
            "can_trade_now": self.orchestrator.risk_manager.can_trade()
        }

    def _get_news_sentiment(self) -> Dict[str, Any]:
        sentiment = self.orchestrator.context_agent.analyze_current_market()
        return {
            "sentiment": sentiment.get("sentiment"),
            "confidence": sentiment.get("confidence_score"),
            "key_drivers": sentiment.get("key_drivers"),
            "fii_dii_signal": self.orchestrator.last_fii_signal
        }

    def _get_options_intelligence(self) -> Dict[str, Any]:
        intel = self.orchestrator.last_options_analysis
        return {
            "pcr": intel.get("pcr"),
            "max_pain": intel.get("max_pain"),
            "oi_support": intel.get("support_oi"),
            "oi_resistance": intel.get("resistance_oi"),
            "market_bias": intel.get("bias")
        }

    def _poll_technical_strategies(self) -> List[Dict[str, Any]]:
        mtf = self.orchestrator.data_provider.get_multi_timeframe("NIFTY")
        signals = []
        for name, strat in self.orchestrator.strategy_selector.strategies.items():
            try:
                sig, reason = strat.generate_signal(mtf["5minute"], mtf["15minute"], mtf["60minute"])
                if sig != "HOLD":
                    signals.append({"strategy": name, "signal": sig, "reason": reason})
            except Exception as e:
                logger.error(f"Error polling strategy {name}: {e}")
        return signals

    def run_iteration(self):
        """Native ReAct Loop using Interactions API with multi-model fallback."""
        now = datetime.now()
        logger.info("🧠 Agentic Brain: Starting reasoning iteration...")
        
        mkt = self._get_market_context()
        news = self._get_news_sentiment()
        opt = self._get_options_intelligence()
        strats = self._poll_technical_strategies()
        
        prompt = f"""
You are the Lead Quant Strategist. Decide if we should TRADE or HOLD.

[MARKET CONTEXT]
Price: {mkt['nifty_price']} | VIX: {mkt['vix']}
Regime: {mkt['regime']} | Expiry Day: {mkt['is_expiry_day']}
Narrative: {mkt['regime_narrative']}

[SENTIMENT]
Bias: {news['sentiment']} ({news['confidence']}%)
FII/DII: {news['fii_dii_signal']}
Drivers: {news['key_drivers']}

[OPTIONS]
PCR: {opt['pcr']} | Max Pain: {opt['max_pain']}
Bias: {opt['market_bias']}

[STRATEGY SIGNALS]
{json.dumps(strats) if strats else "No technical triggers active."}

[GOAL]
Analyze the confluence of all data. 
- If technicals, news, and options align -> TRADE.
- If it is EXPIRY DAY, strongly prefer SPREADS over naked options to manage Gamma risk.
- If conflicting -> HOLD.

Respond ONLY with a JSON object:
{{
    "thought": "your detailed reasoning",
    "action": "TRADE" | "HOLD",
    "strategy": "name of strategy",
    "side": "BUY_CE" | "BUY_PE",
    "confidence": 1-100
}}
"""
        models_to_try = [self.primary_model, self.fallback_model]
        res_text = None
        used_model = None

        for model_name in models_to_try:
            try:
                logger.info(f"🧠 Brain: Calling Gemini API ({model_name})...")
                interaction = self.client.interactions.create(
                    model=model_name,
                    input=prompt
                )
                res_text = interaction.outputs[-1].text
                used_model = model_name
                break # Success!
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "too_many_requests" in err_msg.lower():
                    logger.warning(f"⚠️ Brain: Quota hit for {model_name}. Attempting next fallback...")
                    continue
                else:
                    logger.error(f"❌ Brain: Gemini failed for {model_name}: {e}")
                    continue

        # If all Gemini models failed, fallback to LMStudio
        if not res_text:
            logger.warning("🚨 [LMStudio Fallback] All Gemini models failed or reached quota. Using Local LLM...")
            try:
                res_text = self.orchestrator.lm_agent._chat(prompt, max_tokens=500)
                used_model = "LMStudio-Local"
            except Exception as e:
                logger.error(f"❌ [CRITICAL] LMStudio Fallback also failed: {e}")

        if not res_text:
            return "DECISION_REACHED: " + json.dumps({"action": "HOLD", "reasoning": "All AI models failed (Gemini 429 + LMStudio Offline)."})

        try:
            logger.debug(f"🧠 Brain ({used_model}): Raw Output: {res_text[:200]}...")
            
            # Clean up potential markdown
            clean_json = res_text.strip()
            if "```json" in clean_json:
                clean_json = clean_json.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_json:
                clean_json = clean_json.split("```")[1].split("```")[0].strip()
                
            decision = json.loads(clean_json)
            logger.info(f"🧠 Brain Decision ({used_model}): {decision['action']} | Thought: {decision.get('thought')[:100]}...")
            
            if decision['action'] == "TRADE":
                result_obj = {
                    "action": "TRADE",
                    "strategy": decision.get("strategy", "Agentic_Brain"),
                    "side": decision.get("side"),
                    "confidence": decision.get("confidence", 50),
                    "reasoning": decision.get("thought")
                }
                return f"DECISION_REACHED: {json.dumps(result_obj)}"
            
            return "DECISION_REACHED: " + json.dumps({"action": "HOLD", "reasoning": decision.get("thought")})
            
        except (json.JSONDecodeError, KeyError) as je:
            logger.error(f"🧠 Brain: Invalid output from {used_model}: {je}. Raw output was: {res_text[:500]}")
            fallback = {"action": "HOLD", "reasoning": f"AI model ({used_model}) returned malformed response."}
            return f"DECISION_REACHED: {json.dumps(fallback)}"
