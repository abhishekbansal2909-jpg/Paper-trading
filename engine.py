import time
import math
from scraper import LiveMarketFetcher
from broker import init_db, get_open_trades, open_trade, update_trade_state, close_trade

CAPITAL_PER_TRADE = 250000.0
WATCHLIST = ["YESBANK", "PRESTIGE", "IRFC", "SAMMAANCAP", "MPHASIS"] # Add your universe here

def run_live_engine():
    init_db()
    fetcher = LiveMarketFetcher()
    print("V11 Live Engine Started...")

    while True:
        open_positions = {t['symbol']: t for t in get_open_trades()}

        for symbol in WATCHLIST:
            # 1. Manage Active Trades (TSL Tracking)
            if symbol in open_positions:
                trade = open_positions[symbol]
                walls = fetcher.fetch_live_walls(symbol)
                if not walls: continue
                
                current_spot = walls[0]['SPOT']
                cur_return = ((current_spot - trade['entry_price']) / trade['entry_price']) * 100.0 if trade['direction'] == 'LONG' else ((trade['entry_price'] - current_spot) / trade['entry_price']) * 100.0
                
                mfe_pct = max(trade['mfe_pct'], cur_return)
                tsl_active = trade['tsl_active']
                current_stop = trade['current_stop_pct']
                
                # Fetch ATR to maintain dynamic trail distance
                atr = fetcher.get_daily_atr(symbol) or 2.0 
                activation_hurdle = atr * 2.0
                trail_dist = atr * 1.25

                if not tsl_active and mfe_pct >= activation_hurdle:
                    tsl_active = 1
                
                if tsl_active:
                    calculated_tsl = mfe_pct - trail_dist
                    if calculated_tsl > current_stop:
                        current_stop = calculated_tsl

                if cur_return <= current_stop:
                    outcome = "TSL_HIT" if tsl_active else "STOPPED_OUT"
                    close_trade(trade['id'], current_spot, outcome)
                    print(f"[{symbol}] CLOSED {outcome} at {current_spot}")
                else:
                    update_trade_state(trade['id'], current_stop, mfe_pct, tsl_active)

            # 2. Scan for New Entries
            else:
                walls = fetcher.fetch_live_walls(symbol)
                for w in walls:
                    if w['CONCENTRATION'] >= 0.25 and w['DIST_PCT'] <= 0.75:
                        atr = fetcher.get_daily_atr(symbol)
                        if not atr: continue
                        
                        direction = "SHORT" if w['OPTION_TYP'] == 'CE' else "LONG"
                        qty = math.floor(CAPITAL_PER_TRADE / w['SPOT'])
                        
                        if qty > 0:
                            open_trade(symbol, direction, qty, w['SPOT'], atr * 1.5)
                            print(f"[{symbol}] OPENED {direction} at {w['SPOT']}")
                            break # Only take one position per symbol

        print("Tick complete. Sleeping for 5 minutes...")
        time.sleep(300) # 5-minute event loop

if __name__ == "__main__":
    run_live_engine()
