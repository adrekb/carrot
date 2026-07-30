---
kind: external_dependency
name: DuckDuckGo Web Search Integration
slug: duckduckgo-search
category: external_dependency
category_hints:
    - vendor_identity
    - client_constraint
scope:
    - '**'
source_files:
    - carrot/recap.py
---

### Identity & Role
DuckDuckGo search API provides web search capabilities for Carrot's daily recap feature without requiring API keys.

### Integration Points
- `carrot/recap.py` uses `duckduckgo_search.DDGS` class for text-based queries.
- Default query focuses on tech breakthroughs, AI, programming, and science news.
- Results limited to 5 items maximum per search call.

### Usage Model
- Searches executed within context manager for proper resource cleanup.
- Results formatted as structured data with title, URL, and summary fields.
- Fallback to RSS feeds when web search is unavailable.
- No authentication required - completely free tier access.

### Constraints
- Rate limiting may apply based on DuckDuckGo's policies.
- Results quality depends on search query formulation.
- Network connectivity required for web search functionality.