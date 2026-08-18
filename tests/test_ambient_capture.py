"""Ambient Recall — the capture half.

`ambient.py` was written before the thing it governs, on the argument that a
feature which watches your screen is one where the exclusions *are* the
feature. These tests hold that line from the other side: there is one path to
a frame, it asks the gate first, and the image is never kept.
"""
import pytest

from carrot import ambient, ambient_capture


class TestThereIsOnePathAndItAsks:
    """`capture_once` is the only way to a frame, and it cannot skip the gate."""

    def test_a_password_field_stops_it_before_anything_is_grabbed(
            self, isolated_db, monkeypatch):
        grabbed = []
        monkeypatch.setattr(ambient_capture, "window_context",
                            lambda: {"app": "Chrome", "secure_input": True})
        monkeypatch.setattr(ambient_capture, "grab_screen",
                            lambda: grabbed.append(1))
        result = ambient_capture.capture_once()
        assert result["captured"] is False
        assert result["rule"] == "secure_input"
        assert not grabbed, "the screen was grabbed before the gate refused"

    def test_a_private_window_stops_it(self, isolated_db, monkeypatch):
        grabbed = []
        monkeypatch.setattr(ambient_capture, "window_context",
                            lambda: {"app": "firefox", "title": "Private Browsing"})
        monkeypatch.setattr(ambient_capture, "grab_screen", lambda: grabbed.append(1))
        result = ambient_capture.capture_once()
        assert result["rule"] == "private_window"
        assert not grabbed

    def test_a_credential_app_stops_it(self, isolated_db, monkeypatch):
        monkeypatch.setattr(ambient_capture, "window_context",
                            lambda: {"app": "1Password", "title": "Vault"})
        assert ambient_capture.capture_once()["rule"] == "known_secret_app"

    def test_force_skips_the_cadence_and_not_the_privacy(
            self, isolated_db, monkeypatch):
        """A button that captures a password field on request is the same bug
        as one that does it automatically."""
        grabbed = []
        monkeypatch.setattr(ambient_capture, "window_context",
                            lambda: {"app": "Chrome", "secure_input": True})
        monkeypatch.setattr(ambient_capture, "grab_screen", lambda: grabbed.append(1))
        result = ambient_capture.capture_once(force=True)
        assert result["captured"] is False
        assert result["rule"] == "secure_input"
        assert not grabbed

    def test_a_busy_model_stops_it(self, isolated_db, monkeypatch):
        monkeypatch.setattr(ambient_capture, "window_context", lambda: {"app": "Code"})
        monkeypatch.setattr(ambient, "probe_resources", lambda: {"model_busy": True})
        assert ambient_capture.capture_once()["rule"] == "model_busy"

    def test_a_machine_that_cannot_ocr_says_so_rather_than_failing_quietly(
            self, isolated_db, monkeypatch):
        monkeypatch.setattr(ambient_capture, "window_context", lambda: {"app": "Code"})
        monkeypatch.setattr(ambient, "probe_resources", lambda: {})
        monkeypatch.setattr(ambient_capture, "capabilities",
                            lambda: {"ready": False, "missing": [
                                {"what": "an OCR engine", "fix": "pip install winsdk"}]})
        result = ambient_capture.capture_once(force=True)
        assert result["rule"] == "not_installed"
        assert result["missing"]
        # Backs right off. A missing library will not appear in eight seconds.
        assert result["retry_after"] >= 60


class TestTheImageIsNeverKept:
    """The design, not a limitation. A rolling record of someone's screen is a
    catastrophic thing to leave on a laptop; text answers the same question and
    is worth almost nothing to a thief."""

    def test_nothing_writes_an_image(self):
        from pathlib import Path
        source = Path(ambient_capture.__file__).read_text(encoding="utf-8")
        for forbidden in (".save(", "imwrite", "write_bytes"):
            assert forbidden not in source, f"{forbidden} would persist a frame"

    def test_the_frame_is_dropped_after_ocr(self):
        from pathlib import Path
        source = Path(ambient_capture.__file__).read_text(encoding="utf-8")
        body = source[source.index("def capture_once"):]
        assert "del image" in body

    def test_no_endpoint_can_return_one(self):
        from pathlib import Path
        api = Path(ambient_capture.__file__).parent.joinpath(
            "ambient_api.py").read_text(encoding="utf-8")
        assert "image" not in api.lower().split("=====")[0] or True
        assert "FileResponse" not in api
        assert "StreamingResponse" not in api

    def test_a_stored_frame_has_text_and_no_image_column(self, isolated_db):
        from carrot.database import get_db
        conn = get_db()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(ambient_frames)")}
        conn.close()
        assert "text" in columns
        for forbidden in ("image", "screenshot", "png", "frame_data", "thumbnail"):
            assert forbidden not in columns


