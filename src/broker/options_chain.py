import json
import logging
import pandas as pd
from datetime import datetime
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)

class OptionsManager:
    def __init__(self, api_key):
        self.api_key = api_key
        self.access_token = self._load_access_token()
        self.kite = KiteConnect(api_key=self.api_key)
        if self.access_token:
            self.kite.set_access_token(self.access_token)
        self.instruments_df = None

    def _load_access_token(self):
        try:
            with open("kite_session.json", "r") as f:
                data = json.load(f)
                return data.get("access_token")
        except FileNotFoundError:
            return None

    def load_instruments(self):
        """Fetches the daily instruments list and caches it as a DataFrame."""
        logger.info("Fetching master instruments list from Zerodha...")
        instruments = self.kite.instruments("NFO")
        self.instruments_df = pd.DataFrame(instruments)
        logger.info(f"Loaded {len(self.instruments_df)} NFO instruments.")

    def get_current_weekly_expiry(self, name="NIFTY"):
        if self.instruments_df is None:
            self.load_instruments()
            
        df = self.instruments_df[self.instruments_df["name"] == name]
        df = df[df["segment"] == "NFO-OPT"]
        
        # Get unique expirations, sort ascending, and return the closest future one
        today = pd.Timestamp(datetime.today().date())
        # Convert expiry Series to datetime
        df.loc[:, 'expiry_dt'] = pd.to_datetime(df['expiry'])
        import numpy as np
        future_expiries = np.sort(df[df['expiry_dt'] >= today]['expiry_dt'].unique())
        
        if len(future_expiries) == 0:
            return None
            
        closest_expiry = future_expiries[0]
        return pd.Timestamp(closest_expiry).strftime('%Y-%m-%d')

    def get_options_symbols(self, name="NIFTY", current_spot=22000, strike_interval=50, num_strikes=3):
        """
        Calculates ATM strike and fetches ITM, ATM, OTM symbols for the current expiry.
        """
        expiry = self.get_current_weekly_expiry(name)
        if not expiry:
            logger.error("Could not find suitable expiry.")
            return []

        # Calculate ATM strike
        atm_strike = round(current_spot / strike_interval) * strike_interval
        
        strikes_to_fetch = [
            atm_strike + (i * strike_interval) for i in range(-num_strikes, num_strikes + 1)
        ]

        df = self.instruments_df[
            (self.instruments_df["name"] == name) & 
            (self.instruments_df["segment"] == "NFO-OPT") &
            (pd.to_datetime(self.instruments_df['expiry']) == pd.to_datetime(expiry))
        ]
        
        target_options = df[df["strike"].isin(strikes_to_fetch)]
        
        options_list = []
        for _, row in target_options.iterrows():
            options_list.append({
                "tradingsymbol": row["tradingsymbol"],
                "instrument_token": row["instrument_token"],
                "strike": row["strike"],
                "instrument_type": row["instrument_type"]
            })
            
        return options_list

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    
    api_key = os.getenv("KITE_API_KEY")
    om = OptionsManager(api_key)
    om.load_instruments()
    
    print("\n--- Testing Options Fetcher ---")
    expiry = om.get_current_weekly_expiry("NIFTY")
    print(f"Current NIFTY Expiry: {expiry}")
    
    symbols = om.get_options_symbols("NIFTY", current_spot=22000, strike_interval=50, num_strikes=1)
    for sym in symbols:
        print(sym)
