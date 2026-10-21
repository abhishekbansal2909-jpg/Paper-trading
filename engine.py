import datetime
import sqlite3
import time
import pandas as pd
from scraper import LiveMarketFetcher

# ---------------------------------------------------------
# 1. CAPITAL, SIZING & CONSTRAINTS
# ---------------------------------------------------------
AVAILABLE_CAPITAL = 1000000  # ₹10 Lakhs margin pool
fetcher = LiveMarketFetcher()

# V11 Quantitative Backtest Settings
MIN_DTE = 8
MAX_DTE = 22
MIN_CHAIN_CONCENTRATION = 0.25
PROXIMITY_THRESHOLD = 0.0075

def get_master_lot_sizes():
    """Fetches official NFO lot sizes dynamically using the open Kite instruments dump."""
    print("Fetching official NFO lot sizes via Kite API...")
    url = "https://api.kite.trade/instruments"
    try:
        df = pd.read_csv(url)
        nfo_df = df[df['exchange'] == 'NFO'].copy()
        lot_map = nfo_df.drop_duplicates(subset=['name'])[['name', 'lot_size']]
        
        lot_dict = pd.Series(
            lot_map['lot_size'].values, 
            index=lot_map['name'].str.strip().str.upper()
        ).to_dict()
        
        print(f"Loaded official lot sizes for {len(lot_dict)} NFO tickers.")
        return lot_dict
    except Exception as e:
        print(f"Warning: Failed to fetch Kite lot sizes ({e}).")
        return {}

NSE_MASTER_LOTS = get_master_lot_sizes()
DYNAMIC_WATCHLIST = list(NSE_MASTER_LOTS.keys())

def get_lot_size(symbol):
    sym = symbol.strip().upper()
    return int(NSE_MASTER_LOTS.get(sym, 1))

