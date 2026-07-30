"""Deep-research morning briefing agent.

A native analyzer -> researcher -> synthesizer state machine (LangGraph-style,
zero extra dependencies) that personalizes the daily recap:

  1. analyze    — mine recent conversations, goals, and reminders for topics
  2. research   — run live web searches per topic and read the top pages
  3. synthesize — stream a markdown briefing with source links

Every step is yielded as an event so the UI can show the agent working, and
an overnight scheduler can run the whole pipeline autonomously.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

import httpx
from duckduckgo_search import DDGS

from carrot.database import get_db
from carrot.ollama_client import OllamaClient, ThinkTagStreamFilter
from carrot.config import get_config, set_config
from carrot import recap as recap_mod

BRIEFINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "briefings")

DEFAULT_INTENTS = [
    "latest AI and open source model releases",
    "programming languages and developer tooling news",
    "computer science research breakthroughs this week",
]

MAX_INTENTS = 5
RESULTS_PER_TOPIC = 4
PAGES_PER_TOPIC = 2
PAGE_CHARS = 3000

DEEP_RESEARCH_SYSTEM = (
    "You are Carrot's deep research assistant. You compile precise, well-sourced "
    "morning briefings for a CS student. Never use emojis. Never mention the tools "
    "or APIs you used."
)


# ===== analyzer =====

def _recent_activity(days: int = 7, max_msgs: int = 40) -> dict:
    """Collect recent user messages, goals, and open reminders from SQLite."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT m.content FROM messages m "
        "JOIN conversations c ON m.conversation_id = c.id "
        "WHERE m.timestamp >= ? AND m.role = 'user' AND c.metadata NOT LIKE '%recap%' "
        "ORDER BY m.timestamp DESC LIMIT ?",
        (cutoff, max_msgs),
    ).fetchall()
    goals = conn.execute("SELECT title FROM goals LIMIT 10").fetchall()
    reminders = conn.execute("SELECT title FROM reminders LIMIT 10").fetchall()
    conn.close()
    return {
        "messages": [r["content"][:200] for r in rows],
        "goals": [r["title"] for r in goals],
        "reminders": [r["title"] for r in reminders],
    }


