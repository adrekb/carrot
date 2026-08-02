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
from urllib.parse import urljoin, urlparse

LOG = logging.getLogger(__name__)

import httpx

from . import policy

USER_AGENT = "Mozilla/5.0 (compatible; Carrot/1.0; local assistant)"
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
    dropped = 0
    for item in raw:
        url = item.get("href") or item.get("url") or ""
        if not url:
            continue
        title = (item.get("title") or "").strip()
        snippet = (item.get("body") or item.get("description") or "").strip()
        if not _is_relevant(query, title, snippet):
            dropped += 1
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
    if dropped:
        LOG.info("dropped %d off-topic result(s) for %r", dropped, query[:80])
    return results


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
                          headers={"User-Agent": USER_AGENT}) as client:
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
                result["error"] = f"HTTP {response.status_code}"
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
