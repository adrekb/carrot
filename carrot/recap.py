import json
import os
import feedparser
from datetime import datetime, date

import httpx
from duckduckgo_search import DDGS

from carrot.database import get_db
from carrot.ollama_client import OllamaClient
from carrot.config import get_config

RSS_SOURCES_DEFAULT = [
    "https://hnrss.org/newest",
    "https://www.reddit.com/r/programming/.rss",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://www.theverge.com/rss/index.xml",
]

DUCKDUCKGO_QUERY = "latest tech breakthroughs AI programming science news 2026"
WEB_SEARCH_MAX_RESULTS = 5


def fetch_feed(url: str):
    try:
        feed = feedparser.parse(url)
        entries = []
        for entry in feed.entries[:10]:
            entries.append(
                {
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source": feed.feed.get("title", ""),
                }
            )
        return entries
    except Exception:
        return []


def fetch_all_feeds(feed_urls: list = None):
    config = get_config()
    if feed_urls is None:
        feed_urls = config.get("recap_rss_feeds", RSS_SOURCES_DEFAULT)
    all_items = []
    for url in feed_urls:
        items = fetch_feed(url)
        all_items.extend(items)
    return all_items


def fetch_live_tech_news(query: str = DUCKDUCKGO_QUERY, max_results: int = WEB_SEARCH_MAX_RESULTS) -> str:
    search_context = []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            for i, res in enumerate(results, 1):
                search_context.append(
                    f"[{i}] Title: {res['title']}\nURL: {res['href']}\nSnippet: {res['body']}\n"
                )
        return "\n".join(search_context)
    except Exception as e:
        return f"Failed to fetch live web data: {str(e)}"


def fetch_live_tech_articles(max_results: int = WEB_SEARCH_MAX_RESULTS) -> list:
    results_list = []
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(DUCKDUCKGO_QUERY, max_results=max_results))
            for res in results:
                results_list.append(
                    {
                        "title": res.get("title", ""),
                        "url": res.get("href", ""),
                        "summary": res.get("body", ""),
                        "source": "web_search",
                    }
                )
    except Exception:
        pass
    return results_list


