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

# V11 Risk Management Multipliers
ATR_STOP_MULTIPLIER = 1.5
ATR_TSL_ACTIVATION = 2.0
ATR_TSL_TRAIL = 1.25

def get_master_lot_sizes():
    print("Fetching official NFO lot sizes via Kite API...")
    url = "https://api.kite.trade/instruments"
    try:
        df = pd.read_csv(url)
        nfo_df = df[df['exchange'] == 'NFO'].copy()
        lot_map = nfo_df.drop_duplicates(subset=['name'])[['name', 'lot_size']]
        lot_dict = pd.Series(lot_map['lot_size'].values, index=lot_map['name'].str.strip().str.upper()).to_dict()
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
def log_trade_to_db(ticker, direction, qty, entry_price, atr_pct, initial_stop_pct):
    try:
        conn = sqlite3.connect("paper_trades.db")
        cursor = conn.cursor()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute("""
            INSERT INTO trades (
                symbol, direction, qty, entry_price, atr_pct, 
                current_stop_pct, mfe_pct, tsl_active, 
                status, exit_price, net_pnl, entry_time, exit_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticker, direction, qty, entry_price, atr_pct,
            initial_stop_pct, 0.0, 0, 'OPEN', None, 0.0, ts, None
        ))
        conn.commit()
        conn.close()
        print(f"[DB] Logged {direction} trade for {ticker} @ ₹{entry_price:.2f} | ATR: {atr_pct*100:.2f}% | Stop: {initial_stop_pct*100:.2f}%")
    except Exception as e:
        print(f"[DB ERROR] Failed to log trade: {e}")

def get_open_positions():
    try:
        conn = sqlite3.connect("paper_trades.db")
        cursor = conn.cursor()
        cursor.execute("SELECT id, symbol, direction, qty, entry_price, atr_pct, current_stop_pct, mfe_pct, tsl_active FROM trades WHERE status = 'OPEN'")
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
            SET status = 'STOPPED_OUT', exit_price = ?, net_pnl = ?, exit_time = ?
            WHERE id = ?
        """, (exit_price, pnl, ts, trade_id))
        conn.commit()
        conn.close()
        print(f"[DB] Closed trade ID {trade_id} @ ₹{exit_price:.2f} | Net PnL: ₹{pnl:,.2f}")
    except Exception as e:
        print(f"[DB ERROR] Failed to close trade: {e}")

