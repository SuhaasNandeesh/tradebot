import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
logging.basicConfig(level=logging.INFO)
from src.agents.context_agent import ContextAgent
from src.data.news_fetcher import NewsFetcher
fetcher = NewsFetcher()
fetcher.update_macro_news_cache()
agent = ContextAgent()
print(agent.summarize_news_for_telegram())
