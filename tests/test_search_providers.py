"""Exa first, DuckDuckGo underneath, and neither one able to take search down.

Exa's public endpoint needs no key and counts against the caller's IP. That is
a good fit for an app that runs on the user's own machine — every install has
its own allowance rather than sharing one server-side quota — and it is also
why the fall-through is the design rather than a safety net: a per-IP daily cap
is a limit a busy machine *will* reach, and the only sensible behaviour then is
to keep working with something uncapped.

Nothing here touches the network. The endpoint was exercised by hand while this
was written — it answers unauthenticated, returns text/event-stream, and hands
back prose rather than JSON — and these pin the parsing and the ordering that
came out of that, which is the part that breaks silently.
"""
import pytest

from carrot import websearch


# One result exactly as the live endpoint formats it, including the trailing
# ellipsis line Exa puts in its highlights.
EXA_BLOCK = """Title: Introducing the 2026 Corvette ZR1X
URL: https://news.gm.com/2026-corvette-zr1x.html
Published: 2025-06-17T00:00:00.000Z
Author: N/A
Highlights:
Chevrolet is introducing an all-wheel drive Corvette worthy of the storied ZR1
designation: the 2026 Corvette ZR1X.
...

---

Title: A Conceptual Overview of asyncio
URL: https://docs.python.org/3/howto/a-conceptual-overview-of-asyncio.html
Published: 2024-01-01T00:00:00.000Z
Author: Python
Highlights:
Coroutines, event loops, and efficient I/O-bound task management.
"""


class TestReadingWhatExaSends:
    def test_a_block_becomes_the_shape_the_rest_of_the_module_expects(self):
        results = websearch._parse_exa_block(EXA_BLOCK)
        assert [r["href"] for r in results] == [
            "https://news.gm.com/2026-corvette-zr1x.html",
            "https://docs.python.org/3/howto/a-conceptual-overview-of-asyncio.html",
        ]
        assert results[0]["title"] == "Introducing the 2026 Corvette ZR1X"
        assert "all-wheel drive" in results[0]["body"]
        assert results[0]["published"].startswith("2025-06-17")

    def test_highlights_run_over_several_lines(self):
        """They are the passages Exa matched on, not a one-line summary, and
        truncating at the first newline would throw most of it away."""
        body = websearch._parse_exa_block(EXA_BLOCK)[0]["body"]
        assert "designation" in body, "only the first line of the highlight survived"

    def test_a_result_with_no_url_is_not_a_result(self):
        assert websearch._parse_exa_block("Title: nowhere\nHighlights:\nwords") == []

    def test_junk_does_not_raise(self):
        """A format that moves should cost the results, not the process."""
        for text in ("", "not remotely the format", "---\n---", "URL:\n"):
            assert isinstance(websearch._parse_exa_block(text), list)


class TestTheOrderAndTheDowngrade:
    def _providers(self, monkeypatch, exa, ddg):
        calls = []

        def fake_exa(query, max_results):
            calls.append("exa")
            if isinstance(exa, Exception):
                raise exa
            return exa

        def fake_ddg(query, max_results, region):
            calls.append("ddg")
            if isinstance(ddg, Exception):
                raise ddg
            return ddg

        monkeypatch.setattr(websearch, "_exa_search", fake_exa)
        monkeypatch.setattr(websearch, "_ddg_search", fake_ddg)
        return calls

    HIT = [{"title": "t", "href": "https://example.com/a", "body": "b"}]

    def test_exa_is_asked_first_and_ddg_is_not_asked_at_all(self, monkeypatch):
        monkeypatch.setattr(websearch, "search_provider", lambda: "auto")
        calls = self._providers(monkeypatch, self.HIT, self.HIT)
        websearch._raw_search("q", 3, "wt-wt")
        assert calls == ["exa"]

    def test_a_capped_exa_downgrades_rather_than_failing(self, monkeypatch):
        """What running out of the daily allowance looks like from here."""
        monkeypatch.setattr(websearch, "search_provider", lambda: "auto")
        calls = self._providers(
            monkeypatch, RuntimeError("429 Too Many Requests"), self.HIT)
        assert websearch._raw_search("q", 3, "wt-wt") == self.HIT
        assert calls == ["exa", "ddg"]

    def test_an_empty_answer_also_downgrades(self, monkeypatch):
        """Out of quota and nothing found look identical from here, and both
        want the other provider tried."""
        monkeypatch.setattr(websearch, "search_provider", lambda: "auto")
        calls = self._providers(monkeypatch, [], self.HIT)
        assert websearch._raw_search("q", 3, "wt-wt") == self.HIT
        assert calls == ["exa", "ddg"]

    def test_the_setting_chooses_which_goes_first(self, monkeypatch):
        monkeypatch.setattr(websearch, "search_provider", lambda: "duckduckgo")
        calls = self._providers(monkeypatch, self.HIT, self.HIT)
        websearch._raw_search("q", 3, "wt-wt")
        assert calls == ["ddg"]

    def test_choosing_one_does_not_switch_the_other_off(self, monkeypatch):
        """"My search stopped working today" is a worse outcome than "some of
        these came from the other engine"."""
        monkeypatch.setattr(websearch, "search_provider", lambda: "duckduckgo")
        calls = self._providers(monkeypatch, self.HIT, RuntimeError("down"))
        assert websearch._raw_search("q", 3, "wt-wt") == self.HIT
        assert calls == ["ddg", "exa"]

    def test_both_down_is_an_empty_result_not_an_exception(self, monkeypatch):
        """A research run that loses one query should narrow its scope, not
        collapse — the contract `search` already had."""
        monkeypatch.setattr(websearch, "search_provider", lambda: "auto")
        self._providers(monkeypatch, RuntimeError("a"), RuntimeError("b"))
        assert websearch.search("q") == []

    def test_the_failure_names_both_providers(self, monkeypatch):
        monkeypatch.setattr(websearch, "search_provider", lambda: "auto")
        self._providers(monkeypatch, RuntimeError("capped"), RuntimeError("offline"))
        with pytest.raises(RuntimeError) as exc:
            websearch._raw_search("q", 3, "wt-wt")
        assert "capped" in str(exc.value) and "offline" in str(exc.value)


