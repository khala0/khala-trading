"""
Price data fetcher. Pulls OHLCV candles from Yahoo Finance. Works directly
from the backend (no CORS issue server-side -- the allorigins.win proxy
trick is only needed for browser-side fetches, not from Flask).
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import time

# Map friendly symbols to Yahoo Finance tickers
SYMBOL_MAP = {
    'XAUUSD': 'GC=F',
    'XAGUSD': 'SI=F',
    'EURUSD': 'EURUSD=X',
    'GBPUSD': 'GBPUSD=X',
    'GBPJPY': 'GBPJPY=X',
    'AUDUSD': 'AUDUSD=X',
    'USDJPY': 'USDJPY=X',
    'US30': '^DJI',
    'NAS100': '^NDX',
    'BTCUSD': 'BTC-USD',
}

YF_URL = (
    'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
    '?interval={interval}&range={range_}'
)

_cache = {}
CACHE_TTL_SECONDS = 20


def fetch_candles(symbol: str, interval='5m', range_='5d'):
    """
    Returns a list of dicts: [{'open','high','low','close','volume','time'}, ...]
    ordered oldest -> newest. Cached briefly to avoid hammering Yahoo on
    every dashboard refresh.
    """
    cache_key = (symbol, interval, range_)
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]['ts'] < CACHE_TTL_SECONDS:
        return _cache[cache_key]['data']

    ticker = SYMBOL_MAP.get(symbol.upper())
    if not ticker:
        raise ValueError(f'Unknown symbol: {symbol}')

    url = YF_URL.format(ticker=urllib.parse.quote(ticker), interval=interval, range_=range_)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})

    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    result = data['chart']['result'][0]
    timestamps = result['timestamp']
    quote = result['indicators']['quote'][0]

    candles = []
    for i in range(len(timestamps)):
        o, h, l, c = quote['open'][i], quote['high'][i], quote['low'][i], quote['close'][i]
        if None in (o, h, l, c):
            continue
        candles.append({
            'time': timestamps[i],
            'open': o, 'high': h, 'low': l, 'close': c,
            'volume': quote.get('volume', [0] * len(timestamps))[i] or 0,
        })

    _cache[cache_key] = {'ts': now, 'data': candles}
    return candles
