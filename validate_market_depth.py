
import os
import sys
import time
import logging
import json
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.path.join(os.getcwd(), 'Code/ai-app/tradebot'))

from src.broker.kite_ws import KiteStreamer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Depth_Validator")

def validate_depth():
    load_dotenv()
    
    streamer = KiteStreamer()
    streamer.start()
    
    logger.info("Connecting to Kite WebSocket...")
    
    # Wait for connection
    timeout = 10
    start_time = time.time()
    while not streamer.is_connected and (time.time() - start_time) < timeout:
        time.sleep(1)
        
    if not streamer.is_connected:
        logger.error("❌ Failed to connect to WebSocket within timeout.")
        return

    logger.info("✅ Connected. Waiting for NIFTY 50 ticks with depth...")
    
    # Monitor for 10 seconds to catch at least one tick
    found_depth = False
    start_time = time.time()
    while (time.time() - start_time) < 15:
        # NIFTY 50 Token
        tick = streamer.latest_ticks.get(256265)
        if tick and "depth" in tick:
            depth = tick["depth"]
            logger.info("📊 LIVE MARKET DEPTH RECEIVED:")
            print(json.dumps(depth, indent=2))
            
            # Verify depth structure
            if depth.get("buy") and depth.get("sell"):
                logger.info("✅ SUCCESS: Market Depth verified with real data.")
                found_depth = True
                break
        time.sleep(1)

    if not found_depth:
        logger.warning("⚠️ Received ticks but 'depth' was missing or empty. Market may be closed or token is incorrect.")

if __name__ == "__main__":
    validate_depth()
