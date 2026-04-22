import math
import logging

logger = logging.getLogger(__name__)

def norm_cdf(x: float) -> float:
    """Cumulative distribution function for standard normal distribution."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def norm_pdf(x: float) -> float:
    """Probability density function for standard normal distribution."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

class GreekMonitor:
    """
    Computes Black-Scholes Greeks (Delta, Gamma, Vega, Theta) 
    using native math library to avoid heavy Scipy dependencies.
    """
    def __init__(self, risk_free_rate: float = 0.07):
        self.r = risk_free_rate  # Indian risk-free rate ~7%

    def calculate_greeks(self, option_type: str, S: float, K: float, T: float, sigma: float) -> dict:
        """
        S: Spot price
        K: Strike price
        T: Time to maturity in years (e.g. 1/365 = 1 day)
        sigma: Implied Volatility (annualized, e.g. 0.20 = 20%)
        """
        if T <= 0 or S <= 0 or K <= 0 or sigma <= 0:
            return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "price": 0}

        d1 = (math.log(S / K) + (self.r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        pdf_d1 = norm_pdf(d1)
        cdf_d1 = norm_cdf(d1)
        cdf_neg_d1 = norm_cdf(-d1)
        cdf_d2 = norm_cdf(d2)
        cdf_neg_d2 = norm_cdf(-d2)

        gamma = pdf_d1 / (S * sigma * math.sqrt(T))
        vega  = S * pdf_d1 * math.sqrt(T)

        if option_type.upper() == "CE":
            delta = cdf_d1
            # Theta in units per day
            theta = (- (S * sigma * pdf_d1) / (2 * math.sqrt(T)) 
                     - self.r * K * math.exp(-self.r * T) * cdf_d2) / 365.0
            price = S * cdf_d1 - K * math.exp(-self.r * T) * cdf_d2
        elif option_type.upper() == "PE":
            delta = cdf_d1 - 1.0
            theta = (- (S * sigma * pdf_d1) / (2 * math.sqrt(T)) 
                     + self.r * K * math.exp(-self.r * T) * cdf_neg_d2) / 365.0
            price = K * math.exp(-self.r * T) * cdf_neg_d2 - S * cdf_neg_d1
        else:
            raise ValueError("option_type must be CE or PE")

        return {
            "price": round(price, 2),
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 4),
            "vega":  round(vega, 4)
        }

    def estimate_iv(self, option_type: str, S: float, K: float, T: float, market_price: float, tol=1e-4, max_iter=100) -> float:
        """
        Estimates Implied Volatility using Newton-Raphson method.
        """
        if T <= 0 or market_price <= 0:
            return 0.0

        sigma = 0.25 # Initial guess 25%
        for i in range(max_iter):
            greeks = self.calculate_greeks(option_type, S, K, T, sigma)
            price_diff = greeks["price"] - market_price
            
            if abs(price_diff) < tol:
                return round(sigma, 4)
                
            vega = greeks["vega"]
            # To avoid division by zero
            if vega < 1e-6:
                break
                
            sigma = sigma - price_diff / vega
            if sigma <= 0:
                sigma = 0.01  # Floor IV to 1% if jumping into negatives
                
        return round(sigma, 4)
