# 0004 — The tinted chip kept the solid fill's ink

**Reported as:** one word — *"illegible"* — with a screenshot of the history
popover, the selected Agent filter unreadable.

## Cause

A cascade gap rather than a bad colour.

```css
.history-chip.active            { background: var(--accent-fill); color: var(--on-accent); }
.history-chip.chip-agent.active { border-color: var(--green); background: color-mix(in srgb, var(--green) 14%, transparent); }
```

`--on-accent` is a near-black chosen to sit on solid orange. The second rule is
more specific, replaced the fill with a 14% green tint, and said nothing about
the ink — so the near-black stayed behind on it at **1.64:1**. The same bug on
the Code chip, in yellow.

Whatever overrides the background has to override the foreground with it. They
are one decision that was written as two.

## Fixed

The label is `--text`, not the status colour.

Making it green is the obvious repair and reads well in three of the four cases.
The light theme's yellow on its own 14% tint measures **4.35:1** — under the
floor at 11px, on the *lightest* surface it can sit on and worse on the others.
The ring and the dots already say which filter is on; the label only has to be
readable. Measured across both themes and every surface a chip can land on:
worst case **9.44:1**.

## Held by

`tests/test_theme.py::TestTheTintedHistoryChipsAreReadable`.

Worth recording: the first version of that test asserted that the rule contained
`color:` and **passed against the very CSS it was written to catch** —
`border-color:` ends in `color:`, and every one of these rules has one. It was
caught by reverting the fix and watching the test not fail. A test for a
regression is not finished until it has been seen to fail.
