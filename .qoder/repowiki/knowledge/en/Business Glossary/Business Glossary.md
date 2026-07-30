---
kind: business_term
name: Business Glossary
category: business_term
scope:
    - '**'
---

### Daily Recap
- Definition：A morning briefing feature that aggregates technology and science news from RSS feeds and web search, then synthesizes them into a concise summary using local AI models. Stored as special conversations with metadata type 'recap' in the database.
- Aliases：recap、morning briefing、daily summary

### Hybrid Search
- Definition：Search methodology combining exact phrase matching through SQLite FTS5 full-text search with conceptual meaning matching via vector embeddings from the nomic-embed-text model. Enables both precise keyword searches and semantic understanding of queries.
- Aliases：hybrid search、FTS5 + embeddings、semantic search

### Query Classification
- Definition：Process where Ollama analyzes user queries to extract structured metadata including search keywords, time cutoffs, intent categories (recall, search, code, reminder, goal, general), and named entities. Used to optimize search performance and routing.
- Aliases：query classifier、intent extraction、search metadata

### Voice Profiles
- Definition：Pre-configured voice settings in Kokoro TTS that combine specific voice types (af_heart, bf_qlwn, af_blswy) with speed and volume parameters. Includes themed profiles like 'us_rabbit', 'us_calm', and 'us_energetic' for consistent personality across responses.
- Aliases：voice styles、TTS profiles、audio personalities

### Global Shortcut
- Definition：System-wide keyboard shortcut (Alt+Space) that activates a compact overlay window over any active application, allowing users to speak or type commands without switching contexts. Part of the desktop app's productivity features.
- Aliases：global hotkey、overlay shortcut、system-wide shortcut

### Multi-pane Dashboard
- Definition：Unified desktop interface layout featuring multiple simultaneous view panels including chat, editor, notes, goals, reminders, and terminal. Designed as the primary workspace replacing traditional sidebar navigation for better multitasking.
- Aliases：dashboard layout、multi-panel UI、workspace
