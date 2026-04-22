import sqlite3
import logging
import os
import json
from datetime import datetime

import chromadb
from chromadb.config import Settings
from src.core.lmstudio_embedder import get_embedder

logger = logging.getLogger(__name__)


class TradeJournal:
    def __init__(self, db_path="trade_journal.db", chroma_path="./chroma_db"):
        self.db_path = db_path
        self._init_sqlite()

        # Disable telemetry to prevent noisy PostHog errors
        self.chroma_client = chromadb.PersistentClient(
            path=chroma_path,
            settings=Settings(anonymized_telemetry=False)
        )
        try:
            self.memory_collection = self.chroma_client.get_or_create_collection(
                name="trading_memory",
                embedding_function=get_embedder(),
            )
        except Exception as e:
            if "dimension" in str(e).lower():
                logger.critical(f"ChromaDB dimension mismatch detected: {e}. Creating versioned collection to preserve history.")
                new_col_name = f"trading_memory_v{int(datetime.now().timestamp())}"
                self.memory_collection = self.chroma_client.create_collection(
                    name=new_col_name,
                    embedding_function=get_embedder(),
                )
            else:
                raise e

    def _init_sqlite(self):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    signal TEXT,
                    context TEXT,
                    pnl REAL,
                    strategy_code TEXT,
                    strategy_id TEXT,
                    is_paper_trade INTEGER DEFAULT 1,
                    decision_price REAL,
                    fill_price REAL,
                    slippage REAL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT,
                    context_sentiment TEXT,
                    context_confidence INTEGER,
                    strategy_code TEXT,
                    backtest_win_rate REAL,
                    backtest_sharpe REAL,
                    backtest_pnl REAL,
                    backtest_trades INTEGER,
                    is_deployed INTEGER DEFAULT 0,
                    deployment_count INTEGER DEFAULT 0
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS confluence_outcomes (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT,
                    trade_id         INTEGER,
                    strategy_name    TEXT,
                    confluence_score INTEGER,
                    regime           TEXT,
                    actual_direction TEXT,
                    technical_pred   TEXT,
                    options_pred     TEXT,
                    news_pred        TEXT,
                    technical_correct INTEGER,
                    options_correct   INTEGER,
                    news_correct      INTEGER,
                    regime_correct    INTEGER,
                    pnl              REAL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS evaluation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    strategy_name TEXT,
                    signal TEXT,
                    reason TEXT,
                    confluence_score REAL,
                    regime TEXT,
                    context TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            conn.commit()
        self._migrate_sqlite()

    def _migrate_sqlite(self):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cols = [
                ('strategy_id', 'TEXT'),
                ('is_paper_trade', 'INTEGER DEFAULT 1'),
                ('decision_price', 'REAL'),
                ('fill_price', 'REAL'),
                ('slippage', 'REAL')
            ]
            for col, dtype in cols:
                try:
                    cursor.execute(f'ALTER TABLE trades ADD COLUMN {col} {dtype}')
                    conn.commit()
                except sqlite3.OperationalError: pass

    def log_evaluation(self, strategy_name: str, signal: str, reason: str, confluence_score: float = 0.0, regime: str = "UNKNOWN", context: dict = None):
        """Logs every evaluation attempt, even if it results in HOLD."""
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO evaluation_logs (timestamp, strategy_name, signal, reason, confluence_score, regime, context)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (datetime.now().isoformat(), strategy_name, signal, reason, confluence_score, regime, json.dumps(context or {})))
            conn.commit()

    def log_trade_start(self, symbol: str, signal: str, context_str: str,
                        strategy_code: str, strategy_id: str = None, 
                        is_paper_trade: bool = True, decision_price: float = 0.0) -> int:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO trades (timestamp, symbol, signal, context, strategy_code, strategy_id, is_paper_trade, decision_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (datetime.now().isoformat(), symbol, signal, context_str,
                  strategy_code, str(strategy_id) if strategy_id else None,
                  1 if is_paper_trade else 0, decision_price))
            return cursor.lastrowid

    def log_trade_end(self, trade_id: int, final_pnl: float, fill_price: float = 0.0):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT decision_price, signal, symbol, context, strategy_id FROM trades WHERE id = ?', (trade_id,))
            row = cursor.fetchone()
            if row:
                dec_p, sig, sym, ctx, strat = row
                mult = 1.0 if "BUY" in str(sig) else -1.0
                slippage = (fill_price - dec_p) * mult if fill_price > 0 and dec_p > 0 else 0.0
                cursor.execute('UPDATE trades SET pnl = ?, fill_price = ?, slippage = ? WHERE id = ?', (final_pnl, fill_price, slippage, trade_id))
                
                # RAG
                label = "WIN" if final_pnl > 0 else "LOSS"
                doc = f"Trade {trade_id}: {sig} {sym} Result: {label} (₹{final_pnl:.2f}). Slippage: Rs{slippage:.2f}. Strat: {strat}."
                try:
                    self.memory_collection.add(
                        documents=[doc], ids=[f"trade_{trade_id}"],
                        metadatas=[{"trade_id": trade_id, "pnl": final_pnl, "outcome": label, "slippage": slippage}]
                    )
                except Exception: pass
            else:
                cursor.execute('UPDATE trades SET pnl = ? WHERE id = ?', (final_pnl, trade_id))
            conn.commit()

    def update_trade_pnl(self, trade_id: int, final_pnl: float):
        self.log_trade_end(trade_id, final_pnl)

    def log_blocked_signal(self, strategy_name: str, signal: str, reason: str, context: dict):
        sentiment = context.get("sentiment", "NEUTRAL")
        doc = f"BLOCKED SIGNAL: {strategy_name} {signal} REJECTED. Reason: {reason}. Context: {sentiment}."
        try:
            blocked_id = f"blocked_{strategy_name}_{datetime.now().strftime('%H%M%S')}"
            self.memory_collection.add(documents=[doc], metadatas=[{"strategy": strategy_name, "type": "BLOCKED", "reason": reason}], ids=[blocked_id])
        except Exception: pass

    def override_trade_outcome(self, feedback: str):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, strategy_id, context, signal, pnl FROM trades ORDER BY id DESC LIMIT 1')
            row = cursor.fetchone()
            if not row: return "No recent trades found."
            tid, strat, context, sig, pnl = row
            doc_text = f"MANUAL OVERRIDE for Trade {tid}. Strat: {strat}. Feedback: {feedback}. Prev: {pnl}. Context: {context}."
            try:
                self.memory_collection.upsert(documents=[doc_text], metadatas=[{"trade_id": tid, "type": "RLHF_OVERRIDE"}], ids=[f"override_{tid}"])
                return f"✅ Feedback recorded for Trade {tid} ({strat})"
            except Exception as e: return f"❌ Override failed: {e}"

    def log_confluence_outcome(self, trade_id: int, strategy_name: str, conf_score: int, regime: str, actual_direction: str,
                               tech_pred: str, opts_pred: str, news_pred: str,
                               tech_c: int, opts_c: int, news_c: int, regm_c: int, pnl: float):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO confluence_outcomes (timestamp, trade_id, strategy_name, confluence_score, regime, actual_direction, technical_pred, options_pred, news_pred, technical_correct, options_correct, news_correct, regime_correct, pnl)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (datetime.now().isoformat(), trade_id, strategy_name, conf_score, regime, actual_direction, tech_pred, opts_pred, news_pred, tech_c, opts_c, news_c, regm_c, pnl))
            conn.commit()

    def get_factor_accuracy(self, min_trades: int = 5) -> dict:
        defaults = {"technical": 0.6, "options": 0.5, "news": 0.5, "regime": 0.55}
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM confluence_outcomes")
            if cursor.fetchone()[0] < min_trades: return defaults
            cursor.execute("SELECT AVG(technical_correct), AVG(options_correct), AVG(news_correct), AVG(regime_correct), COUNT(*) FROM confluence_outcomes WHERE timestamp > datetime('now', '-30 days')")
            row = cursor.fetchone()
            if not row or row[0] is None: return defaults
            return {"technical": round(row[0] or 0.5, 3), "options": round(row[1] or 0.5, 3), "news": round(row[2] or 0.5, 3), "regime": round(row[3] or 0.5, 3), "sample_size": row[4]}

    def get_strategy_regime_stats(self, strategy_name: str, regime: str, days: int = 14) -> dict:
        defaults = {"total_trades": 0, "win_rate": 0.5, "conditional_expectancy": 0.0} 
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), AVG(pnl) FROM confluence_outcomes WHERE strategy_name = ? AND regime = ? AND timestamp > datetime('now', ?)", (strategy_name, regime, f"-{days} days"))
            row = cursor.fetchone()
            if not row or row[0] == 0: return defaults
            total = row[0]; wins = row[1] or 0; ev = row[2] or 0.0
            return {"total_trades": total, "win_rate": round(wins / total, 3), "conditional_expectancy": round(ev, 2)}

    def log_strategy(self, context_sentiment: str, context_confidence: int, strategy_code: str, backtest_results: dict) -> int:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO strategies (created_at, context_sentiment, context_confidence, strategy_code, backtest_win_rate, backtest_sharpe, backtest_pnl, backtest_trades)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (datetime.now().isoformat(), context_sentiment, context_confidence, strategy_code, backtest_results.get("win_rate"), backtest_results.get("sharpe_ratio"), backtest_results.get("total_pnl"), backtest_results.get("total_trades")))
            return cursor.lastrowid

    def get_best_strategy(self) -> dict:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, strategy_code, backtest_win_rate, backtest_sharpe, backtest_pnl FROM strategies ORDER BY backtest_sharpe DESC LIMIT 1")
            row = cursor.fetchone()
            if row: return {"id": row[0], "code": row[1], "win_rate": row[2], "sharpe": row[3], "pnl": row[4]}
            return {}

    def query_past_experience(self, current_context_str: str, n_results: int = 3) -> list:
        try:
            results = self.memory_collection.query(query_texts=[current_context_str], n_results=n_results)
            if results and results['documents'] and results['documents'][0]: return results['documents'][0]
            return []
        except Exception: return []

    def get_recent_trades(self, limit: int = 5, is_paper_trade: bool = True) -> list:
        paper_flag = 1 if is_paper_trade else 0
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, timestamp, symbol, signal, pnl FROM trades WHERE is_paper_trade = ? ORDER BY id DESC LIMIT ?', (paper_flag, limit))
            return cursor.fetchall()

    def get_recent_evaluations(self, limit: int = 10) -> list:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp, strategy_name, signal, reason, confluence_score, regime, context
                FROM evaluation_logs 
                ORDER BY id DESC LIMIT ?
            ''', (limit,))
            return cursor.fetchall()

    def get_unrealized_outcomes(self, hours: int = 24) -> list:
        """Fetches blocked signals for regret analysis."""
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp, strategy_name, signal, reason, confluence_score, regime, context
                FROM evaluation_logs 
                WHERE signal != 'HOLD' AND reason LIKE '%BLOCKED%'
                AND timestamp > datetime('now', ?)
            ''', (f"-{hours} hours",))
            return cursor.fetchall()

    def get_total_pnl(self, is_paper_trade: bool = True) -> float:
        paper_flag = 1 if is_paper_trade else 0
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT SUM(pnl) FROM trades WHERE is_paper_trade = ? AND pnl IS NOT NULL', (paper_flag,))
            result = cursor.fetchone()
            return result[0] if result and result[0] else 0.0

    def get_todays_pnl(self, is_paper_trade: bool = True) -> float:
        today = datetime.now().strftime("%Y-%m-%d")
        paper_flag = 1 if is_paper_trade else 0
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(pnl) FROM trades WHERE timestamp LIKE ? AND is_paper_trade = ? AND pnl IS NOT NULL", (f"{today}%", paper_flag))
            result = cursor.fetchone()
            return result[0] if result and result[0] else 0.0

    def clear_journal(self):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trades")
            cursor.execute("DELETE FROM confluence_outcomes")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='trades' OR name='confluence_outcomes'")
            conn.commit()

    def is_pre_market_complete_today(self) -> bool:
        """Checks if the morning intelligence has already been logged for today."""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM system_metadata WHERE key = 'last_pre_market_alert'")
            row = cursor.fetchone()
            return row is not None and row[0] == today

    def mark_pre_market_complete_today(self):
        """Marks the morning intelligence as broadcasted for today."""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO system_metadata (key, value) VALUES ('last_pre_market_alert', ?)", (today,))
            conn.commit()
