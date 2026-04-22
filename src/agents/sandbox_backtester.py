import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
import os
import json
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ── Sandbox constants ─────────────────────────────────────────────────────────
SLIPPAGE_PCT    = 0.0005   # 0.05% per trade
BROKERAGE_FLAT  = 20.0     # ₹20 per order (Zerodha flat fee)
MIN_TRADES      = 5        # Strategy must generate at least 5 trades
MIN_WIN_RATE    = 0.40     # 40% minimum win rate
MIN_SHARPE      = 0.5      # Minimum annualized Sharpe ratio
MAX_TRADE_FREQ  = 0.20     # No more than 20% of bars can be entry signals


class SandboxBacktester:
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("KITE_API_KEY")
        self.access_token = self._load_access_token()
        self.kite = KiteConnect(api_key=self.api_key) if self.api_key and self.api_key != 'your_kite_api_key' else None
        if self.kite and self.access_token:
            self.kite.set_access_token(self.access_token)

    def _load_access_token(self):
        try:
            with open("kite_session.json", "r") as f:
                return json.load(f).get("access_token")
        except Exception:
            return None

    # ── Data Fetching ─────────────────────────────────────────────────────────

    def _get_historical_data(self, instrument_token: int = 256265, days: int = 30) -> pd.DataFrame:
        """
        Fetches real historical OHLCV candles from Kite for backtesting.
        Default: 30 days of 5-minute candles for NIFTY.
        """
        if not self.kite or not self.access_token:
            logger.warning("Kite session missing. Using mock data for Sandbox.")
            return self._generate_mock_data()

        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=days)
            
            # Kite historical_data(token, from, to, interval)
            # 5-minute interval limit is 60 days per request. 30 days is safe.
            logger.info(f"Fetching {days} days of historical data for token {instrument_token}...")
            records = self.kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval="5minute"
            )
            
            if not records:
                logger.warning(f"No records returned for {instrument_token}. Falling back to mock.")
                return self._generate_mock_data()
                
            df = pd.DataFrame(records)
            df = df.rename(columns={
                'date': 'timestamp', 
                'close': 'last_price',
                'open': 'open', 
                'high': 'high', 
                'low': 'low', 
                'volume': 'volume'
            })
            
            # Ensure timestamp is datetime and timezone-naive for simplicity in sandbox
            df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_localize(None)
            
            # Add synthetic VIX if missing (often not in historical OHLC)
            if 'vix' not in df.columns:
                df['vix'] = 15.0 # Neutral baseline
                
            logger.info(f"Successfully loaded {len(df)} real market bars.")
            return df
            
        except Exception as e:
            msg = str(e)
            if "Insufficient permissions" in msg or "TokenException" in msg:
                logger.error("!!! KITE HISTORICAL DATA ADD-ON NOT DETECTED !!!")
                logger.error("Please ensure you have purchased the 'Historical Data' add-on (₹2000/mo) in Kite Developer portal.")
            else:
                logger.error(f"Kite historical data fetch failed: {e}. Falling back to mock.")
            return self._generate_mock_data()

    def _generate_mock_data(self, periods: int = 500) -> pd.DataFrame:
        """Generates realistic random-walk OHLCV data if the API is unavailable."""
        dates = pd.date_range(end=datetime.now(), periods=periods, freq='5min')
        closes = [22000.0]
        for _ in range(periods - 1):
            chg = np.random.normal(0, 15)
            closes.append(max(closes[-1] + chg, 15000))

        opens  = [c + np.random.normal(0, 5) for c in closes]
        highs  = [max(o, c) + abs(np.random.normal(0, 8)) for o, c in zip(opens, closes)]
        lows   = [min(o, c) - abs(np.random.normal(0, 8)) for o, c in zip(opens, closes)]
        vols   = np.random.randint(5000, 50000, size=periods).tolist()
        vix    = [15.0 + abs(np.random.normal(0, 2)) for _ in range(periods)]

        return pd.DataFrame({
            'timestamp': dates, 'last_price': closes, 'open': opens,
            'high': highs, 'low': lows, 'volume': vols, 'vix': vix
        })

    # ── Code Execution (Sandboxed via RestrictedPython) ──────────────────────

    def _safe_exec(self, strategy_code: str) -> object:
        """
        Executes strategy code with RestrictedPython if available, 
        otherwise falls back to a plain exec() inside a try/except.
        The caller must NEVER pass untrusted code without RestrictedPython installed.
        """
        namespace = {}
        try:
            from RestrictedPython import compile_restricted, safe_globals
            from RestrictedPython.Guards import safe_builtins, guarded_iter_unpack_sequence
            safe_g = dict(safe_globals)
            safe_g['__builtins__'] = dict(safe_builtins)
            # Allow pandas & numpy inside strategy
            safe_g['__builtins__']['__import__'] = __import__
            byte_code = compile_restricted(strategy_code, '<strategy>', 'exec')
            exec(byte_code, safe_g)
            namespace = safe_g
        except ImportError:
            # RestrictedPython not installed — use plain exec with a warning
            logger.warning(
                "RestrictedPython not installed. Falling back to exec()."
                " Install it: pip install RestrictedPython"
            )
            exec(strategy_code, namespace)
        return namespace

    # ── Core Backtest Engine ──────────────────────────────────────────────────

    def run_backtest(self, strategy_code: str) -> dict:
        """
        Executes strategy code and simulates trades over historical OHLCV data.
        Computes real PnL with slippage + brokerage, Sharpe ratio, win rate,
        max drawdown, and applies a multi-factor approval gate.
        """
        logger.info("Starting Sandbox Backtest for generated strategy...")
        try:
            namespace = self._safe_exec(strategy_code)
            GeneratedStrategy = namespace.get("GeneratedStrategy")
            if not GeneratedStrategy:
                return {"success": False, "error": "Class 'GeneratedStrategy' not found.", "deployable": False}

            strategy_instance = GeneratedStrategy()
            df = self._get_historical_data()

            signals, trade_pnls = [], []
            entry_price = None
            active_signal = None
            peak_equity = 0.0
            max_drawdown = 0.0
            equity_curve = [0.0]

            for i in range(20, len(df)):
                window = df.iloc[:i].copy()
                try:
                    signal = strategy_instance.generate_signal(window)
                except Exception as e:
                    return {"success": False, "error": f"strategy.generate_signal() error: {e}", "deployable": False}

                signals.append(signal)
                current_price = df.iloc[i]['last_price']

                # Simulate entry
                if signal in ('BUY_CE', 'BUY_PE') and active_signal is None:
                    fill_price = current_price * (1 + SLIPPAGE_PCT)
                    entry_price = fill_price
                    active_signal = signal

                # Simulate exit on opposite signal or HOLD after being in trade
                elif active_signal is not None and signal == 'HOLD':
                    fill_price = current_price * (1 - SLIPPAGE_PCT)
                    gross_pnl = fill_price - entry_price
                    net_pnl = gross_pnl - (2 * BROKERAGE_FLAT / 65)  # per-unit brokerage
                    trade_pnls.append(net_pnl)
                    equity_curve.append(equity_curve[-1] + net_pnl)
                    peak_equity = max(peak_equity, equity_curve[-1])
                    drawdown = peak_equity - equity_curve[-1]
                    max_drawdown = max(max_drawdown, drawdown)
                    active_signal = None
                    entry_price = None

            # Close any open trade at end of data
            if active_signal is not None and entry_price is not None:
                last_price = df.iloc[-1]['last_price'] * (1 - SLIPPAGE_PCT)
                net_pnl = (last_price - entry_price) - (2 * BROKERAGE_FLAT / 65)
                trade_pnls.append(net_pnl)

            total_trades = len(trade_pnls)
            ce_count = signals.count('BUY_CE')
            pe_count = signals.count('BUY_PE')

            # ── Gate 1: Minimum trade count ───────────────────────────────────
            if total_trades < MIN_TRADES:
                return {
                    "success": False,
                    "reason": f"Too few trades ({total_trades} < {MIN_TRADES}). Strategy is inactive.",
                    "deployable": False,
                    "total_trades": total_trades,
                }

            # ── Gate 2: Over-trading ──────────────────────────────────────────
            if (ce_count + pe_count) > len(df) * MAX_TRADE_FREQ:
                return {
                    "success": False,
                    "reason": "Overtrading (hyperactive signal generator). Rejected.",
                    "deployable": False,
                    "total_trades": total_trades,
                }

            wins       = sum(1 for p in trade_pnls if p > 0)
            win_rate   = wins / total_trades if total_trades else 0
            total_pnl  = sum(trade_pnls)
            avg_win    = np.mean([p for p in trade_pnls if p > 0]) if wins else 0
            avg_loss   = np.mean([p for p in trade_pnls if p <= 0]) if (total_trades - wins) else 0

            # Sharpe: annualized from trade-level returns
            if len(trade_pnls) > 1 and np.std(trade_pnls) > 0:
                # Assume ~4 trades/day scale: 4 * 250 trading sessions/year
                periods_per_year = 4 * 250
                sharpe = (np.mean(trade_pnls) / np.std(trade_pnls)) * np.sqrt(periods_per_year)
            else:
                sharpe = 0.0

            # ── Gate 3: Win rate ──────────────────────────────────────────────
            if win_rate < MIN_WIN_RATE:
                logger.warning(f"Strategy rejected: win_rate={win_rate:.1%} < {MIN_WIN_RATE:.1%}")
                return {
                    "success": False,
                    "reason": f"Win rate too low ({win_rate:.1%}). Min required: {MIN_WIN_RATE:.1%}.",
                    "deployable": False,
                    "win_rate": round(win_rate, 4),
                    "total_trades": total_trades,
                    "total_pnl": round(total_pnl, 2),
                }

            # ── Gate 4: Sharpe ratio ──────────────────────────────────────────
            if sharpe < MIN_SHARPE:
                logger.warning(f"Strategy rejected: sharpe={sharpe:.2f} < {MIN_SHARPE}")
                return {
                    "success": False,
                    "reason": f"Sharpe ratio too low ({sharpe:.2f}). Min required: {MIN_SHARPE}.",
                    "deployable": False,
                    "sharpe": round(sharpe, 2),
                    "win_rate": round(win_rate, 4),
                    "total_trades": total_trades,
                    "total_pnl": round(total_pnl, 2),
                }

            logger.info(
                f"Strategy APPROVED | Trades: {total_trades} | "
                f"Win Rate: {win_rate:.1%} | Sharpe: {sharpe:.2f} | "
                f"PnL: ₹{total_pnl:.2f} | MaxDD: {max_drawdown:.2f}"
            )

            return {
                "success": True,
                "deployable": True,
                "total_trades": total_trades,
                "ce_trades": ce_count,
                "pe_trades": pe_count,
                "win_rate": round(win_rate, 4),
                "total_pnl": round(total_pnl, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "sharpe_ratio": round(sharpe, 2),
                "max_drawdown": round(max_drawdown, 2),
            }

        except Exception as e:
            logger.error(f"Sandbox Backtest failed: {e}")
            return {"success": False, "error": str(e), "deployable": False}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dummy_strategy = '''
class GeneratedStrategy:
    def generate_signal(self, df):
        if len(df) < 10: return "HOLD"
        fast = df['last_price'].rolling(5).mean().iloc[-1]
        slow = df['last_price'].rolling(20).mean().iloc[-1]
        if fast > slow + 10:
            return "BUY_CE"
        elif fast < slow - 10:
            return "BUY_PE"
        return "HOLD"
'''
    sb = SandboxBacktester()
    res = sb.run_backtest(dummy_strategy)
    import json as _json
    print(_json.dumps(res, indent=2))
