"""Web search and page fetching, with the network boundary enforced.

Both Carrot Research and Carrot Agent pull text off the open web, and both feed
what they find to a model. That makes this module the point where two things
have to be true:

* **A fetch cannot be aimed at the local network.** Every hop is checked, not
  just the URL the caller passed. Following redirects with the HTTP client's
  own ``follow_redirects`` would let a public URL bounce to ``127.0.0.1`` and
  hand back the contents of some other service on the machine, so redirects are
  walked one at a time with the address check re-run on each.
* **What comes back is untrusted.** Every result is screened for
  prompt-injection and returned with that verdict attached, so the caller can
  taint its run rather than discovering the problem after the model has acted.

Nothing here decides whether a fetch is *allowed* — that is the policy
kernel's job and the caller's. This module refuses only what is unsafe at the
transport level.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List
from urllib.parse import quote, urljoin, urlparse

LOG = logging.getLogger(__name__)

import httpx

from . import policy

# A header set a real browser would send. The previous value announced itself
# as a bot ("compatible; Carrot/1.0"), and sent nothing else — no Accept, no
# Accept-Language. Every serious news site runs bot management that 403s
# exactly that shape, which is why Politico and Cook Political refused while
# AP and NYT let us through. The user asked to read a public page from their
# own machine; this sends what their own browser would send for it, and
# nothing more. No cookie forging, no proxy rotation, no captcha work — a
# site that still says no is taken at its word.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
MAX_REDIRECTS = 5
MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_CHARS = 6000

TEXTUAL_TYPES = ("text/html", "text/plain", "application/xhtml", "application/json", "text/markdown")

_STRIP_TAGS = ["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg", "iframe"]


# ===== Search =====

# Words that carry no topical signal, so matching on them means nothing.
_STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "best", "by", "can", "compare",
    "comparison", "do", "does", "for", "from", "has", "have", "how", "in", "is", "it",
    "its", "latest", "list", "most", "new", "of", "on", "or", "other", "run", "running",
    "that", "the", "their", "there", "these", "this", "to", "top", "use", "using",
    "what", "when", "which", "who", "why", "with", "you", "your",
}
_MIN_TERM_LEN = 3


def _terms(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9.+-]*", (text or "").lower())
            if len(w) >= _MIN_TERM_LEN and w not in _STOPWORDS}


def _is_relevant(query: str, title: str, snippet: str) -> bool:
    """Does this result share any real topic word with the query?

    A search backend that breaks does not fail loudly — it returns a page of
    unrelated results (an RTX 4090 query coming back with centimetre-to-feet
    converters). Feeding those into a research report is far worse than
    returning nothing, so anything with no lexical overlap is dropped.
    """
    wanted = _terms(query)
    if not wanted:
        return True
    overlap = len(wanted & _terms(f"{title} {snippet}"))
    # One shared word is coincidence on a long query — "RTX" alone matches a
    # motorcycle. Ask for a second term once the query is specific enough.
    return overlap >= (2 if len(wanted) >= 4 else 1)


# Paths that never contain an answer, only a door to one.
_NON_CONTENT_PATHS = re.compile(
    r"^/(login|signin|sign-in|signup|sign-up|register|account|auth|cart|checkout|"
    r"pricing|download|downloads|contact|about|careers|jobs|legal|privacy|terms|"
    r"cookie|subscribe|newsletter)(/|$)", re.I)


def is_content_url(url: str) -> bool:
    """Reject links that cannot answer a specific question.

    A research run that "reads github.com" has read the GitHub homepage —
    a marketing page with no bearing on the question. Bare domains and
    sign-in/marketing paths are doors, not documents.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    path = parsed.path or "/"
    # A bare domain is a homepage. Allow it only when a query string carries
    # the actual request (some sites put article ids there).
    if path in ("", "/") and not parsed.query:
        return False
    if _NON_CONTENT_PATHS.match(path):
        return False
    return True


def _raw_search(query: str, max_results: int, region: str) -> List[Dict[str, Any]]:
    """Query DDG through whichever client library is installed.

    ``duckduckgo_search`` was renamed to ``ddgs``; the abandoned package
    still installs but now proxies to Bing and returns junk, so the new
    one is tried first.
    """
    last_error = None
    try:
        from ddgs import DDGS

        with DDGS() as client:
            return list(client.text(query, max_results=max_results, region=region))
    except ImportError:
        pass
    except Exception as exc:
        last_error = exc
    try:
        from duckduckgo_search import DDGS as LegacyDDGS

        with LegacyDDGS() as client:
            return list(client.text(query, max_results=max_results, region=region))
    except ImportError:
        pass
    except Exception as exc:
        last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no DuckDuckGo client installed — pip install ddgs")


