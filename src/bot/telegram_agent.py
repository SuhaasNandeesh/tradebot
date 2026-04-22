import os
import sys
import logging
import json
import uuid

# Add the project root to the python path so 'src' can be found when running directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import telegram

from src.data.news_fetcher import NewsFetcher
from src.agents.context_agent import ContextAgent
from src.memory.journal import TradeJournal

logger = logging.getLogger(__name__)


class TelegramAgent:
    def __init__(self, broker=None, risk_manager=None, position_manager=None, execution_agent=None):
        load_dotenv()
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.broker = broker
        self.risk_manager = risk_manager          # RiskManager singleton from Orchestrator
        self.position_manager = position_manager  # PositionManager singleton
        self.execution_agent = execution_agent    # ExecutionAgent singleton

        self.news_fetcher = NewsFetcher()
        self.context_agent = ContextAgent()
        self.journal = TradeJournal()

        if not self.token or self.token == "your_telegram_bot_token":
            logger.warning("Telegram token is not configured!")

    # ── Auth ──────────────────────────────────────────────────────────────────
    
    def escape_markdown(self, text: str) -> str:
        """
        Escapes basic Markdown (V1) entities that frequently cause parsing errors.
        In financial data and LLM outputs, underscores, unmatched brackets, and asterisks cause crashes.
        """
        if not text: return text
        # Escape stray underscores (very common LLM hallucination for ticker names)
        text = text.replace("_", "\\_")
        # Replace brackets to avoid Telegram attempting to parse a broken hyperlink
        text = text.replace("[", "\\[").replace("]", "\\]")
        return text

    def is_authorized(self, update: Update) -> bool:
        """Ensure only the owner can control the bot."""
        if not self.chat_id:
            return True  # Allow all when chat_id not set (test mode only)
        return str(update.effective_chat.id) == self.chat_id

    # ── Broadcast Utility ─────────────────────────────────────────────────────

    def broadcast_message_sync(self, text: str, parse_mode: str = 'Markdown'):
        """Broadcasts a paginated message synchronously from any thread, line-aware to prevent breaking markdown."""
        import asyncio
        from telegram.error import BadRequest
        if not self.token or not self.chat_id: return
        
        bot = telegram.Bot(token=self.token)
        max_length = 4000
        lines = text.split('\n')
        current_chunk = ""
        
        async def send(chunk):
            if not chunk.strip(): return
            try:
                # Attempt with requested parse_mode
                await bot.send_message(chat_id=self.chat_id, text=chunk, parse_mode=parse_mode)
            except BadRequest as e:
                if "Can't parse entities" in str(e):
                    logger.warning(f"Markdown failed in broadcast, falling back to plain: {e}")
                    await bot.send_message(chat_id=self.chat_id, text=chunk)
                else:
                    logger.error(f"Broadcast failed: {e}")
            except Exception as e:
                logger.error(f"Unexpected broadcast error: {e}")

        for line in lines:
            # If a single line is absurdly long, we must break it (rare for market reports)
            if len(line) > max_length:
                if current_chunk:
                    asyncio.run(send(current_chunk))
                    current_chunk = ""
                for i in range(0, len(line), max_length):
                    asyncio.run(send(line[i:i+max_length]))
                continue

            if len(current_chunk) + len(line) + 1 > max_length:
                asyncio.run(send(current_chunk))
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"
                
        if current_chunk.strip():
            asyncio.run(send(current_chunk))

    async def send_paginated_message(self, update: Update, text: str):
        """Helper to send long messages in 4000-char chunks, line-aware to prevent breaking markdown."""
        from telegram.error import BadRequest
        max_length = 4000
        lines = text.split('\n')
        current_chunk = ""
        
        async def send_chunk(chunk):
            if not chunk.strip(): return
            try:
                await update.message.reply_text(chunk, parse_mode='Markdown')
            except BadRequest as e:
                if "Can't parse entities" in str(e):
                    logger.warning(f"Markdown failed in reply, falling back to plain: {e}")
                    await update.message.reply_text(chunk)
                else:
                    logger.error(f"Reply failed: {e}")

        for line in lines:
            if len(line) > max_length:
                if current_chunk:
                    await send_chunk(current_chunk)
                    current_chunk = ""
                for i in range(0, len(line), max_length):
                    await update.message.reply_text(line[i:i+max_length])
                continue

            if len(current_chunk) + len(line) + 1 > max_length:
                await send_chunk(current_chunk)
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"
                
        if current_chunk.strip():
            await send_chunk(current_chunk)

    async def safe_reply(self, update: Update, text: str, parse_mode: str = 'Markdown', reply_markup=None):
        """Helper to send reply_text with automatic Markdown fallback on error."""
        from telegram.error import BadRequest
        try:
            await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except BadRequest as e:
            if "Can't parse entities" in str(e):
                logger.warning(f"Markdown failed in reply, falling back to plain: {e}")
                await update.message.reply_text(text, reply_markup=reply_markup)
            else:
                logger.error(f"Reply failed: {e}")

    # ── Standard Commands ─────────────────────────────────────────────────────

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if not self.is_authorized(update):
            await self.safe_reply(update,
                f"⛔️ Unauthorized.\n\nYour Chat ID is `{chat_id}`.\n"
                f"Set `TELEGRAM_CHAT_ID={chat_id}` in `.env` and restart."
            )
            return
        await self.safe_reply(update, "🏦 Institutional TradeBot is online. Type /help for commands.")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        msg = (
            "🏦 **Institutional TradeBot Commands**\n\n"
            "📊 *Market Intelligence*\n"
            "*/status* — System & broker connection\n"
            "*/eval* — Why the bot is or isn't trading right now (Latest logs)\n"
            "*/news* — Live pre-market strategy dashboard (LLM)\n"
            "*/summary* — Quantitative market sentiment analysis\n"
            "*/ask* `<query>` — Query live market intelligence\n"
            "*/calendar* — Today's events + upcoming expiry/RBI/FOMC dates\n"
            "*/sensex* `[price]` — SENSEX ATM options spread snapshot\n"
            "*/banknifty* — NIFTY–BANKNIFTY correlation & divergence status\n\n"
            "📈 *Positions & Trades*\n"
            "*/positions* — Open positions with live PnL\n"
            "*/trades* — Last 5 executed trades\n"
            "*/pnl* — Net realized profit/loss\n"
            "*/risk* — Circuit breaker status & risk parameters\n"
            "*/vix* — Current India VIX level & volatility signal\n\n"
            "🤖 *LM Studio AI (Local Nemotron)*\n"
            "*/reflection* — Analyse today's trades & get improvement suggestions\n"
            "*/insights* — 30-day pattern mining: best regime, strategy, hour\n\n"
            "⚙️ *Controls*\n"
            "*/pause* — 🚨 Pull circuit breaker & halt all trading\n"
            "*/resume* — ✅ Resume trading (reset circuit breaker)\n"
            "*/execute* `<ACTION> <SYMBOL> <QTY>` — Manual trade\n"
            "*/help* — This menu"
        )
        await self.safe_reply(update, msg)

    async def eval_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        logs = self.journal.get_recent_evaluations(limit=10)
        if not logs:
            await self.safe_reply(update, "📭 No evaluation logs found.")
            return
        
        msg = "🔍 **Trade Evaluation Journal**\n\n"
        for l in reversed(logs):
            # Handle unpacking dynamically to avoid ValueError if schema changes
            ts = l[0]; strat = l[1]; sig = l[2]; reason = l[3]; score = l[4]; regime = l[5]
            
            time_str = ts.split('T')[1][:5] if 'T' in ts else ts
            icon = "✅" if sig != "HOLD" and "SUCCESS" in reason else "⚠️" if sig != "HOLD" else "⚪️"
            msg += f"`{time_str}` {icon} **{strat}** → `{sig}`\n"
            msg += f"Score: `{score:.0f}` | Regime: `{regime}`\n"
            msg += f"Reason: _{self.escape_markdown(reason)}_\n\n"
            
        await self.send_paginated_message(update, msg)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        broker_status = "🔴 Disconnected"
        nifty_price = "N/A"
        if self.broker and self.broker.is_connected:
            broker_status = "🟢 Connected"
            nifty = self.broker.get_nifty()
            nifty_price = f"₹{nifty:,.2f}" if nifty else "N/A"

        paper = os.getenv("PAPER_TRADE", "True")
        mode = "📄 Paper Trading" if paper.lower() in ("true", "1", "t") else "🔴 LIVE TRADING"

        open_count = 0
        if self.position_manager:
            open_count = len(self.position_manager.get_open_positions())

        cb_status = "🔴 ACTIVE" if (self.risk_manager and self.risk_manager.is_circuit_breaker_active) else "🟢 Clear"

        msg = (
            f"🏦 **System Status**\n"
            f"Broker: `{broker_status}`\n"
            f"NIFTY Spot: `{nifty_price}`\n"
            f"Mode: `{mode}`\n"
            f"Open Positions: `{open_count}`\n"
            f"Circuit Breaker: `{cb_status}`\n"
        )
        
        # UI Action Buttons
        buttons = []
        if self.risk_manager and self.risk_manager.is_circuit_breaker_active:
            buttons.append(InlineKeyboardButton("▶️ RESUME TRADING", callback_data="cmd_resume"))
        else:
            buttons.append(InlineKeyboardButton("🚨 PAUSE BOT", callback_data="cmd_pause"))
            
        if open_count > 0:
            buttons.append(InlineKeyboardButton("🔴 SQUARE OFF ALL", callback_data="cmd_squareoff_confirm"))
            
        keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)

    # ── Trading Control Commands ──────────────────────────────────────────────

    async def pause_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Activate circuit breaker and optionally square off all open positions."""
        if not self.is_authorized(update):
            return

        if self.risk_manager:
            self.risk_manager.activate_circuit_breaker(reason="Manual Telegram /pause command")

        # Square off all open positions immediately
        if self.execution_agent:
            self.execution_agent.square_off_all()
            await update.message.reply_text(
                "🚨 **CIRCUIT BREAKER ACTIVATED**\n\n"
                "✅ All trading halted.\n"
                "✅ All open positions squared off.\n\n"
                "Use `/resume` to restart trading.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "🚨 **CIRCUIT BREAKER ACTIVATED**\n\n"
                "✅ Trading halted. Broker not connected — please manually close positions in Kite.\n"
                "Use `/resume` to restart.",
                parse_mode='Markdown'
            )
        logger.critical("Circuit breaker activated via Telegram /pause command.")

    async def resume_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Deactivate the circuit breaker and resume automated trading."""
        if not self.is_authorized(update):
            return
        if self.risk_manager:
            daily_pnl = self.risk_manager.daily_pnl
            if daily_pnl <= self.risk_manager.max_daily_loss:
                await update.message.reply_text(
                    f"⚠️ Cannot resume: Daily PnL (₹{daily_pnl:.2f}) is still below max loss limit "
                    f"(₹{self.risk_manager.max_daily_loss:.2f}). Close out for the day.",
                    parse_mode='Markdown'
                )
                return
            self.risk_manager.deactivate_circuit_breaker()
        await update.message.reply_text("✅ **Circuit breaker deactivated. Trading resumed.**", parse_mode='Markdown')

    # ── Intelligence & Market Commands ───────────────────────────────────────

    async def news_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        await update.message.reply_text("📡 Fetching live market intelligence... Please wait.")
        import asyncio
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, self.context_agent.summarize_news_for_telegram)
        if summary and not summary.startswith("❌"):
            safe_summary = self.escape_markdown(summary)
            msg = f"📰 **Live Market Intelligence**\n\n{safe_summary}"
            await self.send_paginated_message(update, msg)
        else:
            await update.message.reply_text(summary or "❌ Live news fetch failed.")

    async def summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        await update.message.reply_text("🧠 Analyzing current market context...")
        import asyncio
        loop = asyncio.get_event_loop()
        analysis = await loop.run_in_executor(None, self.context_agent.analyze_current_market)
        if not analysis or "error" in analysis:
            error_msg = analysis.get('error', 'Unknown intelligence error') if analysis else 'Intelligence service unreachable'
            await update.message.reply_text(f"❌ Analysis Error: {error_msg}")
            return
        msg = (
            "🧠 **Market Context Summary**\n"
            f"Sentiment: `{analysis.get('sentiment', 'UNKNOWN')}`\n"
            f"Confidence: `{analysis.get('confidence_score', 0)}%`\n\n"
            f"**Key Drivers:**\n- " + "\n- ".join(analysis.get('key_drivers', [])) + "\n\n"
            f"**Implication:** {analysis.get('trading_implication', '')}"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

    async def vix_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shows current India VIX and volatility signal."""
        if not self.is_authorized(update):
            return
        vix = None
        if self.broker:
            vix = self.broker.get_vix()

        if vix is None:
            await update.message.reply_text(
                "📊 **India VIX**\n\nVIX data not available (market may be closed or WebSocket not connected).",
                parse_mode='Markdown'
            )
            return

        if vix < 12:
            signal = "🟢 VERY LOW — Complacency zone. Options cheap. Good for buying."
        elif vix < 16:
            signal = "🟢 LOW — Normal market. Options buying is viable."
        elif vix < 20:
            signal = "🟡 MODERATE — Be selective. Prefer selling strategies."
        elif vix < 25:
            signal = "🔴 HIGH — Volatile. Avoid naked option buys. 🚧 Gate active."
        else:
            signal = "🔴🔴 EXTREME — Circuit breaker territory. No new positions."

        cb_note = ""
        if self.risk_manager and self.risk_manager.is_circuit_breaker_active and self.risk_manager.current_vix:
            cb_note = "\n⚠️ Trading halted due to VIX threshold breach."

        msg = f"📊 **India VIX: `{vix:.2f}`**\n\n{signal}{cb_note}"
        await update.message.reply_text(msg, parse_mode='Markdown')

    async def risk_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shows current risk manager status."""
        if not self.is_authorized(update):
            return
        if not self.risk_manager:
            await update.message.reply_text("❌ Risk Manager not connected.")
            return

        status = self.risk_manager.get_status()
        cb_icon = "🔴 ACTIVE" if status["circuit_breaker_active"] else "🟢 Clear"
        pnl = status["daily_pnl"]
        max_loss = status["max_daily_loss"]
        pnl_pct = (pnl / abs(max_loss) * 100) if max_loss != 0 else 0
        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        vix_str = f"{status['vix']:.2f}" if status["vix"] else "N/A"

        msg = (
            f"🛡️ **Risk Manager Status**\n\n"
            f"Circuit Breaker: `{cb_icon}`\n"
            f"Reason: `{status['reason']}`\n\n"
            f"{pnl_icon} Daily PnL: `₹{pnl:.2f}` ({pnl_pct:.1f}% of limit)\n"
            f"Max Daily Loss Limit: `₹{max_loss:.2f}`\n"
            f"Max Position Size: `{status['max_position_size']} lots`\n\n"
            f"India VIX: `{vix_str}` (Threshold: {status['vix_threshold']})"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

    # ── Position & PnL Commands ───────────────────────────────────────────────

    async def positions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shows all open positions with live unrealized PnL."""
        if not self.is_authorized(update):
            return
        if not self.position_manager:
            await update.message.reply_text("❌ Position Manager not connected.")
            return
            
        summary = self.position_manager.get_summary()
        
        # Inline Buttons for Position Management
        open_count = len(self.position_manager.get_open_positions())
        keyboard = None
        if open_count > 0:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔴 SQ. OFF ALL", callback_data="cmd_squareoff_confirm")
            ]])
            # Send summary with embedded UI buttons
            await update.message.reply_text(summary, parse_mode='Markdown', reply_markup=keyboard)
        else:
            await self.send_paginated_message(update, summary)

    async def trades_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        trades = self.journal.get_recent_trades(limit=5)
        if not trades:
            await update.message.reply_text("📭 No recent trades found.")
            return
        msg = "📜 **Recent Trades**\n\n"
        for t in trades:
            tid, timestamp, symbol, signal, pnl = t
            pnl_str = f"+₹{pnl:.2f}" if pnl and pnl > 0 else f"₹{pnl:.2f}" if pnl else "Open"
            date_str = timestamp[:16] if timestamp else "?"
            # Wrapping symbol and signal in backticks to escape underscores
            msg += f"`{tid}` | `{symbol}` | `{signal}` | {pnl_str} | `{date_str}`\n"
        await self.send_paginated_message(update, msg)

    async def pnl_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        total_pnl = self.journal.get_total_pnl()
        today_pnl = self.journal.get_todays_pnl()
        unrealized = 0.0
        if self.position_manager:
            unrealized = self.position_manager.get_total_unrealized_pnl()

        icon = "🟢" if total_pnl >= 0 else "🔴"
        today_icon = "🟢" if today_pnl >= 0 else "🔴"
        unreal_icon = "🟢" if unrealized >= 0 else "🔴"

        msg = (
            f"{icon} **Total Realized P&L:** `₹{total_pnl:.2f}`\n"
            f"{today_icon} **Today's Realized P&L:** `₹{today_pnl:.2f}`\n"
            f"{unreal_icon} **Unrealized P&L:** `₹{unrealized:.2f}`"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

    # ── Manual Execution ──────────────────────────────────────────────────────

    async def execute_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        args = context.args
        if len(args) != 3:
            await update.message.reply_text(
                "❌ Usage: `/execute <BUY/SELL> <SYMBOL> <QTY>`", parse_mode='Markdown'
            )
            return
        action, symbol, qty = args[0].upper(), args[1].upper(), args[2]
        if action not in ['BUY', 'SELL'] or not qty.isdigit():
            await update.message.reply_text("❌ Action must be BUY/SELL and QTY must be an integer.")
            return

        keyboard = [[
            InlineKeyboardButton("✅ CONFIRM", callback_data=f"trade|{action}|{symbol}|{qty}"),
            InlineKeyboardButton("❌ CANCEL",  callback_data="cancel_trade"),
        ]]
        await update.message.reply_text(
            f"⚠️ **Trade Confirmation Required**\n\n"
            f"Action: `{action}`\nSymbol: `{symbol}`\nQty: `{qty}`\n\n"
            f"Are you sure?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    async def trade_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not self.is_authorized(update):
            await query.answer("Unauthorized", show_alert=True)
            return
        await query.answer()
        data = query.data

        if data == "cancel_trade":
            await query.edit_message_text("🚫 **Trade/Action Cancelled**", parse_mode='Markdown')
            return

        # Handle UI Macros from '/status' and '/positions'
        if data == "cmd_pause":
            if self.risk_manager:
                self.risk_manager.activate_circuit_breaker(reason="Manual Telegram UI Pause")
            await query.edit_message_text("🚨 **CIRCUIT BREAKER ACTIVATED**\nAll automated trading halted.", parse_mode='Markdown')
            return
            
        if data == "cmd_resume":
            if self.risk_manager:
                self.risk_manager.deactivate_circuit_breaker()
            await query.edit_message_text("✅ **Trading Resumed.**", parse_mode='Markdown')
            return
            
        if data == "cmd_squareoff_confirm":
            # Fire an intermediate confirmation before nuclear button
            keyboard = [[
                InlineKeyboardButton("💥 YES, CLOSE ALL POSITIONS", callback_data="cmd_squareoff_execute"),
                InlineKeyboardButton("❌ CANCEL", callback_data="cancel_trade"),
            ]]
            await query.edit_message_text(
                "⚠️ **WARNING: NUKING ALL TRADES**\nAre you absolutely sure you want to Market-Sell all open positions natively right now?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            return

        if data == "cmd_squareoff_execute":
            if self.execution_agent:
                self.execution_agent.square_off_all()
                await query.edit_message_text("☢️ **ALL POSITIONS SQUARED OFF.**", parse_mode='Markdown')
            else:
                await query.edit_message_text("❌ Execution Agent unavailable.", parse_mode='Markdown')
            return

        # Traditional Manual Trade Executor
        if data.startswith("trade|"):
            _, action, symbol, qty = data.split('|')
            qty_int = int(qty)

            if not self.execution_agent:
                await query.edit_message_text(
                    "❌ **Execution Agent Not Connected**\nRun via the full Orchestrator (main.py).",
                    parse_mode='Markdown'
                )
                return

            if not self.risk_manager or not self.risk_manager.can_trade():
                await query.edit_message_text(
                    "🚨 **Trade Blocked**\nCircuit breaker is active. Use `/resume` first.",
                    parse_mode='Markdown'
                )
                return

            try:
                order_id = self.execution_agent.place_order(symbol, action, qty_int)
                if order_id:
                    trade_id = self.journal.log_trade(
                        symbol, action, "MANUAL_TELEGRAM_EXECUTION", strategy_id=None
                    )
                    await query.edit_message_text(
                        f"✅ **Trade Executed**\n\n"
                        f"Action: `{action}`\nSymbol: `{symbol}`\nQty: `{qty}`\n"
                        f"Order ID: `{order_id}`\nJournal ID: `{trade_id}`",
                        parse_mode='Markdown'
                    )
                    self.broadcast_message_sync(
                        f"🚨 **[MANUAL OVERRIDE]** {action} {qty} {symbol} — Order: `{order_id}`"
                    )
                else:
                    await query.edit_message_text(
                        "❌ **Order Placement Failed**\nCheck logs for details.", parse_mode='Markdown'
                    )
            except Exception as e:
                await query.edit_message_text(f"❌ **Trade Failed**\n`{str(e)}`", parse_mode='Markdown')

    async def ask_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        query = " ".join(context.args)
        if not query:
            await update.message.reply_text("❌ Usage: `/ask <your market question>`", parse_mode='Markdown')
            return
        await update.message.reply_text("🤔 Consulting global market data... Please wait.")
        import asyncio
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, self.context_agent.answer_question, query)
        await self.send_paginated_message(update, f"💡 **Market Intelligence**\n\n{answer}")


    # ── New Institutional Commands ─────────────────────────────────────────────

    async def reflection_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /reflection — Trigger LM Studio evening reflection on today's trades.
        Uses nemotron-3-nano by default (model-agnostic via env var).
        """
        if not self.is_authorized(update):
            return
        await update.message.reply_text(
            "🤖 **Generating Evening Reflection...**\n"
            "_LM Studio is analysing today's trades and confluence outcomes._",
            parse_mode='Markdown'
        )
        try:
            from src.core.lmstudio_agent import LMStudioAgent
            from src.memory.journal import TradeJournal
            agent   = LMStudioAgent()
            journal = TradeJournal()
            import asyncio
            loop = asyncio.get_event_loop()
            result  = await loop.run_in_executor(None, agent.evening_reflection, journal.db_path)
            await self.send_paginated_message(update, result["formatted_report"])
        except Exception as e:
            await update.message.reply_text(f"❌ Reflection failed: {e}")

    async def insights_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /insights — 30-day pattern mining with LM Studio analysis.
        Reveals: best regime, best hour, confluence score accuracy, etc.
        """
        if not self.is_authorized(update):
            return
        await update.message.reply_text(
            "🔬 **Mining Trade Patterns (30 days)...**\n"
            "_Querying SQLite + LM Studio analysis. Please wait._",
            parse_mode='Markdown'
        )
        try:
            from src.core.lmstudio_agent import LMStudioAgent
            from src.memory.journal import TradeJournal
            agent   = LMStudioAgent()
            journal = TradeJournal()
            import asyncio
            loop = asyncio.get_event_loop()
            report  = await loop.run_in_executor(None, agent.mine_patterns, journal.db_path)
            await self.send_paginated_message(update, report)
        except Exception as e:
            await update.message.reply_text(f"❌ Insights failed: {e}")

    async def calendar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /calendar — Today's trading day status + upcoming market events.
        Shows: holidays, expiry days, RBI MPC meetings, FOMC dates.
        """
        if not self.is_authorized(update):
            return
        try:
            from src.data.market_calendar import MarketCalendar
            cal = MarketCalendar()
            msg = cal.format_telegram_calendar(days_ahead=7)
            await self.send_paginated_message(update, msg)
        except Exception as e:
            await update.message.reply_text(f"❌ Calendar error: {e}")

    async def sensex_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /sensex — SENSEX options snapshot (ATM CE/PE strikes + lot info).
        """
        if not self.is_authorized(update):
            return
        try:
            from src.broker.bse_options import BSEOptionsManager, round_to_sensex_strike
            bse   = BSEOptionsManager()
            # Use last known SENSEX price or fallback
            sensex_price = float(context.args[0]) if context.args else 80000.0
            atm   = round_to_sensex_strike(sensex_price)
            buy_c, sell_c = bse.get_sensex_spread_options("BUY_CE",  sensex_price, 75)
            buy_p, sell_p = bse.get_sensex_spread_options("BUY_PE",  sensex_price, 75)
            msg = (
                f"📊 **SENSEX Options Snapshot**\n"
                f"SENSEX (approx): `{sensex_price:,.0f}` | ATM: `{atm}`\n"
                f"Lot size: `20` | Strike spacing: `200 pts`\n\n"
                f"**Bull Call Spread (BUY CE):**\n"
                f"  Buy:  `{buy_c['tradingsymbol']}`\n"
                f"  Sell: `{sell_c['tradingsymbol']}`\n\n"
                f"**Bear Put Spread (BUY PE):**\n"
                f"  Buy:  `{buy_p['tradingsymbol']}`\n"
                f"  Sell: `{sell_p['tradingsymbol']}`\n\n"
                f"_Use `/sensex <price>` to check a specific level._"
            )
            await update.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ SENSEX snapshot error: {e}")

    async def banknifty_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /banknifty — NIFTY–BANKNIFTY correlation status.
        Shows live divergence signal and confluence score adjustment.
        """
        if not self.is_authorized(update):
            return
        try:
            from src.core.correlation_tracker import CorrelationTracker
            # Access shared tracker from orchestrator if available
            tracker = getattr(self, '_corr_tracker', None)
            if tracker is None:
                tracker = CorrelationTracker()
                tracker.update(22000, 48000)  # Placeholder
            msg = tracker.format_telegram()
            msg += (
                "\n\n_Correlation updates every 5 min during market hours._\n"
                "Interpretation:\n"
                "  r ≥ 0.70 → NIFTY signals trusted ✅\n"
                "  r 0.50-0.70 → Caution, -10 confluence ⚠️\n"
                "  r < 0.50 → Trade blocked, -20 confluence 🚨"
            )
            await update.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ BANKNIFTY correlation error: {e}")

    async def override_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_authorized(update):
            return
        feedback = " ".join(context.args)
        if not feedback:
            await update.message.reply_text("❌ Usage: `/override <Your feedback about the last trade>`", parse_mode='Markdown')
            return
        
        result = self.journal.override_trade_outcome(feedback)
        await update.message.reply_text(result)

    # ── Bot Runner ────────────────────────────────────────────────────────────

    def run(self):
        if not self.token or self.token == "your_telegram_bot_token":
            logger.error("Cannot start bot. Invalid token.")
            return

        app = ApplicationBuilder().token(self.token).build()

        app.add_handler(CommandHandler("start",     self.start_command))
        app.add_handler(CommandHandler("help",      self.help_command))
        app.add_handler(CommandHandler("status",    self.status_command))
        app.add_handler(CommandHandler("eval",      self.eval_command))
        app.add_handler(CommandHandler("news",      self.news_command))
        app.add_handler(CommandHandler("summary",   self.summary_command))
        app.add_handler(CommandHandler("positions", self.positions_command))
        app.add_handler(CommandHandler("trades",    self.trades_command))
        app.add_handler(CommandHandler("pnl",       self.pnl_command))
        app.add_handler(CommandHandler("risk",      self.risk_command))
        app.add_handler(CommandHandler("vix",       self.vix_command))
        app.add_handler(CommandHandler("pause",     self.pause_command))
        app.add_handler(CommandHandler("resume",    self.resume_command))
        app.add_handler(CommandHandler("execute",    self.execute_command))
        app.add_handler(CommandHandler("ask",        self.ask_command))
        app.add_handler(CommandHandler("override",   self.override_command))
        app.add_handler(CommandHandler("reflection", self.reflection_command))
        app.add_handler(CommandHandler("insights",   self.insights_command))
        app.add_handler(CommandHandler("calendar",   self.calendar_command))
        app.add_handler(CommandHandler("sensex",     self.sensex_command))
        app.add_handler(CommandHandler("banknifty",  self.banknifty_command))
        app.add_handler(CallbackQueryHandler(self.trade_callback))

        logger.info("Telegram bot listening for commands...")
        app.run_polling()


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    agent = TelegramAgent()
    agent.run()
