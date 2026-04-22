# Gemini Project Context: TradeBot

TradeBot is an AI-augmented algorithmic trading platform specifically designed for the Indian equity and options markets (NSE/BSE). It integrates traditional quantitative strategies with modern LLM-based reasoning (Google Gemini, LMStudio) to perform market context analysis, strategy selection, and pre-trade sanity checks.

## Project Overview

- **Purpose:** Automate end-to-end trading—from market data ingestion and sentiment analysis to strategy execution and risk management.
- **Primary Markets:** National Stock Exchange (NSE) and Bombay Stock Exchange (BSE).
- **Broker Integration:** Zerodha Kite Connect API.
- **AI Integration:** Uses LangChain and LangGraph to coordinate agents. Employs Google Gemini for high-level reasoning and LMStudio for local LLM inference.
- **Data & Intelligence:** Analyzes options chains (PCR, Max Pain, OI buildup), news sentiment, and technical indicators.

## Core Architecture

The project follows an **Orchestrator** pattern (see `main.py`) that coordinates several specialized agents and managers:

### 1. Agents (`src/agents/`)
- **AgenticBrain:** The central cognitive unit coordinating multi-step reasoning.
- **ContextAgent:** Analyzes macro market sentiment, news, and global cues.
- **StrategySelector:** Matches the current market regime with the most appropriate trading strategy.
- **ExecutionAgent:** Handles order placement, modification, and tracking via the broker API.

### 2. Core Logic (`src/core/`)
- **RiskManager:** Enforces capital allocation rules, max drawdowns, and position sizing.
- **PositionManager:** Tracks open positions, P&L, and manages stop-loss/take-profit triggers.
- **GreekMonitor & GammaScalper:** Specialized tools for options trading and delta-neutral strategies.
- **MarketRegime:** Identifies if the market is trending, ranging, or volatile.

### 3. Data & Brokerage (`src/data/`, `src/broker/`)
- **KiteStreamer/KiteWS:** Handles real-time WebSocket data from Zerodha.
- **OptionsManager:** Manages options chains, strike selection, and greeks calculation.
- **NSEDataFetcher:** Pulls historical and real-time data from NSE.
- **MarketCalendar:** Tracks market holidays and trading sessions.

### 4. Strategies (`src/strategies/`)
Includes a variety of implementations:
- `supertrend_vwap.py`: Trend-following.
- `boomerang_reversion.py`: Mean reversion.
- `spread_builder.py`: Multi-leg options spreads.
- `orb.py`: Opening Range Breakout.

## Building and Running

### Prerequisites
- Python 3.11+
- Zerodha Kite Connect API credentials.
- Google Gemini API key or LMStudio running locally.

### Installation
```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration
Copy `.env.example` to `.env` and fill in the required API keys and configuration:
- `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_ACCESS_TOKEN`
- `GOOGLE_API_KEY`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

### Execution Commands
- **Start the Bot:** `python main.py`
- **Verify Readiness:** `python verify_readiness.py`
- **Run Dashboard:** `python dashboard.py`
- **Backtesting:** `python run_all_backtests.py`
- **Simulate Scenarios:** `python simulate_institutional_scenarios.py`

### Testing
The project uses `unittest` for integration and unit tests.
- **E2E Integration Test:** `python test_e2e_integration.py`
- **Run All Tests:** `pytest` (if installed) or `python -m unittest discover`

## Development Conventions

- **State Management:** Persistent state is stored in `trade_state.db` (SQLite).
- **Audit Logging:** All trades and significant events are logged in `trade_journal.db`.
- **Asynchronous Tasks:** Uses `APScheduler` for background tasks like heartbeat checks and data refreshes.
- **Safety First:** Employs `RestrictedPython` for safe execution of dynamic logic and a robust `RiskManager` to prevent catastrophic losses.
- **Logging:** Comprehensive logging is directed to `logs/bot_output.log`.
