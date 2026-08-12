"""How much a model can hold, for models that will not say.

Ollama answers this. `/api/show` returns `<arch>.context_length` and the local
client already reads it, which is why the local context setting can honestly
tell you that a model is running at its own limit rather than at the one you
picked.

No hosted provider does. Anthropic's `/v1/models` returns ids and display
names; OpenAI's returns ids and ownership. Neither returns a context length,
and a custom OpenAI-compatible endpoint — someone's own vLLM, a proxy, a
router — returns whatever its author felt like. So for everything that is not
Ollama the number has to come from somewhere else, and this module is that
somewhere.

It is deliberately three-tiered, because the honest answer differs:

* **probed** — the model was asked and answered. Local models only.
* **known** — matched against the table below. Right until a provider ships
  something new, which they do constantly.
* **set** — the user told us. Always wins, because they can read the model
  card and we cannot.
* **unknown** — no match and nothing configured. Reported as unknown rather
  than guessed at, because a wrong context window is worse than none: it is
  the number the UI would use to tell someone their conversation fits.

The table is patterns, not exact ids. `claude-opus-4-5-20251101` and
`claude-opus-4-5` and `anthropic/claude-opus-4-5` all have to resolve, and
pinning exact ids guarantees the table is stale the week it is written. What
does not change under a version suffix is the family.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config, set_config

# Where per-model overrides live. Keyed "provider/model", so the same model
# name served by two providers can be told apart.
OVERRIDE_KEY = "model_context_overrides"

# Nobody's context window is smaller than this, and a value below it is
# almost certainly a mistyped thousands separator.
MIN_WINDOW = 1024
# Above this the number is not a context window, it is a typo with zeros.
MAX_WINDOW = 10_000_000

# Ordered: first match wins, so longer and more specific patterns come first.
# The values are the *input* window, which is the one that decides whether a
# conversation fits — output limits are a separate number and not this one.
KNOWN: Tuple[Tuple[str, int], ...] = (
    # Embedding and reranking models, first — they are not chat models and
    # their windows are small, but their names carry a chat family's name
    # ("codestral-embed", "text-embedding-3") and would otherwise inherit that
    # family's figure. A 256k badge on an 8k embedder is not a rounding error,
    # it is a wrong answer about a model that cannot chat at all.
    (r"embed|rerank", 8_192),
    # Anthropic
    (r"claude.*opus-4", 200_000),
    (r"claude.*sonnet-4", 200_000),
    (r"claude.*haiku-4", 200_000),
    (r"claude.*opus", 200_000),
    (r"claude.*sonnet", 200_000),
    (r"claude.*haiku", 200_000),
    (r"claude", 200_000),
    # OpenAI
    (r"gpt-4o", 128_000),
    (r"gpt-4\.1", 1_047_576),
    (r"gpt-4-turbo", 128_000),
    (r"gpt-4", 8_192),
    (r"gpt-3\.5", 16_385),
    (r"^o[134](-|$)", 200_000),
    (r"gpt-5", 400_000),
    (r"gpt", 128_000),
    # Google
    (r"gemini.*2\.5.*pro", 1_048_576),
    (r"gemini.*2\.5", 1_048_576),
    (r"gemini.*1\.5.*pro", 2_097_152),
    (r"gemini.*1\.5", 1_048_576),
    (r"gemini", 1_048_576),
    # Open-weight families, for a hosted endpoint serving them
    (r"llama.*4", 10_485_760),
    (r"llama.*3\.[123]", 131_072),
    (r"llama", 8_192),
    (r"magistral", 128_000),
    (r"devstral", 128_000),
    (r"codestral-mamba", 256_000),
    (r"codestral", 256_000),
    (r"ministral", 128_000),
    (r"mistral-large", 131_072),
    (r"mistral-medium", 128_000),
    (r"mistral-small", 128_000),
    (r"mistral-nemo", 128_000),
    (r"pixtral", 128_000),
    (r"mixtral", 32_768),
    (r"mistral", 32_768),
    (r"grok", 131_072),
    (r"kimi", 131_072),
    (r"glm", 131_072),
    (r"qwen.*2\.5", 131_072),
    (r"qwen", 32_768),
    (r"deepseek", 65_536),
    (r"gemma.*4", 131_072),
    (r"gemma.*3", 131_072),
    (r"gemma", 8_192),
    (r"phi", 131_072),
    (r"command-r", 131_072),
)

_COMPILED: List[Tuple[Any, int]] = [(re.compile(p, re.I), n) for p, n in KNOWN]

SOURCE_PROBED = "probed"
SOURCE_KNOWN = "known"
SOURCE_SET = "set"
SOURCE_UNKNOWN = "unknown"

SOURCE_MEANING = {
    SOURCE_PROBED: "reported by the model itself",
    SOURCE_KNOWN: "from Carrot's table for this model family",
    SOURCE_SET: "you set this",
    SOURCE_UNKNOWN: "this provider does not report it and Carrot has no entry",
}


# ===== Overrides =====

def _overrides() -> Dict[str, int]:
    raw = get_config().get(OVERRIDE_KEY) or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if MIN_WINDOW <= number <= MAX_WINDOW:
            out[str(key)] = number
    return out


def key_for(provider: str, model: str) -> str:
    return f"{provider or 'ollama'}/{model or ''}"


def set_override(provider: str, model: str, tokens: Optional[int]) -> Dict[str, int]:
    """Record — or clear, with ``None`` — what a model can hold.

    Validated here rather than at the edge because there is more than one edge:
    the settings form, the model picker and a direct config write all land on
    this, and a 0 or a negative from any of them would be a context window the
    router divides by.
    """
    current = dict(_overrides())
    name = key_for(provider, model)
    if tokens is None:
        current.pop(name, None)
    else:
        number = int(tokens)
        if not MIN_WINDOW <= number <= MAX_WINDOW:
            raise ValueError(
                f"a context window has to be between {MIN_WINDOW:,} and "
                f"{MAX_WINDOW:,} tokens")
        current[name] = number
    set_config(OVERRIDE_KEY, current)
    return current


# ===== Lookup =====

def from_table(model: str) -> int:
    """The table's answer for this model name, or 0."""
    name = (model or "").strip()
    if not name:
        return 0
    # A provider-qualified name ("anthropic/claude-opus-4-5", "openai:gpt-4o")
    # has to match on the model part, not on the provider's name.
    tail = re.split(r"[/:]", name)[-1] if re.search(r"[/:]", name) else name
    for pattern, tokens in _COMPILED:
        if pattern.search(tail) or pattern.search(name):
            return tokens
    return 0