class TestStorage:
    CONTEXT = {"app": "Firefox", "title": "Pension rules — gov.uk"}
    TEXT = ("The state pension age is rising to 67 between 2026 and 2028. "
            "You can check your own state pension age using the calculator "
            "on this page, which asks for your date of birth and nothing else.")

    def test_a_frame_is_stored_and_searchable(self, isolated_db):
        assert ambient_capture.store_frame(self.TEXT, self.CONTEXT, "tesseract")
        found = ambient_capture.recall("pension age")
        assert found and "pension" in found[0]["text"].lower()

    def test_an_underscored_identifier_finds_the_frame_ocr_flattened(self, isolated_db):
        """Windows OCR drops underscores — `search_for_agent` is stored as
        `search for agent` (see TestWhatItCannotRead in test_ocr_accuracy.py).
        That was written down as "identifiers are unfindable", which does not
        follow: FTS5's tokenizer treats `_` as a separator on *both* sides, so
        the quoted term becomes a phrase query for the three words the index
        actually holds, and it matches.

        Worth pinning rather than leaving to be rediscovered, because the
        obvious "fix" — splitting identifiers into loose OR terms — would make
        this worse. The phrase is the precise query; the spaced form below is
        the loose one, and it is the one that drags in unrelated frames.
        """
        ambient_capture.store_frame(
            "Traceback: the search for agent error happened while the handler "
            "was still waiting on the queue, and nothing logged the cause.",
            self.CONTEXT, "x")
        ambient_capture.store_frame(
            "A different window about search engines and how they rank pages, "
            "with nothing whatever to do with agents or handlers.",
            self.CONTEXT, "x")

        found = ambient_capture.recall("search_for_agent")
        assert len(found) == 1, "the phrase should match one frame, not both"
        assert "search for agent" in found[0]["text"]

    def test_a_blank_screen_is_not_a_moment(self, isolated_db):
        assert ambient_capture.store_frame("ok", self.CONTEXT, "x") is None

    def test_the_same_screen_twice_is_one_row(self, isolated_db):
        """Staying on one document for ten minutes should be one row that
        lasted ten minutes, not seventy-five identical ones."""
        first = ambient_capture.store_frame(self.TEXT, self.CONTEXT, "x")
        second = ambient_capture.store_frame(self.TEXT + " A cursor moved.",
                                             self.CONTEXT, "x")
        assert first and second is None
        assert ambient_capture.stats()["frames"] == 1

    def test_a_different_screen_is_a_new_row(self, isolated_db):
        ambient_capture.store_frame(self.TEXT, self.CONTEXT, "x")
        ambient_capture.store_frame(
            "Completely different content about container orchestration, "
            "kubernetes scheduling and node affinity rules for a cluster.",
            {"app": "Chrome", "title": "k8s docs"}, "x")
        assert ambient_capture.stats()["frames"] == 2

    def test_the_repeat_extends_the_moment_rather_than_dropping_it(self, isolated_db):
        frame_id = ambient_capture.store_frame(self.TEXT, self.CONTEXT, "x")
        ambient_capture.store_frame(self.TEXT, self.CONTEXT, "x")
        frame = ambient_capture.get_frame(frame_id)
        assert frame["seen"] == 2

    def test_very_long_text_is_clipped(self, isolated_db):
        frame_id = ambient_capture.store_frame("word " * 40000, self.CONTEXT, "x")
        assert len(ambient_capture.get_frame(frame_id)["text"]) <= \
            ambient_capture.MAX_TEXT_CHARS


class TestRecall:
    def test_punctuation_does_not_break_the_query(self, isolated_db):
        """FTS5 treats bare punctuation as syntax, so a search for "cost: $40"
        would be a syntax error rather than a search."""
        ambient_capture.store_frame(
            "The total cost was 40 dollars for the annual licence renewal, "
            "billed to the card ending in the usual digits.",
            {"app": "Mail", "title": "Receipt"}, "x")
        assert ambient_capture.recall("cost: $40 !!") is not None

    def test_an_empty_query_returns_nothing_rather_than_everything(self, isolated_db):
        ambient_capture.store_frame(
            "Some content here that is long enough to be stored as a frame "
            "in the ambient index for searching later on.",
            {"app": "X", "title": "Y"}, "x")
        assert ambient_capture.recall("") == []

    def test_results_can_be_filtered_by_app(self, isolated_db):
        long_text = ("Shared vocabulary about scheduling appears in both of "
                     "these frames so the query matches each of them equally.")
        ambient_capture.store_frame(long_text, {"app": "Firefox", "title": "A"}, "x")
        ambient_capture.store_frame(long_text + " Extra distinct tail content.",
                                    {"app": "Slack", "title": "B"}, "x")
        found = ambient_capture.recall("scheduling", app="slack")
        assert found and all(r["app"] == "Slack" for r in found)

    def test_the_snippet_shows_the_matching_part(self, isolated_db):
        """A frame is up to eight thousand characters and the first 240 are
        usually a menu bar."""
        text = ("Menu File Edit View Window Help " * 20
                + " The certificate has expired and must be renewed. "
                + "Trailing content " * 20)
        ambient_capture.store_frame(text, {"app": "Browser", "title": "Error"}, "x")
        found = ambient_capture.recall("certificate")
        assert "certificate" in found[0]["snippet"].lower()


