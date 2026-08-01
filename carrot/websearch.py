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

import re
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

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

def search(query: str, max_results: int = 6, region: str = "wt-wt") -> List[Dict[str, str]]:
    """Free web search via DuckDuckGo. Returns [{title, url, snippet}].

    Failures return an empty list rather than raising: a research run that
    loses one query should narrow its scope, not collapse.
    """
    if not (query or "").strip():
        return []
    try:
        from duckduckgo_search import DDGS

        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results, region=region))
    except Exception:
        return []

    results = []
    for item in raw:
        url = item.get("href") or item.get("url") or ""
        if not url:
            continue
        results.append({
            "title": (item.get("title") or "").strip(),
            "url": url,
            "snippet": (item.get("body") or "").strip(),
        })
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
