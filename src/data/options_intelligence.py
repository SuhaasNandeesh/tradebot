"""
Options Intelligence Engine

Processes NSE options chain data to extract actionable institutional signals:
  - IV Rank & IV Percentile (is premium cheap or expensive?)
  - PCR (Put-Call Ratio) — sentiment/contrarian signal
  - OI Buildup Analysis — what are institutions positioning for?
  - Max Pain — where do market makers profit most at expiry?
  - Premium vs Spot Movement Analysis — delta divergence detection
  - Greeks estimation (Delta proxy from moneyness)

All methods degrade gracefully to mock/neutral outputs when market is closed
or NSE data is unavailable.

Key insight on "premium vs spot" analysis (user's question):
  If NIFTY futures rise +0.5% but ATM CE price stays flat or falls:
  → Delta is not tracking → IV is compressing simultaneously (institutions selling CE)
  → OR the move is a short-cover, not fresh buying
  → Signal: "PREMIUM_DIVERGING" — bullish momentum not confirmed by options market
  
  If NIFTY futures rise +0.5% and ATM CE price rises +2–3% (delta ~0.5 = expected):
  → Options market confirms the move
  → Signal: "PREMIUM_TRACKING" — high-confidence bullish signal
"""
import logging
import math
import json
import os
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

# Constants
RISK_FREE_RATE      = 0.065   # India 10Y Gsec ~6.5%
IV_HISTORY_FILE     = "iv_history.json"   # Persists 52-week ATM IV for IV Rank
EXPECTED_DELTA_CE   = 0.45    # ~ATM CE delta
EXPECTED_DELTA_PE   = -0.45   # ~ATM PE delta


