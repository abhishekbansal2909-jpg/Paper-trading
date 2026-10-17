import streamlit as st
import sqlite3
import pandas as pd

st.set_page_config(page_title="V11 Engine Monitor", layout="wide")
st.title("V11 Live Futures Pipeline")

def load_data(status):
    conn = sqlite3.connect("paper_trades.db")
    df = pd.read_sql_query(f"SELECT * FROM trades WHERE status = '{status}'", conn)
    conn.close()
    return df

st.subheader("Active Positions (Live TSL State)")
df_open = load_data("OPEN")
if not df_open.empty:
    st.dataframe(df_open[['symbol', 'direction', 'qty', 'entry_price', 'mfe_pct', 'current_stop_pct', 'tsl_active']], use_container_width=True)
else:
    st.info("No active trades.")

st.subheader("Closed Trade Ledger")
df_closed = load_data("TSL_HIT' OR status = 'STOPPED_OUT")
if not df_closed.empty:
    st.metric("Total Net PnL (INR)", f"₹{df_closed['net_pnl'].sum():,.2f}")
    st.dataframe(df_closed[['symbol', 'direction', 'entry_price', 'exit_price', 'status', 'net_pnl']], use_container_width=True)
else:
    st.info("No closed trades yet.")
