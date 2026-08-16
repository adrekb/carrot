"""Links between documents, and the graph they make.

The rules worth pinning are the ones that are invisible until they are wrong:
a link inside a code fence is not a link, a title that two notes share resolves
the same way on every machine, and a link to something unwritten is a normal
thing to have rather than an error.
"""
from carrot import links, notes


class TestWhatCountsAsALink:
    def test_a_plain_wikilink(self):
        assert [l["target"] for l in links.extract_links("see [[Lecture 1]]")] == ["Lecture 1"]

    def test_an_alias_does_not_leak_into_the_target(self):
        """`[[Lecture 1|the first one]]` points at Lecture 1, not at the label."""
        found = links.extract_links("see [[Lecture 1|the first one]]")
        assert found[0]["target"] == "Lecture 1"
        assert found[0]["alias"] == "the first one"

    def test_code_is_not_a_link(self):
        """Otherwise documentation about this feature forges edges by
        describing itself."""
        assert links.extract_links("inline `[[Nope]]` here") == []
        assert links.extract_links("```\n[[Nope]]\n```") == []

    def test_an_unclosed_bracket_is_not_a_link(self):
        """Somebody mid-keystroke is not a link that spans a paragraph."""
        assert links.extract_links("[[half written\nnext line") == []

    def test_an_empty_target_is_not_a_link(self):
        assert links.extract_links("[[]] and [[   ]]") == []


class TestResolving:
    def test_case_and_spacing_do_not_matter(self, isolated_db):
        made = notes.create_note("Lecture 1", "")
        for spelling in ("lecture 1", "LECTURE 1", "  Lecture   1  "):
            assert links.resolve(spelling)["id"] == made["id"]

    def test_nothing_called_that(self, isolated_db):
        notes.create_note("Lecture 1", "")
        assert links.resolve("Lecture 2") is None

    def test_a_shared_title_resolves_the_same_way_every_time(self, isolated_db):
        """Two notes called the same thing is a situation to be deterministic
        about, not correct about — lowest id wins, so the same text resolves to
        the same note across reloads and across machines."""
        a = notes.create_note("Notes", "one")
        b = notes.create_note("Notes", "two")
        winner = min(a["id"], b["id"])
        assert links.resolve("Notes")["id"] == winner


class TestTheGraph:
    def test_a_resolved_link_is_an_edge(self, isolated_db):
        a = notes.create_note("Lecture 1", "see [[Philosophers]]")
        b = notes.create_note("Philosophers", "")
        graph = links.graph()
        assert {"source": a["id"], "target": b["id"], "resolved": True} in graph["edges"]

    def test_something_mentioned_but_unwritten_still_appears(self, isolated_db):
        """It is how you write — you mention the thing and make it later."""
        notes.create_note("Lecture 1", "see [[Descartes]]")
        ghosts = [n for n in links.graph()["nodes"] if not n["exists"]]
        assert [g["title"] for g in ghosts] == ["Descartes"]

    def test_a_note_linking_to_itself_is_not_an_edge(self, isolated_db):
        notes.create_note("Lecture 1", "see [[Lecture 1]]")
        assert links.graph()["edges"] == []

    def test_the_same_link_twice_is_one_edge(self, isolated_db):
        notes.create_note("Lecture 1", "[[Philosophers]] and again [[Philosophers]]")
        notes.create_note("Philosophers", "")
        assert len(links.graph()["edges"]) == 1

    def test_degree_counts_both_directions(self, isolated_db):
        """A note everything points at is as central as one pointing everywhere."""
        notes.create_note("Hub", "")
        notes.create_note("A", "[[Hub]]")
        notes.create_note("B", "[[Hub]]")
        hub = next(n for n in links.graph()["nodes"] if n["title"] == "Hub")
        assert hub["degree"] == 2

    def test_a_canvas_body_is_not_scanned_for_links(self, isolated_db):
        """Its body is JSON. Scanning it yields noise, not links."""
        notes.create_note("Board", '{"nodes": [{"title": "[[Lecture 1]]"}]}',
                          doc_format=notes.FORMAT_CANVAS)
        notes.create_note("Lecture 1", "")
        assert links.graph()["edges"] == []


class TestBacklinks:
    def test_it_reports_who_points_here_and_why(self, isolated_db):
        """The surrounding line is the point — a bare list of titles makes you
        open each one to remember why it points here."""
        notes.create_note("Lecture 1", "Kant follows [[Philosophers]] closely.")
        target = notes.create_note("Philosophers", "")
        back = links.backlinks(target["id"])
        assert [b["title"] for b in back] == ["Lecture 1"]
        assert "Kant follows" in back[0]["contexts"][0]

    def test_nothing_points_here(self, isolated_db):
        made = notes.create_note("Alone", "")
        assert links.backlinks(made["id"]) == []

    def test_the_loser_of_a_title_collision_claims_nothing(self, isolated_db):
        """Otherwise both notes called "Notes" report the same inbound links,
        and one of them is lying."""
        a = notes.create_note("Notes", "")
        b = notes.create_note("Notes", "")
        notes.create_note("Source", "see [[Notes]]")
        loser = max(a["id"], b["id"])
        assert links.backlinks(loser) == []


class TestSuggest:
    def test_titles_that_start_with_it_come_first(self, isolated_db):
        """The difference is obvious the moment the list is wrong."""
        notes.create_note("About Lectures", "")
        notes.create_note("Lecture 1", "")
        assert [s["title"] for s in links.suggest("lect")] == ["Lecture 1", "About Lectures"]

    def test_an_empty_query_offers_everything(self, isolated_db):
        notes.create_note("One", "")
        notes.create_note("Two", "")
        assert len(links.suggest("")) == 2
