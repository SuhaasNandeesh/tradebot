"""
BSE Options — SENSEX options via Kite BFO (BSE F&O) segment.

SENSEX options differ from NIFTY:
  - Exchange: BSE (not NSE)
  - Kite segment: BFO
  - Lot size: 20 (not 65)
  - Strike spacing: 200 points
  - Expiry: Fridays (not Thursdays like NIFTY)
  - Symbol format: SENSEX<DDMONYY><STRIKE>CE/PE

Note: SENSEX index itself is not traded. Only options on SENSEX are traded on BSE.
The underlying SENSEX value comes from BSE (not NSE).
"""
import os
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

BSE_SEGMENT    = "BFO"
SENSEX_LOT_SIZE = 20
SENSEX_STRIKE_SPACING = 200

# Kite instrument tokens for SENSEX futures (for VWAP reference)
# SENSEX_FUT token varies by expiry — fetch dynamically via get_instruments(exchange='BFO')
SENSEX_FUTURES_SYMBOL = "SENSEX"   # Base symbol in BFO segment


def round_to_sensex_strike(price: float) -> int:
    """Round SENSEX price to nearest 200-point strike."""
    return int(round(price / SENSEX_STRIKE_SPACING) * SENSEX_STRIKE_SPACING)


def get_sensex_expiry_code(d: date = None) -> str:
    """
    Returns the nearest Friday expiry code in Kite format: DDMONYY
    E.g., 28MAR25
    """
    # Per User Correction: SENSEX weekly expiry in 2026 is Thursday (weekday==3)
    days_ahead = (3 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7   # Already Thursday — next Thursday
    expiry = d.date() + timedelta(days=days_ahead) if hasattr(d, 'date') else d + timedelta(days=days_ahead)
    return expiry.strftime("%d%b%y").upper()   # e.g., 26MAR26


class BSEOptionsManager:
    """
    Manages SENSEX options lookup and execution via Kite BFO segment.
    Falls back to mock data if Kite is not configured.
    """

    def __init__(self, kite=None):
        self.kite = kite
        self._bfo_instruments = None

    def set_kite(self, kite):
        self.kite = kite
        self._bfo_instruments = None  # Reset cache

    def _get_bfo_instruments(self) -> list:
        """Fetches BFO (BSE F&O) instruments from Kite. Cached per session."""
        if self._bfo_instruments is not None:
            return self._bfo_instruments
        if not self.kite:
            logger.warning("[BSEOptions] Kite not configured. Cannot fetch BFO instruments.")
            return []
        try:
            instruments = self.kite.instruments("BFO")
            # Filter to SENSEX options only
            self._bfo_instruments = [
                i for i in instruments
                if i.get("name", "").upper() == "SENSEX"
                and i.get("instrument_type") in ("CE", "PE")
            ]
            logger.info(f"[BSEOptions] Loaded {len(self._bfo_instruments)} SENSEX BFO options")
            return self._bfo_instruments
        except Exception as e:
            logger.error(f"[BSEOptions] Failed to load BFO instruments: {e}")
            return []

    def get_sensex_atm_option(self, signal: str, spot: float,
                               num_strikes: int = 1) -> Optional[dict]:
        """
        Returns the ATM (or near-OTM) SENSEX option for the given signal.
        signal: 'BUY_CE' or 'BUY_PE'
        Returns dict with tradingsymbol, instrument_token, strike, etc.
        """
        atm_strike = round_to_sensex_strike(spot)
        opt_type   = "CE" if signal == "BUY_CE" else "PE"
        instruments = self._get_bfo_instruments()

        if not instruments:
            # Return mock for paper trading
            expiry_code = get_sensex_expiry_code()
            mock_sym    = f"SENSEX{expiry_code}{atm_strike}{opt_type}"
            return {
                "tradingsymbol":   mock_sym,
                "instrument_token": 0,
                "strike":           atm_strike,
                "instrument_type":  opt_type,
                "lot_size":         SENSEX_LOT_SIZE,
                "exchange":         "BFO",
                "is_mock":          True,
            }

        # Find instruments matching ATM strike and option type
        candidates = [
            i for i in instruments
            if i.get("strike") == atm_strike
            and i.get("instrument_type") == opt_type
        ]

        if not candidates:
            logger.warning(f"[BSEOptions] No BFO option found for SENSEX {atm_strike} {opt_type}")
            return None

        # Pick nearest expiry
        nearest = min(candidates, key=lambda x: x.get("expiry", date.max))
        nearest["lot_size"]  = SENSEX_LOT_SIZE
        nearest["exchange"]  = "BFO"
        nearest["is_mock"]   = False
        return nearest

    def get_sensex_spread_options(self, signal: str, spot: float,
                                   confluence_score: int = 75) -> tuple[Optional[dict], Optional[dict]]:
        """
        Returns (buy_leg, sell_leg) for a SENSEX spread order.
        Uses SpreadBuilder logic adapted for SENSEX 200-point spacing.
        """
        opt_type = "CE" if signal == "BUY_CE" else "PE"
        atm      = round_to_sensex_strike(spot)

        if confluence_score >= 85:
            sell_distance = SENSEX_STRIKE_SPACING * 3   # 600 pts OTM
        elif confluence_score >= 65:
            sell_distance = SENSEX_STRIKE_SPACING * 2   # 400 pts OTM
        else:
            sell_distance = SENSEX_STRIKE_SPACING        # 200 pts OTM

        if signal == "BUY_CE":
            sell_strike = atm + sell_distance
        else:
            sell_strike = atm - sell_distance

        buy_leg  = self.get_sensex_atm_option(signal, spot)
        # Get sell leg at the OTM strike
        sell_instruments = [
            i for i in self._get_bfo_instruments()
            if i.get("strike") == sell_strike and i.get("instrument_type") == opt_type
        ] or []

        if sell_instruments:
            sell_leg = min(sell_instruments, key=lambda x: x.get("expiry", date.max))
            sell_leg["lot_size"] = SENSEX_LOT_SIZE
            sell_leg["exchange"] = "BFO"
        else:
            # Mock sell leg for paper trading
            expiry_code = get_sensex_expiry_code()
            sell_leg = {
                "tradingsymbol":    f"SENSEX{expiry_code}{sell_strike}{opt_type}",
                "instrument_token": 0,
                "strike":           sell_strike,
                "instrument_type":  opt_type,
                "lot_size":         SENSEX_LOT_SIZE,
                "exchange":         "BFO",
                "is_mock":          True,
            }

        return buy_leg, sell_leg

    def get_lot_size(self) -> int:
        return SENSEX_LOT_SIZE

    def get_strike_spacing(self) -> int:
        return SENSEX_STRIKE_SPACING


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mgr = BSEOptionsManager()   # No Kite — uses mock

    # Test strike rounding
    assert round_to_sensex_strike(79750) == 79800
    assert round_to_sensex_strike(79900) == 79800
    assert round_to_sensex_strike(80100) == 80000
    print("[PASS] SENSEX strike rounding")

    # Test expiry code
    expiry = get_sensex_expiry_code()
    assert len(expiry) in (7, 8), f"Bad expiry code: {expiry}"
    print(f"[PASS] SENSEX expiry code: {expiry}")

    # Test ATM option (mock)
    opt = mgr.get_sensex_atm_option("BUY_CE", 80000)
    assert opt is not None
    assert opt["strike"] == 80000
    assert opt["instrument_type"] == "CE"
    assert opt["lot_size"] == 20
    print(f"[PASS] SENSEX ATM CE (mock): {opt['tradingsymbol']}")

    # Test spread (mock)
    buy, sell = mgr.get_sensex_spread_options("BUY_CE", 80000, confluence_score=80)
    assert buy["strike"] == 80000
    assert sell["strike"] == 80400  # 2 strikes OTM at 200 spacing
    print(f"[PASS] SENSEX spread: buy={buy['tradingsymbol']}, sell={sell['tradingsymbol']}")

    print("\nBSEOptionsManager PASS ✅")