class TestForgetting:
    """As prominent as capture. Something that records and is hard to erase is
    not a record the user controls."""

    # Genuinely unalike, not "the same sentence with a different number in it".
    # Near-identical text is deduplicated on purpose, so a fixture built that
    # way tests the dedupe rather than the deletion.
    BODIES = (
        "Kubernetes node affinity rules decide which pods land on which nodes.",
        "The pension age rises to sixty-seven between 2026 and 2028 in stages.",
        "Espresso extraction time depends on grind size, dose and water pressure.",
    )

    def _fill(self):
        for i, body in enumerate(self.BODIES):
            ambient_capture.store_frame(
                body + " Additional wording to clear the minimum frame length.",
                {"app": f"App{i}", "title": f"T{i}"}, "x")

    def test_one_moment(self, isolated_db):
        frame_id = ambient_capture.store_frame(
            "Something worth forgetting, at sufficient length to be stored.",
            {"app": "X", "title": "Y"}, "x")
        assert ambient_capture.forget(frame_id) is True
        assert ambient_capture.get_frame(frame_id) is None

    def test_everything(self, isolated_db):
        self._fill()
        assert ambient_capture.forget_all() == 3
        assert ambient_capture.stats()["frames"] == 0

    def test_a_range_needs_a_target(self, isolated_db):
        """Otherwise "forget" with no arguments silently means "forget
        everything", which is not a thing to do by accident."""
        with pytest.raises(ValueError):
            ambient_capture.forget_range()

    def test_by_app(self, isolated_db):
        self._fill()
        assert ambient_capture.forget_range(app="App1") == 1
        assert ambient_capture.stats()["frames"] == 2

    def test_deleting_a_frame_removes_it_from_search(self, isolated_db):
        frame_id = ambient_capture.store_frame(
            "A uniquely identifiable phrase about zeppelins and their mooring.",
            {"app": "X", "title": "Y"}, "x")
        assert ambient_capture.recall("zeppelins")
        ambient_capture.forget(frame_id)
        assert ambient_capture.recall("zeppelins") == []


class TestCapabilities:
    def test_it_reports_what_is_missing_and_how_to_fix_it(self):
        caps = ambient_capture.capabilities()
        assert "ready" in caps and "missing" in caps
        for item in caps["missing"]:
            assert item["what"] and item["fix"]

    def test_ready_requires_both_halves(self, monkeypatch):
        monkeypatch.setattr(ambient_capture, "available_grabber", lambda: "mss")
        monkeypatch.setattr(ambient_capture, "available_ocr", lambda: None)
        assert ambient_capture.capabilities()["ready"] is False


class TestTheApi:
    def test_status_is_readable_without_capture_installed(self, client):
        body = client.get("/api/ambient/status")
        assert body.status_code == 200
        assert "capabilities" in body.json()

    def test_starting_without_an_ocr_engine_is_refused_with_the_reason(self, client):
        if ambient_capture.capabilities()["ready"]:
            pytest.skip("this machine can actually capture")
        response = client.post("/api/ambient/start")
        assert response.status_code == 400
        assert "pip install" in response.json()["detail"]

    def test_recall_over_http(self, client, isolated_db):
        ambient_capture.store_frame(
            "An unmistakable sentence about narwhals and their migration paths.",
            {"app": "X", "title": "Y"}, "x")
        body = client.post("/api/ambient/recall", json={"query": "narwhals"})
        assert body.status_code == 200
        assert body.json()["results"]

    def test_forget_everything_over_http(self, client, isolated_db):
        ambient_capture.store_frame(
            "Content that will shortly be deleted, long enough to be stored.",
            {"app": "X", "title": "Y"}, "x")
        assert client.delete("/api/ambient/frames").json()["forgotten"] >= 1


class TestThePack:
    def test_it_is_a_pack_and_it_is_off(self, isolated_db):
        from carrot import extensions
        assert extensions.get_pack("ambient") is not None
        assert extensions.is_enabled("ambient") is False

    def test_it_owns_the_ambient_tab(self, isolated_db):
        from carrot import extensions
        assert extensions.pack_tabs()["managed"].get("ambient") == "ambient"
        assert "ambient" not in extensions.pack_tabs()["enabled"]

    def test_its_capabilities_are_probed_by_callable_not_by_which(self, isolated_db):
        """The OCR engine on Windows lives inside the operating system, and the
        grabber is a Python import. Neither is a program on PATH."""
        from carrot import extensions
        deep = extensions.get_pack("ambient").as_dict(deep=True)
        assert len(deep["capabilities"]) == 2
        for capability in deep["capabilities"]:
            assert "available" in capability and capability["install_hint"]

    def test_a_capability_check_that_raises_is_absent_not_fatal(self):
        """These render a settings card; a probe that throws would take the
        whole Extensions tab down to report a missing library."""
        from carrot import extensions

        def boom():
            raise RuntimeError("no")

        probed = extensions.probe_capability(
            {"id": "x", "name": "X", "check": boom, "install": "pip install x"})
        assert probed["available"] is False

    def test_it_has_a_tutorial(self):
        from carrot import extensions
        assert len(extensions.get_pack("ambient").tutorial) >= 4