def update_mfe_and_stop(trade_id, mfe_pct, current_stop_pct, tsl_active):
    try:
        conn = sqlite3.connect("paper_trades.db")
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE trades 
            SET mfe_pct = ?, current_stop_pct = ?, tsl_active = ?
            WHERE id = ?
        """, (mfe_pct, current_stop_pct, tsl_active, trade_id))
        conn.commit()
        conn.close()
    except Exception as e:
        pass

# ---------------------------------------------------------
# 3. RISK MANAGEMENT & POSITION MONITORING
# ---------------------------------------------------------
def calculate_indian_futures_fees(entry_price, exit_price, qty, direction):
    entry_val = entry_price * qty
    exit_val = exit_price * qty
    total_val = entry_val + exit_val

    entry_brokerage = min(entry_val * 0.0003, 20.0) + min(exit_val * 0.0003, 20.0)
    stt = (exit_val if direction == "BUY" else entry_val) * 0.0005 
    txn = total_val * 0.000019
    sebi = total_val * 0.000001
    stamp = (entry_val if direction == "BUY" else exit_val) * 0.00002
    gst = (entry_brokerage + txn + sebi) * 0.18

    return entry_brokerage + stt + txn + sebi + stamp + gst

def manage_open_positions():
    global AVAILABLE_CAPITAL
    open_trades = get_open_positions()
    if not open_trades: return

    for trade in open_trades:
        trade_id, symbol, direction, qty, entry_price, atr_pct, current_stop_pct, mfe_pct, tsl_active = trade
        try:
            walls = fetcher.fetch_live_walls(symbol)
            if not walls: continue
            spot = walls[0]['SPOT']
            
            # 1. Calculate Live Return
            pnl_pct = (spot - entry_price) / entry_price if direction == "BUY" else (entry_price - spot) / entry_price
            db_update_needed = False

            # 2. Track Maximum Favourable Excursion (MFE)
            if pnl_pct > mfe_pct:
                mfe_pct = pnl_pct
                db_update_needed = True

            # 3. Dynamic TSL Logic (Mirrored from v11)
            dynamic_tsl_activation = atr_pct * ATR_TSL_ACTIVATION
            dynamic_tsl_trail = atr_pct * ATR_TSL_TRAIL

            if not tsl_active and mfe_pct >= dynamic_tsl_activation:
                tsl_active = 1
                print(f"[TSL ACTIVATED] {symbol} crossed ATR threshold.")
                db_update_needed = True
            
            if tsl_active:
                calculated_tsl_stop = mfe_pct - dynamic_tsl_trail
                # We store stops as negative numbers relative to entry (e.g., -0.02 means 2% loss limit)
                # If calculated_tsl_stop is positive, we are locking in profit.
                if calculated_tsl_stop > current_stop_pct:
                    current_stop_pct = calculated_tsl_stop
                    db_update_needed = True

            if db_update_needed:
                update_mfe_and_stop(trade_id, mfe_pct, current_stop_pct, tsl_active)

            # 4. Evaluate Stop/TSL Exit
            if pnl_pct <= current_stop_pct:
                gross_pnl = (spot - entry_price) * qty if direction == "BUY" else (entry_price - spot) * qty
                fees = calculate_indian_futures_fees(entry_price, spot, qty, direction)
                net_pnl = gross_pnl - fees
                
                reason = "TSL HIT" if tsl_active else "STOPPED OUT"
                print(f"[{reason}] {symbol} exited at ₹{spot:.2f} | Net PnL: ₹{net_pnl:.2f}")
                
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
    if ticker in open_symbols: return

    qty = get_lot_size(ticker)
    margin_required = qty * entry_price * 0.20 
    
    if margin_required > AVAILABLE_CAPITAL:
        print(f"Insufficient capital for {ticker}.")
        return

    # Fetch ATR for dynamic stop (fallback to 2% if YF fails)
    atr_val = fetcher.get_daily_atr(ticker)
    atr_pct = (atr_val / 100.0) if atr_val and atr_val > 0 else 0.02
    initial_stop_pct = -(atr_pct * ATR_STOP_MULTIPLIER)

    AVAILABLE_CAPITAL -= margin_required
    log_trade_to_db(ticker, direction, qty, entry_price, atr_pct, initial_stop_pct)

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
                if not walls: continue
                    
                spot = walls[0]['SPOT']
                pe_wall = next((w for w in walls if w['OPTION_TYP'] == 'PE'), None)
                ce_wall = next((w for w in walls if w['OPTION_TYP'] == 'CE'), None)
                
                if pe_wall and ce_wall and (pe_wall['WALL_STRIKE'] == ce_wall['WALL_STRIKE']):
                    continue
                
                if pe_wall:
                    dte, total_oi = pe_wall.get('DAYS_TO_EXPIRY', 0), pe_wall.get('TOTAL_TYPE_OI', 1)
                    if (MIN_DTE <= dte <= MAX_DTE) and ((pe_wall.get('WALL_OI', 0) / total_oi if total_oi > 0 else 0) >= MIN_CHAIN_CONCENTRATION):
                        if abs(spot - pe_wall['WALL_STRIKE']) / spot <= PROXIMITY_THRESHOLD: 
                            process_signal(symbol, spot, "BUY")
                            
                if ce_wall:
                    dte, total_oi = ce_wall.get('DAYS_TO_EXPIRY', 0), ce_wall.get('TOTAL_TYPE_OI', 1)
                    if (MIN_DTE <= dte <= MAX_DTE) and ((ce_wall.get('WALL_OI', 0) / total_oi if total_oi > 0 else 0) >= MIN_CHAIN_CONCENTRATION):
                        if abs(spot - ce_wall['WALL_STRIKE']) / spot <= PROXIMITY_THRESHOLD: 
                            process_signal(symbol, spot, "SELL")
            
            time.sleep(60) 
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            time.sleep(10)

if __name__ == "__main__":
    conn = sqlite3.connect("paper_trades.db")
    # Upgraded schema to include atr_pct
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT, direction TEXT, qty INTEGER, 
                        entry_price REAL, atr_pct REAL, current_stop_pct REAL, 
                        mfe_pct REAL, tsl_active INTEGER, 
                        status TEXT, exit_price REAL, net_pnl REAL, 
                        entry_time TEXT, exit_time TEXT)''')
    conn.close()
    run_engine()
