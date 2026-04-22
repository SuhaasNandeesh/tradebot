import streamlit as st
import pandas as pd
import sqlite3
import json
import time
from datetime import datetime
import os

st.set_page_config(page_title="TradeBot Agentic Brain", layout="wide")

def get_db_connection(db_name):
    return sqlite3.connect(db_name, check_same_thread=False)

def load_data():
    journal_conn = get_db_connection("trade_journal.db")
    state_conn = get_db_connection("trade_state.db")
    
    trades_df = pd.read_sql_query("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 10", journal_conn)
    evals_df = pd.read_sql_query("SELECT * FROM evaluations ORDER BY timestamp DESC LIMIT 20", journal_conn)
    positions_df = pd.read_sql_query("SELECT * FROM positions", state_conn)
    
    return trades_df, evals_df, positions_df

st.title("🧠 TradeBot Agentic Dashboard")
st.markdown("---")

# Sidebar for controls and status
st.sidebar.header("System Status")
if os.path.exists("tradebot.pid"):
    st.sidebar.success("✅ Bot is Running")
else:
    st.sidebar.error("❌ Bot is Offline")

# Layout
col1, col2, col3 = st.columns(3)

try:
    trades, evals, positions = load_data()

    # Metrics
    with col1:
        st.metric("Total Trades (Today)", len(trades))
    with col2:
        st.metric("Open Positions", len(positions))
    with col3:
        pnl = trades['pnl'].sum() if not trades.empty else 0.0
        st.metric("Net P&L", f"₹{pnl:.2f}", delta=f"{pnl:.2f}")

    st.markdown("### 👁️ Agentic Intelligence Feed (Live Thinking)")
    
    for idx, row in evals.iterrows():
        with st.expander(f"🕒 {row['timestamp']} | {row['strategy_name']} | Result: {row['signal']}"):
            st.write(f"**Reasoning:** {row['reason']}")
            st.progress(float(row['confluence_score'])/100.0)
            if row['context']:
                try:
                    ctx = json.loads(row['context'])
                    st.json(ctx)
                except:
                    st.text(row['context'])

    st.markdown("---")
    
    c1, c2 = st.columns(2)
    
    with c1:
        st.subheader("📈 Recent Trades")
        st.dataframe(trades[['timestamp', 'symbol', 'signal', 'pnl', 'strategy_id']])

    with c2:
        st.subheader("⚡ Active Positions")
        if not positions.empty:
            st.dataframe(positions)
        else:
            st.info("No active positions currently.")

except Exception as e:
    st.warning(f"Waiting for data... (Ensure the bot has run at least once) | Error: {e}")

# Auto-refresh
time.sleep(5)
st.rerun()