def derive_intents(client: OllamaClient, model: str, activity: dict) -> list:
    """Ask the model to turn the user's recent activity into search intents."""
    has_signal = any(activity.get(k) for k in ("messages", "goals", "reminders"))
    if not has_signal:
        return list(DEFAULT_INTENTS)

    context_lines = []
    for msg in activity["messages"][:25]:
        context_lines.append(f"- chat: {msg}")
    for g in activity["goals"]:
        context_lines.append(f"- goal: {g}")
    for r in activity["reminders"]:
        context_lines.append(f"- reminder: {r}")

    schema = {
        "type": "object",
        "properties": {
            "intents": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["intents"],
    }
    prompt = (
        "Based on this user's recent activity, produce 3-5 distinct web search "
        "queries covering the topics they currently care about. Each query should "
        "be specific enough to return fresh, relevant news or updates. Always "
        "include at least one general tech/AI news query.\n\n"
        "Recent activity:\n" + "\n".join(context_lines) +
        '\n\nReturn JSON: {"intents": ["query 1", "query 2", ...]}'
    )
    try:
        raw = client.structured_chat(
            [
                {"role": "system", "content": DEEP_RESEARCH_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            model=model,
            response_format=schema,
        )
        intents = json.loads(raw).get("intents", [])
        intents = [i.strip() for i in intents if isinstance(i, str) and i.strip()]
        if intents:
            return intents[:MAX_INTENTS]
    except Exception:
        pass
    return list(DEFAULT_INTENTS)


# ===== researcher =====

def search_topic(topic: str, max_results: int = RESULTS_PER_TOPIC) -> list:
    """DuckDuckGo search for one topic; returns [{title, url, snippet}]."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(topic, max_results=max_results))
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in results
        ]
    except Exception:
        return []


def fetch_page_text(url: str, max_chars: int = PAGE_CHARS) -> str:
    """Fetch and strip a web page down to readable text (sync)."""
    try:
        resp = httpx.get(url, timeout=12.0, follow_redirects=True)
        resp.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        lines = [ln.strip() for ln in soup.get_text(separator="\n", strip=True).split("\n") if ln.strip()]
        return "\n".join(lines)[:max_chars]
    except Exception:
        return ""


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else url


# ===== synthesizer =====

def _build_briefing_prompt(topics: list, articles: list) -> str:
    """topics: [{topic, sources: [{title, url, snippet, content}]}]"""
    sections = []
    for t in topics:
        lines = [f"## Research topic: {t['topic']}"]
        for s in t["sources"]:
            lines.append(f"- {s['title']} ({s['url']})\n  {s.get('snippet', '')[:200]}")
            if s.get("content"):
                lines.append(f"  Page excerpt: {s['content'][:800]}")
        sections.append("\n".join(lines))

    rss_lines = [
        f"- [{a['source']}] {a['title']}\n  {a.get('summary', '')[:200]}"
        for a in articles[:10]
    ]

    return (
        "Write today's personalized morning briefing from the research below.\n\n"
        "Structure:\n"
        "1. '## TL;DR' — 2-3 sentences.\n"
        "2. One '## <topic>' section per research topic with 2-4 bullet points. "
        "Cite sources inline as markdown links: [site name](url).\n"
        "3. '## Headlines' — 3 quick bullets from the RSS feed items.\n\n"
        "Rules: markdown only, no emojis, cite a link for every claim, keep it tight.\n\n"
        "=== Web research ===\n" + "\n\n".join(sections) +
        "\n\n=== RSS feed items ===\n" + "\n".join(rss_lines)
    )


def briefing_path(day: datetime = None) -> str:
    day = day or datetime.now()
    return os.path.join(BRIEFINGS_DIR, f"{day.strftime('%Y-%m-%d')}.md")


def save_briefing(markdown: str) -> str:
    os.makedirs(BRIEFINGS_DIR, exist_ok=True)
    path = briefing_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    return path


def get_today_briefing():
    path = briefing_path()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return {"date": datetime.now().strftime("%Y-%m-%d"), "markdown": f.read()}


# ===== the pipeline =====

def run_deep_research_stream(model: str = None, include_web_search: bool = True):
    """Full deep-research pipeline as a generator of trace events.

    Yields dicts:
      {"stage": str, "detail": str}          — pipeline progress lines
      {"intents": [str]}                     — derived research topics
      {"search": {topic, title, url}}        — each search hit as it is found
      {"thinking": str} / {"token": str}     — model reasoning / summary stream
      {"done": True, "summary": str}         — final briefing
      {"error": str}                         — fatal error
    """
    config = get_config()
    client = OllamaClient()
    if not client.is_available():
        yield {"error": "Ollama not available"}
        return
    model = model or config.get("ollama_model_recap") or config.get("ollama_model")

    # --- 1. analyze ---
    yield {"stage": "analyze", "detail": "reading your recent conversations, goals, and reminders"}
    activity = _recent_activity()
    n_signals = len(activity["messages"]) + len(activity["goals"]) + len(activity["reminders"])
    if n_signals:
        yield {"stage": "analyze", "detail": f"found {n_signals} signals — deriving research topics"}
    else:
        yield {"stage": "analyze", "detail": "no recent activity — using default tech topics"}
    intents = derive_intents(client, model, activity) if include_web_search else []
    if intents:
        yield {"intents": intents}
        for intent in intents:
            yield {"stage": "analyze", "detail": f"topic: {intent}"}

    # --- 2. research ---
    topics = []
    if include_web_search:
        for intent in intents:
            yield {"stage": "research", "detail": f"searching: {intent}"}
            results = search_topic(intent)
            if not results:
                yield {"stage": "research", "detail": "no results — skipping topic"}
                continue
            for r in results:
                yield {"search": {"topic": intent, "title": r["title"], "url": r["url"]}}
            sources = []
            for r in results[:PAGES_PER_TOPIC]:
                yield {"stage": "research", "detail": f"reading {_host(r['url'])}"}
                content = fetch_page_text(r["url"])
                sources.append({**r, "content": content})
            sources.extend(results[PAGES_PER_TOPIC:])
            topics.append({"topic": intent, "sources": sources})

    feed_urls = config.get("recap_rss_feeds", recap_mod.RSS_SOURCES_DEFAULT)
    yield {"stage": "feeds", "detail": f"fetching {len(feed_urls)} RSS feeds"}
    articles = []
    for url in feed_urls:
        items = recap_mod.fetch_feed(url)
        articles.extend(items)
        yield {"stage": "feeds", "detail": f"{_host(url)} — {len(items)} items"}
    articles = articles[: config.get("recap_max_items", 10)]

    if not topics and not articles:
        yield {"error": "no research material found (web and RSS both unavailable)"}
        return

    # --- 3. synthesize ---
    total_sources = sum(len(t["sources"]) for t in topics)
    yield {
        "stage": "summarize",
        "detail": f"synthesizing {total_sources} sources + {len(articles)} headlines with {model or client.default_model}",
    }
    prompt = _build_briefing_prompt(topics, articles)
    tag_filter = ThinkTagStreamFilter()
    full = []
    header = f"# Morning Briefing — {datetime.now().strftime('%A, %B %d, %Y')}\n\n"
    yield {"token": header}
    try:
        for chunk in client.generate(prompt, model=model, system=DEEP_RESEARCH_SYSTEM, stream=True):
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

    summary = header + "".join(full).strip()
    save_briefing(summary)
    recap_mod.save_recap_to_db(
        {"success": True, "summary": summary, "articles_count": len(articles),
         "timestamp": datetime.now().isoformat()},
        articles,
    )
    yield {"done": True, "summary": summary}


def run_deep_research(model: str = None, include_web_search: bool = True) -> dict:
    """Blocking wrapper used by the overnight scheduler."""
    last = {}
    for ev in run_deep_research_stream(model=model, include_web_search=include_web_search):
        last = ev
    if last.get("done"):
        return {"success": True, "summary": last.get("summary", "")}
    return {"success": False, "error": last.get("error", "unknown failure")}


# ===== overnight scheduler =====

_scheduler_started = False


def is_due(now: datetime, target_hhmm: str, last_run_date: str) -> bool:
    """True when the daily run time has passed and today's run hasn't happened."""
    if last_run_date == now.strftime("%Y-%m-%d"):
        return False
    try:
        hh, mm = (int(p) for p in target_hhmm.split(":"))
    except (ValueError, AttributeError):
        hh, mm = 4, 0
    return (now.hour, now.minute) >= (hh, mm)


def check_overnight_run(now: datetime = None) -> bool:
    """Run the pipeline if the overnight schedule says it is due."""
    config = get_config()
    if not config.get("recap_auto_enabled", False):
        return False
    now = now or datetime.now()
    if not is_due(now, config.get("recap_auto_time", "04:00"), config.get("recap_auto_last_run", "")):
        return False
    set_config("recap_auto_last_run", now.strftime("%Y-%m-%d"))
    run_deep_research()
    return True


def _scheduler_loop():
    while True:
        try:
            check_overnight_run()
        except Exception:
            pass
        time.sleep(60)


def start_scheduler():
    """Start the overnight-recap daemon thread (idempotent)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True, name="carrot-overnight-recap").start()
