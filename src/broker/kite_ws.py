import os
import json
import logging
import threading
import time
from kiteconnect import KiteTicker

logger = logging.getLogger(__name__)

# Instrument tokens
TOKEN_NIFTY50    = 256265
TOKEN_SENSEX     = 265
TOKEN_INDIA_VIX  = 264969
TOKEN_BANKNIFTY  = 260105   # BANKNIFTY index (NSE)

# Default subscriptions: NIFTY spot, SENSEX spot, India VIX, BANKNIFTY (for correlation)
DEFAULT_TOKENS = [TOKEN_NIFTY50, TOKEN_SENSEX, TOKEN_INDIA_VIX, TOKEN_BANKNIFTY]


class KiteStreamer:
    def __init__(self):
        self.api_key = os.getenv("KITE_API_KEY")
        self.access_token = self._load_access_token()
        self.latest_ticks = {}
        self.last_tick_time = 0
        self.symbol_to_token = {}
        self.is_connected = False
        self.order_callback = None  # Hook for OMS
        self.tick_callback = None   # Hook for High-Freq evaluation
        self._subscribed_tokens = set(DEFAULT_TOKENS)
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._reconnect_base_delay = 5  # seconds
        self.kws = None
        self._instance_lock = threading.Lock()
        self._running = False

    def _load_access_token(self):
        try:
            with open("kite_session.json", "r") as f:
                data = json.load(f)
                return data.get("access_token")
        except FileNotFoundError:
            logger.error("kite_session.json not found. Please run kite_auth.py first.")
            return None

    def on_ticks(self, ws, ticks):
        self.last_tick_time = time.time()
        for tick in ticks:
            token = tick.get("instrument_token")
            if token:
                self.latest_ticks[token] = tick
                
        if self.tick_callback:
            self.tick_callback(ticks)

    def on_connect(self, ws, response):
        logger.info("Successfully connected to Kite WebSocket")
        self.is_connected = True
        self._reconnect_attempts = 0
        tokens = list(self._subscribed_tokens)
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        logger.info(f"Subscribed to {len(tokens)} tokens: {tokens}")

    def on_close(self, ws, code, reason):
        logger.warning(f"WebSocket closed: {code} - {reason}")
        self.is_connected = False
        # Only auto-reconnect if it wasn't a manual stop AND we are supposed to be running
        if code != 1000 and self._running:
            self._schedule_reconnect()

    def on_error(self, ws, code, reason):
        logger.error(f"WebSocket error: {code} - {reason}")
        if not self._running:
            return

        if "403" in str(reason) or "Forbidden" in str(reason):
            logger.critical("🚨 KITE SESSION EXPIRED (403 Forbidden). Attempting automated TOTP recovery...")
            
            from src.broker.kite_totp_auth import run_totp_auth
            if run_totp_auth():
                logger.info("✅ Automated TOTP recovery successful. Restarting ticker...")
                # Reload access token
                self.access_token = self._load_access_token()
                # Use a small delay before restart to ensure session is active on broker side
                if self._running:
                    threading.Timer(2.0, self.start).start()
            else:
                logger.critical("❌ Automated TOTP recovery failed. Manual intervention required.")
                logger.critical("Please run 'python src/broker/kite_auth.py' to generate a fresh session.")
                self._max_reconnect_attempts = 0 # Kill reconnect loop 
                self.stop()

    def on_reconnect(self, ws, attempts_count):
        logger.info(f"Reconnecting to WebSocket... Attempt #{attempts_count}")

    def on_order_update(self, ws, data):
        """Triggered when an order state changes (COMPLETE, REJECTED, etc)"""
        logger.info(f"O-UPDATE via WS: {data.get('order_id')} -> {data.get('status')}")
        if self.order_callback:
            self.order_callback(data)

    def on_noreconnect(self, ws):
        logger.critical("WebSocket exhausted all reconnects. Manual restart required.")

    def _schedule_reconnect(self):
        """Exponential backoff reconnect on unexpected disconnect."""
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.critical("Max WebSocket reconnect attempts reached. Giving up.")
            return
        delay = self._reconnect_base_delay * (2 ** self._reconnect_attempts)
        self._reconnect_attempts += 1
        logger.info(f"Scheduling WebSocket reconnect in {delay}s (attempt {self._reconnect_attempts})...")
        threading.Timer(delay, self.start).start()

    def subscribe_tokens(self, tokens: list):
        """Dynamically subscribe to additional instrument tokens (e.g., option strikes)."""
        new_tokens = set(tokens) - self._subscribed_tokens
        if not new_tokens:
            return
        self._subscribed_tokens.update(new_tokens)
        if self.kws and self.is_connected:
            token_list = list(new_tokens)
            self.kws.subscribe(token_list)
            self.kws.set_mode(self.kws.MODE_FULL, token_list)
            logger.info(f"Dynamically subscribed to new tokens: {token_list}")

    def unsubscribe_tokens(self, tokens: list):
        """Remove tokens from active subscription (e.g., expired strikes)."""
        tokens_to_drop = [t for t in tokens if t in self._subscribed_tokens]
        if not tokens_to_drop:
            return
        self._subscribed_tokens -= set(tokens_to_drop)
        if self.kws and self.is_connected:
            self.kws.unsubscribe(tokens_to_drop)
            logger.info(f"Unsubscribed from tokens: {tokens_to_drop}")

    def register_symbol_token(self, symbol: str, token: int):
        """Cross-references human-readable symbols to WebSocket tick tokens."""
        self.symbol_to_token[symbol] = token

    def stop(self):
        """Cleanly stop the WebSocket ticker."""
        with self._instance_lock:
            self._running = False
            if self.kws:
                logger.info("Stopping existing KiteStreamer instance...")
                try:
                    self.kws.stop()
                except Exception as e:
                    logger.error(f"Error during kws.stop(): {e}")
                self.kws = None
                self.is_connected = False

    def start(self):
        with self._instance_lock:
            self._running = True
            if self.kws:
                logger.warning("KiteStreamer is already running or leaked. Stopping before restart.")
                try:
                    self.kws.stop()
                except:
                    pass

            if not self.api_key or not self.access_token:
                # Reload access token in case it was refreshed
                self.access_token = self._load_access_token()
                if not self.access_token:
                    logger.error("Cannot start ticker without api_key and access_token")
                    return

            logger.info("Starting KiteStreamer WebSocket...")
            self.kws = KiteTicker(self.api_key, self.access_token)
            self.kws.on_ticks = self.on_ticks
            self.kws.on_connect = self.on_connect
            self.kws.on_close = self.on_close
            self.kws.on_error = self.on_error
            self.kws.on_reconnect = self.on_reconnect
            self.kws.on_noreconnect = self.on_noreconnect
            self.kws.on_order_update = self.on_order_update

            # kws.connect(threaded=True) already spawns the management thread.
            self.kws.connect(threaded=True)

    def get_latest_price(self, instrument_token: int):
        tick = self.latest_ticks.get(instrument_token)
        if tick:
            return tick.get("last_price")
        return None

    def get_vix(self) -> float:
        """Returns the latest India VIX value."""
        return self.get_latest_price(TOKEN_INDIA_VIX)

    def get_nifty(self) -> float:
        """Returns the latest NIFTY 50 spot price."""
        return self.get_latest_price(TOKEN_NIFTY50)

    def get_sensex(self) -> float:
        """Returns the latest SENSEX spot price."""
        return self.get_latest_price(TOKEN_SENSEX)

    def get_banknifty(self) -> float:
        """Returns the latest BANKNIFTY index price (for correlation tracking)."""
        return self.get_latest_price(TOKEN_BANKNIFTY)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    streamer = KiteStreamer()
    streamer.start()

    try:
        while True:
            time.sleep(2)
            nifty = streamer.get_nifty()
            sensex = streamer.get_sensex()
            vix = streamer.get_vix()
            logger.info(f"Live -> NIFTY: {nifty} | SENSEX: {sensex} | VIX: {vix}")
    except KeyboardInterrupt:
        pass

