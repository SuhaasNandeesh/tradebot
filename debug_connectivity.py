import os
import json
import logging
import time
from kiteconnect import KiteConnect
from src.broker.kite_ws import KiteStreamer
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ConnectivityDebug")

def check_connectivity():
    load_dotenv()
    
    if not os.path.exists("kite_session.json"):
        logger.error("❌ kite_session.json missing!")
        return
        
    with open("kite_session.json", "r") as f:
        session = json.load(f)
    
    api_key = os.getenv("KITE_API_KEY")
    access_token = session.get("access_token")
    
    logger.info(f"Checking session for API Key: {api_key[:4]}...")
    
    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        profile = kite.profile()
        logger.info(f"✅ API Connection Successful. User: {profile['user_name']}")
    except Exception as e:
        logger.error(f"❌ API Connection Failed: {e}")
        return

    logger.info("Starting WebSocket Ticker for 30 seconds...")
    streamer = KiteStreamer()
    streamer.start()
    
    start_t = time.time()
    tick_count = 0
    while time.time() - start_t < 30:
        nifty = streamer.get_nifty()
        vix = streamer.get_vix()
        if nifty:
            tick_count += 1
            logger.info(f"📡 Tick Received! NIFTY: {nifty} | VIX: {vix}")
        else:
            logger.info("⌛ Waiting for ticks...")
        time.sleep(5)
    
    streamer.stop()
    if tick_count > 0:
        logger.info(f"✅ WebSocket Test Passed. Received {tick_count} updates.")
    else:
        logger.error("❌ WebSocket Test Failed. No ticks received in 30s.")

if __name__ == "__main__":
    check_connectivity()
