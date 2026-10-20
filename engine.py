import datetime
import io
import sqlite3
import time
import pandas as pd
import requests
from scraper import LiveMarketFetcher

# ---------------------------------------------------------
# 1. CAPITAL & DYNAMIC LOT SIZING
# ---------------------------------------------------------
AVAILABLE_CAPITAL = 1000000  # ₹10 Lakhs margin pool
fetcher = LiveMarketFetcher()

def get_master_lot_sizes():
    """Fetches official NFO lot sizes dynamically using the open Kite instruments dump."""
    print("Fetching official NFO lot sizes via Kite API...")
    url = "https://api.kite.trade/instruments"
    try:
        df = pd.read_csv(url)
        # Filter for F&O exchange and drop duplicate tickers to map one lot size per symbol
        nfo_df = df[df['exchange'] == 'NFO'].copy()
        lot_map = nfo_df.drop_duplicates(subset=['name'])[['name', 'lot_size']]
        
        lot_dict = pd.Series(
            lot_map['lot_size'].values, 
            index=lot_map['name'].str.strip().str.upper()
        ).to_dict()
        
        print(f"Loaded official lot sizes for {len(lot_dict)} NFO tickers.")
        return lot_dict
    except Exception as e:
        print(f"Warning: Failed to fetch Kite lot sizes ({e}). Fallback to 1.")
        return {}

NSE_MASTER_LOTS = get_master_lot_sizes()

def get_lot_size(symbol, entry_price=100):
    """Returns official exchange multiple from Kite."""
    sym = symbol.strip().upper()
    # Cast to int, falling back to 1 if missing
    return int(NSE_MASTER_LOTS.get(sym, 1))

# ---------------------------------------------------------
# 2. WATCHLIST CONFIGURATION
# ---------------------------------------------------------
WATCHLIST = [
    "YESBANK", "SAMMAANCAP", "PRESTIGE", "IRFC", 
    "LICHSGFIN", "INDUSTOWER", "MPHASIS"
]

# ---------------------------------------------------------
# 3. DATABASE OPERATIONS
# ---------------------------------------------------------
def log_trade_to_db(ticker, direction, qty, entry_price):
    """Inserts newly executed trade matching existing schema."""
    try:
        conn = sqlite3.connect("paper_trades.db")
        cursor = conn.cursor()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute("""
            INSERT INTO trades (
                symbol, direction, qty, entry_price, 
                current_stop_pct, mfe_pct, tsl_active, 
                status, exit_price, net_pnl, entry_time, exit_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticker, direction, qty, entry_price,
            0.02, 0.0, 0, 'OPEN', None, 0.0, ts, None
        ))
        
        conn.commit()
        conn.close()
        print(f"[DB] Logged {direction} trade for {ticker} ({qty} shares @ ₹{entry_price:.2f})")
    except Exception as e:
        print(f"[DB ERROR] Failed to log trade: {e}")

def get_open_positions():
    """Retrieves all currently open positions."""
    try:
        conn = sqlite3.connect("paper_trades.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, symbol, direction, qty, entry_price, current_stop_pct, mfe_pct, tsl_active FROM trades WHERE status = 'OPEN'")
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch open positions: {e}")
        return []

def close_trade_in_db(trade_id, exit_price, pnl):
    """Marks an open trade as CLOSED with final PnL."""
    try:
        conn = sqlite3.connect("paper_trades.db")
        cursor = conn.cursor()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            UPDATE trades 
            SET status = 'CLOSED', exit_price = ?, net_pnl = ?, exit_time = ?
            WHERE id = ?
        """, (exit_price, pnl, ts, trade_id))
        conn.commit()
        conn.close()
        print(f"[DB] Closed trade ID {trade_id} @ ₹{exit_price:.2f} | PnL: ₹{pnl:,.2f}")
    except Exception as e:
        print(f"[DB ERROR] Failed to close trade: {e}")

# ---------------------------------------------------------
# 4. POSITION MONITOR & TSL (TRAILING STOP LOSS)
# ---------------------------------------------------------
def calculate_indian_futures_fees(entry_price, exit_price, qty, direction):
    """Calculates Zerodha brokerage + 2026 STT + Exchange + Stamp + GST for Futures ONLY."""
    entry_val = entry_price * qty
    exit_val = exit_price * qty
    total_val = entry_val + exit_val

    entry_brokerage = min(entry_val * 0.0003, 20.0)
    exit_brokerage = min(exit_val * 0.0003, 20.0)
    total_brokerage = entry_brokerage + exit_brokerage

    # 2026 Revised STT Rate
    sell_val = exit_val if direction == "BUY" else entry_val
    stt = sell_val * 0.0005 

    txn = total_val * 0.000019
    sebi = total_val * 0.000001
    buy_val = entry_val if direction == "BUY" else exit_val
    stamp = buy_val * 0.00002
    gst = (total_brokerage + txn + sebi) * 0.18

    return total_brokerage + stt + txn + sebi + stamp + gst

def manage_open_positions():
    open_trades = get_open_positions()
    if not open_trades:
        return

    print(f"Monitoring {len(open_trades)} active positions...")
    for trade in open_trades:
        trade_id, symbol, direction, qty, entry_price, current_stop_pct, mfe_pct, tsl_active = trade
        try:
            walls = fetcher.fetch_live_walls(symbol)
            if not walls:
                continue
            spot = walls[0]['SPOT']
            
            # Calculate PnL percentage
            if direction == "BUY":
                pnl_pct = (spot - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - spot) / entry_price
            
            # Trailing Stop Loss trigger at 1.5% profit
            if pnl_pct >= 0.015 and not tsl_active:
                tsl_active = 1
                current_stop_pct = 0.005  # Lock in +0.5% minimum profit
                print(f"[TSL ACTIVATED] {symbol} locked at +0.5%")
            
            # Stop condition check
            if (direction == "BUY" and spot <= entry_price * (1 - current_stop_pct)) or \
               (direction == "SELL" and spot >= entry_price * (1 + current_stop_pct)):
                
                # Calculate Gross PnL using real lot quantity
                gross_pnl = (spot - entry_price) * qty if direction == "BUY" else (entry_price - spot) * qty
                
                # Deduct true F&O fees and taxes
                fees = calculate_indian_futures_fees(entry_price, spot, qty, direction)
                net_pnl = gross_pnl - fees
                
                print(f"[EXIT TRIGGERED] {symbol} hit stop level at ₹{spot:.2f}")
                print(f"--> Gross PnL: ₹{gross_pnl:.2f} | Fees & Taxes: ₹{fees:.2f} | Net PnL: ₹{net_pnl:.2f}")
                
                close_trade_in_db(trade_id, spot, net_pnl)
                
        except Exception as e:
            print(f"Error managing position {symbol}: {e}")

# ---------------------------------------------------------
# 5. ENTRY SIGNAL EVALUATION
# ---------------------------------------------------------
def process_signal(ticker, entry_price, direction):
    global AVAILABLE_CAPITAL
    
    # Check if we already have an open position in this stock
    open_symbols = [row[1] for row in get_open_positions()]
    if ticker in
