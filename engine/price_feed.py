"""
Price data fetcher. Pulls OHLCV candles from Yahoo Finance. Works directly
from the backend (no CORS issue server-side -- the allorigins.win proxy
trick is only needed for browser-side fetches, not from Flask).

FIXED 2026-07-17 -- this was trading a *related but not identical*
instrument to what actually gets executed on Exness, in two ways:

1. XAUUSD/XAGUSD were mapped to COMEX futures (GC=F / SI=F). Futures and
   spot/CFD gold track closely but aren't identical (contango/backwardation,
   different settlement/session gaps), so structure and SL/TP were being
   computed against a slightly different price series than the one that
   actually fills the trade. Each now tries a spot-style ticker first
   (XAUUSD=X / XAGUSD=X) and falls back to the futures ticker only if that
   fails -- I can't confirm from this sandbox (no network access to Yahoo)
   that the spot ticker is populated the same way for every plan/region, so
   the fallback matters. Verify with GET /api/price/XAUUSD after deploying.
2. US30/NAS100 were mapped to cash indices (^DJI / ^NDX), which only update
   during NYSE cash hours (~9:30am-4pm ET) -- meaning candles are flat/stale
   for the other ~17.5 hours a day, right when Exness's CFD is still moving.
   Switched to the corresponding index futures (YM=F / NQ=F), which trade
   nearly 24/5 and track a 24-hour CFD much more closely, with the cash
   index kept as a fallback. Trade-off: futures roll to a new contract a
   few times a year, which can show up as a small one-candle basis jump --
   comparatively minor next to 17.5 dead hours a day.

Each entry below is a list of tickers tried in order; the first one that
returns usable data wins.
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import time

# Map friendly symbols to Yahoo Finance tickers, in fallback order.
SYMBOL_MAP = {
    'XAUUSD': ['XAUUSD=X', 'GC=F'],
    'XAGUSD': ['XAGUSD=X', 'SI=F'],
    'EURUSD': ['EURUSD=X'],
    'GBPUSD': ['GBPUSD=X'],
    'GBPJPY': ['GBPJPY=X'],
    'AUDUSD': ['AUDUSD=X'],
    'USDJPY': ['USDJPY=X'],
    'US30': ['YM=F', '^DJI'],
    'NAS100': ['NQ=F', '^NDX'],
    'BTCUSD': ['BTC-USD'],
}

YF_URL = (
    'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
    '?interval={interval}&range={range_}'
)

_cache = {}
CACHE_TTL_SECONDS = 20
FETCH_RETRIES = 2  # extra attempts per ticker before moving to the next fallback


def resample_candles(candles, bars_per_group):
    """
    Aggregates consecutive lower-timeframe candles into higher-timeframe bars.
    E.g. resample_candles(one_hour_candles, 4) builds 4-hour bars from 1-hour data.

    Groups from the START of the list forward (oldest first), so the most
    recent group may be a partial/still-forming bar if the data doesn't
    divide evenly -- that's expected and fine, since it mirrors how the
    live/currently-forming higher-timeframe candle actually behaves.
    """
    if bars_per_group <= 1:
        return list(candles)

    resampled = []
    for i in range(0, len(candles), bars_per_group):
        group = candles[i:i + bars_per_group]
        if not group:
            continue
        resampled.append({
            'time': group[0]['time'],
            'open': group[0]['open'],
            'high': max(c['high'] for c in group),
            'low': min(c['low'] for c in group),
            'close': group[-1]['close'],
            'volume': sum(c.get('volume', 0) for c in group),
        })
    return resampled


def fetch_multi_timeframe(symbol, htf_1h_range='3mo', ltf_5m_range='5d'):
    """
    Fetches everything needed for the 4H (trend) -> 1H (refinement) -> 5M
    (execution) hierarchy in one call:
        - 1-hour candles (used directly for refinement, and resampled for 4H)
        - 4-hour candles (built by resampling the 1-hour data, 4 bars per group)
        - 5-minute candles (execution-only, never used for trend decisions)

    Returns a dict: {'candles_1h': [...], 'candles_4h': [...], 'candles_5m': [...]}
    """
    candles_1h = fetch_candles(symbol, interval='60m', range_=htf_1h_range)
    candles_4h = resample_candles(candles_1h, 4)
    candles_5m = fetch_candles(symbol, interval='5m', range_=ltf_5m_range)
    return {
        'candles_1h': candles_1h,
        'candles_4h': candles_4h,
        'candles_5m': candles_5m,
    }


def _fetch_one_ticker(ticker, interval, range_):
    """Single HTTP round-trip to Yahoo for one ticker. Raises on any failure
    (network, HTTP status, unexpected payload shape) -- caller decides what
    to do next (try the next fallback ticker, serve stale cache, or raise)."""
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
    if not candles:
        raise ValueError(f'{ticker}: response parsed but contained no usable candles')
    return candles


def fetch_candles(symbol: str, interval='5m', range_='5d'):
    """
    Returns a list of dicts: [{'open','high','low','close','volume','time'}, ...]
    ordered oldest -> newest. Cached briefly to avoid hammering Yahoo on
    every dashboard refresh.

    Tries each fallback ticker for `symbol` in order. If every ticker fails
    on this call but we have ANY previously cached data for this exact
    (symbol, interval, range_) -- even if past its TTL -- serves that stale
    data instead of raising, so a transient Yahoo/network blip degrades to
    "slightly old data" rather than a hard error. Only raises if every
    ticker fails AND there's no cached data at all to fall back to.
    """
    cache_key = (symbol, interval, range_)
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]['ts'] < CACHE_TTL_SECONDS:
        return _cache[cache_key]['data']

    tickers = SYMBOL_MAP.get(symbol.upper())
    if not tickers:
        raise ValueError(f'Unknown symbol: {symbol}')

    last_error = None
    for ticker in tickers:
        for attempt in range(FETCH_RETRIES + 1):
            try:
                candles = _fetch_one_ticker(ticker, interval, range_)
                _cache[cache_key] = {'ts': now, 'data': candles}
                return candles
            except Exception as e:
                last_error = e
                if attempt < FETCH_RETRIES:
                    time.sleep(0.5)

    # Every ticker (and every retry) failed. Fall back to whatever's cached,
    # however stale, rather than breaking the whole signal pipeline.
    if cache_key in _cache:
        print(f'[price_feed] WARNING: live fetch failed for {symbol} ({last_error}); '
              f'serving stale cached data from {now - _cache[cache_key]["ts"]:.0f}s ago')
        return _cache[cache_key]['data']

    raise RuntimeError(f'Could not fetch {symbol} from any ticker ({tickers}) and no cached data '
                        f'to fall back to. Last error: {last_error}')
