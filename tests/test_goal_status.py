"""Reading goals back: status, progress, and the ledger in Write.

Chat could propose a goal and could not read one. So the app collected the
answer to "what have I committed to" and had no way to say it, which is the
same shape as the original bug — the knowing was there and the getting-at-it
was not.
"""
from carrot import commitments, goals


class TestStatusIsAQuestionNotAScreen:
    """It only works if one function answers it, or chat and an editor asking
    over MCP disagree about what is overdue."""

    def make(self):
        for title, subject, deadline in (
            ("Finish the thesis", "thesis", "2027-03-12"),
            ("Submit the grant", "grant", "2026-01-31"),
            ("Ship v2", "v2", "2026-08"),
        ):
            made = goals.propose(title=title, subject=subject, deadline=deadline)
            goals.decide(made["id"], accepted=True)

    def test_a_past_date_is_overdue(self, isolated_db):
        self.make()
        report = goals.status_report()
        assert [g["title"] for g in report["overdue"]] == ["Submit the grant"]

    def test_a_month_deadline_is_not_late_on_the_first(self):
        """"By March" is a promise about March. Calling it late on 1 March is
        the pedantry that makes a tracker feel like an adversary."""
        assert not goals._is_overdue("2026-08", "2026-08-01")
        assert goals._is_overdue("2026-08", "2026-09-01")

    def test_a_goal_with_no_deadline_has_nothing_to_be_late_for(self, isolated_db):
        made = goals.propose(title="Rewrite the pipeline", subject="pipeline",
                             target="before the audit")
        goals.decide(made["id"], accepted=True)
        assert goals.status_report()["overdue"] == []

    def test_undated_goals_sort_after_dated_ones(self, isolated_db):
        """Rather than being given an invented date to sort by."""
        self.make()
        made = goals.propose(title="Rewrite the pipeline", subject="pipeline",
                             target="before the audit")
        goals.decide(made["id"], accepted=True)
        titles = [g["title"] for g in goals.status_report()["open"]]
        assert titles[-1] == "Rewrite the pipeline"

    def test_chat_can_read_them_back(self, isolated_db):
        from carrot import agent_tools

        self.make()
        assert "list_goals" in agent_tools.TOOLS
        out = agent_tools.TOOLS["list_goals"]["handler"]()
        assert "Finish the thesis" in out and "Past their date" in out

    def test_an_editor_asking_over_mcp_gets_the_same_sentences(self, isolated_db):
        from carrot import agent_tools, mcp_server

        self.make()
        assert (mcp_server.TOOLS["list_goals"]["handler"]()
                == agent_tools.TOOLS["list_goals"]["handler"]())

    def test_nothing_tracked_says_how_one_starts(self, isolated_db):
        assert "date or a target" in goals.render_status()


class TestProgressIsCheap:
    """A tracker that makes you confirm every step is one you stop telling
    things to."""

    def open_goal(self):
        made = goals.propose(title="Finish the thesis", subject="thesis",
                             deadline="2027-03-12")
        goals.decide(made["id"], accepted=True)
        return goals.get_goal(made["id"])

    def test_a_step_lands_on_the_goal_without_asking(self, isolated_db):
        goal = self.open_goal()
        updated = commitments.note_progress_from_turn(
            "finished chapter 3 of the thesis, moving to 4")
        assert updated["id"] == goal["id"]
        assert updated["metadata"]["progress"][-1]["note"].startswith("finished chapter 3")

    def test_it_does_not_create_a_second_goal(self, isolated_db):
        """Two rows for one commitment is how a tracker starts lying about how
        much you have on."""
        self.open_goal()
        commitments.note_progress_from_turn("finished chapter 3 of the thesis")
        assert len(goals.by_status(goals.STATUS_ACCEPTED)) == 1

    def test_progress_about_nothing_tracked_is_ignored(self, isolated_db):
        self.open_goal()
        assert commitments.note_progress_from_turn("done with the washing up") is None

    def test_the_subject_has_to_actually_appear(self, isolated_db):
        """Otherwise the first open goal collects every "done!" in the
        conversation."""
        self.open_goal()
        assert commitments.note_progress_from_turn("finished!") is None

    def test_the_longest_matching_subject_wins(self, isolated_db):
        for title, subject in (("Finish the thesis", "thesis"),
                               ("Finish thesis chapter 4", "thesis chapter")):
            made = goals.propose(title=title, subject=subject, deadline="2027-03-12")
            goals.decide(made["id"], accepted=True)
        updated = commitments.note_progress_from_turn("finished the thesis chapter draft")
        assert updated["title"] == "Finish thesis chapter 4"

    def test_a_progress_sentence_does_not_also_propose_a_goal(self):
        """"Finished chapter 3 of the thesis" carries committing language, so
        without precedence one sentence updates a goal and offers a second."""
        from pathlib import Path

        app_src = (Path(__file__).resolve().parents[1] / "carrot" / "app.py"
                   ).read_text(encoding="utf-8")
        block = app_src[app_src.index("goal_chips_enabled"):]
        assert block.index("note_progress_from_turn") < block.index("propose_from_turn")
        assert "if not progressed:" in block[:1200]

    def test_a_finished_goal_takes_no_more_notes(self, isolated_db):
        goal = self.open_goal()
        goals.mark_done(goal["id"])
        assert goals.note_progress(goal["id"], "more") is None


