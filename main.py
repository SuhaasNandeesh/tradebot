import os
os.environ["ANONYMIZED_TELEMETRY"] = "0"
os.environ["CHROMA_ANONYMIZED_TELEMETRY"] = "0"
os.environ["CHROMA_TELEMETRY"] = "0"
import sys
import time
import logging
import json
import requests
from datetime import datetime, time as dtime
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import warnings
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")

from src.broker.kite_ws import KiteStreamer
from src.bot.telegram_agent import TelegramAgent
from src.agents.context_agent import ContextAgent
from src.agents.execution_agent import ExecutionAgent
from src.agents.strategy_selector import StrategySelector
from src.memory.journal import TradeJournal
from src.core.risk_manager import RiskManager
from src.core.position_manager import PositionManager
from src.data.market_data import MarketDataProvider, compute_atr
from src.data.nse_data import NSEDataFetcher
from src.data.options_intelligence import OptionsIntelligence
from src.data.market_calendar import MarketCalendar
from src.broker.options_chain import OptionsManager
from src.broker.bse_options import BSEOptionsManager
from src.strategies.spread_builder import SpreadBuilder
from src.core.lmstudio_agent import LMStudioAgent
from src.core.greek_monitor import GreekMonitor
from src.core.gamma_scalper import GammaScalper
from src.core.correlation_tracker import CorrelationTracker
from src.agents.agentic_brain import AgenticBrain

