import sys
import os
import logging

# Add project root to path
# Assuming we are in <root>/scratch/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.core.lmstudio_agent import LMStudioAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_null_context():
    agent = LMStudioAgent()
    print("Testing pre_trade_sanity with context=None...")
    try:
        # This used to crash at context.get('sentiment')
        result = agent.pre_trade_sanity(
            signal="BUY_CE",
            instrument="NIFTY",
            context=None,
            options_analysis={},
            confluence={}
        )
        print(f"SUCCESS: Result received: {result}")
        assert result["approved"] == True
        assert result["reason"] == "LM Studio unavailable"
    except AttributeError as e:
        print(f"FAILURE: Caught expected AttributeError: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"FAILURE: Caught unexpected exception: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_null_context()
    print("\nVerification PASSED ✅")
