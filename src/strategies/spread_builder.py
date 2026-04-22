"""
Spread Builder — builds bull/bear option spreads instead of naked options.

Why spreads over naked options:
  1. Defined max loss (net premium paid only)
  2. 40-60% lower cost vs naked options
  3. Profitable even on moderate moves (not just big moves)
  4. No gamma blow-up if IV spikes

Bull Call Spread (BUY_CE signal):
  BUY  ATM CE  (e.g., NIFTY 22000 CE)
  SELL OTM CE  (e.g., NIFTY 22200 CE — 2 strikes above)
  Net premium: ~₹100 bought - ₹40 sold = ₹60 net cost
  Max profit: (200 strike diff - 60 premium) × lots
  Max loss: ₹60 premium × lots

Bear Put Spread (BUY_PE signal):
  BUY  ATM PE  (e.g., NIFTY 22000 PE)
  SELL OTM PE  (e.g., NIFTY 21800 PE — 2 strikes below)
  Net premium: ~₹100 bought - ₹40 sold = ₹60 net cost
  Same risk/reward profile, bearish direction

Strike spacing for NIFTY: 50 points
Strike spacing for SENSEX: 200 points
Strike spacing for BANKNIFTY: 100 points

Conviction-based sell leg distance:
  Score ≥ 85: Sell 3 strikes OTM (wider spread — expect larger move)
  Score 65-85: Sell 2 strikes OTM (standard)
  Score < 65: Sell 1 strike OTM (tight spread — lower confidence)
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

STRIKE_SPACING = {
    "NIFTY":     50,
    "BANKNIFTY": 100,
    "SENSEX":    200,
}

USE_SPREADS    = os.getenv("USE_SPREAD_STRATEGY", "true").lower() == "true"
SPREAD_SELL_LEG_MULTIPLIER = int(os.getenv("SPREAD_SELL_LEG_STRIKES", "2"))  # OTM distance


def round_to_strike(price: float, spacing: int) -> int:
    """Round spot price to nearest valid strike."""
    return int(round(price / spacing) * spacing)


class SpreadBuilder:
    """
    Constructs bull/bear option spread order pairs.
    Returns structured dicts for ExecutionAgent to place as 2-leg orders.
    """

    def __init__(self):
        self.use_spreads = USE_SPREADS

    def get_spread_strikes(self, signal: str, spot: float,
                            instrument: str = "NIFTY",
                            confluence_score: int = 75,
                            iv_estimate: float = 15.0,
                            days_to_expiry: float = 3.0) -> dict:
        """
        Returns the buy and sell strike prices for a spread.
        
        Returns:
          {
            buy_strike:  int,   # ATM or near-ATM
            sell_strike: int,   # OTM leg
            spread_type: str,   # 'BULL_CALL_SPREAD' or 'BEAR_PUT_SPREAD'
            option_type: str,   # 'CE' or 'PE'
            max_spread_width: int,
          }
        """
        spacing   = STRIKE_SPACING.get(instrument.upper(), 50)
        atm       = round_to_strike(spot, spacing)

        # Base distance based on conviction
        if confluence_score >= 85:
            sell_leg_strikes = 3
        elif confluence_score >= 65:
            sell_leg_strikes = 2
        else:
            sell_leg_strikes = 1
            
        # 1. IV Adjustment: If IV is very high (>20%), IV crush is likely.
        # User requested: don't sell far OTM when IV is already high.
        if iv_estimate > 20.0:
            sell_leg_strikes = max(1, sell_leg_strikes - 1)
        # If IV is low (<12%), selling further OTM might be cheaper to build wide spreads
        elif iv_estimate < 12.0:
            sell_leg_strikes += 1

        # 2. Gamma Risk near expiry: 0-1 DTE has massive gamma spikes on OTM strikes.
        # Tighten spread to avoid blowout if pinned.
        if days_to_expiry <= 1.0:
            sell_leg_strikes = 1

        # Bound the spread logic
        sell_leg_strikes = min(max(sell_leg_strikes, 1), 4)

        sell_distance = spacing * sell_leg_strikes

        if signal == "BUY_CE":
            buy_strike  = atm
            sell_strike = atm + sell_distance   # Sell higher CE (profit capped above this)
            opt_type    = "CE"
            spread_type = "BULL_CALL_SPREAD"
        else:  # BUY_PE
            buy_strike  = atm
            sell_strike = atm - sell_distance   # Sell lower PE (profit capped below this)
            opt_type    = "PE"
            spread_type = "BEAR_PUT_SPREAD"

        return {
            "buy_strike":       buy_strike,
            "sell_strike":      sell_strike,
            "option_type":      opt_type,
            "spread_type":      spread_type,
            "max_spread_width": abs(sell_strike - buy_strike),
            "spacing":          spacing,
            "sell_leg_strikes": sell_leg_strikes,
        }

    def get_strike_for_conviction(self, signal: str, spot: float,
                                   instrument: str = "NIFTY",
                                   confluence_score: int = 75) -> int:
        """
        Returns the entry strike based on conviction level:
          Score ≥ 85 → Slightly OTM (lower cost, higher leverage)
          Score 65-85 → ATM (balanced)
          Score 40-65 → Slightly ITM (higher delta, less theta risk)
        
        For spread mode, this is the BUY leg strike.
        """
        spacing = STRIKE_SPACING.get(instrument.upper(), 50)
        atm     = round_to_strike(spot, spacing)

        if confluence_score >= 85:
            # High conviction: slightly OTM for leverage
            if signal == "BUY_CE":
                return atm + spacing      # 1 strike OTM
            else:
                return atm - spacing
        elif confluence_score >= 65:
            return atm                    # ATM — standard
        else:
            # Lower conviction: ITM for more delta protection from theta
            if signal == "BUY_CE":
                return atm - spacing      # 1 strike ITM
            else:
                return atm + spacing

    def format_symbol(self, instrument: str, strike: int,
                       option_type: str, expiry_code: str) -> str:
        """
        Format: NIFTY25MAR22000CE  (Kite NFO tradingsymbol format)
        expiry_code: e.g., '25MAR' or '2503' depending on Kite format
        """
        return f"{instrument.upper()}{expiry_code}{strike}{option_type}"

    def get_spread_summary(self, spread_strikes: dict, buy_premium: float,
                            sell_premium: float, lots: int,
                            lot_size: int) -> dict:
        """
        Computes P&L profile for a spread position.
        Returns max_profit, max_loss, breakeven for Telegram alert.
        """
        net_premium = buy_premium - sell_premium
        qty         = lots * lot_size
        max_loss    = net_premium * qty
        max_profit  = (spread_strikes["max_spread_width"] - net_premium) * qty
        breakeven   = spread_strikes["buy_strike"] + net_premium if "CE" in spread_strikes["option_type"] \
                      else spread_strikes["buy_strike"] - net_premium

        return {
            "net_premium":  round(net_premium, 2),
            "max_loss":     round(max_loss, 2),
            "max_profit":   round(max_profit, 2),
            "breakeven":    round(breakeven, 2),
            "rrr":          round(max_profit / max_loss, 2) if max_loss > 0 else 0,
        }


if __name__ == "__main__":
    sb = SpreadBuilder()

    # NIFTY at 22150, BULLISH, score=80, normal IV, 3 DTE
    spread = sb.get_spread_strikes("BUY_CE", 22150, "NIFTY", confluence_score=80, iv_estimate=15.0, days_to_expiry=3.0)
    print(f"Bull Call Spread (score=80): {spread}")
    assert spread["buy_strike"] == 22150
    assert spread["sell_strike"] == 22250   # 2 strikes OTM

    # High IV (reduce spread width)
    spread_high_iv = sb.get_spread_strikes("BUY_CE", 22150, "NIFTY", confluence_score=80, iv_estimate=25.0, days_to_expiry=3.0)
    assert spread_high_iv["sell_strike"] == 22200  # 1 strike OTM

    # Expiry Day Gamma clamp
    spread_0dte = sb.get_spread_strikes("BUY_CE", 22150, "NIFTY", confluence_score=95, iv_estimate=15.0, days_to_expiry=0.2)
    assert spread_0dte["sell_strike"] == 22200  # clamped to 1 strike OTM

    print("\nSpreadBuilder PASS ✅")