def search(query: str, max_results: int = 6, region: str = "wt-wt") -> List[Dict[str, str]]:
    """Free web search via DuckDuckGo. Returns [{title, url, snippet}].

    Failures return an empty list rather than raising: a research run that
    loses one query should narrow its scope, not collapse. Results with no
    topical overlap with the query are discarded — see ``_is_relevant``.
    """
    if not (query or "").strip():
        return []
    try:
        raw = _raw_search(query, max_results, region)
    except Exception as exc:
        LOG.warning("web search failed for %r: %s", query[:80], exc)
        return []

    results = []
    rejects = []
    dropped = 0
    for item in raw:
        url = item.get("href") or item.get("url") or ""
        if not url:
            continue
        title = (item.get("title") or "").strip()
        snippet = (item.get("body") or item.get("description") or "").strip()
        kept = {"title": title, "url": url, "snippet": snippet}
        if not _is_relevant(query, title, snippet):
            dropped += 1
            rejects.append(kept)
            continue
        results.append(kept)
    if dropped:
        LOG.info("dropped %d off-topic result(s) for %r", dropped, query[:80])
    # The filter exists to catch a backend that has broken and is returning
    # unit converters for a GPU query. It is not there to decide that the
    # Associated Press is not about American politics — but with a short query
    # and a headline that shares no wording, that is exactly what it does, and
    # an empty result page reads to the user as a blocked source. So it may
    # thin a page of results; it may never empty one. If nothing survived, the
    # backend was probably fine and the wording simply did not line up.
    if not results and rejects:
        LOG.info("relevance filter emptied the page for %r; keeping raw results",
                 query[:80])
        results = rejects
    # Reputable sources first. The model reads from the top, so ordering does
    # most of the work that a block list would do, without losing the long
    # tail of legitimate small sites.
    results = rank_results(results)
    for item in results:
        item["source_rank"] = source_rank(item["url"])
        # Carried on every result so the model can prefer an article over an
        # index, and so a citation can be drawn without fetching the page
        # again. A result used to be title/url/snippet and nothing else, which
        # is why nothing downstream could say where an answer came from or
        # when it was written.
        item["kind"] = page_kind(item["url"])
        item["site"] = site_name(item["url"])
        item["date"] = date_from_url(item["url"])
    return results


# ===== Source quality =====
# A free search backend will happily return content farms. Asked for recent
# American political news it came back with 242movietv.com, saboridades.net
# and doktergaul.com — sites that scrape and reword, carry no byline, and are
# worthless to cite. Relevance filtering does not catch them, because their
# whole trick is to match the query wording closely.
#
# This is a ranking, not a block list: an unknown domain is still usable and
# still returned, it just sorts below a known one. Blocking would break the
# long tail of legitimate small sites, which is most of the web.

REPUTABLE_DOMAINS = {
    # Wire services and major outlets
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "npr.org",
    "nytimes.com", "washingtonpost.com", "wsj.com", "ft.com", "economist.com",
    "theguardian.com", "bloomberg.com", "politico.com", "axios.com",
    "thehill.com", "cnn.com", "nbcnews.com", "abcnews.go.com", "cbsnews.com",
    "pbs.org", "propublica.org", "theatlantic.com", "newyorker.com",
    "aljazeera.com", "dw.com", "france24.com", "cnbc.com", "forbes.com",
    # Reference, standards, primary sources
    "wikipedia.org", "britannica.com", "nature.com", "science.org",
    "arxiv.org", "pubmed.ncbi.nlm.nih.gov", "nih.gov", "who.int",
    "congress.gov", "supremecourt.gov", "federalregister.gov", "gao.gov",
    "census.gov", "bls.gov", "cdc.gov", "nasa.gov", "noaa.gov",
    # Technical
    "github.com", "stackoverflow.com", "developer.mozilla.org", "python.org",
    "docs.python.org", "kernel.org", "ietf.org", "w3.org",
}

# Whole-domain suffixes that are institutional by construction.
REPUTABLE_SUFFIXES = (".gov", ".edu", ".mil", ".int", ".ac.uk", ".gov.uk", ".edu.au")

# Shapes that correlate with scraped filler rather than reporting.
_FARM_HINTS = re.compile(
    r"(^|\.)(blogspot|wordpress|weebly|wixsite|medium)\.com$|"
    r"(movie|film|stream|watch|download|casino|bet|slot|crypto|forex)",
    re.I)


def domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def source_rank(url: str) -> int:
    """Lower sorts first. 0 = known-good, 1 = neutral, 2 = looks like filler."""
    host = domain_of(url)
    if not host:
        return 2
    if host in REPUTABLE_DOMAINS or any(host.endswith(d) for d in REPUTABLE_DOMAINS):
        return 0
    if host.endswith(REPUTABLE_SUFFIXES):
        return 0
    if _FARM_HINTS.search(host):
        return 2
    return 1


# ===== What kind of page this is =====
#
# Asked for "recent us politics news", six of six results were section fronts:
# nytimes.com/section/politics, nbcnews.com/politics, apnews.com. The model
# read one and summarised the navigation — "The New York Times (covering US,
# World News)" — because that is genuinely what is on a front page. Add the
# month to the same query and four of six come back as dated articles.
#
# A front is not a bad source, it is the wrong kind of page to answer from, and
# nothing in the result told anyone which was which.

# /2026/aug/05/slug, /2026-07-14, /2026/07/14/, and Reuters' trailing
# -2026-07-14, which is why the year may be preceded by a hyphen and not only
# a slash.
_DATE_IN_PATH = re.compile(
    r"[/-](20\d{2})[/-]"
    r"(0?[1-9]|1[0-2]|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"(?:[a-z]*)[/-](0?[1-9]|[12]\d|3[01])(?=[/-]|$)",
    re.I,
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def date_from_url(url: str) -> str:
    """The publication date a news URL carries, as ISO, or ``""``.

    Not a substitute for reading the page, but it is free, it is right far more
    often than not, and it is the difference between a card that says "5 Aug
    2026" and one that says nothing.
    """
    match = _DATE_IN_PATH.search(url or "")
    if not match:
        return ""
    year, raw_month, day = match.groups()
    if raw_month.isdigit():
        month = int(raw_month)
    else:
        month = _MONTHS.get(raw_month.lower()[:3], 0)
    if not 1 <= month <= 12:
        return ""
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def page_kind(url: str) -> str:
    """``"article"`` or ``"front"``.

    A front is a homepage or a section index: little or no path, no date, and
    no slug. An article has somewhere to be.
    """
    try:
        path = urlparse(url or "").path.strip("/")
    except Exception:
        return "front"
    if not path:
        return "front"                      # bare domain
    if date_from_url(url):
        return "article"                    # dated URLs are articles
    segments = [s for s in path.split("/") if s]
    last = segments[-1].lower()
    # A slug: several words, or long enough that it is a headline not a label.
    slugish = last.count("-") >= 2 or last.count("_") >= 2 or len(last) > 24
    if slugish:
        return "article"
    # /news/, /politics/, /world/asia/china — labels, however many levels deep.
    return "front"


def site_name(url: str) -> str:
    """A domain as a person would write it: bbc.com -> BBC, theguardian.com -> The Guardian."""
    host = domain_of(url)
    if not host:
        return ""
    known = {
        "bbc.co.uk": "BBC", "bbc.com": "BBC", "apnews.com": "AP News",
        "abcnews.go.com": "ABC News", "whitehouse.gov": "The White House",
        "nypost.com": "New York Post", "thehill.com": "The Hill",
        "cnbc.com": "CNBC", "usatoday.com": "USA Today", "pbs.org": "PBS",
        "cbsnews.com": "CBS News", "foxnews.com": "Fox News",
        "latimes.com": "LA Times", "time.com": "TIME", "vox.com": "Vox",
        "theatlantic.com": "The Atlantic", "foreignpolicy.com": "Foreign Policy",
        "reason.com": "Reason", "semafor.com": "Semafor", "thedailybeast.com": "The Daily Beast",
        "theguardian.com": "The Guardian", "nytimes.com": "The New York Times",
        "washingtonpost.com": "The Washington Post", "reuters.com": "Reuters",
        "wsj.com": "The Wall Street Journal", "ft.com": "Financial Times",
        "nbcnews.com": "NBC News",
        "cnn.com": "CNN", "npr.org": "NPR", "aljazeera.com": "Al Jazeera",
        "politico.com": "POLITICO", "axios.com": "Axios",
        "bloomberg.com": "Bloomberg", "scmp.com": "SCMP",
        "newyorker.com": "The New Yorker", "economist.com": "The Economist",
    }
    if host in known:
        return known[host]
    label = host.rsplit(".", 2)[0] if host.count(".") > 1 else host.split(".")[0]
    return label.replace("-", " ").title()


def rank_results(results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Sort by source quality, then by having something to say.

    Source quality stays the primary key — an article on a content farm should
    not outrank the front page of Reuters. Within a tier an article beats an
    index, because the index cannot answer the question and the model reads
    from the top.
    """
    def key(pair):
        index, item = pair
        return (source_rank(item["url"]),
                0 if page_kind(item["url"]) == "article" else 1,
                index)

    return [item for _, item in sorted(enumerate(results), key=key)]


# ===== Fetch =====

def _is_textual(content_type: str) -> bool:
    return any(kind in content_type.lower() for kind in TEXTUAL_TYPES)


def _extract(html: str, base_url: str) -> Dict[str, Any]:
    """HTML to readable text plus the page title and its outbound links."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"title": "", "text": re.sub(r"<[^>]+>", " ", html), "links": []}

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""

    # Hidden text is the classic injection carrier: white-on-white, zero-size,
    # or display:none content that a reader never sees but a scraper does.
    for element in soup.select('[style*="display:none"], [style*="display: none"], [hidden]'):
        element.decompose()
    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    links = []
    for anchor in soup.find_all("a", href=True)[:200]:
        text = anchor.get_text(strip=True)
        if text:
            links.append({"text": text[:120], "url": urljoin(base_url, anchor["href"])})

    lines = [line.strip() for line in soup.get_text("\n", strip=True).split("\n") if line.strip()]
    return {"title": title, "text": "\n".join(lines), "links": links}


# A section front — nytimes.com/section/politics, politico.com/politics — is a
# list of links with almost no prose. Reading one and finding nothing to
# summarise is what makes a model read it again, and then read three more like
# it. Naming the shape lets the tool hand back the headlines instead.
INDEX_MIN_LINKS = 25
INDEX_MAX_WORDS_PER_LINK = 12


def looks_like_an_index(text: str, links: List[Dict[str, str]]) -> bool:
    """Is this a list of headlines rather than an article?"""
    if len(links) < INDEX_MIN_LINKS:
        return False
    words = len(text.split())
    return words < len(links) * INDEX_MAX_WORDS_PER_LINK


def _status_message(status: int, url: str) -> str:
    """Say who refused, and what to do about it.

    "HTTP 403" reads to a model like a transient failure worth retrying, and
    that is exactly what it did — the same URL twice in one turn.
    """
    host = urlparse(url).netloc or url
    if status in (401, 403):
        return (f"HTTP {status} — {host} blocks automated readers. Do not retry "
                f"this URL; use a different source for the same story.")
    if status == 429:
        return (f"HTTP 429 — {host} is rate limiting. Use a different source "
                f"rather than retrying.")
    if status == 451:
        return f"HTTP 451 — {host} is legally unavailable here. Use another source."
    if status == 404:
        return f"HTTP 404 — that page does not exist on {host}."
    return f"HTTP {status} from {host}."


def fetch(url: str, max_chars: int = DEFAULT_MAX_CHARS, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Fetch one URL and return readable text, or an explained failure.

    The return always has the same shape, so callers never branch on whether an
    exception escaped: ``{url, final_url, title, text, links, error, screening,
    tainted}``.
    """
    result: Dict[str, Any] = {
        "url": url, "final_url": url, "title": "", "text": "", "links": [],
        "error": "", "screening": {"tainted": False, "signals": []}, "tainted": False,
        "truncated": False,
    }

    current = url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False,
                          headers=dict(BROWSER_HEADERS)) as client:
            for _ in range(MAX_REDIRECTS + 1):
                check = policy.check_url(current)
                if check.denied:
                    result["error"] = check.reason
                    return result

                response = client.get(current)
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        result["error"] = "redirect without a destination"
                        return result
                    current = urljoin(current, location)
                    continue
                break
            else:
                result["error"] = f"more than {MAX_REDIRECTS} redirects"
                return result

            result["final_url"] = current
            if response.status_code >= 400:
                result["error"] = _status_message(response.status_code, current)
                result["blocked"] = response.status_code in (401, 403, 429, 451)
                return result

            content_type = response.headers.get("content-type", "")
            if not _is_textual(content_type):
                result["error"] = f"not a readable document ({content_type or 'unknown type'})"
                return result

            body = response.content[:MAX_BYTES]
            html = body.decode(response.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as exc:
        result["error"] = f"request failed: {exc}"
        return result
    except Exception as exc:
        result["error"] = f"could not read the page: {exc}"
        return result

    extracted = _extract(html, current)
    text = extracted["text"]
    result["truncated"] = len(text) > max_chars
    text = text[:max_chars]

    screening = policy.screen_untrusted(text, origin=current)
    result.update({
        "title": extracted["title"],
        "text": policy.sanitize_untrusted(text),
        "links": extracted["links"],
        "screening": screening,
        "tainted": screening["tainted"],
    })
    return result


def fetch_many(urls: List[str], max_chars: int = DEFAULT_MAX_CHARS) -> List[Dict[str, Any]]:
    """Fetch several pages, keeping failures in place so callers see the gap."""
    return [fetch(url, max_chars=max_chars) for url in urls]


def host_label(url: str) -> str:
    """A short, human-readable name for a source: the bare domain."""
    host = (urlparse(url or "").hostname or url or "").lower()
    return host[4:] if host.startswith("www.") else host


# ===== Backend health and fallback =====
#
# A broken search client does not error — it returns confident nonsense
# ("dynamic programming" answered with soup recipes and Target). Checking
# once, up front, turns five minutes of grinding into one clear sentence.

_CANARY_QUERY = "wikipedia dynamic programming algorithm"
_CANARY_TERMS = {"dynamic", "programming", "algorithm", "wikipedia"}
_health_cache: dict = {}
_HEALTH_TTL_SECONDS = 300


def search_backend_healthy(force: bool = False) -> bool:
    """Is web search returning results that relate to the query at all?

    Cached briefly so a research run with many sub-questions pays for one
    probe, not one per agent.
    """
    import time
    now = time.monotonic()
    cached = _health_cache.get("value")
    if cached is not None and not force and now - _health_cache.get("at", 0) < _HEALTH_TTL_SECONDS:
        return cached
    try:
        raw = _raw_search(_CANARY_QUERY, 5, "wt-wt")
    except Exception as exc:
        LOG.warning("search backend probe failed: %s", exc)
        _health_cache.update(value=False, at=now)
        return False
    hits = 0
    for item in raw or []:
        blob = f"{item.get('title', '')} {item.get('body') or item.get('description') or ''}"
        if len(_CANARY_TERMS & _terms(blob)) >= 2:
            hits += 1
    healthy = hits >= 1
    if not healthy:
        LOG.warning("search backend returned %d results, none on-topic for the probe", len(raw or []))
    _health_cache.update(value=healthy, at=now)
    return healthy


def search_wikipedia(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Wikipedia's own search API — a second opinion that needs no key.

    Not a general web search, but for factual and technical questions it
    is often better than the fourth blog result, and it keeps research
    usable when the general backend is down.
    """
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "srlimit": max_results, "format": "json"},
            headers={"User-Agent": "Carrot/1.0 (local assistant)"},
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("search", [])
    except Exception as exc:
        LOG.info("wikipedia search failed for %r: %s", query[:60], exc)
        return []
    results = []
    for page in pages:
        title = page.get("title", "")
        snippet = re.sub(r"<[^>]+>", "", page.get("snippet", ""))
        results.append({
            "title": title,
            "url": "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
            "snippet": snippet,
        })
    return results


def search_all(query: str, max_results: int = 6, region: str = "wt-wt") -> List[Dict[str, str]]:
    """General web search, topped up from Wikipedia when it comes back thin."""
    results = search(query, max_results=max_results, region=region)
    if len(results) >= 2:
        return results
    seen = {r["url"] for r in results}
    for extra in search_wikipedia(query, max_results=max_results):
        if extra["url"] not in seen:
            results.append(extra)
    return results


# Words that mark a link as furniture rather than a story. A section front
# carries a hundred of these around a dozen real headlines.
NAV_WORDS = {
    "subscribe", "sign in", "log in", "menu", "search", "newsletters",
    "advertisement", "privacy", "terms", "contact", "about", "careers",
    "cookies", "accessibility", "home", "sections", "more", "share",
    "facebook", "twitter", "instagram", "youtube", "tiktok", "rss", "skip",
}
HEADLINE_MIN_WORDS = 4
MAX_HEADLINES = 20


def headline_links(links: List[Dict[str, str]], base_url: str) -> List[Dict[str, str]]:
    """The links on a section front that are plausibly stories.

    A headline is several words long and points somewhere deeper on the same
    site. Nav links are one or two words and point at the top level, which is
    what makes this separable without understanding the page.
    """
    host = urlparse(base_url).netloc
    picked, seen = [], set()
    for link in links:
        text = (link.get("text") or "").strip()
        url = link.get("url") or ""
        if not text or not url or url in seen:
            continue
        if text.lower() in NAV_WORDS or len(text.split()) < HEADLINE_MIN_WORDS:
            continue
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != host:
            continue
        # A story lives below the section, not beside it.
        if parsed.path.count("/") < 2:
            continue
        seen.add(url)
        picked.append({"text": text, "url": url})
        if len(picked) >= MAX_HEADLINES:
            break
    return picked
