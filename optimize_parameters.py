import optuna
import logging
from src.agents.sandbox_backtester import SandboxBacktester

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Optimizer")

class StrategyOptimizer:
    def __init__(self):
        self.backtester = SandboxBacktester()

    def objective(self, trial):
        # Hyperparameters to tune
        fast_ema = trial.suggest_int("fast_ema", 5, 15)
        slow_ema = trial.suggest_int("slow_ema", 20, 50)
        breakout_threshold = trial.suggest_float("breakout_threshold", 5.0, 20.0)

        # We inject these parameters into a dynamically generated strategy string
        strategy_code = f"""
class GeneratedStrategy:
    def __init__(self):
        self.name = "Optuna_EMA_Pullback"

    def generate_signal(self, df):
        if len(df) < {slow_ema}: return "HOLD"
        fast = df['last_price'].rolling({fast_ema}).mean().iloc[-1]
        slow = df['last_price'].rolling({slow_ema}).mean().iloc[-1]

        if fast > slow + {breakout_threshold}:
            return "BUY_CE"
        elif fast < slow - {breakout_threshold}:
            return "BUY_PE"
        return "HOLD"
"""
        # Run the backtester
        results = self.backtester.run_backtest(strategy_code)

        if not results.get("success", False) or not results.get("deployable", False):
            # Penalize heavily if it fails safety checks or is un-deployable
            return -9999.0

        # Optimize for Sharpe Ratio, but we could also optimize for win_rate or max_drawdown
        return results.get("sharpe_ratio", -9999.0)

    def optimize(self, n_trials=20):
        logger.info(f"Starting parameter optimization over {n_trials} trials...")
        study = optuna.create_study(direction="maximize")
        # To avoid blowing up test times, catch errors
        try:
            study.optimize(self.objective, n_trials=n_trials)

            logger.info("Optimization finished.")
            logger.info("Best trial:")
            trial = study.best_trial
            logger.info(f"  Value (Sharpe): {trial.value}")
            logger.info("  Params: ")
            for key, value in trial.params.items():
                logger.info(f"    {key}: {value}")

            return trial.params
        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            return None

if __name__ == "__main__":
    optimizer = StrategyOptimizer()
    optimizer.optimize(n_trials=5)
