
import os
import json
import logging
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from src.broker.options_chain import OptionsManager

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def test():
    load_dotenv()
    api_key = os.getenv("KITE_API_KEY")
    om = OptionsManager(api_key)
    
    print("\n--- Phase 1: Load Instruments ---")
    om.load_instruments()
    
    print(f"\n--- Phase 2: Expiry Check ---")
    expiry = om.get_current_weekly_expiry("NIFTY")
    print(f"Current NIFTY Expiry: {expiry}")
    
    if not expiry:
        print("FAILED: No expiry found")
        return

    print(f"\n--- Phase 3: Symbol Resolution ---")
    # Simulate a spot price
    spot = 22450 
    symbols = om.get_options_symbols("NIFTY", spot, strike_interval=50, num_strikes=0)
    print(f"Found {len(symbols)} symbols")
    for s in symbols:
        print(s)

if __name__ == "__main__":
    test()
