"""
BANKNIFTY Correlation Tracker

Institutional insight: NIFTY and BANKNIFTY normally move together (correlation > 0.85).
When they diverge (correlation drops below 0.70) it signals:
  - A sector-specific move (banking stocks being sold/bought disproportionately)
  - Institutional repositioning
  - Risk-off within financials even if broader market holds up

This is an early warning signal. When NIFTY-BANKNIFTY correlation drops:
  - Reduce confidence in NIFTY directional signals by 10 confluence points
  - Flag potential reversal risk

Uses 20-bar rolling log returns on 5-minute data.
"""
import math
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

DIVERGENCE_THRESHOLD    = 0.70   # Below this = divergence warning
STRONG_DIVERGENCE       = 0.50   # Below this = strong divergence (block trade)
CORRELATION_WINDOW      = 20     # 20 bars × 5min = 100 minutes


class CorrelationTracker:
    """
    Tracks rolling 20-bar correlation between NIFTY and BANKNIFTY returns.
    Feed it price updates every 5 minutes from the market evaluator.
    """

    def __init__(self, window: int = CORRELATION_WINDOW):
        self.window     = window
        self._nifty_px  = deque(maxlen=window + 1)   # +1 to compute returns
        self._bnifty_px = deque(maxlen=window + 1)

    def update(self, nifty_price: float, banknifty_price: float):
        """Feed current prices every 5 minutes. Auto-computes returns internally."""
        if nifty_price > 0:
            self._nifty_px.append(nifty_price)
        if banknifty_price > 0:
            self._bnifty_px.append(banknifty_price)

    def _log_returns(self, prices: deque) -> list[float]:
        """Compute log returns from price series."""
        px = list(prices)
        if len(px) < 2:
            return []
        return [math.log(px[i] / px[i-1]) for i in range(1, len(px))]

    def get_correlation(self) -> Optional[float]:
        """
        Returns Pearson correlation of log returns over the rolling window.
        Returns None if insufficient data (< 5 bars).
        """
        nr = self._log_returns(self._nifty_px)
        br = self._log_returns(self._bnifty_px)

        n = min(len(nr), len(br))
        if n < 5:
            return None   # Not enough data yet

        nr, br = nr[-n:], br[-n:]

        # Pearson correlation
        mean_n = sum(nr) / n
        mean_b = sum(br) / n
        cov    = sum((nr[i] - mean_n) * (br[i] - mean_b) for i in range(n))
        std_n  = math.sqrt(sum((x - mean_n) ** 2 for x in nr) or 1e-10)
        std_b  = math.sqrt(sum((x - mean_b) ** 2 for x in br) or 1e-10)

        corr = cov / (std_n * std_b)
        return max(-1.0, min(1.0, corr))

    def get_confluence_adjustment(self) -> int:
        """
        Returns a score adjustment for the confluence gate based on correlation:
          correlation > 0.70 → 0 (no adjustment, markets confirming each other)
          0.50-0.70          → -10 (divergence warning, reduce confidence)
          < 0.50             → -20 (strong divergence, block the trade)
        """
        corr = self.get_correlation()
        if corr is None:
            return 0   # Not enough data — no opinion

        if corr >= DIVERGENCE_THRESHOLD:
            return 0
        elif corr >= STRONG_DIVERGENCE:
            return -10
        else:
            return -20

    def get_status(self) -> dict:
        """Returns a status dict for use in Telegram /banknifty command."""
        corr = self.get_correlation()
        bars = min(len(self._nifty_px), len(self._bnifty_px))

        if corr is None:
            return {
                "correlation":  None,
                "status":       "INSUFFICIENT_DATA",
                "icon":         "⏳",
                "description":  f"Warming up ({bars}/{self.window + 1} bars collected)",
                "adjustment":   0,
            }

        if corr >= DIVERGENCE_THRESHOLD:
            icon   = "✅"
            status = "CONFIRMING"
            descr  = f"NIFTY & BANKNIFTY moving together ({corr:.2f}). Signal confidence intact."
        elif corr >= STRONG_DIVERGENCE:
            icon   = "⚠️"
            status = "DIVERGING"
            descr  = f"Divergence alert ({corr:.2f}). Confluence reduced by 10 pts. Caution."
        else:
            icon   = "🚨"
            status = "STRONG_DIVERGENCE"
            descr  = f"Strong divergence ({corr:.2f}). Trade blocked — conflicting market signals."

        return {
            "correlation":  round(corr, 3),
            "status":       status,
            "icon":         icon,
            "description":  descr,
            "adjustment":   self.get_confluence_adjustment(),
            "bars_used":    bars,
        }

    def format_telegram(self) -> str:
        """Returns Telegram-formatted status message."""
        s = self.get_status()
        if s["correlation"] is None:
            return f"📊 **BANKNIFTY Correlation**: {s['description']}"
        return (
            f"{s['icon']} **NIFTY–BANKNIFTY Correlation**: `{s['correlation']:.3f}`\n"
            f"Status: `{s['status']}` | Confluence adj: `{s['adjustment']:+d}` pts\n"
            f"{s['description']}"
        )


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    ct = CorrelationTracker(window=5)

    # Simulate highly correlated prices
    nifty_prices    = [22000, 22050, 22100, 22080, 22120, 22150]
    bnifty_prices   = [47000, 47100, 47220, 47180, 47280, 47350]
    for n, b in zip(nifty_prices, bnifty_prices):
        ct.update(n, b)

    corr = ct.get_correlation()
    status = ct.get_status()
    print(f"[PASS] Correlated scenario: corr={corr:.3f} | {status['status']}")
    assert corr is not None and corr > 0.90

    # Simulate divergence (NIFTY up, BANKNIFTY down)
    ct2 = CorrelationTracker(window=5)
    nifty2  = [22000, 22050, 22100, 22150, 22200, 22250]
    bnifty2 = [47000, 46950, 46900, 46850, 46800, 46750]   # Moving opposite
    for n, b in zip(nifty2, bnifty2):
        ct2.update(n, b)

    corr2 = ct2.get_correlation()
    adj2  = ct2.get_confluence_adjustment()
    print(f"[PASS] Divergence scenario: corr={corr2:.3f} | adj={adj2:+d} pts")
    assert corr2 is not None and corr2 < 0 and adj2 <= -20

    print(f"\n{ct.format_telegram()}")
    print("\nCorrelationTracker PASS ✅")