# ---------------------------------------------------------
# 2. DATABASE OPERATIONS
# ---------------------------------------------------------
def log_trade_to_db(ticker, direction, qty, entry_price):
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
    try:
        conn = sqlite3.connect("paper_trades.db")
        cursor = conn.cursor()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            UPDATE trades 
            SET status = 'STOPPPED_OUT', exit_price = ?, net_pnl = ?, exit_time = ?
            WHERE id = ?
        """, (exit_price, pnl, ts, trade_id))
        conn.commit()
        conn.close()
        print(f"[DB] Closed trade ID {trade_id} @ ₹{exit_price:.2f} | PnL: ₹{pnl:,.2f}")
    except Exception as e:
        print(f"[DB ERROR] Failed to close trade: {e}")

# ---------------------------------------------------------
# 3. POSITION MONITORING & RISK MANAGEMENT
# ---------------------------------------------------------
def calculate_indian_futures_fees(entry_price, exit_price, qty, direction):
    entry_val = entry_price * qty
    exit_val = exit_price * qty
    total_val = entry_val + exit_val

    entry_brokerage = min(entry_val * 0.0003, 20.0)
    exit_brokerage = min(exit_val * 0.0003, 20.0)
    total_brokerage = entry_brokerage + exit_brokerage

    sell_val = exit_val if direction == "BUY" else entry_val
    stt = sell_val * 0.0005 

    txn = total_val * 0.000019
    sebi = total_val * 0.000001
    buy_val = entry_val if direction == "BUY" else exit_val
    stamp = buy_val * 0.00002
    gst = (total_brokerage + txn + sebi) * 0.18

    return total_brokerage + stt + txn + sebi + stamp + gst

def manage_open_positions():
    global AVAILABLE_CAPITAL
    open_trades = get_open_positions()
    if not open_trades:
        return

    for trade in open_trades:
        trade_id, symbol, direction, qty, entry_price, current_stop_pct, mfe_pct, tsl_active = trade
        try:
            walls = fetcher.fetch_live_walls(symbol)
            if not walls:
                continue
            spot = walls[0]['SPOT']
            
            pnl_pct = (spot - entry_price) / entry_price if direction == "BUY" else (entry_price - spot) / entry_price
            
            if pnl_pct >= 0.015 and not tsl_active:
                tsl_active = 1
                current_stop_pct = 0.005 
                print(f"[TSL ACTIVATED] {symbol} locked at +0.5%")
            
            if (direction == "BUY" and spot <= entry_price * (1 - current_stop_pct)) or \
               (direction == "SELL" and spot >= entry_price * (1 + current_stop_pct)):
                
                gross_pnl = (spot - entry_price) * qty if direction == "BUY" else (entry_price - spot) * qty
                fees = calculate_indian_futures_fees(entry_price, spot, qty, direction)
                net_pnl = gross_pnl - fees
                
                print(f"[EXIT TRIGGERED] {symbol} hit stop level at ₹{spot:.2f}")
                print(f"--> Gross PnL: ₹{gross_pnl:.2f} | Fees & Taxes: ₹{fees:.2f} | Net PnL: ₹{net_pnl:.2f}")
                
                close_trade_in_db(trade_id, spot, net_pnl)
                AVAILABLE_CAPITAL += (entry_price * qty * 0.20) + net_pnl
                
        except Exception as e:
            print(f"Error managing position {symbol}: {e}")

# ---------------------------------------------------------
# 4. ENTRY EXECUTION
# ---------------------------------------------------------
def process_signal(ticker, entry_price, direction):
    global AVAILABLE_CAPITAL
    
    open_symbols = [row[1] for row in get_open_positions()]
    if ticker in open_symbols:
        return

    qty = get_lot_size(ticker)
    margin_required = qty * entry_price * 0.20 
    
    if margin_required > AVAILABLE_CAPITAL:
        print(f"Insufficient capital for {ticker}. Required: ₹{margin_required:.2f}")
        return

    AVAILABLE_CAPITAL -= margin_required
    log_trade_to_db(ticker, direction, qty, entry_price)

# ---------------------------------------------------------
# 5. MAIN QUANTITATIVE LOOP
# ---------------------------------------------------------
def run_engine():
    print(f"\n🚀 Engine Active | Universe: {len(DYNAMIC_WATCHLIST)} Symbols | Capital: ₹{AVAILABLE_CAPITAL:,.2f}")
    
    while True:
        try:
            manage_open_positions()
            
            for symbol in DYNAMIC_WATCHLIST:
                time.sleep(1)
                walls = fetcher.fetch_live_walls(symbol)
                
                if not walls:
                    continue
                    
                spot = walls[0]['SPOT']
                pe_wall = next((w for w in walls if w['OPTION_TYP'] == 'PE'), None)
                ce_wall = next((w for w in walls if w['OPTION_TYP'] == 'CE'), None)
                
                # Straddle Protection: Skip if walls share the same strike
                if pe_wall and ce_wall and (pe_wall['WALL_STRIKE'] == ce_wall['WALL_STRIKE']):
                    continue
                
                # Evaluate PE Wall (Support -> BUY Signal)
                if pe_wall:
                    dte = pe_wall.get('DAYS_TO_EXPIRY', 0)
                    total_oi = pe_wall.get('TOTAL_TYPE_OI', 1)
                    concentration = pe_wall.get('WALL_OI', 0) / total_oi if total_oi > 0 else 0
                    
                    if (MIN_DTE <= dte <= MAX_DTE) and (concentration >= MIN_CHAIN_CONCENTRATION):
                        if abs(spot - pe_wall['WALL_STRIKE']) / spot <= PROXIMITY_THRESHOLD: 
                            print(f"[{symbol}] BUY SIGNAL | Spot: ₹{spot} | PE Wall: ₹{pe_wall['WALL_STRIKE']}")
                            process_signal(symbol, spot, "BUY")
                            
                # Evaluate CE Wall (Resistance -> SELL Signal)
                if ce_wall:
                    dte = ce_wall.get('DAYS_TO_EXPIRY', 0)
                    total_oi = ce_wall.get('TOTAL_TYPE_OI', 1)
                    concentration = ce_wall.get('WALL_OI', 0) / total_oi if total_oi > 0 else 0
                    
                    if (MIN_DTE <= dte <= MAX_DTE) and (concentration >= MIN_CHAIN_CONCENTRATION):
                        if abs(spot - ce_wall['WALL_STRIKE']) / spot <= PROXIMITY_THRESHOLD: 
                            print(f"[{symbol}] SELL SIGNAL | Spot: ₹{spot} | CE Wall: ₹{ce_wall['WALL_STRIKE']}")
                            process_signal(symbol, spot, "SELL")
            
            time.sleep(60) 
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            time.sleep(10)

if __name__ == "__main__":
    conn = sqlite3.connect("paper_trades.db")
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT, direction TEXT, qty INTEGER, 
                        entry_price REAL, current_stop_pct REAL, 
                        mfe_pct REAL, tsl_active INTEGER, 
                        status TEXT, exit_price REAL, net_pnl REAL, 
                        entry_time TEXT, exit_time TEXT)''')
    conn.close()
    run_engine()