class TestTheGoalsDocument:
    """Write holds a document called Goals. It is the table, rendered — not a
    markdown file that gets parsed back, which is the design that eventually
    loses data: the moment somebody edits the prose there are two sources of
    truth and a regex between them, and the regex always loses."""

    def test_it_is_in_the_list_without_anybody_creating_it(self, isolated_db):
        from carrot import systemdocs

        assert "Goals" in [d["title"] for d in systemdocs.listing()]

    def test_it_is_pinned_and_read_only(self, isolated_db):
        from carrot import systemdocs

        doc = systemdocs.get(systemdocs.GOALS_ID)
        assert doc["pinned"] and doc["readonly"] and doc["system"]

    def test_it_is_generated_from_the_rows_every_time(self, isolated_db):
        from carrot import systemdocs

        assert "Nothing tracked yet" in systemdocs.get(systemdocs.GOALS_ID)["body"]
        made = goals.propose(title="Finish the thesis", subject="thesis",
                             deadline="2027-03-12")
        goals.decide(made["id"], accepted=True)
        body = systemdocs.get(systemdocs.GOALS_ID)["body"]
        assert "Finish the thesis" in body and "Nothing tracked yet" not in body

    def test_proposed_and_accepted_are_kept_apart(self, isolated_db):
        """A list mixing "you agreed to this" with "Carrot wondered about
        this" is not a record of what you promised."""
        from carrot import systemdocs

        goals.propose(title="Rewrite the pipeline", subject="pipeline",
                      target="before the audit")
        assert "Waiting on you" in systemdocs.get(systemdocs.GOALS_ID)["body"]

    def test_progress_shows_under_its_goal(self, isolated_db):
        from carrot import systemdocs

        made = goals.propose(title="Finish the thesis", subject="thesis",
                             deadline="2027-03-12")
        goals.decide(made["id"], accepted=True)
        goals.note_progress(made["id"], "chapter 3 done")
        assert "chapter 3 done" in systemdocs.get(systemdocs.GOALS_ID)["body"]

    def test_writing_to_it_is_refused_rather_than_ignored(self, client):
        """A save that appears to work and changes nothing is how somebody
        loses an afternoon's edits."""
        from carrot import systemdocs

        resp = client.put(f"/api/notes/{systemdocs.GOALS_ID}",
                          json={"content": "# my own goals"})
        assert resp.status_code == 409
        assert "ticking a chip" in resp.json()["detail"]

    def test_deleting_it_is_refused(self, client):
        from carrot import systemdocs

        assert client.delete(f"/api/notes/{systemdocs.GOALS_ID}").status_code == 409

    def test_it_appears_at_the_top_of_the_write_list(self, client):
        from carrot import systemdocs

        rows = client.get("/api/notes").json()
        assert rows and rows[0]["id"] == systemdocs.GOALS_ID

    def test_opening_it_gives_what_the_list_promised(self, client):
        from carrot import systemdocs

        listed = client.get("/api/notes").json()[0]
        opened = client.get(f"/api/notes/{systemdocs.GOALS_ID}").json()
        assert opened["title"] == listed["title"] == "Goals"
