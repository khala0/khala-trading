"""
Khala Trading -- News Filter
------------------------------
Blocks new trade signals around high-impact economic news releases, using
ForexFactory's public calendar page (scraped, no API key needed).

Honesty about reliability: this scrapes HTML rather than using an official
API, since ForexFactory doesn't offer one publicly. That means:
  - It can break if ForexFactory changes their page structure
  - It should be used as a helpful filter, not a guarantee
  - Every scrape is wrapped in try/except with a safe fallback (if the
    scrape fails, the filter reports "no known news" rather than crashing
    the whole signal pipeline)

FIXED 2026-07-17 -- two bugs that would have made this filter silently
ineffective:

1. Timezone. ForexFactory shows times in America/New_York for anonymous,
   logged-out visitors (its default display timezone) -- NOT UTC. The
   previous version treated scraped time strings as if they were already
   UTC. With only a +/-30 minute blackout window, a 4-5 hour (EST/EDT)
   offset meant the filter was checking a window nowhere near the actual
   release. This version localizes to America/New_York explicitly and
   converts to UTC via zoneinfo, which also gets EST/EDT right
   automatically.

2. Day tracking. The calendar's default view spans the whole week, not
   just today, so every event row was previously being stamped with
   "today's" date regardless of which day it actually belonged to. This
   version tracks the day-breaker rows ForexFactory uses to group each
   day's events and carries that date forward, the same way the page's
   own rowspan/grouping does visually.

VERIFY BOTH of these against a real NFP/CPI/FOMC day using
/api/admin/news-check before relying on this in production -- this module
was written and unit-tested against realistic sample HTML in a sandbox
with no live internet access, so the live scrape has still never been
run against the real site. See _parse_forexfactory_html's docstring for
what to check if it stops matching events.
"""

import urllib.request
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FOREXFACTORY_CALENDAR_URL = 'https://www.forexfactory.com/calendar'
FOREXFACTORY_DISPLAY_TZ = ZoneInfo('America/New_York')  # default for anonymous/logged-out visitors

# Map our internal symbols to the currencies whose news should block them.
SYMBOL_CURRENCIES = {
    'XAUUSD': ['USD'],
    'XAGUSD': ['USD'],
    'EURUSD': ['EUR', 'USD'],
    'GBPUSD': ['GBP', 'USD'],
    'GBPJPY': ['GBP', 'JPY'],
    'AUDUSD': ['AUD', 'USD'],
    'USDJPY': ['USD', 'JPY'],
    'US30': ['USD'],
    'NAS100': ['USD'],
    'BTCUSD': ['USD'],
}

_cache = {'events': None, 'fetched_at': 0}
CACHE_TTL_SECONDS = 1800  # re-scrape at most every 30 minutes

