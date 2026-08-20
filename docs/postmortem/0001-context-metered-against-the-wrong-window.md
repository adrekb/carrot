# 0001 — Context metered against the wrong window

**Reported as:** an answer to "c8 zr1X specs" that opened with a numbered
"Thinking Process", restated its sources as notes, listed a set of constraints
nobody had sent it, wrote three drafts and a closing "Let's go." — and only then
gave the answer, as fifteen rows of `2026 Chevrolet Corvette ZR1X Torque: 973
lb-ft [url]`. Followed by: *"its quite capable idk why its doing this"*.

## Cause

The model was fine. Probed directly on a short prompt it returned 663 characters
of reasoning on Ollama's separate `thinking` channel and 31 characters of clean
answer on `content`; replayed with tool results in the conversation it produced
a correctly cited answer that spontaneously flagged a discrepancy between two
sources.

What differed on the real turn was size: five searches, six pages read, and
`MAX_READ_CHARS` is 20,000 per page.

`_window_tokens` reported `OllamaClient.context_limit` — the model's ceiling,
262,144 tokens. Ollama was running the request at `context_length`, the ceiling
clamped by `DEFAULT_NUM_CTX`, which is 32,768 so that a 256k KV cache does not
turn a laptop into a swap file.

So the meter, the pruner, `rounds_left` and the stop-and-answer gate all
measured against a number eight times larger than the one the request would be
allowed. Thirty thousand tokens of read pages is nowhere near 90% of 262,144, so
the pruner — which was correct, and had been written for exactly this — never
ran. Ollama truncated instead, and it truncates the **front** of the prompt,
where the system directive is.

The invented constraints list is the tell. The model was reconstructing
instructions that had been evicted before it ever saw them.

**Two defensible things, wrong together:** capping `num_ctx` to protect memory
is right, and reporting a model's true ceiling in the picker is right. Using the
second to budget the first is what broke.

## Fixed

`_window_tokens` reports `context_length` for local routes — the window the turn
runs in, not the one the model could hold. The ceiling is still what the model
picker shows, because that is describing a model rather than a turn.

Two things went in alongside it:

- Tool results are now trimmed to their head **plus the passages that mention
  the question**, because the head of a web page is its navigation. On a page
  shaped like the reported one the old trim kept 525 characters, all of it menu,
  and lost the horsepower figure; the new one keeps 435 and the figure survives.
- The server's own `prompt_eval_count` is read off the final frame. A prompt at
  98% of the window was truncated to fit and now says so, and the ratio between
  what was measured and what was estimated calibrates the four-characters-to-a-
  token estimator for the rest of the turn.

## Held by

`tests/test_prompt_budget.py`:

- `test_not_the_ceiling_the_model_could_hold` — the exact mismatch.
- `test_the_fact_survives_the_trim` and
  `test_the_head_alone_would_have_kept_the_navigation` — the pair, so the
  difference is not theoretical.
- `test_the_estimate_matches_what_the_trim_will_do` — `prunable_tokens` decides
  between pruning and giving up, so it must not promise room the trim will not
  deliver.

`tests/test_context_overflow.py` holds the measured-truncation half.