async def fetch_article_content(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            content = soup.get_text(separator="\n", strip=True)
            lines = [line.strip() for line in content.split("\n") if line.strip()]
            return "\n".join(lines[:2000])
    except Exception:
        return ""


def _build_recap_prompt(articles: list, web_context: str = None) -> str:
    max_items = min(len(articles), 10)
    rss_context = "\n".join(
        f"- [{a['source']}] {a['title']}\n  {a['summary'][:300]}"
        for a in articles[:max_items]
    )

    web_section = ""
    if web_context:
        web_section = f"\n--- Live Web Search Results ---\n\n{web_context}\n"

    return f"""You are Carrot's Daily Recap assistant. Synthesize the provided news into a clean morning briefing for a CS student.

Structure your response with:
1. A TL;DR summary (2-3 sentences max)
2. Key stories (3-5 bullet points, each with the source)
3. One "Did you know?" fact if any stands out

Use Markdown headers for organization. Do not use emojis. Do not mention that you used an API or search tool.

RSS Articles:
{rss_context}
{web_section}"""


RECAP_SYSTEM = "You are Carrot, a helpful AI assistant that provides concise daily news summaries for a CS student."


def summarize_news(articles: list, model: str = None, web_context: str = None):
    client = OllamaClient()
    if not client.is_available():
        return {"success": False, "error": "Ollama not available"}

    prompt = _build_recap_prompt(articles, web_context)

    summary = client.generate(prompt, model=model, system=RECAP_SYSTEM)
    return {
        "success": True,
        "summary": summary,
        "articles_count": len(articles),
        "web_results_count": len(fetch_live_tech_articles()),
        "timestamp": datetime.now().isoformat(),
    }


def run_recap(model: str = None, include_web_search: bool = True):
    config = get_config()
    articles = fetch_all_feeds(config.get("recap_rss_feeds"))
    max_items = config.get("recap_max_items", 10)
    articles = articles[:max_items]

    web_context = None
    if include_web_search:
        web_context = fetch_live_tech_news()

    result = summarize_news(articles, model or config.get("ollama_model_recap"), web_context=web_context)
    if result.get("success"):
        save_recap_to_db(result, articles)
    return result


def run_recap_stream(model: str = None, include_web_search: bool = True):
    """Run the recap pipeline as a generator of progress/trace events.

    Yields dicts:
      {"stage": str, "detail": str}   — pipeline progress lines
      {"thinking": str}               — model reasoning tokens (if any)
      {"token": str}                  — summary tokens as they stream
      {"done": True, "summary": str}  — final result
      {"error": str}                  — fatal error
    """
    from carrot.ollama_client import ThinkTagStreamFilter

    config = get_config()
    client = OllamaClient()
    if not client.is_available():
        yield {"error": "Ollama not available"}
        return

    feed_urls = config.get("recap_rss_feeds", RSS_SOURCES_DEFAULT)
    yield {"stage": "feeds", "detail": f"fetching {len(feed_urls)} RSS feeds"}
    articles = []
    for url in feed_urls:
        items = fetch_feed(url)
        articles.extend(items)
        host = url.split("/")[2] if "://" in url else url
        yield {"stage": "feeds", "detail": f"{host} — {len(items)} items"}

    max_items = config.get("recap_max_items", 10)
    articles = articles[:max_items]

    web_context = None
    if include_web_search:
        yield {"stage": "web", "detail": "searching the web for tech news"}
        web_context = fetch_live_tech_news()
        ok = web_context and not web_context.startswith("Failed")
        yield {"stage": "web", "detail": "web search complete" if ok else "web search unavailable, using RSS only"}

    model = model or config.get("ollama_model_recap") or config.get("ollama_model")
    yield {"stage": "summarize", "detail": f"summarizing {len(articles)} articles with {model or client.default_model}"}

    prompt = _build_recap_prompt(articles, web_context)
    tag_filter = ThinkTagStreamFilter()
    full = []
    try:
        for chunk in client.generate(prompt, model=model, system=RECAP_SYSTEM, stream=True):
            for ev in tag_filter.feed(chunk):
                if ev["type"] == "thinking":
                    yield {"thinking": ev["text"]}
                else:
                    full.append(ev["text"])
                    yield {"token": ev["text"]}
        for ev in tag_filter.flush():
            if ev["type"] == "thinking":
                yield {"thinking": ev["text"]}
            else:
                full.append(ev["text"])
                yield {"token": ev["text"]}
    except Exception as e:
        yield {"error": f"model failed: {e}"}
        return

    summary = "".join(full).strip()
    result = {
        "success": True,
        "summary": summary,
        "articles_count": len(articles),
        "timestamp": datetime.now().isoformat(),
    }
    save_recap_to_db(result, articles)
    yield {"done": True, "summary": summary}


def save_recap_to_db(result: dict, articles: list):
    conn = get_db()
    conv_id = f"recap_{datetime.now().strftime('%Y%m%d')}"
    existing = conn.execute(
        "SELECT id FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
            (conv_id, "Daily Recap", datetime.now().isoformat(), datetime.now().isoformat(), json.dumps({"type": "recap"})),
        )
    conn.execute(
        "INSERT OR REPLACE INTO messages (conversation_id, role, content, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
        (conv_id, "system", result.get("summary", ""), datetime.now().isoformat(), json.dumps({"source": "carrot_recap"})),
    )
    for article in articles[:5]:
        conn.execute(
            "INSERT OR IGNORE INTO messages (conversation_id, role, content, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
            (conv_id, "user", f"[{article['source']}] {article['title']}\n{article.get('summary', '')[:500]}", article.get("published") or datetime.now().isoformat(), json.dumps({"url": article.get("link", "")})),
        )
    conn.commit()
    conn.close()


def get_recaps(limit: int = 7):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM conversations WHERE metadata LIKE '%recap%' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_at": r["created_at"],
            "metadata": json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]