_MONTH_ABBR = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1
)}
_DAY_CELL_RE = re.compile(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\W+(\d{1,2})\b', re.IGNORECASE)


def fetch_high_impact_events():
    """
    Scrapes ForexFactory's calendar page for high-impact ("red folder")
    events. Returns a list of dicts: {'datetime': datetime (UTC, naive),
    'currency': str, 'event': str}. Returns an empty list (not an error)
    if the scrape fails for any reason -- callers should treat an empty
    list as "no known high-impact news," which fails safe (trades are
    allowed, not blocked).

    Cached for CACHE_TTL_SECONDS to avoid hammering ForexFactory on every
    signal request.
    """
    now = time.time()
    if _cache['events'] is not None and now - _cache['fetched_at'] < CACHE_TTL_SECONDS:
        return _cache['events']

    events = []
    try:
        req = urllib.request.Request(
            FOREXFACTORY_CALENDAR_URL,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
        events = _parse_forexfactory_html(html)
    except Exception:
        # Fail safe: if scraping breaks (site structure changed, blocked,
        # network issue), return no events rather than crashing the signal
        # pipeline. This means the filter effectively turns itself off
        # rather than blocking all trading by mistake.
        events = []

    _cache['events'] = events
    _cache['fetched_at'] = now
    return events


def _extract_day_cell_date(row_html, fallback_year):
    """
    Looks for a 'Jul 13' - style date inside a calendar row (ForexFactory
    prints this once per day -- e.g. on a calendar__row--day-breaker row,
    or the first event row's calendar__date cell -- then leaves it blank
    on subsequent rows via rowspan). Returns a date(), or None if this
    particular row doesn't carry one.
    """
    text = re.sub(r'<[^>]+>', ' ', row_html)  # strip tags -> plain text
    m = _DAY_CELL_RE.search(text)
    if not m:
        return None
    month = _MONTH_ABBR[m.group(1).lower()]
    day = int(m.group(2))
    try:
        return datetime(fallback_year, month, day).date()
    except ValueError:
        return None


def _parse_forexfactory_html(html):
    """
    Parses ForexFactory's calendar HTML for high-impact events.

    HOW TO FIX THIS IF FOREXFACTORY CHANGES THEIR PAGE:
    ForexFactory marks high-impact events with a specific CSS class on the
    impact icon (historically something like "icon--ff-impact-red" or
    "high" inside a "calendar__impact" cell). If this function starts
    returning an empty list on a day you know has major news (NFP, CPI,
    FOMC), view-source the calendar page and search for the impact icon's
    class name, then update HIGH_IMPACT_MARKER below to match. If dates
    look wrong, do the same for _DAY_CELL_RE / _extract_day_cell_date.

    This regex-based approach is deliberately simple (no BeautifulSoup
    dependency) -- swap in a proper HTML parser if the structure turns out
    to be too irregular for regex to handle reliably.
    """
    HIGH_IMPACT_MARKER = 'impact--high'  # adjust if ForexFactory's markup differs

    events = []
    rows = re.findall(r'<tr[^>]*calendar__row[^>]*>.*?</tr>', html, re.DOTALL)

    now_ny = datetime.now(FOREXFACTORY_DISPLAY_TZ)
    current_date = now_ny.date()  # best guess until the first day cell updates it
    fallback_year = now_ny.year

    for row in rows:
        # Every row can carry a date cell; FF only prints it on the first
        # row of each day and leaves it blank afterwards (rowspan), so we
        # carry the last seen value forward across rows, same as the page
        # visually groups them.
        row_date = _extract_day_cell_date(row, fallback_year)
        if row_date is not None:
            # Handle the one edge case a bare month/day can't: a week view
            # that crosses a Dec -> Jan year boundary.
            if (row_date - current_date).days < -180:
                row_date = row_date.replace(year=row_date.year + 1)
            current_date = row_date

        if HIGH_IMPACT_MARKER not in row:
            continue

        time_match = re.search(r'calendar__time[^>]*>([^<]*)<', row)
        currency_match = re.search(r'calendar__currency[^>]*>([^<]*)<', row)
        event_match = re.search(r'calendar__event[^>]*>.*?title="([^"]*)"', row, re.DOTALL) \
            or re.search(r'calendar__event[^>]*>([^<]*)<', row)

        if not (time_match and currency_match):
            continue

        time_str = time_match.group(1).strip()
        currency = currency_match.group(1).strip()
        event_name = event_match.group(1).strip() if event_match else 'High-impact event'

        parsed_time_utc = _parse_event_time(time_str, current_date)
        if parsed_time_utc is None:
            continue

        events.append({
            'datetime': parsed_time_utc,
            'currency': currency,
            'event': event_name,
        })

    return events


def _parse_event_time(time_str, event_date):
    """
    Parses ForexFactory's time strings (e.g. '8:30am') into a UTC datetime.
    Both `event_date` and `time_str` are in ForexFactory's display timezone
    (America/New_York for an anonymous scrape) -- localize first, then
    convert to UTC, so callers can compare directly against UTC 'now'.
    """
    time_str = time_str.strip().lower()
    if not time_str or time_str in ('all day', 'tentative'):
        return None
    try:
        parsed = datetime.strptime(time_str, '%I:%M%p')
    except ValueError:
        return None
    local_dt = datetime.combine(event_date, parsed.time(), tzinfo=FOREXFACTORY_DISPLAY_TZ)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def is_near_high_impact_news(symbol, minutes_before=30, minutes_after=30, now=None):
    """
    Checks whether the current time falls within a blackout window around
    any high-impact event for currencies relevant to `symbol`.

    Returns (is_blocked: bool, matching_event: dict or None).
    Fails safe: if the news fetch itself fails, returns (False, None) --
    i.e. does not block trading just because the news check couldn't run.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    relevant_currencies = SYMBOL_CURRENCIES.get(symbol.upper(), [])
    if not relevant_currencies:
        return False, None

    events = fetch_high_impact_events()

    for event in events:
        if event['currency'] not in relevant_currencies:
            continue
        window_start = event['datetime'] - timedelta(minutes=minutes_before)
        window_end = event['datetime'] + timedelta(minutes=minutes_after)
        if window_start <= now <= window_end:
            return True, event

    return False, None