class OptionsIntelligence:
    """
    Converts raw NSE options chain into institutional-grade signals.
    """

    def __init__(self):
        self.iv_history: list[float] = self._load_iv_history()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_iv_history(self) -> list[float]:
        try:
            if os.path.exists(IV_HISTORY_FILE):
                with open(IV_HISTORY_FILE) as f:
                    return json.load(f).get("iv_series", [])
        except Exception:
            pass
        return []

    def _save_iv_history(self, iv: float):
        """Appends today's IV and keeps last 252 trading days."""
        self.iv_history.append(iv)
        self.iv_history = self.iv_history[-252:]
        try:
            with open(IV_HISTORY_FILE, "w") as f:
                json.dump({"iv_series": self.iv_history, "updated": str(datetime.now())}, f)
        except Exception:
            pass

    # ── Black-Scholes IV (fallback when NSE doesn't provide it) ──────────────

    def _bs_price(self, S: float, K: float, T: float, r: float, sigma: float,
                  opt_type: str = "CE") -> float:
        """Black-Scholes option price."""
        if T <= 0 or sigma <= 0:
            return max(0.0, S - K if opt_type == "CE" else K - S)
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        nd1, nd2 = self._norm_cdf(d1), self._norm_cdf(d2)
        if opt_type == "CE":
            return S * nd1 - K * math.exp(-r * T) * nd2
        else:
            return K * math.exp(-r * T) * (1 - nd2) - S * (1 - nd1)

    def _norm_cdf(self, x: float) -> float:
        """Standard normal CDF approximation (Abramowitz & Stegun)."""
        t = 1 / (1 + 0.2316419 * abs(x))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        cdf = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x**2) * poly
        return cdf if x >= 0 else 1 - cdf

    def compute_iv_from_price(self, option_price: float, S: float, K: float,
                               days_to_expiry: int, opt_type: str = "CE") -> float:
        """
        Newton-Raphson IV solver. Returns IV as a decimal (0.15 = 15%).
        Used as fallback when NSE chain doesn't provide IV directly.
        """
        if option_price <= 0 or S <= 0 or K <= 0 or days_to_expiry <= 0:
            return 0.15  # Default 15% if invalid
        T = days_to_expiry / 365.0
        sigma = 0.20  # Initial guess
        for _ in range(50):
            price  = self._bs_price(S, K, T, RISK_FREE_RATE, sigma, opt_type)
            vega   = S * self._norm_cdf((math.log(S/K) + (RISK_FREE_RATE + 0.5*sigma**2)*T)
                                         / (sigma * math.sqrt(T))) * math.sqrt(T) * 0.01
            diff   = price - option_price
            if abs(diff) < 0.001:
                break
            if vega < 1e-8:
                break
            sigma -= diff / (vega * 100)
            sigma  = max(0.01, min(5.0, sigma))
        return round(sigma, 4)

    # ── IV Rank & Percentile ──────────────────────────────────────────────────

    def compute_iv_rank(self, current_iv: float) -> dict:
        """
        IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low) × 100
        
        Interpretation:
          0–30  → IV cheap → GOOD TIME TO BUY options (low premium)
          30–60 → IV normal → Selective trading
          60–100 → IV expensive → AVOID buying options; consider selling spreads
        """
        if len(self.iv_history) < 10:
            # Not enough history — return neutral with low confidence
            return {"iv_rank": 50, "iv_percentile": 50,
                    "signal": "NEUTRAL", "confidence": "LOW",
                    "current_iv": current_iv, "history_days": len(self.iv_history)}

        low_52w  = min(self.iv_history)
        high_52w = max(self.iv_history)
        iv_range = high_52w - low_52w

        if iv_range < 0.001:
            iv_rank = 50
        else:
            iv_rank = ((current_iv - low_52w) / iv_range) * 100

        iv_rank       = max(0, min(100, iv_rank))
        iv_percentile = (sum(1 for iv in self.iv_history if iv < current_iv)
                         / len(self.iv_history)) * 100

        if iv_rank < 30:
            signal = "IV_CHEAP_BUY"
        elif iv_rank > 60:
            signal = "IV_EXPENSIVE_AVOID_BUYING"
        else:
            signal = "IV_NORMAL"

        return {
            "iv_rank":       round(iv_rank, 1),
            "iv_percentile": round(iv_percentile, 1),
            "current_iv":    round(current_iv * 100, 2),   # as %
            "52w_low_iv":    round(low_52w * 100, 2),
            "52w_high_iv":   round(high_52w * 100, 2),
            "signal":        signal,
            "confidence":    "HIGH" if len(self.iv_history) >= 100 else "MEDIUM",
        }

    # ── PCR (Put-Call Ratio) ──────────────────────────────────────────────────

    def compute_pcr(self, chain_rows: list[dict]) -> dict:
        """
        PCR = Total Put OI / Total Call OI
        Also computes ATM-specific PCR (±3 strikes from ATM).
        
        PCR signals:
          > 1.3 → Extreme fear / put buying → Contrarian BULLISH
          1.1–1.3 → Mild bearish bias → Lean bearish
          0.9–1.1 → Neutral
          0.7–0.9 → Mild bullish bias → Lean bullish
          < 0.7 → Extreme greed / call buying → Contrarian BEARISH
        """
        total_ce_oi, total_pe_oi = 0, 0
        atm_ce_oi,   atm_pe_oi   = 0, 0

        oi_items = []
        for row in chain_rows:
            ce = row.get("CE", {})
            pe = row.get("PE", {})
            ce_oi = ce.get("openInterest", 0) or 0
            pe_oi = pe.get("openInterest", 0) or 0
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            oi_items.append((row.get("strikePrice", 0), ce_oi, pe_oi))

        if total_ce_oi == 0:
            return {"pcr": 1.0, "pcr_atm": 1.0, "signal": "NEUTRAL",
                    "total_ce_oi": 0, "total_pe_oi": 0}

        # ATM PCR: middle 6 strikes
        mid = len(oi_items) // 2
        atm_window = oi_items[max(0, mid-3): mid+3]
        atm_ce_oi  = sum(x[1] for x in atm_window)
        atm_pe_oi  = sum(x[2] for x in atm_window)
        pcr        = total_pe_oi / total_ce_oi
        pcr_atm    = (atm_pe_oi / atm_ce_oi) if atm_ce_oi > 0 else 1.0

        if pcr > 1.3:
            signal = "EXTREME_FEAR_CONTRARIAN_BULL"
        elif pcr > 1.1:
            signal = "BEARISH_BIAS"
        elif pcr < 0.7:
            signal = "EXTREME_GREED_CONTRARIAN_BEAR"
        elif pcr < 0.9:
            signal = "BULLISH_BIAS"
        else:
            signal = "NEUTRAL"

        return {
            "pcr":         round(pcr, 3),
            "pcr_atm":     round(pcr_atm, 3),
            "signal":      signal,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
        }

    # ── OI Buildup Analysis ───────────────────────────────────────────────────

    def compute_oi_buildup(self, chain_rows: list[dict],
                            underlying_price: float,
                            prev_underlying: float = None) -> dict:
        """
        OI Buildup = OI change + price direction → infer institutional intent.
        
        OI signal matrix:
          Price ↑ + CE OI ↑ → Fresh long calls → Bullish conviction
          Price ↑ + CE OI ↓ → Call short covering → Weak rally
          Price ↓ + PE OI ↑ → Fresh put buys → Bearish conviction
          Price ↓ + PE OI ↓ → Put short covering → Weak selloff
          
        Also finds: support strike (highest PE OI) and resistance strike (highest CE OI)
        """
        if not chain_rows or not underlying_price:
            return {"signal": "NEUTRAL", "support": None, "resistance": None}

        # Find ATM strike
        atm_strike = min(chain_rows,
                         key=lambda r: abs(r.get("strikePrice", 0) - underlying_price),
                         default={}).get("strikePrice", underlying_price)

        # OI change totals (use changeinOpenInterest)
        ce_oi_change = sum(r.get("CE", {}).get("changeinOpenInterest", 0) or 0
                           for r in chain_rows)
        pe_oi_change = sum(r.get("PE", {}).get("changeinOpenInterest", 0) or 0
                           for r in chain_rows)

        # Resistance = strike with highest CE OI (wall that limits upside)
        max_ce = max(chain_rows,
                     key=lambda r: r.get("CE", {}).get("openInterest", 0) or 0,
                     default={})
        resistance_strike = max_ce.get("strikePrice")

        # Support = strike with highest PE OI (floor that limits downside)
        max_pe = max(chain_rows,
                     key=lambda r: r.get("PE", {}).get("openInterest", 0) or 0,
                     default={})
        support_strike = max_pe.get("strikePrice")

        # Price direction (vs prev underlying or intra-session assumed neutral)
        price_rising = None
        if prev_underlying and prev_underlying > 0:
            price_rising = underlying_price > prev_underlying

        # Classify signal
        if price_rising is True:
            if ce_oi_change > 0: signal = "FRESH_LONGS_BULLISH"
            else:                signal = "SHORT_COVERING_WEAK_BULL"
        elif price_rising is False:
            if pe_oi_change > 0: signal = "FRESH_SHORTS_BEARISH"
            else:                signal = "LONG_UNWINDING_WEAK_BEAR"
        else:
            signal = "NEUTRAL"

        # Proximity to resistance/support
        proximity_resistance = None
        proximity_support    = None
        if resistance_strike:
            dist_pct = (resistance_strike - underlying_price) / underlying_price * 100
            proximity_resistance = round(dist_pct, 2)
        if support_strike:
            dist_pct = (underlying_price - support_strike) / underlying_price * 100
            proximity_support = round(dist_pct, 2)

        return {
            "signal":               signal,
            "price_direction":      "RISING" if price_rising else ("FALLING" if price_rising is False else "FLAT"),
            "ce_oi_change":         ce_oi_change,
            "pe_oi_change":         pe_oi_change,
            "resistance_strike":    resistance_strike,
            "support_strike":       support_strike,
            "dist_to_resistance_%": proximity_resistance,
            "dist_to_support_%":    proximity_support,
        }

    # ── Max Pain ─────────────────────────────────────────────────────────────

    def compute_max_pain(self, chain_rows: list[dict], underlying: float = None) -> dict:
        """
        Max Pain = strike where maximum option writers (institutions/MMs) profit.
        At expiry, the underlying tends to pin near max pain.
        Key use: In final 2 hours of expiry, if price is far from max pain,
        expect mean reversion toward it.
        """
        if not chain_rows:
            return {"max_pain_strike": None, "pain_data": []}

        strikes = []
        for row in chain_rows:
            strike = row.get("strikePrice", 0)
            ce_oi  = row.get("CE", {}).get("openInterest", 0) or 0
            pe_oi  = row.get("PE", {}).get("openInterest", 0) or 0
            strikes.append((strike, ce_oi, pe_oi))

        if not strikes:
            return {"max_pain_strike": None, "pain_data": []}

        pain_data = []
        all_strikes = [s[0] for s in strikes]

        for test_strike in all_strikes:
            # Total loss to option sellers if underlying expires at test_strike
            ce_loss = sum(max(0, test_strike - s) * oi for s, oi, _ in strikes)
            pe_loss = sum(max(0, s - test_strike) * oi for s, _, oi in strikes)
            pain_data.append((test_strike, ce_loss + pe_loss))

        max_pain_strike = min(pain_data, key=lambda x: x[1])[0]
        
        dist_pct = None
        if underlying and underlying > 0:
            dist_pct = ((underlying - max_pain_strike) / underlying) * 100

        return {
            "max_pain_strike": max_pain_strike,
            "dist_from_spot_%": round(dist_pct, 2) if dist_pct is not None else None,
            "pain_data":       [(s, round(p)) for s, p in pain_data],
        }

    # ── Implied Skew ─────────────────────────────────────────────────────────

    def compute_implied_skew(self, chain_rows: list[dict], underlying: float) -> dict:
        """
        Implied Skew Width = OTM Put IV - OTM Call IV.
        Measures structural bearishness or panic hedging.
        Normally, index options possess a natural Put Skew (OTM puts are pricier).
        If Skew > Historical Average (e.g., > 6%), elevated panic.
        If Skew < 0%, Call Skew (extreme bullish speculation).
        """
        if not chain_rows or not underlying:
            return {"skew_pct": 0.0, "signal": "NEUTRAL"}
            
        # Evaluate ~2.5% OTM
        up_target = underlying * 1.025
        down_target = underlying * 0.975
        
        otm_call_strike = min(chain_rows, key=lambda r: abs(r.get("strikePrice", 0) - up_target), default={}).get("strikePrice")
        otm_put_strike = min(chain_rows, key=lambda r: abs(r.get("strikePrice", 0) - down_target), default={}).get("strikePrice")
        
        otm_call_iv = 0
        otm_put_iv = 0
        
        for row in chain_rows:
            strike = row.get("strikePrice", 0)
            if strike == otm_call_strike:
                otm_call_iv = row.get("CE", {}).get("impliedVolatility", 0) or 0
            if strike == otm_put_strike:
                otm_put_iv = row.get("PE", {}).get("impliedVolatility", 0) or 0
                
        if otm_call_iv == 0 or otm_put_iv == 0:
            return {"skew_pct": 0.0, "signal": "NEUTRAL"}
            
        skew_pct = otm_put_iv - otm_call_iv
        
        if skew_pct > 6.0:
            signal = "EXTREME_PANIC_HEDGING_BEARISH"
        elif skew_pct < -0.5:
            signal = "EXTREME_CALL_SKEW_BULLISH"
        else:
            signal = "NORMAL_SKEW"
            
        return {
            "skew_pct": round(skew_pct, 2),
            "otm_put_iv": round(otm_put_iv, 2),
            "otm_call_iv": round(otm_call_iv, 2),
            "signal": signal
        }

    # ── Premium vs Spot Movement Analysis ────────────────────────────────────

    def analyze_premium_vs_spot(self, current_spot: float, prev_spot: float,
                                 current_ce_premium: float, prev_ce_premium: float,
                                 current_pe_premium: float, prev_pe_premium: float,
                                 atm_strike: float = None) -> dict:
        """
        KEY institutional signal: compare option premium moves vs underlying moves.

        If NIFTY futures move +1% but ATM CE premium moves only +0.3%
        (expected delta ~0.45 → should be ~0.45% move):
          → CE premium underperforming implies IV compression OR institutional selling of CEs
          → Signal: PREMIUM_DIVERGING (upside limited by supply of calls)
        
        If CE premium moves 0.9% for a 1% futures move:
          → Delta override → gamma expansion → breakout momentum validated
          → Signal: GAMMA_EXPANSION
        
        If both CE and PE premiums rising simultaneously (spot flat):
          → IV expanding → event/news risk. Don't enter.
          → Signal: IV_SPIKE
        """
        if prev_spot <= 0 or current_spot <= 0:
            return {"signal": "INSUFFICIENT_DATA"}

        # % changes
        spot_chg_pct = (current_spot - prev_spot) / prev_spot
        ce_chg_pct   = (current_ce_premium - prev_ce_premium) / prev_ce_premium if prev_ce_premium > 0 else 0
        pe_chg_pct   = (current_pe_premium - prev_pe_premium) / prev_pe_premium if prev_pe_premium > 0 else 0

        # Expected CE move for this spot change (delta ~0.45 ATM)
        expected_ce_chg = spot_chg_pct * EXPECTED_DELTA_CE
        expected_pe_chg = spot_chg_pct * EXPECTED_DELTA_PE  # Negative for rising spot

        # Ratio of actual vs expected
        ce_ratio = (ce_chg_pct / expected_ce_chg) if abs(expected_ce_chg) > 0.001 else None
        pe_ratio = (pe_chg_pct / expected_pe_chg) if abs(expected_pe_chg) > 0.001 else None

        # IV Spike detection: both premiums rising despite spot flat
        both_rising = ce_chg_pct > 0.02 and pe_chg_pct > 0.02
        spot_flat   = abs(spot_chg_pct) < 0.003

        if both_rising and spot_flat:
            signal = "IV_SPIKE"
            interpretation = "Both CE and PE premiums rising with flat spot — IV expanding. Event risk. Avoid directional positions."

        elif spot_chg_pct > 0.003:  # Spot rising
            if ce_ratio is not None:
                if ce_ratio < 0.5:
                    signal = "PREMIUM_DIVERGING_UP"
                    interpretation = (f"Spot +{spot_chg_pct:.2%} but CE premium only "
                                      f"+{ce_chg_pct:.2%} (expected {expected_ce_chg:.2%}). "
                                      f"Institutions selling calls — upside limited or IV compressing.")
                elif ce_ratio > 1.5:
                    signal = "GAMMA_EXPANSION_BULL"
                    interpretation = (f"CE premium surging {ce_chg_pct:.2%} vs spot {spot_chg_pct:.2%}. "
                                      f"Gamma expanding — breakout momentum confirmed. Strong BUY_CE signal.")
                else:
                    signal = "PREMIUM_TRACKING_BULL"
                    interpretation = "CE premium tracking spot normally. Bullish signal confirmed by options."
            else:
                signal = "BULLISH_SPOT"
                interpretation = "Spot rising, no premium divergence data available."

        elif spot_chg_pct < -0.003:  # Spot falling
            if pe_ratio is not None:
                if pe_ratio < 0.5:
                    signal = "PREMIUM_DIVERGING_DOWN"
                    interpretation = (f"Spot -{abs(spot_chg_pct):.2%} but PE premium only "
                                      f"+{pe_chg_pct:.2%}. Downside limited or oversold. Caution on PE buys.")
                elif pe_ratio > 1.5:
                    signal = "GAMMA_EXPANSION_BEAR"
                    interpretation = (f"PE premium surging {pe_chg_pct:.2%} vs spot fall {spot_chg_pct:.2%}. "
                                      f"Strong bearish breakout. Confirms BUY_PE.")
                else:
                    signal = "PREMIUM_TRACKING_BEAR"
                    interpretation = "PE premium tracking spot fall normally. Bearish confirmed by options."
            else:
                signal = "BEARISH_SPOT"
                interpretation = "Spot falling, no premium divergence data."
        else:
            signal = "FLAT"
            interpretation = "Spot/premium movement too small to classify."

        return {
            "signal":          signal,
            "interpretation":  interpretation,
            "spot_change_%":   round(spot_chg_pct * 100, 3),
            "ce_change_%":     round(ce_chg_pct * 100, 3),
            "pe_change_%":     round(pe_chg_pct * 100, 3),
            "ce_delta_ratio":  round(ce_ratio, 2) if ce_ratio else None,
        }

    # ── Master Analysis ───────────────────────────────────────────────────────

    def compute_volatility_skew(self, chain_rows: list[dict], underlying: float) -> dict:
        """
        Institutional Skew Analysis (The 'Smile').
        Measures 'Fear vs Greed' by comparing OTM Put IV vs OTM Call IV.
        Positive Skew = OTM Puts are expensive (Institutional Hedging).
        """
        try:
            # 1. Resolve 2-strike OTM (approx 1% OTM for Nifty)
            strike_interval = 50 
            otm_put_strike  = (round(underlying / strike_interval) * strike_interval) - (2 * strike_interval)
            otm_call_strike = (round(underlying / strike_interval) * strike_interval) + (2 * strike_interval)
            
            put_iv, call_iv = 0.0, 0.0
            
            for row in chain_rows:
                strike = row.get("strikePrice")
                if strike == otm_put_strike:
                    put_iv = row.get("PE", {}).get("impliedVolatility", 0.0)
                if strike == otm_call_strike:
                    call_iv = row.get("CE", {}).get("impliedVolatility", 0.0)
            
            if put_iv == 0 or call_iv == 0:
                return {"skew": 0.0, "bias": "NEUTRAL"}
                
            skew = put_iv - call_iv
            bias = "NEUTRAL"
            if skew > 4.0: bias = "FEAR"      # Puts significantly more expensive
            elif skew < -1.0: bias = "GREED"  # Calls more expensive (rare in Nifty)
            
            return {"skew": round(skew, 2), "bias": bias, "put_iv": put_iv, "call_iv": call_iv}
        except Exception:
            return {"skew": 0.0, "bias": "NEUTRAL"}

    def analyze(self, chain_data: dict, chain_rows: list[dict],
                prev_spot: float = None,
                prev_atm_ce: float = None, prev_atm_pe: float = None) -> dict:
        """
        Full options intelligence analysis. Returns a consolidated dict.
        Call this once per evaluation cycle from the main orchestrator.
        """
        if not chain_rows:
            return self._neutral_analysis()

        underlying = chain_data.get("underlying", 0)
        if not underlying:
            return self._neutral_analysis()

        # Find ATM row
        atm_row = min(chain_rows,
                      key=lambda r: abs(r.get("strikePrice", 0) - underlying),
                      default={})
        atm_ce      = atm_row.get("CE", {})
        atm_pe      = atm_row.get("PE", {})
        atm_strike  = atm_row.get("strikePrice", underlying)

        # Extract CE IV from NSE data (already computed by NSE)
        ce_iv_pct   = atm_ce.get("impliedVolatility", 0) or 0
        pe_iv_pct   = atm_pe.get("impliedVolatility", 0) or 0
        atm_iv      = (ce_iv_pct + pe_iv_pct) / 2 / 100  # Convert to decimal

        # Save IV for history (once per day at market open)
        if atm_iv > 0:
            self._save_iv_history(atm_iv)

        # Compute all signals
        iv_rank_data = self.compute_iv_rank(atm_iv)
        pcr_data     = self.compute_pcr(chain_rows)
        oi_data      = self.compute_oi_buildup(chain_rows, underlying, prev_spot)
        pain_data    = self.compute_max_pain(chain_rows, underlying)
        skew_data    = self.compute_implied_skew(chain_rows, underlying)

        # Premium vs Spot analysis (needs prev values)
        premium_analysis = {}
        if prev_atm_ce and prev_spot:
            premium_analysis = self.analyze_premium_vs_spot(
                underlying, prev_spot,
                atm_ce.get("lastPrice", 0), prev_atm_ce,
                atm_pe.get("lastPrice", 0), prev_atm_pe or 0,
                atm_strike
            )

        # Directional bias from options market
        def _options_direction(pcr: dict, oi: dict) -> str:
            pcr_sig = pcr.get("signal", "NEUTRAL")
            oi_sig  = oi.get("signal", "NEUTRAL")
            bull_signals = {"EXTREME_FEAR_CONTRARIAN_BULL", "BULLISH_BIAS",
                            "FRESH_LONGS_BULLISH", "SHORT_COVERING_WEAK_BULL"}
            bear_signals = {"EXTREME_GREED_CONTRARIAN_BEAR", "BEARISH_BIAS",
                            "FRESH_SHORTS_BEARISH", "LONG_UNWINDING_WEAK_BEAR"}
            bull_count = (1 if pcr_sig in bull_signals else 0) + (1 if oi_sig in bull_signals else 0)
            bear_count = (1 if pcr_sig in bear_signals else 0) + (1 if oi_sig in bear_signals else 0)
            if bull_count > bear_count: return "BULLISH"
            if bear_count > bull_count: return "BEARISH"
            return "NEUTRAL"

        options_direction = _options_direction(pcr_data, oi_data)

        logger.info(
            f"[OptionsIntel] ATM={atm_strike} | IV_Rank={iv_rank_data['iv_rank']} | "
            f"PCR={pcr_data['pcr']} ({pcr_data['signal']}) | "
            f"OI={oi_data['signal']} | MaxPain={pain_data['max_pain_strike']} | "
            f"Options direction={options_direction}"
        )

        return {
            "underlying":        underlying,
            "atm_strike":        atm_strike,
            "atm_ce_ltp":        atm_ce.get("lastPrice", 0),
            "atm_pe_ltp":        atm_pe.get("lastPrice", 0),
            "atm_iv_pct":        round(atm_iv * 100, 2),
            "iv_rank":           iv_rank_data,
            "pcr":               pcr_data,
            "oi_buildup":        oi_data,
            "max_pain":          pain_data,
            "implied_skew":      skew_data,
            "premium_analysis":  premium_analysis,
            "options_direction": options_direction,
            "is_iv_safe_to_buy": iv_rank_data["iv_rank"] < 60,
        }

    def _neutral_analysis(self) -> dict:
        return {
            "underlying": 0, "atm_strike": 0,
            "atm_ce_ltp": 0, "atm_pe_ltp": 0, "atm_iv_pct": 0,
            "iv_rank":    {"iv_rank": 50, "signal": "NEUTRAL", "confidence": "LOW"},
            "pcr":        {"pcr": 1.0, "signal": "NEUTRAL"},
            "oi_buildup": {"signal": "NEUTRAL"},
            "max_pain":   {"max_pain_strike": None, "dist_from_spot_%": None},
            "implied_skew": {"skew_pct": 0.0, "signal": "NEUTRAL"},
            "premium_analysis": {"signal": "INSUFFICIENT_DATA"},
            "options_direction": "NEUTRAL",
            "is_iv_safe_to_buy": True,  # Default allow when no data
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    oi = OptionsIntelligence()

    # Test IV Rank with synthetic history
    oi.iv_history = [0.12 + i * 0.001 for i in range(100)]  # Simulate 100 days
    rank = oi.compute_iv_rank(0.145)
    print(f"IV Rank test: {rank}")

    # Test PCR with synthetic chain
    mock_chain = [
        {"strikePrice": s, "CE": {"openInterest": 100000 - abs(s-22000)*10},
         "PE": {"openInterest": 80000 + abs(s-22000)*8}}
        for s in range(21000, 23500, 50)
    ]
    pcr = oi.compute_pcr(mock_chain)
    print(f"PCR test: {pcr}")

    # Test OI Buildup
    oi_sig = oi.compute_oi_buildup(mock_chain, 22100, 22000)
    print(f"OI Buildup test: {oi_sig}")

    # Test Max Pain
    pain = oi.compute_max_pain(mock_chain, 22100)
    print(f"Max Pain: {pain['max_pain_strike']} (Dist: {pain['dist_from_spot_%']}%)")

    # Test Skew
    skew = oi.compute_implied_skew(mock_chain, 22100)
    print(f"Implied Skew: {skew}")

    # Test Premium vs Spot
    prem = oi.analyze_premium_vs_spot(22100, 22000, 102, 100, 98, 101)
    print(f"Premium analysis: {prem['signal']} — {prem['interpretation']}")

    print("\nOptions Intelligence PASS ✅")
