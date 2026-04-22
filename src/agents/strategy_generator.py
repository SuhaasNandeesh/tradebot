import logging
from langchain_core.prompts import PromptTemplate
from src.core.llm_provider import get_llm

logger = logging.getLogger(__name__)

STRATEGY_PROMPT = """
You are an elite quantitative researcher designing an intraday options trading strategy for NIFTY 50 in the Indian market.

Current market context:
- Sentiment: {sentiment}
- Confidence Score: {confidence_score}/100
- Key Drivers: {key_drivers}
- India VIX: {vix} (Current market volatility)

Write a Python class named `GeneratedStrategy` that implements a robust intraday options signal engine.

STRICT REQUIREMENTS:
1. The class MUST have a method: `def generate_signal(self, df):` returning exactly one of: 'BUY_CE', 'BUY_PE', or 'HOLD'
2. The input `df` is a pandas DataFrame with these columns:
   - `timestamp` (datetime): bar timestamp
   - `last_price` (float): closing/last traded price of the underlying spot index
   - `open` (float): bar open price
   - `high` (float): bar high price
   - `low` (float): bar low price
   - `volume` (int): bar volume
   - `vix` (float): India VIX level for that bar (if available, else NaN)
3. Return 'HOLD' if the dataframe has fewer than 20 rows (warmup period)
4. Return 'HOLD' if VIX column is available and VIX exceeds 20.0 (high volatility — avoid buying options)
5. Use ONLY pandas and numpy operations (no external libraries)
6. Implement at least TWO confirming indicators (e.g. EMA crossover + volume filter, RSI + VWAP)
7. Your logic MUST reflect the given sentiment: BULLISH → lean towards BUY_CE, BEARISH → lean towards BUY_PE, NEUTRAL → require stronger confirmation
8. Signal must have an exit mechanism: once you've entered (non-HOLD signal), subsequent calls should return HOLD until the next setup forms

9. Do NOT use underscore-prefixed attributes (e.g. self._state). Use public names only (e.g. self.last_signal).

RESPOND ONLY with raw executable Python. No markdown, no backticks, no explanations.
"""

class StrategyGenerator:
    def __init__(self):
        self.llm = get_llm()
        self.prompt = PromptTemplate(
            template=STRATEGY_PROMPT,
            input_variables=["sentiment", "confidence_score", "key_drivers", "vix"]
        )

    def generate_strategy_code(self, context_dict: dict) -> str:
        if not self.llm:
            logger.error("LLM not configured.")
            return None

        # Inject VIX if not in context
        if "vix" not in context_dict:
            context_dict["vix"] = "N/A (market closed)"

        logger.info("Generating new trading strategy code based on market context...")
        chain = self.prompt | self.llm

        try:
            response = chain.invoke(context_dict)
            # Strip any accidental markdown fences
            code = response.content.replace('```python', '').replace('```', '').strip()
            return code
        except Exception as e:
            logger.error(f"Failed to generate strategy: {e}")
            return None


if __name__ == "__main__":
    import os, sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    logging.basicConfig(level=logging.INFO)
    sg = StrategyGenerator()
    dummy_context = {
        "sentiment": "BULLISH",
        "confidence_score": 80,
        "key_drivers": "['RBI held rates', 'Strong FII buying', 'Positive US futures']",
        "vix": "13.5",
    }
    code = sg.generate_strategy_code(dummy_context)
    print("\n--- GENERATED CODE ---\n")
    print(code)

