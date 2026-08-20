# 0002 — Every citation was a dead link

**Reported as:** *"nice chips didn't render"*, with a screenshot of an answer
whose sources were raw URLs wrapping across three lines each.

The rendering was the visible half. The other half was worse.

## Cause

The model wrote its sources as `[https://carbuzz.com/…/2026/]` — a bare URL in
square brackets, which is not markdown link syntax. Two things followed.

**GFM autolinking took the closing bracket into the URL.** Every `href` ended
`%5D`, so every citation 404'd. They exist to be checked; one that cannot be
opened is worse than none.

**And none of them became chips.** `markCitations` skipped a link whose text was
a URL, on the reasoning that a URL is not a name — and `CITE_MAX_CHARS` is 34,
so the length check would have dropped it anyway, a real URL being eighty. An
app with a chip built for exactly this rendered its sources as raw addresses.

## Fixed

The bracket comes back out of the href, but only when there is an unclosed `[`
immediately before the link — an address that genuinely ends in `%5D` keeps it.

A bare URL becomes a chip named by its domain. A URL is not a name but it
contains one; the rest is on the hover and in the href, where it can still be
checked. A URL alone on its own line is still a sources list and still left
alone.

## Held by

`tests/test_ui_regressions.py::TestABareUrlCitationIsStillACitation` — in
particular `test_the_swallowed_bracket_is_taken_back_out_of_the_href` and
`test_it_only_fires_when_the_bracket_was_punctuation`.
