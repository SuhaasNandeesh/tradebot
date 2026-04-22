import os
import sys
import logging
from dotenv import load_dotenv

# Add the project root to the python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ReadinessCheck")

def verify():
    logger.info("🛡️ Starting Institutional Readiness Verification...")
    
    try:
        load_dotenv()
        
        # 1. Test Singletons
        from src.core.risk_manager import RiskManager
        from src.memory.journal import TradeJournal
        from src.data.market_data import MarketDataProvider
        from src.agents.strategy_selector import StrategySelector
        
        logger.info("✅ Core modules imported.")
        
        rm = RiskManager()
        logger.info(f"✅ RiskManager initialized (Lot sizes: NIFTY={rm.get_lot_size('NIFTY')}, SENSEX={rm.get_lot_size('SENSEX')})")
        
        journal = TradeJournal()
        logger.info("✅ TradeJournal (SQLite/RAG) initialized.")
        
        mdp = MarketDataProvider()
        logger.info("✅ MarketDataProvider initialized.")
        
        selector = StrategySelector(rm, journal, mdp)
        logger.info(f"✅ StrategySelector initialized with {len(selector.strategies)} strategies.")
        
        # 2. Check for common uninitialized attribute bugs
        context = {"sentiment": "BULLISH", "confidence_score": 80}
        # Dry run the signal logic (will likely return NONE/HOLD due to no live data, but shouldn't crash)
        strategy, signal, confluence = selector.select_with_confluence(context, symbol="NIFTY")
        logger.info(f"✅ Strategy Selector dry-run complete. Result: {strategy} -> {signal}")
        
        logger.info("🚀 SUCCESS: Dependency Graph Resolved. Agent is READY for trades.")
        return True
        
    except Exception as e:
        logger.error(f"❌ READINESS FAILED: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    if verify():
        sys.exit(0)
    else:
        sys.exit(1)
