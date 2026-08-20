# 0003 — A search for an apostrophe was a 500

**Found by:** typing one query into the search page while verifying an unrelated
redesign. `routes (independent` returned Internal Server Error.

## Cause

The user's words went straight into `WHERE m.messages_fts MATCH ?`. FTS5 parses
that argument as a query language: parentheses group, a colon is a column
filter, a hyphen and an apostrophe are syntax. Five ordinary searches were
server errors:

```
routes (independent   fts5: syntax error near ""
don't                 fts5: syntax error near "'"
foo:bar               no such column: foo
C:/path/to/file       no such column: C
a-b                   no such column: b
```

The apostrophe one says how broken it was: a contraction in a search box,
answered with a stack trace.

## Fixed

Each word is wrapped in double quotes, which makes it a phrase literal — inside
quotes FTS5 treats everything but a quote as text. Words are joined by nothing,
which is FTS5's implicit AND and was already the behaviour for a plain
multi-word query.

The trade: a typed `OR` now searches for the word "or". Nothing in the UI ever
offered the query language, and a box that answers punctuation with a server
error is broken in a way that a box without boolean operators is not.

A `try/except sqlite3.OperationalError` sits around the MATCH as well. The
quoting is what makes a syntax error impossible; the catch is what stops a
future edit to the quoting from turning one back into a 500.

## Held by

`tests/test_search_results.py::TestAnOrdinarySearchCannotBeASyntaxError`, which
parametrises the five reported queries plus a run of quote characters, a bare
`*`, a leading caret and a trailing hyphen; and
`TestTheBraces::test_an_unparseable_match_is_an_empty_result`, which removes the
quoting to prove the catch works.
