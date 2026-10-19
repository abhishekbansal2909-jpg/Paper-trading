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
                net_pnl = (spot - entry_price) * qty if direction == "BUY" else (entry_price - spot) * qty
                print(f"[EXIT TRIGGERED] {symbol} hit stop level at ₹{spot:.2f}")
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
    if ticker in open_symbols:
        return
    
    lot_size = get_lot_size(ticker, entry_price)
    quantity = lot_size * 1  # Trade exactly 1 lot
    contract_value = quantity * entry_price
    margin_required = contract_value * 0.20  # 20% margin
    
    if AVAILABLE_CAPITAL >= margin_required:
        print(f"--> [SIGNAL EXECUTED] {direction} 1 lot ({quantity} shares) of {ticker} @ ₹{entry_price:.2f}")
        AVAILABLE_CAPITAL -= margin_required
        log_trade_to_db(ticker, direction, quantity, entry_price)
    else:
        print(f"Margin rejected for {ticker}: Requires ₹{margin_required:,.2f}, Available: ₹{AVAILABLE_CAPITAL:,.2f}")

def scan_and_evaluate():
    for ticker in WATCHLIST:
        try:
            walls = fetcher.fetch_live_walls(ticker)
            if not walls:
                continue
            
            spot = walls[0]['SPOT']
            call_wall = None
            put_wall = None
            
            for w in walls:
                if w['OPTION_TYP'] == 'CE':
                    call_wall = w['WALL_STRIKE']
                elif w['OPTION_TYP'] == 'PE':
                    put_wall = w['WALL_STRIKE']
            
            print(f"[{ticker}] Spot: ₹{spot:.2f} | Call Wall: {call_wall} | Put Wall: {put_wall}")
            
            # Extreme wall exhaustion trigger logic
            if call_wall and spot >= call_wall:
                print(f"--> [WALL HIT] {ticker} testing Call Wall resistance ({call_wall})")
                process_signal(ticker, spot, "SELL")
            elif put_wall and spot <= put_wall:
                print(f"--> [WALL HIT] {ticker} testing Put Wall support ({put_wall})")
                process_signal(ticker, spot, "BUY")
                
        except Exception as e:
            print(f"Error scanning {ticker}: {e}")

# ---------------------------------------------------------
# 6. MAIN LOOP
# ---------------------------------------------------------
def main():
    print("V1.1 Live Options Engine Initialized.")
    
    while True:
        # Weekend protection
        if datetime.datetime.today().weekday() >= 5:
            print("Weekend detected. Engine resting for 5 minutes...")
            time.sleep(300)
            continue
        
        now = datetime.datetime.now()
        print(f"\n--- [TICK] Cycle Started at {now.strftime('%H:%M:%S')} ---")
        
        # 1. Manage active positions and trailing stops
        manage_open_positions()
        
        # 2. Scan watchlist for new wall breaches
        scan_and_evaluate()
        
        print("Tick complete. Sleeping for 5 minutes...")
        time.sleep(300)

if __name__ == "__main__":
    main()
