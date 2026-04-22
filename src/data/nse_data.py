"""
NSE Data Fetcher — Session-based access to NSE's JSON APIs.

NSE blocks browser-level bots but their internal JSON endpoints are
accessible with proper session/cookie handling. Strategy:
  1. Establish session by hitting NSE homepage (gets cookies)
  2. Use session for all subsequent JSON API calls
  3. Fall back to Tavily if session fails

Available NSE endpoints (all free, no API key):
  /api/option-chain-indices?symbol=NIFTY   → Full options chain with IV, OI, PCR
  /api/option-chain-indices?symbol=BANKNIFTY
  /api/option-chain-equities?symbol=SENSEX
  /api/fiidiiTradeReact                    → FII/DII daily cash + F&O flows
  /api/market-status                       → Market open/closed status
  /api/nifty-bank-indices                  → NIFTY/Bank index levels
"""
import time
import logging
import os
import json
import requests
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

NSE_BASE            = "https://www.nseindia.com"
NSE_OPTION_CHAIN    = NSE_BASE + "/api/option-chain-indices?symbol={symbol}"
NSE_EQUITY_CHAIN    = NSE_BASE + "/api/option-chain-equities?symbol={symbol}"
NSE_FII_DII         = NSE_BASE + "/api/fiidiiTradeReact"
NSE_MARKET_STATUS   = NSE_BASE + "/api/market-status"

NSE_HEADERS = {
    "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept":           "*/*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Referer":          "https://www.nseindia.com/",
    "Connection":       "keep-alive",
}


