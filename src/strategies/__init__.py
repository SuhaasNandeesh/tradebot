"""
Institutional Strategy Engine for NIFTY 50 / SENSEX Intraday Options Trading.

Design philosophy:
  - 2-3 confirming indicators max (conviction without over-confluence)
  - Multi-timeframe: 15m trend direction + 5m entry timing
  - No trades in first 15 min (9:15-9:30) or after 2:30 PM
  - Anti-revenge trade: cooling-off after 2 consecutive losses
  - VIX gate: no option buying when VIX > 20
"""
