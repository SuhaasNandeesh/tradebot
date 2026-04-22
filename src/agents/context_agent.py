import os
import logging
import json
from datetime import datetime
from google import genai
from google.genai import types
from langchain_core.prompts import PromptTemplate
from src.core.llm_provider import get_llm
from src.data.news_fetcher import NewsFetcher

from src.data.market_calendar import MarketCalendar

logger = logging.getLogger(__name__)

CONTEXT_PROMPT = """
You are an expert Institutional Quant Trader for the Indian Stock Market (NIFTY 50 / SENSEX).
Analyze the following live macroeconomic news and determine the overall market sentiment for the upcoming trading session.

News Data:
{news_data}

Respond ONLY with a raw JSON object string (do not use Markdown code blocks). Use exactly this structure:
{{
    "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
    "confidence_score": <int 1-100>,
    "key_drivers": ["driver 1", "driver 2"],
    "trading_implication": "<brief 1 sentence strategy recommendation>"
}}
"""

class ContextAgent:
    def __init__(self):
        self.llm_complex = get_llm(task_type="complex")
        self.llm_fast = get_llm(task_type="fast")
        self.fetcher = NewsFetcher()
        self.market_calendar = MarketCalendar()
        self.prompt = PromptTemplate(template=CONTEXT_PROMPT, input_variables=["news_data"])
        self.gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    def check_high_priority_alerts(self) -> dict:
        """
        Institutional Hardware-Level Sentiment.
        Polls for 'Black Swan' or high-priority news triggers.
        Returns: {trigger_active: bool, severity: str, narrative: str}
        """
        triggers = ["flash crash", "NSE outage", "RBI emergency", "war escalation", "trading halt"]
        query = f"NIFTY SENSEX {' OR '.join(triggers)} latest news"
        
        logger.info(f"Checking high-priority alerts: {query}")
        try:
            # Using unified search instead of direct tavily_client call
            search_results = self.fetcher.search(query=query, search_depth="advanced", max_results=3)
            context_str = "\n".join([f"Title: {r['title']}\nContent: {r['content']}" for r in search_results])
            
            if not context_str:
                return {"active": False, "severity": "NONE", "reason": "No high-priority alerts found in news sources."}
            
            prompt = f"""
            Identify if any CRITICAL TRADING HALT or MARKET EMERGENCY is reported in these snippets.
            Snippets: {context_str}
            
            Respond ONLY with JSON: {{"active": true/false, "severity": "CRITICAL/WARN/NONE", "reason": "..."}}
            """
            # Using Gemini 3.1 Flash Lite for sub-second latency
            res = self.llm_fast.invoke(prompt)
            content = res.content.strip().strip('```json').strip('```').strip()
            return json.loads(content)
        except Exception as e:
            return {"active": False, "severity": "NONE", "reason": f"Check failed: {e}"}

    def _get_fallback_news_from_gemini(self) -> str:
        """Uses Gemini's native Google Search grounding as a high-fidelity fallback."""
        logger.info("Tavily failed/limited. Attempting Gemini Google Search Grounding fallback...")
        query = "Latest breaking market news impacting NIFTY 50 and Indian economy in the last few hours"
        try:
            # Using the Google Search tool natively via the Client
            response = self.gemini_client.models.generate_content(
                model="gemini-3-flash-preview", 
                contents=f"Search for and provide a detailed summary of: {query}",
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            # The grounding results are embedded in the text response or via citations
            if response.text:
                return response.text
            return ""
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                logger.warning("Gemini Search Grounding quota hit. Falling back to internal LLM knowledge snapshot...")
                try:
                    # Secondary fallback: Just ask the model to summarize based on its latest internal snapshot
                    response = self.gemini_client.models.generate_content(
                        model="gemini-3-flash-preview",
                        contents=f"Provide a brief market sentiment summary for NIFTY 50 and Indian economy based on your latest internal knowledge. (Note: Search grounding is currently unavailable)."
                    )
                    if response.text:
                        return f"[INTERNAL SNAPSHOT] {response.text}"
                except Exception as ef:
                    logger.error(f"Internal snapshot fallback also failed: {ef}")
            else:
                logger.error(f"Gemini Grounding fallback failed: {e}")
            return ""

    def analyze_current_market(self):
        if not self.llm_fast:
            return {"error": "LLM not configured"}
            
        # ── Institutional High-Priority Audit ────────────────────────────
        alert = self.check_high_priority_alerts()
        if alert.get("active") and alert.get("severity") == "CRITICAL":
            logger.critical(f"🚨 BLACK SWAN DETECTED: {alert['reason']}")
            return {"sentiment": "NEUTRAL", "confidence_score": 0, "error": alert["reason"], "black_swan": True}

        logger.info("Fetching global & domestic macro news for sentiment analysis...")
        news_text = self.fetcher.get_global_macro_sentiment()
        
        if not news_text:
            logger.warning("Sentiment Analysis: Primary and secondary news sources failed. Triggering Gemini grounding fallback.")
            news_text = self._get_fallback_news_from_gemini()
            if not news_text:
                return {"sentiment": "NEUTRAL", "confidence_score": 0, "error": "All live intelligence sources exhausted (Tavily, Firecrawl, Gemini Grounding)."}

        try:
            chain = self.prompt | self.llm_fast
            res = chain.invoke({"news_data": news_text})
            # Clean up potential markdown blocks if LLM ignores "ONLY JSON" instruction
            content = res.content.strip().strip('```json').strip('```').strip()
            return json.loads(content)
        except Exception as e:
            logger.error(f"Market analysis generation failed: {e}")
            return {
                "sentiment": "NEUTRAL",
                "confidence_score": 0,
                "error": f"Analysis failed: {str(e)}"
            }

    def summarize_news_for_telegram(self):
        if not self.llm_fast:
            return "❌ Error: LLM not configured"
            
        logger.info("Fetching live macro news for Telegram summary...")
        news_text = self.fetcher.get_global_macro_sentiment()
        
        if not news_text:
            logger.warning("Telegram News: Primary and secondary search sources failed. Triggering Gemini grounding fallback.")
            news_text = self._get_fallback_news_from_gemini()
            if not news_text:
                return "❌ All live intelligence sources exhausted (Tavily, Firecrawl, Gemini Grounding)."

        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Using single braces for f-string evaluation (current_time)
        # Using double braces for LangChain variables ({news_data})
        prompt_template = f"""
You are an elite, institutional-grade Quantitative Trader and Market Analyst for the Indian Stock Market (NIFTY 50 / SENSEX).
The current local time is {current_time}.

I am providing you with the latest live, unstructured news from the internet.
Your task is to synthesize this raw data into a highly actionable, premium trading blueprint that mimics an elite hedge fund's morning dispatch.

Do not just list news. You must INTERPRET the news, infer the global sentiment, and generate a dynamic trading dashboard. Use the exact structural layout below.

Follow this exact structure (use markdown for beautiful Telegram formatting):

🔄 **[MORNING/INTRADAY] MACRO UPDATE — {current_time}**

───

📰 **CRITICAL NEWS CATALYSTS**
(Synthesize the 3 most strictly relevant, market-moving news items from the provided data. Omit non-market noise entirely.)
• [News 1] - *Impact:* [Brief explanation]
• [News 2] - *Impact:* [Brief explanation]
• [News 3] - *Impact:* [Brief explanation]

───

📈 **UPDATED MARKET BIAS: [e.g. NEUTRAL-BULLISH, BEARISH CONTINUATION]**
• Confidence: [1-100]%
• Rationale: [1-2 sentences explaining why the news dictates this bias. If there is a major event like a war or earnings beat, highlight it.]

───

🎯 **IMPLIED KEY LEVELS & ZONES (NIFTY 50)**
(Deduce structural technical levels based on the news severity. If exact spot prices aren't known, use conceptual levels like 'Previous Day High', 'Major Psychological Support', or extrapolate based on the news.)
• **Resistance**: [Level/Zone] - [Note]
• **Pivot**: [Level/Zone] - [Note]
• **Support**: [Level/Zone] - [Note]

───

📊 **REVISED TRADE SETUPS**

**Scenario A: [e.g., Breakout Momentum]**
• Bias: [e.g., BULLISH]
• Action: [e.g., Buy CE on pullback]
• Triggers: [What needs to happen]
• Invalidation: [What ruins this trade]

**Scenario B: [e.g., Bearish Rejection]**
• Bias: [e.g., BEARISH]
• Action: [e.g., Buy PE breaking support]
• Triggers: [What needs to happen]
• Invalidation: [What ruins this trade]

───

⚠️ **NO-TRADE CONDITIONS (DANGER ZONES)**
• [List 2-4 exact conditions, e.g., VIX spikes, gap is too large, choppy price chop]

───

📋 **LIVE CHECKLIST**
• [ ] Check actual gap / opening drive
• [ ] Compare to global overnight futures
• [ ] Watch first 15-min volume candle

Raw News Data to Analyze:
{{news_data}}
"""
        
        try:
            prompt = PromptTemplate(template=prompt_template, input_variables=["news_data"])
            # Routing back to llm_complex for the institutional report quality
            engine = self.llm_complex if self.llm_complex else self.llm_fast
            chain = prompt | engine
            response = chain.invoke({"news_data": news_text})
            return response.content.strip()
        except Exception as e:
            logger.error(f"Failed to summarize news for Telegram: {e}")
            return f"❌ Error summarizing news: {str(e)}"

    def answer_question(self, query: str):
        if not self.llm_complex: return "❌ Error: LLM not configured"
        
        now = datetime.now()
        current_date_str = now.strftime("%A, %B %d, %Y")
        current_time_str = now.strftime("%I:%M %p")
        
        # ── Calendar System Truth ──
        session_advice = self.market_calendar.get_session_advice()
        calendar_status = f"STATUS: {'CLOSED' if not session_advice['should_trade'] else 'OPEN'} | REASON: {session_advice['reason']}"
        
        # Augment search query to force recent 2026 results
        search_query = f"{query} {now.strftime('%B %Y')}"
        logger.info(f"Answering custom query: {query} (Search: {search_query})")
        
        try:
            # Using unified search instead of direct tavily_client call
            search_results = self.fetcher.search(query=search_query, search_depth="advanced", max_results=4)
            context_str = "\n".join([f"Title: {r['title']}\nContent: {r['content']}" for r in search_results])
            
            if not context_str:
                logger.warning(f"No context found for query: {query}. LLM will answer without grounded search.")
                context_str = "No specific news context available for this query."
            
            prompt_template = """
You are an elite, institutional-grade Quantitative Trader and Market Analyst specializing in the INDIAN STOCK MARKET (NSE/BSE).
Your primary focus is always on NIFTY 50, BANKNIFTY, and SENSEX.

[SYSTEM TRUTH]
Today's Date: {date}
Current Time: {time}
Market Calendar: {calendar_status}
Current Market Focus: National Stock Exchange (NSE) & Bombay Stock Exchange (BSE), India.
[END SYSTEM TRUTH]

When answering, ALWAYS prioritize the Indian market context:
1. Use the [SYSTEM TRUTH] as your absolute temporal and operational anchor. 
2. If [SYSTEM TRUTH] says the market is CLOSED, do not contradict it based on external news results.
3. If search context mentions different dates (like 2024 or 2025) or conflicting holiday info, treat them as HISTORICAL or IRRELEVANT.
4. If asked about trends, prioritize domestic catalysts (FII/DII data, RBI, Indian GDP) over global ones.
5. Mention global markets (USA/Europe) only as secondary context or if specifically requested.

User Question: {question}

Live Data Context from Search: 
{context}

Answer directly, clearly, and concisely in professional trading terminology.
"""
            prompt = PromptTemplate(template=prompt_template, input_variables=["question", "context", "date", "time", "calendar_status"])
            chain = prompt | self.llm_complex
            res = chain.invoke({
                "question": query, 
                "context": context_str, 
                "date": current_date_str, 
                "time": current_time_str,
                "calendar_status": calendar_status
            })
            return res.content.strip()
        except Exception as e:
            logger.error(f"Failed to answer question: {e}")
            return f"❌ Analysis Error: {str(e)}"

if __name__ == "__main__":
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    logging.basicConfig(level=logging.INFO)
    agent = ContextAgent()
    print(json.dumps(agent.analyze_current_market(), indent=4))
