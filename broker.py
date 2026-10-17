import sqlite3
from datetime import datetime

DB_NAME = "paper_trades.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    direction TEXT,
                    qty INTEGER,
                    entry_price REAL,
                    current_stop_pct REAL,
                    mfe_pct REAL,
                    tsl_active INTEGER,
                    status TEXT,
                    exit_price REAL,
                    net_pnl REAL,
                    entry_time TEXT,
                    exit_time TEXT
                )''')
    conn.commit()
    conn.close()

def calculate_futures_fees(entry_price, exit_price, qty, direction):
    entry_val = entry_price * qty
    exit_val = exit_price * qty
    total_val = entry_val + exit_val
    
    brokerage = min(entry_val * 0.0003, 20.0) + min(exit_val * 0.0003, 20.0)
    stt = exit_val * 0.0005 if direction == "LONG" else entry_val * 0.0005
    txn = total_val * 0.000019
    sebi = total_val * 0.000001
    stamp = entry_val * 0.00002 if direction == "LONG" else exit_val * 0.00002
    gst = (brokerage + txn + sebi) * 0.18
    return brokerage + stt + txn + sebi + stamp + gst

def open_trade(symbol, direction, qty, entry_price, initial_stop_pct):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''INSERT INTO trades 
                 (symbol, direction, qty, entry_price, current_stop_pct, mfe_pct, tsl_active, status, entry_time)
                 VALUES (?, ?, ?, ?, ?, 0.0, 0, 'OPEN', ?)''', 
              (symbol, direction, qty, entry_price, -initial_stop_pct, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_open_trades():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    trades = conn.execute("SELECT * FROM trades WHERE status = 'OPEN'").fetchall()
    conn.close()
    return [dict(t) for t in trades]

def close_trade(trade_id, exit_price, outcome_status):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    trade = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    
    gross_pnl = (exit_price - trade['entry_price']) * trade['qty'] if trade['direction'] == 'LONG' else (trade['entry_price'] - exit_price) * trade['qty']
    fees = calculate_futures_fees(trade['entry_price'], exit_price, trade['qty'], trade['direction'])
    net_pnl = gross_pnl - fees
    
    conn.execute('''UPDATE trades SET status = ?, exit_price = ?, net_pnl = ?, exit_time = ? WHERE id = ?''',
                 (outcome_status, exit_price, net_pnl, datetime.now().isoformat(), trade_id))
    conn.commit()
    conn.close()

def update_trade_state(trade_id, new_stop_pct, new_mfe_pct, tsl_active):
    conn = sqlite3.connect(DB_NAME)
    conn.execute('''UPDATE trades SET current_stop_pct = ?, mfe_pct = ?, tsl_active = ? WHERE id = ?''',
                 (new_stop_pct, new_mfe_pct, int(tsl_active), trade_id))
    conn.commit()
    conn.close()
