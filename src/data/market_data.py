"""
Multi-Timeframe Market Data Fetcher.

Fetches OHLCV from Kite for multiple timeframes (5m, 15m, 1hr) and 
caches intra-session to minimize API calls.
"""
import os
import json
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date

logger = logging.getLogger(__name__)

# ── Instrument tokens (spot index — used for OHLCV but NOT for VWAP) ─────────
INSTRUMENT_TOKENS = {
    "NIFTY":     256265,
    "SENSEX":    265,
    "BANKNIFTY": 260105,
}

TIMEFRAMES = ["5minute", "15minute", "60minute"]

# NOTE: VWAP for NIFTY/SENSEX MUST use Futures (not spot index).
# Spot indices have no traded volume — any VWAP from spot data is meaningless.
# Use get_futures_ohlcv() / get_futures_vwap() instead.


class MarketDataProvider:
    """
    Provides multi-timeframe OHLCV data for NIFTY/SENSEX.
    Uses Kite historical API and caches within the same session.
    Falls back to resampling when finer data is available.
    """

    def __init__(self, kite_client=None):
        self.kite = kite_client
        self._cache: dict[str, pd.DataFrame] = {}
        self._cache_time: dict[str, datetime] = {}
        self._cache_ttl_seconds = 300  # Re-fetch after 5 minutes

    def set_kite(self, kite_client):
        self.kite = kite_client

    def _cache_key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}_{timeframe}"

    def _is_cache_fresh(self, key: str) -> bool:
        if key not in self._cache_time:
            return False
        return (datetime.now() - self._cache_time[key]).seconds < self._cache_ttl_seconds

    def _fetch_from_kite(self, token: int, timeframe: str, days: int) -> pd.DataFrame:
        if not self.kite:
            logger.critical("KITE CONNECTION MISSING: Cannot synthesize mock OHLCV in institutional mode.")
            return pd.DataFrame()
        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=days)
            records = self.kite.historical_data(token, from_date, to_date, timeframe)
            if not records:
                logger.error(f"[API] No records returned for {token} at {timeframe}")
                return pd.DataFrame()
            
            df = pd.DataFrame(records)
            df = df.rename(columns={"date": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.set_index("timestamp").sort_index()
            # Normalize column names
            df.rename(columns={"close": "close", "open": "open",
                                "high": "high", "low": "low", "volume": "volume"}, inplace=True)
            
            # ── Data Freshness Check ──────────────────────────────────────────
            # If timeframe is intraday (< 1 day), verify last bar is recent
            is_intraday = timeframe not in ["day"]
            if is_intraday and not df.empty:
                last_bar_time = df.index[-1]
                # Adjust for potential timezone offset (Kite uses UTC+5:30)
                if last_bar_time.tzinfo:
                    now = datetime.now(last_bar_time.tzinfo)
                else:
                    now = datetime.now()
                
                staleness = (now - last_bar_time).total_seconds()
                # 300s (5min) threshold for 1m/5m/15m charts. 
                # Note: 1hr bars might look stale if it's currently 10:15 but last bar was 10:00.
                # So we scale threshold by timeframe minutes.
                tf_minutes = 5 if "5" in timeframe else (15 if "15" in timeframe else 60)
                threshold = max(300, tf_minutes * 60 + 300) 
                
                # Check if currently within market hours (roughly 9:15 to 15:30 IST)
                # Kite timestamps are typically in IST or converted. 
                # If we are outside market hours, staleness is expected.
                is_market_open = (9 * 60 + 15) <= (now.hour * 60 + now.minute) <= (15 * 60 + 35)
                is_weekday = now.weekday() < 5
                
                if staleness > threshold and is_market_open and is_weekday:
                    logger.critical(f"⚠️ STALE DATA DETECTED for {token} ({timeframe}). "
                                    f"Last bar: {last_bar_time} | Now: {now} | "
                                    f"Staleness: {staleness/60:.1f} mins. Threshold: {threshold/60:.1f} mins.")
                    return pd.DataFrame() # Block trading on stale data
                elif staleness > threshold:
                    logger.info(f"Market closed or weekend. Using last available data for {token} ({timeframe}).")
                    
            return df
        except Exception as e:
            logger.exception(f"Kite API Hard Failure ({timeframe}): {e}. Bot will block execution.")
            return pd.DataFrame()

    # Institutional Bot: Mock generation strictly forbidden natively.

    def get_ohlcv(self, symbol: str = "NIFTY", timeframe: str = "5minute",
                  days: int = 5, force_refresh: bool = False) -> pd.DataFrame:
        """
        Returns OHLCV DataFrame for the given symbol and timeframe.
        Caches for 5 minutes to avoid hammering Kite API.
        """
        key = self._cache_key(symbol, timeframe)
        if not force_refresh and self._is_cache_fresh(key):
            return self._cache[key].copy()

        token = INSTRUMENT_TOKENS.get(symbol.upper())
        if not token:
            logger.error(f"Unknown symbol: {symbol}")
            return pd.DataFrame()

        df = self._fetch_from_kite(token, timeframe, days)
        self._cache[key] = df
        self._cache_time[key] = datetime.now()
        logger.info(f"Fetched {len(df)} bars for {symbol} ({timeframe})")
        return df.copy()

    def get_multi_timeframe(self, symbol: str = "NIFTY") -> dict[str, pd.DataFrame]:
        """
        Returns a dict of {timeframe: DataFrame} for 5m, 15m, and 1hr.
        5m uses 5 days; 15m uses 15 days; 1hr uses 30 days.
        """
        return {
            "5minute":   self.get_ohlcv(symbol, "5minute",  days=5),
            "15minute":  self.get_ohlcv(symbol, "15minute", days=15),
            "60minute":  self.get_ohlcv(symbol, "60minute", days=30),
        }

    def get_todays_bars(self, symbol: str = "NIFTY",
                        timeframe: str = "5minute") -> pd.DataFrame:
        """Returns only today's bars from cached data."""
        df = self.get_ohlcv(symbol, timeframe, days=2)
        if df.empty:
            return df
        today = datetime.now().date()
        return df[df.index.date == today].copy()

    def invalidate_cache(self):
        """Force fresh data on next call (call at session start)."""
        self._cache.clear()
        self._cache_time.clear()

    # ── Futures Data (for correct VWAP) ───────────────────────────────────────

    def get_futures_token(self, symbol: str = "NIFTY") -> int:
        """
        Dynamically resolves the near-month futures instrument token from Kite.
        E.g., NIFTY26MARFUT → token 11924994 (changes each expiry cycle).
        Falls back to a known recent token if Kite is not available.
        """
        if not self.kite:
            # Fallback hardcoded near-month tokens (update these if expiry passes)
            fallbacks = {"NIFTY": 11924994, "SENSEX": 11170562, "BANKNIFTY": 11352322}
            return fallbacks.get(symbol.upper(), 0)

        try:
            instruments = self.kite.instruments("NFO")
            today = date.today()
            # Filter: symbol + FUT + earliest expiry in the future
            futures = [
                i for i in instruments
                if i["tradingsymbol"].startswith(symbol.upper())
                and i["instrument_type"] == "FUT"
                and pd.to_datetime(i["expiry"]).date() >= today
            ]
            if not futures:
                return 0
            # Pick nearest expiry
            nearest = min(futures, key=lambda i: pd.to_datetime(i["expiry"]).date())
            tok = nearest["instrument_token"]
            logger.info(f"Futures token for {symbol}: {nearest['tradingsymbol']} = {tok}")
            return tok
        except Exception as e:
            logger.warning(f"Could not resolve futures token for {symbol}: {e}")
            return 0

    def get_futures_ohlcv(self, symbol: str = "NIFTY",
                           timeframe: str = "5minute", days: int = 5) -> pd.DataFrame:
        """
        Fetches OHLCV for near-month futures contract.
        Strict Mode: Returns empty DF if token resolution fails.
        """
        key = f"{symbol}_FUT_{timeframe}"
        if self._is_cache_fresh(key):
            return self._cache[key].copy()

        token = self.get_futures_token(symbol)
        if not token:
            logger.critical(f"STRICT MODE: Futures token for {symbol} not found. Halting strategy.")
            return pd.DataFrame()

        df = self._fetch_from_kite(token, timeframe, days)
        if df.empty:
            logger.critical(f"STRICT MODE: Failed to fetch data for {symbol} futures. API/Network error.")
            return pd.DataFrame()

        self._cache[key]      = df
        self._cache_time[key] = datetime.now()
        return df.copy()

    def get_futures_vwap(self, symbol: str = "NIFTY") -> pd.Series:
        """
        Returns session VWAP computed from futures OHLCV.
        Use this in all strategy indicator computations instead of spot VWAP.
        """
        df = self.get_futures_ohlcv(symbol, "5minute", days=2)
        if df.empty:
            return pd.Series(dtype=float)
        today = datetime.now().date()
        today_df = df[df.index.date == today]
        if today_df.empty:
            return pd.Series(dtype=float)
        return compute_vwap(today_df)


# ── Technical Indicator Library ────────────────────────────────────────────────
# All indicators are pure pandas operations — no external TA libraries needed.

def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — resets each trading day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    # Group by date to reset VWAP daily
    result = pd.Series(index=df.index, dtype=float)
    for date, group in df.groupby(df.index.date):
        tp  = (group["high"] + group["low"] + group["close"]) / 3
        vol = group["volume"]
        cum_tp_vol = (tp * vol).cumsum()
        cum_vol    = vol.cumsum().replace(0, 1e-9)
        result.loc[group.index] = cum_tp_vol / cum_vol
    return result

def compute_supertrend(df: pd.DataFrame, period: int = 10,
                        multiplier: float = 3.0) -> pd.DataFrame:
    """
    Returns DataFrame with columns: ['supertrend', 'trend']
    trend =  1 → Bullish (price above supertrend)
    trend = -1 → Bearish (price below supertrend)
    """
    atr = compute_atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2

    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    supertrend = pd.Series(index=df.index, dtype=float)
    trend      = pd.Series(1, index=df.index, dtype=int)

    for i in range(1, len(df)):
        prev_st  = supertrend.iloc[i - 1] if not pd.isna(supertrend.iloc[i - 1]) else lower.iloc[i]
        prev_tr  = trend.iloc[i - 1]
        curr_cls = df["close"].iloc[i]

        if prev_tr == 1:
            cur_st = max(lower.iloc[i], prev_st)
        else:
            cur_st = min(upper.iloc[i], prev_st)

        if prev_tr == -1 and curr_cls > upper.iloc[i]:
            trend.iloc[i]      = 1
            cur_st             = lower.iloc[i]
        elif prev_tr == 1 and curr_cls < lower.iloc[i]:
            trend.iloc[i]      = -1
            cur_st             = upper.iloc[i]
        else:
            trend.iloc[i]      = prev_tr

        supertrend.iloc[i] = cur_st

    return pd.DataFrame({"supertrend": supertrend, "trend": trend}, index=df.index)

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (not direction)."""
    prev_high  = df["high"].shift(1)
    prev_low   = df["low"].shift(1)
    prev_close = df["close"].shift(1)

    plus_dm  = (df["high"] - prev_high).clip(lower=0)
    minus_dm = (prev_low - df["low"]).clip(lower=0)
    plus_dm  = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0)

    atr      = compute_atr(df, period)
    plus_di  = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean()  / atr.replace(0, 1e-9)
    minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / atr.replace(0, 1e-9)
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9))
    return dx.ewm(com=period - 1, adjust=False).mean()

def compute_bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Standard Bollinger Bands."""
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = sma + (std * std_dev)
    lower = sma - (std * std_dev)
    return pd.DataFrame({"lower": lower, "mid": sma, "upper": upper}, index=series.index)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mdp = MarketDataProvider()
    mtf = mdp.get_multi_timeframe("NIFTY")
    for tf, df in mtf.items():
        print(f"  {tf}: {len(df)} bars | last close: {df['close'].iloc[-1]:.2f}")

    df5 = mtf["5minute"]
    vwap = compute_vwap(df5)
    rsi  = compute_rsi(df5["close"])
    st   = compute_supertrend(df5)
    print(f"\n  VWAP (last): {vwap.iloc[-1]:.2f}")
    print(f"  RSI  (last): {rsi.iloc[-1]:.2f}")
    print(f"  Supertrend trend: {'BULL' if st['trend'].iloc[-1] == 1 else 'BEAR'}")
    print("\nMarketDataProvider PASS ✅")
