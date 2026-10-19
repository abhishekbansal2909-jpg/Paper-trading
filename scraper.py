import pandas as pd
from curl_cffi import requests
import yfinance as yf
import time
from datetime import datetime, timedelta


class LiveMarketFetcher:
    def __init__(self):
        # curl_cffi bypasses Akamai TLS fingerprinting
        self.session = requests.Session(impersonate="chrome120")
        self.session.headers.update({
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/option-chain"  # Critical for bypassing the hang
        })
        self._init_nse_cookies()

    def _init_nse_cookies(self):
        try:
            self.session.get("https://www.nseindia.com", timeout=10)
        except Exception:
            pass

    def get_daily_atr(self, symbol, lookback=7):
        """Fetches last 15 days from Yahoo Finance to calculate 7-day ATR safely."""
        yf_symbol = f"{symbol}.NS"
        try:
            df = yf.download(yf_symbol, period="15d", progress=False)
            if df.empty: return None
            df['Return'] = df['Close'].pct_change().abs() * 100.0
            return df['Return'].rolling(window=lookback).mean().iloc[-1]
        except Exception:
            return None

    def fetch_live_walls(self, symbol, min_oi_threshold=5000):
        url = f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol.replace(' ', '%20')}"
        
        for _ in range(3):
            try:
                res = self.session.get(url, timeout=10)
                if res.status_code == 401:
                    self._init_nse_cookies()
                    continue
                if res.status_code == 200:
                    data = res.json()

                    # SAFETY CHECK: Skip if NSE returns an empty object {}
                    if not data or 'records' not in data:
                        return []
                    
                    spot = data['records']['underlyingValue']
                    options = data['records']['data']
                    
                    df = pd.json_normalize(options)
                    walls = []
                    
                    for opt_type in ['CE', 'PE']:
                        if opt_type in df.columns or f'{opt_type}.openInterest' in df.columns:
                            oi_col = f'{opt_type}.openInterest'
                            strike_col = 'strikePrice'
                            
                            valid_strikes = df[df[oi_col] >= min_oi_threshold].copy()
                            if valid_strikes.empty: continue
                                
                            valid_strikes = valid_strikes.sort_values(by=oi_col, ascending=False)
                            max_row = valid_strikes.iloc[0]
                            total_oi = df[oi_col].sum()
                            
                            walls.append({
                                'SYMBOL': symbol,
                                'SPOT': spot,
                                'OPTION_TYP': opt_type,
                                'WALL_STRIKE': max_row[strike_col],
                                'WALL_OI': max_row[oi_col],
                                'TOTAL_TYPE_OI': total_oi,
                                'CONCENTRATION': max_row[oi_col] / total_oi if total_oi > 0 else 0,
                                'DIST_PCT': abs(((max_row[strike_col] - spot) / spot) * 100.0)
                            })
                    return walls
            except Exception:
                time.sleep(2)
        return []