def window_for(provider: str, model: str, probed: int = 0) -> Dict[str, Any]:
    """What this model can hold, and how confident that is.

    ``probed`` is a value the caller already obtained from the model itself —
    Ollama's ``/api/show``. It beats the table but loses to an override, on
    the grounds that a user who has typed a number is looking at the model
    card and we are pattern-matching a string.
    """
    override = _overrides().get(key_for(provider, model))
    if override:
        return {"tokens": override, "source": SOURCE_SET,
                "why": SOURCE_MEANING[SOURCE_SET]}
    if probed and probed > 0:
        return {"tokens": int(probed), "source": SOURCE_PROBED,
                "why": SOURCE_MEANING[SOURCE_PROBED]}
    known = from_table(model)
    if known:
        return {"tokens": known, "source": SOURCE_KNOWN,
                "why": SOURCE_MEANING[SOURCE_KNOWN]}
    return {"tokens": 0, "source": SOURCE_UNKNOWN,
            "why": SOURCE_MEANING[SOURCE_UNKNOWN]}


# ===== What the turn spends before you type anything =====
#
# The number the Advanced box needs to be honest about. Setting a local model
# to 8k reads like "eight thousand tokens of conversation" and is nothing of
# the sort: the directive and the tool schemas are in the window before the
# first word of the question, and on a multi-turn search they are a third of
# an 8k window on their own.
#
# Measured, not estimated. The tools are serialised exactly as they go to the
# provider and the directive is the real string, so the figure moves when a
# tool is added — which is the point, since a fixed number in the copy would
# be wrong by the next release and nobody would notice.

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token count for English prose and JSON.

    Four characters per token is the standard approximation and is close
    enough for a disclaimer. A real tokeniser here would mean shipping one per
    model family to be more precise about a number whose job is to say
    "several thousand, not zero".
    """
    return max(0, len(text or "") // CHARS_PER_TOKEN)
