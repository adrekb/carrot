"""Quotes for a dashboard widget, from a source that needs no account.

Every mainstream market API wants a key, and a key is an account, and an
account is the thing this app is built not to require. So the constraint here
is unusual: not "which feed is best" but "which feed can a local-first app use
without asking the user to go and sign up for something".

Stooq was the first candidate — plain CSV, no auth, documented. It 404s on
every symbol from here, on both its domains, with and without a browser user
agent, so it is not an option however good it looks on paper.

What is left is Yahoo's chart endpoint. It needs no key and no account, and it
is what this uses. Two honest caveats, both of which shape the code below:

* **It is unofficial.** There is no contract and no deprecation notice; it can
  change shape or start refusing without warning. So every field is read
  defensively, a missing one costs that quote and not the widget, and a total
  failure serves the last good reading marked stale rather than emptying the
  panel.
* **It is delayed and not a trading feed.** Every quote carries the time it is
  for, and the UI shows it. An undated price is indistinguishable from a live
  one, which is the failure that actually matters here — the widget would look
  exactly the same while being hours wrong.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

LOG = logging.getLogger(__name__)

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"
TIMEOUT = 8.0

# The endpoint refuses a bare client. This is the same header set websearch
# sends and for the same reason: it is what the user's own browser would send
# for a public page, and nothing more.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
}

# A dashboard polls and the data is delayed anyway, so this costs nothing in
# freshness and stops four widgets on one screen becoming four requests each
# refresh. One request per symbol is unavoidable — the endpoint is per-symbol —
# which makes the cache load-bearing rather than an optimisation.
CACHE_SECONDS = 60

MAX_SYMBOLS = 10

# Names people recognise, mapped to the tickers the endpoint wants. Nobody
# should have to know that the S&P 500 is `^GSPC` here.
CATALOGUE: List[Dict[str, str]] = [
    {"symbol": "^GSPC", "label": "S&P 500", "kind": "index"},
    {"symbol": "^IXIC", "label": "Nasdaq", "kind": "index"},
    {"symbol": "^DJI", "label": "Dow Jones", "kind": "index"},
    {"symbol": "^VIX", "label": "VIX", "kind": "index"},
    {"symbol": "^FTSE", "label": "FTSE 100", "kind": "index"},
    {"symbol": "NVDA", "label": "NVIDIA", "kind": "equity"},
    {"symbol": "AAPL", "label": "Apple", "kind": "equity"},
    {"symbol": "MSFT", "label": "Microsoft", "kind": "equity"},
    {"symbol": "GOOGL", "label": "Alphabet", "kind": "equity"},
    {"symbol": "AMZN", "label": "Amazon", "kind": "equity"},
    {"symbol": "TSLA", "label": "Tesla", "kind": "equity"},
    {"symbol": "BTC-USD", "label": "Bitcoin", "kind": "crypto"},
    {"symbol": "ETH-USD", "label": "Ethereum", "kind": "crypto"},
    {"symbol": "EURUSD=X", "label": "EUR/USD", "kind": "fx"},
    {"symbol": "GBPUSD=X", "label": "GBP/USD", "kind": "fx"},
    {"symbol": "GC=F", "label": "Gold", "kind": "commodity"},
]

DEFAULT_SYMBOLS = ["^GSPC", "^IXIC", "NVDA", "BTC-USD"]

_LABELS = {entry["symbol"]: entry["label"] for entry in CATALOGUE}

_cache: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# Tickers are a small alphabet. Anything else is not a ticker, and passing it
# through would put arbitrary user text into a URL path.
_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789^.-=")


def _clean(symbol: str) -> str:
    return "".join(c for c in (symbol or "").strip().upper() if c in _ALLOWED)[:16]


def _quote(symbol: str) -> Optional[Dict[str, Any]]:
    """One symbol, or None if it could not be priced.

    Every field is read with a default. This is an unofficial endpoint: a
    shape change should cost one quote, not raise through the widget.
    """
    try:
        response = httpx.get(
            f"{BASE}{symbol}", params={"interval": "1d", "range": "5d"},
            timeout=TIMEOUT, headers=HEADERS, follow_redirects=True,
        )
        response.raise_for_status()
        result = (response.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return None
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        # `previousClose` is frequently absent on indices while
        # `chartPreviousClose` is present; taking only the first is why the
        # change column would have been empty for exactly the symbols the
        # widget shows by default.
        previous = meta.get("previousClose")
        if previous is None:
            previous = meta.get("chartPreviousClose")
        if price is None:
            return None
    except Exception as exc:
        LOG.info("no quote for %s: %s", symbol, exc)
        return None

    quote: Dict[str, Any] = {
        "symbol": symbol,
        "label": _LABELS.get(symbol) or meta.get("shortName") or symbol,
        "currency": meta.get("currency") or "",
        "price": round(float(price), 4),
        "change": None,
        "change_percent": None,
        "at": meta.get("regularMarketTime"),
    }
    if previous:
        change = float(price) - float(previous)
        quote["change"] = round(change, 4)
        quote["change_percent"] = round(change / float(previous) * 100, 2)
    return quote


def quotes(symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """Quotes for these symbols. Never raises."""
    wanted = [s for s in (_clean(x) for x in (symbols or DEFAULT_SYMBOLS)) if s]
    wanted = list(dict.fromkeys(wanted))[:MAX_SYMBOLS]
    if not wanted:
        return {"quotes": [], "error": ""}

    now = time.time()
    out: List[Dict[str, Any]] = []
    failed: List[str] = []
    stale = False

    for symbol in wanted:
        with _lock:
            cached = _cache.get(symbol)
        if cached and now - cached["at"] < CACHE_SECONDS:
            out.append(cached["quote"])
            continue

        quote = _quote(symbol)
        if quote is not None:
            with _lock:
                _cache[symbol] = {"at": now, "quote": quote}
            out.append(quote)
            continue

        # The last good reading, marked, rather than a hole. A widget that
        # empties itself on one failed poll flickers on any connection that is
        # less than perfect, and a price from a minute ago is far closer to
        # the truth than no price.
        if cached:
            stale = True
            out.append({**cached["quote"], "stale": True})
        else:
            failed.append(symbol)
            out.append({
                "symbol": symbol, "label": _LABELS.get(symbol, symbol),
                "price": None, "change": None, "change_percent": None,
                "at": None, "unavailable": True,
            })

    return {
        "quotes": out,
        # Named rather than silently absent, so a typo in a custom symbol is
        # visible instead of just missing from the list.
        "error": (f"no data for {', '.join(failed)}" if failed else ""),
        "stale": stale,
    }