class TestTheKeyIsNotLeaked:
    def test_an_exa_key_is_a_secret(self):
        """It is a credential, so the read-only config endpoint must return a
        boolean for it rather than the key itself."""
        from carrot import config

        assert "exa_api_key" in config.SECRET_KEYS
        redacted = config.redact({"exa_api_key": "exa-secret-value"})
        assert redacted["exa_api_key"] is True
        assert "exa-secret-value" not in str(redacted)


class TestAPdfIsADocument:
    """`read_url` answered "not a readable document (application/pdf)".

    Reported while researching the F-35: Lockheed's own fact sheet is a PDF,
    and so is a great deal of the best material on the open web — filings,
    papers, datasheets, standards. Turning all of it away meant the model read
    whoever had written *about* the primary source instead of the source.

    Nothing new was needed to read them. `attachments.extract_pdf_text` has
    always done this for a PDF the user drags in; the fetcher simply refused
    the content type before anything got a chance to look.
    """

    def test_the_content_type_is_recognised(self):
        assert websearch._is_pdf("application/pdf", "https://x.test/a")
        assert websearch._is_pdf("application/pdf; charset=binary", "https://x.test/a")

    def test_a_silent_server_is_read_from_the_address(self):
        """Plenty of hosts serve a .pdf as octet-stream, or say nothing."""
        assert websearch._is_pdf("application/octet-stream", "https://x.test/facts.pdf")
        assert websearch._is_pdf("", "https://x.test/report.PDF")

    def test_a_web_page_is_still_a_web_page(self):
        assert not websearch._is_pdf("text/html", "https://x.test/article")
        assert not websearch._is_pdf("text/html", "https://x.test/about-pdf-files")

    def test_the_text_comes_back_as_a_readable_document(self, monkeypatch):
        monkeypatch.setattr(
            "carrot.attachments.extract_pdf_text",
            lambda blob, name="": "Program Status\n\n\n\n\nF-35: Air Dominance Delivered\n"
                                  + "x" * 400)
        out = websearch._read_pdf({}, b"%PDF-1.7 ...", "https://x.test/facts.pdf", 15000)
        assert out.get("error") in (None, "")
        assert out["kind"] == "pdf"
        assert "Program Status" in out["text"]
        # Page breaks arrive as runs of blank lines; the model should read prose.
        assert "\n\n\n" not in out["text"]

    def test_a_scan_says_so_rather_than_returning_nothing(self, monkeypatch):
        """An empty success tells the model nothing and it answers from
        snippets — the failure the read gates exist to catch, arriving through
        the one door they do not watch."""
        monkeypatch.setattr("carrot.attachments.extract_pdf_text",
                            lambda blob, name="": "   ")
        out = websearch._read_pdf({}, b"%PDF", "https://x.test/scan.pdf", 15000)
        assert "scan" in out["error"]
        assert not out.get("text")

    def test_a_broken_pdf_names_what_happened(self, monkeypatch):
        def boom(blob, name=""):
            raise ValueError("EOF marker not found")
        monkeypatch.setattr("carrot.attachments.extract_pdf_text", boom)
        out = websearch._read_pdf({}, b"not a pdf", "https://x.test/x.pdf", 15000)
        assert "EOF marker not found" in out["error"]
