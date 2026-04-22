import os
import logging
from datetime import datetime, timedelta
from tavily import TavilyClient
from firecrawl import FirecrawlApp
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

class NewsFetcher:
    def __init__(self):
        load_dotenv()
        self.tavily_key = os.getenv("TAVILY_API_KEY")
        self.firecrawl_key = os.getenv("FIRECRAWL_API_KEY")
        
        self.tavily_client = TavilyClient(api_key=self.tavily_key) if self.tavily_key and self.tavily_key != 'your_tavily_api_key' else None
        self.firecrawl_app = FirecrawlApp(api_key=self.firecrawl_key) if self.firecrawl_key and self.firecrawl_key != 'your_firecrawl_api_key' else None

    def search(self, query: str, max_results: int = 5, search_depth: str = "basic"):
        """
        Unified search interface with automatic failover.
        Priority: Tavily -> Firecrawl.
        Returns: List of dicts with {'title', 'url', 'content'}
        """
        results = []
        
        # ── Tier 1: Tavily ───────────────────────────────────────────────────
        if self.tavily_client:
            try:
                logger.info(f"Searching Tavily for: {query}")
                t_res = self.tavily_client.search(query=query, search_depth=search_depth, max_results=max_results)
                for r in t_res.get('results', []):
                    results.append({
                        "title": r.get('title', 'N/A'),
                        "url": r.get('url', 'N/A'),
                        "content": r.get('content', r.get('snippet', ''))
                    })
                if results:
                    return results
            except Exception as e:
                err_msg = str(e)
                if "432" in err_msg:
                    logger.warning("Tavily Quota Exhausted (432). Switching to fallback...")
                else:
                    logger.error(f"Tavily search unexpected error: {e}")

        # ── Tier 2: Firecrawl ────────────────────────────────────────────────
        if self.firecrawl_app:
            try:
                logger.info(f"Searching Firecrawl (Fallback) for: {query}")
                f_res = self.firecrawl_app.search(query, limit=max_results)
                
                # Handle firecrawl.v2.types.SearchData object
                web_results = getattr(f_res, 'web', []) if hasattr(f_res, 'web') else []
                if not web_results and isinstance(f_res, dict):
                    web_results = f_res.get('data', []) # Fallback to dict access
                
                for r in web_results:
                    results.append({
                        "title": getattr(r, 'title', r.get('title', 'N/A') if isinstance(r, dict) else 'N/A'),
                        "url": getattr(r, 'url', r.get('url', 'N/A') if isinstance(r, dict) else 'N/A'),
                        "content": getattr(r, 'description', r.get('description', '') if isinstance(r, dict) else '')
                    })
                
                if results:
                    logger.info(f"Successfully retrieved {len(results)} results via Firecrawl.")
                    return results
            except Exception as e:
                logger.error(f"Firecrawl search fallback also failed: {e}")

        return results

    def get_global_macro_sentiment(self):
        """Fetches strictly live macro news via unified search interface."""
        query = "Latest breaking market news impacting NIFTY 50 and Indian economy in the last few hours"
        results = self.search(query, max_results=10)
        
        if not results:
            # Check if it was a quota issue specifically to pass up the chain
            # Since search() handles the internal failover, if it's empty, we are truly out of data.
            return None

        context = "\n".join([f"Title: {r['title']}\nContent: {r['content']}" for r in results])
        return context


    def scrape_specific_financial_article(self, url):
        """Uses Firecrawl to deep-scrape a specific important URL live."""
        if not self.firecrawl_app:
            logger.error("Firecrawl API key missing or invalid.")
            return None
            
        logger.info(f"Scrubbing via Firecrawl for {url} (Live)...")
        try:
            result = self.firecrawl_app.scrape_url(url, params={'formats': ['markdown']})
            content = result.get("markdown", "")
            return content
        except Exception as e:
            logger.error(f"Firecrawl scrape failed: {e}")
            return None

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fetcher = NewsFetcher()
    print("--- Testing Tavily Macro Search ---")
    news = fetcher.get_global_macro_sentiment()
    print(f"\nNews Context:\n{news[:1000]}..." if news else "Failed to fetch news. Check API keys.")