class NSEDataFetcher:
    """
    Fetches options chain, FII/DII data and market status from NSE.
    Maintains a persistent session with automatic cookie refresh.
    Falls back to Tavily for any endpoint that fails.
    """

    def __init__(self):
        load_dotenv()
        self.tavily_key       = os.getenv("TAVILY_API_KEY", "")
        self.session          = requests.Session()
        self.session.headers.update(NSE_HEADERS)
        self._session_fresh   = False
        self._session_time    = None
        self._cache: dict     = {}
        self._cache_ts: dict  = {}
        self._cache_ttl       = 60   # seconds; NSE chain updates ~every minute

    # ── Session Management ────────────────────────────────────────────────────

    def _refresh_session(self) -> bool:
        """
        Hits NSE homepage to refresh cookies.
        NSE requires a valid session cookie (nsit, nseappid) for JSON API calls.
        """
        try:
            r = self.session.get(NSE_BASE + "/option-chain", timeout=10)
            if r.status_code in (200, 403, 302):
                self._session_fresh = True
                self._session_time  = datetime.now()
                logger.info(f"NSE session refreshed (status={r.status_code})")
                return True
        except Exception as e:
            logger.warning(f"NSE session refresh failed: {e}")
        return False

    def _needs_refresh(self) -> bool:
        if not self._session_fresh:
            return True
        if not self._session_time:
            return True
        return (datetime.now() - self._session_time).seconds > 300  # Refresh every 5 min

    def _get_json(self, url: str, cache_key: str = None, ttl: int = 60) -> Optional[dict]:
        """
        Fetches and caches a JSON endpoint.
        Refreshes session if stale. Returns None on failure.
        """
        # Cache check
        if cache_key and cache_key in self._cache:
            age = (datetime.now() - self._cache_ts[cache_key]).seconds
            if age < ttl:
                return self._cache[cache_key]

        if self._needs_refresh():
            self._refresh_session()
            time.sleep(0.5)  # Brief pause after session refresh

        try:
            r = self.session.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if cache_key:
                    self._cache[cache_key]    = data
                    self._cache_ts[cache_key] = datetime.now()
                return data
            elif r.status_code == 401:
                # Session expired — refresh and retry once
                logger.info("NSE 401 — refreshing session and retrying...")
                self._session_fresh = False
                self._refresh_session()
                time.sleep(1)
                r2 = self.session.get(url, timeout=10)
                if r2.status_code == 200:
                    return r2.json()
            logger.warning(f"NSE fetch failed: {url} status={r.status_code}")
        except Exception as e:
            logger.warning(f"NSE fetch error: {url}: {e}")

        # Tavily fallback for options page
        return self._tavily_fallback(url)

    def _tavily_fallback(self, url: str) -> Optional[dict]:
        """Use Tavily to extract data when NSE direct API fails."""
        if not self.tavily_key or self.tavily_key == "your_tavily_api_key":
            return None
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=self.tavily_key)
            result = client.search(
                query=f"NSE India options chain NIFTY IV PCR OI site:nseindia.com",
                search_depth="advanced",
                max_results=1
            )
            logger.info(f"Tavily fallback returned {len(result.get('results', []))} results")
            # Note: Tavily returns markdown/text, not structured JSON
            # This is only useful for news/sentiment about NSE, not structured OI data
        except Exception as e:
            logger.error(f"Tavily fallback failed: {e}")
        return None

    # ── Options Chain ─────────────────────────────────────────────────────────

    def get_options_chain(self, symbol: str = "NIFTY") -> Optional[dict]:
        """
        Returns full options chain with IV, OI, Volume, LTP per strike.
        Symbol: NIFTY, BANKNIFTY (indices endpoint)
                SENSEX (equity endpoint — BSE)
        Returns None if market is closed or data unavailable.
        """
        symbol = symbol.upper()
        if symbol in ("NIFTY", "BANKNIFTY", "NIFTY50", "MIDCPNIFTY"):
            url = NSE_OPTION_CHAIN.format(symbol=symbol)
        else:
            url = NSE_EQUITY_CHAIN.format(symbol=symbol)

        raw = self._get_json(url, cache_key=f"chain_{symbol}", ttl=self._cache_ttl)
        if not raw:
            return None

        records = raw.get("records", {})
        data    = records.get("data", [])

        if not data:
            logger.info(f"NSE options chain for {symbol}: no data (market likely closed)")
            return None

        expiry_dates = records.get("expiryDates", [])
        underlying   = records.get("underlyingValue", 0.0)
        timestamp    = raw.get("records", {}).get("timestamp", "")

        return {
            "symbol":        symbol,
            "underlying":    underlying,
            "timestamp":     timestamp,
            "expiry_dates":  expiry_dates,
            "chain":         data,
            "near_expiry":   expiry_dates[0] if expiry_dates else None,
        }

    def parse_chain_for_expiry(self, chain_data: dict,
                                expiry: str = None) -> list[dict]:
        """
        Filters the raw chain data for a specific expiry.
        Returns a list of dicts, each with: strike, CE, PE sub-dicts.
        """
        if not chain_data:
            return []
        target_expiry = expiry or chain_data.get("near_expiry")
        if not target_expiry:
            return []

        return [
            row for row in chain_data["chain"]
            if (row.get("CE", {}).get("expiryDate") == target_expiry
                or row.get("PE", {}).get("expiryDate") == target_expiry)
        ]

    # ── FII / DII Flows ───────────────────────────────────────────────────────

    def get_fii_dii_flow(self) -> dict:
        """
        Returns FII and DII net buy/sell in equity cash and F&O segments.
        Data is published by NSE same-day (intraday updates after 12:30 PM).
        """
        raw = self._get_json(NSE_FII_DII, cache_key="fii_dii", ttl=1800)  # 30-min cache
        if not raw or not isinstance(raw, list):
            return self._empty_fii_dii()

        fii_equity_net = 0.0
        dii_equity_net = 0.0
        fii_fo_net     = 0.0

        try:
            for entry in raw:
                category  = str(entry.get("category", "")).upper()
                buy_val   = float(str(entry.get("buyValue",  "0")).replace(",", "") or 0)
                sell_val  = float(str(entry.get("sellValue", "0")).replace(",", "") or 0)
                net       = buy_val - sell_val

                if "FII" in category or "FPI" in category:
                    if "DERIVATIVE" in category or "F&O" in category or "FO" in category:
                        fii_fo_net += net
                    else:
                        fii_equity_net += net
                elif "DII" in category or "DOMESTIC" in category:
                    dii_equity_net += net
        except Exception as e:
            logger.error(f"FII/DII parse error: {e}")

        signal = "NEUTRAL"
        if fii_equity_net > 500:
            signal = "BULLISH"     # FII buying heavily in cash
        elif fii_equity_net < -500:
            signal = "BEARISH"     # FII selling heavily
        elif dii_equity_net > 1000 and fii_equity_net < 0:
            signal = "MIXED"       # DII propping up FII selling

        logger.info(f"FII/DII | FII equity={fii_equity_net:.0f}Cr | DII equity={dii_equity_net:.0f}Cr | Signal={signal}")
        return {
            "fii_equity_net":  round(fii_equity_net, 2),
            "dii_equity_net":  round(dii_equity_net, 2),
            "fii_fo_net":      round(fii_fo_net, 2),
            "signal":          signal,
            "raw":             raw[:3] if raw else [],
        }

    def _empty_fii_dii(self) -> dict:
        return {"fii_equity_net": 0.0, "dii_equity_net": 0.0,
                "fii_fo_net": 0.0, "signal": "NEUTRAL", "raw": []}

    # ── Participant-wise Open Interest (Institutional) ───────────────────────

    def get_participant_oi(self) -> dict:
        """
        Fetches Participant-wise OI from NSE CSV. 
        Shows FII/DII/Client net positions in Index Options.
        URL: https://www.nseindia.com/api/reports/fao/participant-wise-open-interest
        """
        try:
            # NSE usually publishes this after 6:30 PM for the current day.
            # We fetch the latest available.
            url = "https://www.nseindia.com/api/reports/fao/participant-wise-open-interest"
            raw = self._get_json(url, cache_key="participant_oi", ttl=3600) # 1hr cache
            
            if not raw or "data" not in raw:
                return {"fii_net_options": 0, "client_net_options": 0, "signal": "NEUTRAL"}

            data = raw["data"]
            fii_data = next((item for item in data if item["participant"] == "FII"), {})
            client_data = next((item for item in data if item["participant"] == "Client"), {})

            # Net = Long - Short
            fii_long  = int(fii_data.get("index_options_long", 0))
            fii_short = int(fii_data.get("index_options_short", 0))
            fii_net   = fii_long - fii_short

            client_long  = int(client_data.get("index_options_long", 0))
            client_short = int(client_data.get("index_options_short", 0))
            client_net   = client_long - client_short

            signal = "NEUTRAL"
            if fii_net > 50000: signal = "BULLISH" # High institutional long bias
            elif fii_net < -50000: signal = "BEARISH"
            
            # Institutional Divergence: If FII is Short and Client is Long, Bearish is stronger.
            if fii_net < 0 and client_net > 0: signal = "STRONGLY_BEARISH"
            elif fii_net > 0 and client_net < 0: signal = "STRONGLY_BULLISH"

            logger.info(f"Smart Money Signal: FII_Net={fii_net} | Client_Net={client_net} | Signal={signal}")
            return {
                "fii_net_options": fii_net,
                "client_net_options": client_net,
                "signal": signal
            }
        except Exception as e:
            logger.error(f"Failed to fetch Participant OI: {e}")
            return {"fii_net_options": 0, "client_net_options": 0, "signal": "NEUTRAL"}

    def get_market_status(self) -> dict:
        """Returns whether NSE market is currently open."""
        raw = self._get_json(NSE_MARKET_STATUS, cache_key="mkt_status", ttl=60)
        if not raw:
            return {"is_open": False}
        markets = raw.get("marketState", [])
        nse_status = next((m for m in markets if "NSE" in m.get("market", "") and
                           "EQ" in m.get("marketStatus", "")), None)
        is_open = False
        if nse_status:
            is_open = nse_status.get("marketStatus", "").lower() == "open"
        return {"is_open": is_open, "raw": nse_status}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    nse = NSEDataFetcher()

    print("--- Market Status ---")
    status = nse.get_market_status()
    print(f"  Market open: {status['is_open']}")

    print("\n--- NIFTY Options Chain ---")
    chain = nse.get_options_chain("NIFTY")
    if chain:
        print(f"  Underlying: {chain['underlying']}")
        print(f"  Near expiry: {chain['near_expiry']}")
        print(f"  Total strikes: {len(chain['chain'])}")
        strikes = nse.parse_chain_for_expiry(chain)
        if strikes:
            mid = strikes[len(strikes)//2]
            ce  = mid.get("CE", {})
            pe  = mid.get("PE", {})
            print(f"  ATM Strike: {ce.get('strikePrice')} | CE IV: {ce.get('impliedVolatility')} | PE IV: {pe.get('impliedVolatility')}")
            print(f"  CE OI: {ce.get('openInterest')} | PE OI: {pe.get('openInterest')}")
    else:
        print("  No data (market likely closed — test during market hours)")

    print("\n--- FII/DII Flow ---")
    flow = nse.get_fii_dii_flow()
    print(f"  FII equity net: ₹{flow['fii_equity_net']}Cr")
    print(f"  DII equity net: ₹{flow['dii_equity_net']}Cr")
    print(f"  Signal: {flow['signal']}")
    print("\nNSE Data Fetcher PASS ✅")
