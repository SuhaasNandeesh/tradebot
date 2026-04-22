import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class EnvConfig:
    """Centralized configuration derived from environment variables."""
    # Trading Defaults
    sl_pct: float                 = float(os.getenv("DEFAULT_SL_PCT", "0.01"))  # 1% default (Professional)
    target_pct: float             = float(os.getenv("DEFAULT_TARGET_PCT", "0.03")) # 3% default (Professional)
    atr_multiplier_sl: float      = float(os.getenv("ATR_MULTIPLIER_SL", "1.5"))
    atr_multiplier_tgt: float      = float(os.getenv("ATR_MULTIPLIER_TGT", "3.0"))
    max_hold_minutes: int         = int(os.getenv("MAX_HOLD_MINUTES", "90"))
    stagnant_threshold_pct: float = float(os.getenv("STAGNANT_THRESHOLD_PCT", "0.10"))
    
    # Portfolio Risk
    max_daily_loss: float         = float(os.getenv("MAX_DAILY_LOSS", "-5000"))
    max_position_size: int        = int(os.getenv("MAX_POSITION_SIZE", "2"))
    max_daily_trades: int         = int(os.getenv("MAX_DAILY_TRADES", "5"))
    vix_threshold: float          = float(os.getenv("VIX_THRESHOLD", "20.0"))
    consecutive_loss_halt: int    = int(os.getenv("CONSECUTIVE_LOSS_HALT", "3"))
    
    # Trailing Limits
    trail_sl_breakeven_pct: float = float(os.getenv("TRAIL_SL_BREAKEVEN_PCT", "0.50"))
    trail_sl_trail_pct: float     = float(os.getenv("TRAIL_SL_TRAIL_PCT", "0.80"))
    trail_sl_lock_pct: float      = float(os.getenv("TRAIL_SL_LOCK_PCT", "0.60"))
    expiry_early_exit_pct: float  = float(os.getenv("EXPIRY_EARLY_EXIT_PCT", "0.30"))

config = EnvConfig()
