# Postmortems

Failures that were reported, diagnosed, and fixed — one file each, numbered in
the order they were written up.

**This is an index, not the record.** The record lives where the failure lived:
in the comment above the line that was wrong, and in the docstring of the test
that now holds it. That placement is deliberate and is not going to change —
somebody editing `markCitations` should not have to know a postmortem exists to
find out why bare URLs are handled the way they are.

What an index adds is findability for the person who is *not* reading that file:
someone deciding whether a symptom has been seen before, or reading the project
for the first time and wanting to know what kind of thing goes wrong here.

Borrowed from [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness),
which keeps `docs/postmortem/` with numbered entries. Their idea, our contents.

## What earns an entry

A failure that reached a user and whose cause was not where the symptom was.
Both halves matter. A typo caught by the test suite is not a postmortem; neither
is a bug whose fix is in the same function as its symptom, because the comment
there is already the whole story.

The ones below all share a shape: **two things were each individually defensible
and wrong together.**

## The entries

| # | Title | The one-line version |
|---|-------|----------------------|
| [0001](0001-context-metered-against-the-wrong-window.md) | Context metered against the wrong window | The meter read the model's ceiling while the request ran in a window eight times smaller, so nothing ever pruned and the system directive was silently evicted. |
| [0002](0002-every-citation-was-a-dead-link.md) | Every citation was a dead link | A URL written in square brackets is not a markdown link; GFM autolinking pulled the closing bracket into the href, and every source 404'd. |
| [0003](0003-a-search-for-an-apostrophe-was-a-500.md) | A search for an apostrophe was a 500 | The user's words went straight into `MATCH ?`, which parses them as a query language. `don't` was a server error. |
| [0004](0004-the-tinted-chip-kept-the-solid-fills-ink.md) | The tinted chip kept the solid fill's ink | A more specific rule replaced the background and inherited the foreground: near-black text on a dark green wash, 1.64:1. |

## Writing one

Name the symptom as it was reported, in the reporter's words where you have
them. Then the cause. Then what was changed, and — the part that stops it
happening again — which test now fails if it does.

Keep them short. If it needs more than a page, the interesting part is probably
a design note and belongs in the module docstring instead.