# Setup logging with both file and console output
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format=log_format,
    handlers=[
        logging.FileHandler("logs/bot_output.log", mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self):
        load_dotenv()
        self.check_auth()
        self.scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
        self.executor = ThreadPoolExecutor(max_workers=4)

        # ── Shared Singletons ─────────────────────────────────────────────────
        self.risk_manager     = RiskManager()
        self.position_manager = PositionManager()
        self.journal          = TradeJournal()

        # ── Broker & Data ─────────────────────────────────────────────────────
        self.streamer         = KiteStreamer()
        self.data_provider    = MarketDataProvider()

        # ── Execution & Risk ──────────────────────────────────────────────────
        self.execution_agent  = ExecutionAgent(
            risk_manager     = self.risk_manager,
            position_manager = self.position_manager,
            streamer         = self.streamer,
        )
        self.gamma_scalper    = GammaScalper(self.risk_manager, self.execution_agent)

        # ── Options & Intelligence ────────────────────────────────────────────
        api_key = os.getenv("KITE_API_KEY", "")
        logger.info(f"DEBUG: KITE_API_KEY loaded: {api_key[:4]}...{api_key[-2:]}")
        self.options_manager  = OptionsManager(api_key) if api_key and api_key != "your_kite_api_key" else None
        if not self.options_manager:
            logger.error("DEBUG: options_manager failed to initialize!")
        else:
            logger.info("DEBUG: options_manager initialized successfully.")
        self.nse_fetcher      = NSEDataFetcher()
        self.options_intel    = OptionsIntelligence()
        self.bse_options      = BSEOptionsManager()
        self.spread_builder   = SpreadBuilder()
        self.market_calendar  = MarketCalendar(nse_fetcher=self.nse_fetcher)
        self.lm_agent         = LMStudioAgent()
        self.greek_monitor    = GreekMonitor()
        self.correlation_tracker = CorrelationTracker()

        # ── Agents ────────────────────────────────────────────────────────────
        self.context_agent    = ContextAgent()
        self.agentic_brain    = AgenticBrain(self)
        self.position_manager.sl_modify_callback = self.execution_agent.modify_sl_order
        self.strategy_selector = StrategySelector(
            risk_manager   = self.risk_manager,
            journal        = self.journal,
            data_provider  = self.data_provider,
        )

        # ── Telegram UI ───────────────────────────────────────────────────────
        self.telegram = TelegramAgent(
            broker           = self.streamer,
            risk_manager     = self.risk_manager,
            position_manager = self.position_manager,
            execution_agent  = self.execution_agent,
        )

        # ── State ─────────────────────────────────────────────────────────────
        self.current_context      = {"sentiment": "NEUTRAL", "confidence_score": 50, "key_drivers": ["Default Context"]}
        self.active_trade_id      = None
        self.active_strategy_name = None
        self.active_order_id      = None
        self.active_sell_order_id = None
        self.last_tick_eval_time  = 0
        self.last_nifty_price     = None
        self.streamer.tick_callback = self._on_market_tick
        self.streamer.on_reconnect_sync = self._sync_broker_state

        self.last_options_analysis = {}
        self.last_fii_signal       = "NEUTRAL"
        self.last_confluence       = {}
        self.session_advice        = {}
        self._wire_kite_to_data_provider()

    def _wire_kite_to_data_provider(self):
        try:
            if os.path.exists("kite_session.json"):
                with open("kite_session.json") as f:
                    session = json.load(f)
                api_key = os.getenv("KITE_API_KEY", "")
                if api_key and api_key != "your_kite_api_key":
                    from kiteconnect import KiteConnect
                    kite = KiteConnect(api_key=api_key)
                    kite.set_access_token(session.get("access_token", ""))
                    self.data_provider.set_kite(kite)
                    logger.info("MarketDataProvider wired to KiteConnect.")
        except Exception as e:
            logger.warning(f"Could not wire KiteConnect to data provider: {e}. Using mock data.")

    def check_kite_token(self) -> bool:
        try:
            if not os.path.exists("kite_session.json"): return False
            with open("kite_session.json", "r") as f:
                data = json.load(f)
                login_time_str = str(data.get("login_time", ""))
                today_str = datetime.now().strftime("%Y-%m-%d")
                return today_str in login_time_str
        except Exception as e:
            logger.error(f"Failed to check kite session: {e}")
            return False

    def alert(self, message: str):
        logger.info(f"[ALERT] {message}")
        if hasattr(self, 'telegram'): self.telegram.broadcast_message_sync(message)

    # ── Scheduled Routines ────────────────────────────────────────────────────

    def pre_market_routine(self):
        try:
            logger.info("💓 [HEARTBEAT] Starting pre_market_routine...")
            logger.info("=== PRE-MARKET ROUTINE STARTING ===")
            is_already_done = self.journal.is_pre_market_complete_today()
            
            self.risk_manager.reset_daily()
            self.strategy_selector.reset_session()
            self.data_provider.invalidate_cache()
            
            try:
                broker_positions = self.execution_agent.get_live_positions_from_broker()
                if broker_positions is not None:
                    self.position_manager.sync_with_broker(broker_positions)
                    logger.info(f"State Sync Complete. {len(self.position_manager.get_open_positions())} positions restored/active.")
                else:
                    logger.info(f"Skipping State Sync (Paper Trade or Error). Local positions: {len(self.position_manager.get_open_positions())}")
            except Exception as e:
                logger.error(f"Failed to sync broker positions: {e}")

            self.session_advice = self.market_calendar.get_session_advice()
            if not self.session_advice["should_trade"]:
                if not is_already_done: self.alert(f"🚨 **Market Closed**\n{self.session_advice['reason']}")
                return
            
            self.refresh_fii_dii()
            
            if not self.check_kite_token():
                if not is_already_done: self.alert("🚨 *Zerodha Token Missing or Expired!*")
            else:
                self._wire_kite_to_data_provider()
                if not is_already_done: self.alert("✅ Good Morning! Zerodha token validated.")
                
            self.current_context = self.context_agent.analyze_current_market()
            if isinstance(self.current_context, dict):
                if not is_already_done:
                    self.alert(f"🧠 *Daily Context Set*\nSentiment: `{self.current_context.get('sentiment', 'NEUTRAL')}`")
                    self.journal.mark_pre_market_complete_today()
        except Exception as e:
            logger.error(f"CRITICAL: Pre-market routine failure: {e}")
            if not is_already_done: self.alert(f"⚠️ **Pre-Market Intelligence Error**: {str(e)}")

    def position_monitor_job(self):
        try:
            logger.debug("💓 [HEARTBEAT] Starting position_monitor_job...")
            if not self.streamer.is_connected: return
            vix = self.streamer.get_vix()
            if vix: 
                self.risk_manager.update_vix(vix)
                self.position_manager.update_volatility_regime(vix)
            
            is_paper = os.getenv("PAPER_TRADE", "true").lower() == "true"
            unrealized = self.position_manager.get_total_unrealized_pnl()
            today_pnl = self.journal.get_todays_pnl(is_paper_trade=is_paper)
            self.risk_manager.update_daily_pnl(today_pnl, unrealized)

            # Institutional Gamma & Shock Checks
            self._run_risk_checks()

            # Exit logic
            exit_signals = self.position_manager.update_prices(self.streamer.latest_ticks)
            for sig in exit_signals:
                if self.execution_agent.close_position(sig["order_id"]):
                    closed_pnl = self.position_manager.get_total_unrealized_pnl()
                    fill_p = self.streamer.get_nifty() or 0.0
                    self.journal.log_trade_end(sig["order_id"], closed_pnl, fill_price=fill_p)
                    self.alert(f"✅ **Position Closed** | P&L: `₹{closed_pnl:.2f}` | Fill: {fill_p}")
        except Exception as e:
            logger.error(f"Position monitor job failure: {e}")

    def _run_risk_checks(self):
        # Institutional Gamma Scalping
        net_delta = self.risk_manager.current_delta
        hedge_order = self.gamma_scalper.evaluate_drift(net_delta, "NIFTY")
        if hedge_order: self.alert(f"🌀 **Gamma Scalp Triggered**: {hedge_order['reason']}")

        # Institutional Shock Testing
        nifty = self.streamer.get_nifty()
        if nifty:
            shock_res = self.risk_manager.stress_test_portfolio(nifty)
            if shock_res["status"] == "CRITICAL":
                self.alert(f"🚨 **SHOCK BREACH**: Projected {shock_res['shock_pnl']:.0f} loss on crash. BOT PAUSED.")

    # ── Core Evaluation Loop ──────────────────────────────────────────────────

    def live_market_evaluator(self):
        """Main decision engine: Agentic Brain takes over reasoning."""
        try:
            logger.info("💓 [HEARTBEAT] Starting live_market_evaluator cycle...")
            if not self._is_market_open():
                logger.debug("[_evaluator] Market closed.")
                return

            # 1. System Health & Risk Checks (Basic Gating to save tokens)
            can_trade = self.risk_manager.can_trade()
            has_pos = self.position_manager.has_open_positions()

            if not can_trade or has_pos:
                reason = "Risk Gate" if not can_trade else "Position active"
                logger.debug(f"[_evaluator] Hold: {reason}")
                return

            nifty = self.streamer.get_nifty() or self.last_nifty_price
            if not nifty: return

            # 2. Sync essential data for tools
            try:
                chain_data = self.nse_fetcher.get_options_chain("NIFTY")
                if chain_data:
                    chain_rows = self.nse_fetcher.parse_chain_for_expiry(chain_data)
                    self.last_options_analysis = self.options_intel.analyze(chain_data, chain_rows)
            except Exception: pass

            # 2.5 Ensure valid context for agents
            if not self.current_context or self.current_context.get("key_drivers") == ["Default Context"]:
                logger.info("🧠 Context missing or stale. Lazy-loading market intelligence...")
                try:
                    self.current_context = self.context_agent.analyze_current_market()
                except Exception as e:
                    logger.error(f"Lazy context load failed: {e}")

            # 3. Trigger Agentic Brain Thinking Process
            logger.info("🧠 Orchestrator: Delegating to Agentic Brain...")
            result = self.agentic_brain.run_iteration()
            
            # 4. Handle Agent Decision
            if "DECISION_REACHED" in result:
                try:
                    decision_json = result.split("DECISION_REACHED: ")[1]
                    decision = json.loads(decision_json)
                    
                    if decision.get("action") == "TRADE":
                        strategy_name = decision["strategy"]
                        signal_side = decision["side"]
                        confidence = decision["confidence"]
                        reasoning = decision["reasoning"]

                        mock_confluence = {
                            "approved": True,
                            "score": confidence,
                            "regime": "AGENTIC",
                            "verdict": f"✅ AGENTIC: {reasoning}",
                            "narrative": reasoning
                        }
                        
                        logger.info(f"🚀 Agentic Brain Decision: {signal_side} | Confidence: {confidence}")
                        
                        # LOG SUCCESS EVALUATION
                        self.journal.log_evaluation(
                            strategy_name = strategy_name,
                            signal = signal_side,
                            reason = reasoning,
                            confluence_score = float(confidence),
                            regime = "AGENTIC",
                            context = self.current_context
                        )
                        
                        if os.getenv("E2E_SYNC_MODE", "false").lower() == "true":
                            self._async_nifty_execution(strategy_name, signal_side, mock_confluence, confidence, self.current_context, nifty)
                        else:
                            self.executor.submit(self._async_nifty_execution, strategy_name, signal_side, mock_confluence, confidence, self.current_context, nifty)
                except Exception as e:
                    logger.error(f"Failed to parse Agentic Brain decision: {e}")
            else:
                self._log_eval_hold("AgenticBrain", "No trade proposed by agent", {"score": 0}, self.current_context)
                
        except Exception:
            import traceback
            logger.error(f"❌ live_market_evaluator crashed:\n{traceback.format_exc()}")

    def _log_eval_hold(self, strategy: str, reason: str, confluence: dict = None, context: dict = None):
        """Helper to log evaluation attempts that result in HOLD."""
        conf = confluence or {}
        self.journal.log_evaluation(
            strategy_name    = strategy,
            signal           = "HOLD",
            reason           = str(reason),
            confluence_score = conf.get("score", 0.0),
            regime           = conf.get("regime", "UNKNOWN"),
            context          = context
        )

    def _is_market_open(self) -> bool:
        return dtime(9, 15) <= datetime.now().time() <= dtime(15, 30)

    # ── Execution Logic ───────────────────────────────────────────────────────

    def _async_nifty_execution(self, strategy_name, signal, confluence, adj_score, ctx, nifty):
        """Handles the multi-agent debate and final order placement."""
        try:
            logger.info(f"⚡ [_async_nifty_execution] START: {strategy_name} | {signal}")
            
            # Round 1: Multi-Agent Debate
            debate_context = f"Strat: {strategy_name} | Sig: {signal} | Nifty: {nifty} | Conf: {adj_score}"
            debate_result = self.lm_agent.debate_trade(signal, debate_context)
            logger.info(f"⚡ [Debate] Consensus: {debate_result[:50]}")
            
            if "CONSENSUS: AVOID" in debate_result.upper():
                self._abort_trade(strategy_name, signal, f"Debate Rejected: {debate_result[:100]}", adj_score, confluence, ctx)
                return

            # Round 2: LLM Sanity Check
            sanity = self.lm_agent.pre_trade_sanity(signal, "NIFTY", ctx, self.last_options_analysis, confluence)
            logger.info(f"⚡ [Sanity] Approved: {sanity['approved']}")
            
            if not sanity["approved"]:
                self._abort_trade(strategy_name, signal, f"Sanity Rejected: {sanity['narrative'][:100]}", adj_score, confluence, ctx)
                return
                
            # Round 3: Instrument & Capital Resolution
            force_spread = self.risk_manager.should_force_spreads("NIFTY")
            
            if force_spread:
                logger.info("🛡️ [Risk] Expiry detected. Resolving SPREAD legs.")
                # We use the existing spread_builder logic
                strikes = self.execution_agent.spread_builder.get_spread_strikes(str(signal), nifty, "NIFTY", adj_score)
                buy_details = self.resolve_instrument_details_by_strike("NIFTY", strikes["buy_strike"], strikes["option_type"])
                sell_details = self.resolve_instrument_details_by_strike("NIFTY", strikes["sell_strike"], strikes["option_type"])
                
                if not buy_details or not sell_details:
                    self._abort_trade(strategy_name, signal, "Failed to resolve spread legs", adj_score, confluence, ctx)
                    return
                
                details = buy_details # Primary leg for capital calc
            else:
                details = self.resolve_instrument_details("NIFTY", nifty, signal)
            
            logger.info(f"⚡ [Resolution] Instrument: {details.get('tradingsymbol','FAILED')}")
            
            if not details:
                self._abort_trade(strategy_name, signal, "Failed to resolve contract", adj_score, confluence, ctx)
                return

            # Order Entry Logic
            available_capital = self.execution_agent.get_available_capital()
            tick = self.streamer.latest_ticks.get(details["instrument_token"])
            premium = tick.get("last_price") if tick else 100.0
            
            # For spreads, margin is complex, but for this agent we use a simplified model
            lots = self.risk_manager.calculate_position_size(available_capital, premium, "NIFTY")
            qty = lots * self.risk_manager.get_lot_size("NIFTY")
            
            if qty <= 0:
                self._abort_trade(strategy_name, signal, f"Insufficient Capital (₹{available_capital:.0f})", adj_score, confluence, ctx)
                return

            if force_spread:
                logger.info(f"⚡ [Execution] Entering SPREAD: {buy_details['tradingsymbol']} & {sell_details['tradingsymbol']}")
                order_id = self.execution_agent.enter_spread(
                    buy_symbol=buy_details['tradingsymbol'],
                    sell_symbol=sell_details['tradingsymbol'],
                    instrument="NIFTY",
                    transaction_type="BUY", # Spread is a net buy
                    quantity=qty,
                    entry_price=nifty,
                    strategy_id=strategy_name,
                    buy_token=buy_details['instrument_token'],
                    sell_token=sell_details['instrument_token']
                )
            else:
                logger.info(f"⚡ [Execution] Entering position: {details['tradingsymbol']}")
                # Robust side extraction
                sig_side = signal["side"] if isinstance(signal, dict) else str(signal)
                transaction_type = "BUY" if "BUY" in sig_side.upper() else "SELL"
                
                # 3.5 Calculate ATR for institutional dynamic risk
                atr = 15.0 # Default fallback
                try:
                    df_5m = self.data_provider.get_ohlcv("NIFTY", "5minute", days=3)
                    if not df_5m.empty:
                        atr_series = compute_atr(df_5m, period=14)
                        atr = float(atr_series.iloc[-1])
                except Exception as e:
                    logger.warning(f"Failed to compute ATR, using fallback: {e}")

                order_id = self.execution_agent.enter_position(
                    tradingsymbol = details["tradingsymbol"], 
                    instrument = "NIFTY", transaction_type = transaction_type, 
                    quantity = qty, entry_price = nifty, 
                    strategy_id = strategy_name, instrument_token = details["instrument_token"],
                    atr = atr
                )
            
            if order_id:
                logger.info(f"⚡ [Finalize] Entry Success! ID: {order_id}")
                self._finalize_entry(order_id, details["tradingsymbol"], signal, ctx, strategy_name, nifty, adj_score, confluence)
            else:
                logger.error(f"⚡ [Execution] API Entry Failed for {details['tradingsymbol']}")
        except Exception:
            import traceback
            logger.error(f"❌ _async_nifty_execution crashed:\n{traceback.format_exc()}")

    def _abort_trade(self, strat, sig, reason, score, conf, ctx):
        """Centralized trade abortion logic."""
        self.alert(f"⚠️ **Trade Aborted**: {reason}")
        self.journal.log_blocked_signal(strat, sig, reason, ctx)
        self.journal.log_evaluation(strat, sig, reason, score, conf.get("regime", "UNKNOWN"), ctx)

    def _finalize_entry(self, order_id, symbol, signal, ctx, strat, price, score, conf):
        """Finalizes logging after a successful entry."""
        logger.info(f"⚡ Entering _finalize_entry for {symbol} | ID: {order_id}")
        self.active_trade_id = order_id
        
        # Ensure signal is a string if it's passed as a dict
        sig_str = signal.get("side", str(signal)) if isinstance(signal, dict) else str(signal)
        
        try:
            tid = self.journal.log_trade_start(
                symbol=symbol, signal=sig_str, context_str=json.dumps(ctx),
                strategy_code=strat, strategy_id=strat,
                is_paper_trade=os.getenv("PAPER_TRADE", "true").lower() == "true",
                decision_price=price
            )
            logger.info(f"⚡ Journal Entry Created: ID {tid}")
            self.journal.log_evaluation(strat, sig_str, "SUCCESS", score, conf.get("regime", "UNKNOWN"), ctx)
            self.alert(f"📈 **Trade Entered - `{symbol}`** | Price: {price}")
        except Exception as e:
            logger.error(f"⚡ CRITICAL: Finalize Entry failed: {e}")

    def resolve_instrument_details_by_strike(self, name: str, strike: float, side: str) -> dict:
        """Resolves specific instrument details for a given strike and side."""
        if not self.options_manager: return {}
        try:
            options = self.options_manager.get_options_symbols(name, strike, 50, num_strikes=1)
            target_type = "CE" if "CE" in side.upper() else "PE"
            for opt in options:
                # Precision match on strike
                if opt["instrument_type"] == target_type and abs(opt["strike"] - strike) < 1.0:
                    return opt
            return {}
        except Exception as e:
            logger.error(f"❌ resolve_instrument_details_by_strike Error: {e}")
            return {}

    def resolve_instrument_details(self, name: str, spot_price: float, side: str) -> dict:
        """Dynamically resolves the ATM strike contract details."""
        if not self.options_manager:
            logger.error("❌ resolve_instrument_details: options_manager is None")
            return {}
        try:
            interval = 50 if name == "NIFTY" else 100
            options = self.options_manager.get_options_symbols(name, spot_price, interval, num_strikes=0)
            logger.debug(f"resolve_instrument_details: Found {len(options)} options for {name} @ {spot_price}")
            
            target_type = "CE" if "CE" in side else "PE"
            for opt in options:
                if opt["instrument_type"] == target_type: return opt
            
            logger.error(f"❌ resolve_instrument_details: No {target_type} found in options list")
            return {}
        except Exception as e:
            logger.error(f"❌ resolve_instrument_details: Error: {e}")
            return {}

    # ── System Management ─────────────────────────────────────────────────────


    def _sync_broker_state(self):
        """Re-fetches and syncs live positions from broker after a reconnect."""
        try:
            logger.info("🔄 [RECONCILIATION] Syncing broker state after WebSocket reconnect...")
            broker_positions = self.execution_agent.get_live_positions_from_broker()
            if broker_positions is not None:
                self.position_manager.sync_with_broker(broker_positions)
                logger.info(f"🔄 State Sync Complete. {len(self.position_manager.get_open_positions())} active.")
        except Exception as e:
            logger.error(f"Failed to sync broker state: {e}")

    def _on_market_tick(self, ticks):
        if not self._is_market_open(): return
        if not self.risk_manager.can_trade(): return
        now = time.time()
        for t in ticks:
            if t.get("instrument_token") == 256265:
                current_price = t.get("last_price")
                if not current_price: continue
                if self.last_tick_eval_time == 0 or now - self.last_tick_eval_time >= 300 or abs(current_price - self.last_nifty_price) >= 25.0:
                    self.last_tick_eval_time, self.last_nifty_price = now, current_price
                    self.executor.submit(self.live_market_evaluator)

    def singleton_guard(self):
        """Ensures only one instance of the bot runs."""
        import signal
        pid_file = "tradebot.pid"
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    old_pid = int(f.read().strip())
                if old_pid != os.getpid():
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(2)
                    os.kill(old_pid, signal.SIGKILL)
            except: pass
        with open(pid_file, "w") as f: f.write(str(os.getpid()))

    def system_watchdog_job(self):
        """Ensures the system isn't 'trading blind' by catching silent freezes."""
        try:
            logger.debug("💓 [HEARTBEAT] Starting system_watchdog_job...")
            if not self.streamer.is_connected: return

            if not self._is_market_open():
                return

            now = time.time()
            stall_duration = now - self.streamer.last_tick_time

            # Watchdog 1: Absolute silence from WebSocket wrapper
            if stall_duration > 15:
                logger.critical(f"🚨 SYSTEM STALL: No ticks received at all for {stall_duration:.0f}s. Restarting WS.")
                self.alert(f"⚠️ **DATA STALL**: System blind for {stall_duration:.0f}s. Reconnecting...")
                self.streamer.stop()
                time.sleep(1)
                self.streamer.start()
                return

            # Watchdog 2: Silent Freeze (Exchange timestamps are old despite receiving network pings)
            # We check NIFTY50 (Token 256265) as our pulse
            nifty_tick = self.streamer.latest_ticks.get(256265)
            if nifty_tick and "_recv_time" in nifty_tick:
                exchange_ts = nifty_tick.get("exchange_timestamp")
                if exchange_ts:
                    # exchange_ts is a datetime object from Kite
                    tick_age = (datetime.now() - exchange_ts).total_seconds()
                    if tick_age > 10.0:
                        logger.critical(f"🚨 SILENT FREEZE: Ticks arriving but exchange_timestamp is {tick_age:.0f}s old. Restarting WS.")
                        self.alert(f"⚠️ **EXCHANGE LAG**: Data delayed by {tick_age:.0f}s. Restarting stream...")
                        self.streamer.stop()
                        time.sleep(1)
                        self.streamer.start()

        except Exception as e: logger.error(f"Watchdog failure: {e}")

    def force_eval_job(self):
        """Institutional Heartbeat: Forces evaluation every 1m."""
        if self._is_market_open():
            logger.info("💓 HEARTBEAT: Forcing market evaluation...")
            self.live_market_evaluator()

    def eod_square_off_job(self):
        """Institutional EOD Kill-Switch: Forces all positions to close at 15:15."""
        logger.warning("🕒 EOD DEADLINE REACHED (15:15). Initiating hard square-off...")
        self.alert("🕒 **EOD Deadline Reached (3:15 PM)**. Squaring off all positions and halting for the day.")

        # 1. Activate Circuit Breaker to prevent new entries
        self.risk_manager.activate_circuit_breaker("EOD Market Close")

        # 2. Force close all positions
        closed_positions = self.execution_agent.square_off_all()
        for pos in closed_positions:
            closed_pnl = pos.unrealized_pnl
            fill_p = pos.current_price if hasattr(pos, 'current_price') else pos.net_premium
            self.journal.log_trade_end(pos.order_id, closed_pnl, fill_price=fill_p)
            self.alert(f"✅ **EOD Position Closed** | P&L: `₹{closed_pnl:.2f}` | Est. Fill: {fill_p:.2f}")

        # 3. Final PnL Summary
        time.sleep(2) # Wait for broker fills
        final_pnl = self.journal.get_todays_pnl()
        self.alert(f"🏁 **Day Over**. Final Realized P&L: `₹{final_pnl:.2f}`. See you tomorrow!")
    def start(self):
        self.singleton_guard()
        self.execution_agent.resume_working_orders()
        self.streamer.start()
        # Scheduled triggers
        self.scheduler.add_job(self.pre_market_routine, 'cron', day_of_week='mon-fri', hour=8, minute=30, misfire_grace_time=300)
        self.scheduler.add_job(self.position_monitor_job, 'cron', day_of_week='mon-fri', hour='9-15', minute='*', misfire_grace_time=60)
        self.scheduler.add_job(self.eod_square_off_job, 'cron', day_of_week='mon-fri', hour=15, minute=15, misfire_grace_time=60)
        self.scheduler.add_job(self.force_eval_job, 'interval', minutes=1)
        self.scheduler.add_job(self.system_watchdog_job, 'interval', seconds=15)
        self.scheduler.start()
        
        now = datetime.now().time()
        if dtime(8, 30) <= now < dtime(10, 0):
            self.scheduler.add_job(self.pre_market_routine, 'date', run_date=datetime.now())
        self.telegram.run()

    def check_auth(self):
        if not self.check_kite_token():
            logger.warning("🚨 [AUTH] Kite session missing or expired. Starting interactive login...")
            try:
                from src.broker.kite_auth import start_login_process
                start_login_process()
                if not self.check_kite_token():
                    logger.critical("❌ [AUTH] Login failed. Exiting.")
                    sys.exit(1)
                logger.info("✅ [AUTH] Session refreshed successfully.")
            except Exception as e:
                logger.error(f"❌ [AUTH] Interactive login error: {e}")
                sys.exit(1)

    def refresh_fii_dii(self):
        try:
            cash = self.nse_fetcher.get_fii_dii_flow()
            oi = self.nse_fetcher.get_participant_oi()
            self.last_fii_signal = oi["signal"] if oi["signal"] != "NEUTRAL" else cash["signal"]
            logger.info(f"Updated Institutional Signal: {self.last_fii_signal}")
        except Exception as e: logger.error(f"FII/DII Refresh failed: {e}")

if __name__ == "__main__":
    app = Orchestrator()
    app.start()
