"""Chat proposes a goal; a tick keeps it.

Carrot's reason to exist is continuity — you say something once and it still
knows later. Goals were where that most obviously failed: a goal was something
you went to a tab and typed, so the only goals Carrot ever had were the ones
you thought to enter twice.

The hard part is not noticing commitments. It is not chipping every wish.
"""
from unittest.mock import patch

from carrot import commitments, goals


class TestTheBar:
    """A tracked goal nobody can check is the failure that makes people switch
    the feature off."""

    def test_a_real_commitment_with_a_date_gets_through_the_gate(self):
        assert commitments.looks_like_a_commitment(
            "I need to finish the thesis by March 12th")

    def test_a_wish_does_not(self):
        assert not commitments.looks_like_a_commitment(
            "I should really learn Portuguese one day")

    def test_a_vague_intention_does_not(self):
        assert not commitments.looks_like_a_commitment("I want to get fitter")

    def test_a_hedge_cancels_committing_language(self):
        """"I will maybe ship it" is not a commitment however much `will` is
        in there."""
        assert not commitments.looks_like_a_commitment(
            "Maybe I will rewrite the backend at some point")

    def test_a_question_is_not_a_commitment(self):
        assert not commitments.looks_like_a_commitment("what is the weather")

    def test_the_gate_is_cheap_and_runs_before_the_model(self):
        """Most turns are not commitments, and a second inference on every
        message is the difference between a chat that keeps up and one that
        does not."""
        from carrot import ollama_client

        with patch.object(ollama_client, "OllamaClient") as client:
            assert commitments.propose_from_turn("what is the weather") is None
            assert not client.called


class TestTheRuleThatDoesNotDrift:
    """A local 4B asked "is this a commitment?" says yes far more often than
    it should. The rule lives in code so it holds for models nobody tested."""

    def test_an_iso_date_is_checkable(self):
        assert commitments._is_checkable({"deadline": "2027-03-12"})

    def test_a_month_is_checkable(self):
        assert commitments._is_checkable({"deadline": "2027-03"})

    def test_a_date_nobody_can_compare_is_not(self):
        assert not commitments._is_checkable({"deadline": "March-ish"})

    def test_a_named_target_is_checkable(self):
        assert commitments._is_checkable({"target": "before the demo"})

    def test_nothing_checkable_is_not(self):
        assert not commitments._is_checkable({"deadline": "", "target": ""})

    def test_the_model_saying_yes_does_not_override_it(self, isolated_db):
        """The case the prompt is worst at: committing language, nothing to
        check, and a model that is agreeable."""
        payload = '{"is_commitment": true, "title": "Get fitter", "subject": "fitness"}'
        with _fake_model(payload):
            assert commitments.propose_from_turn("I will get fitter") is None
        assert goals.by_status(goals.STATUS_PROPOSED) == []


def _fake_model(payload):
    """Patched on `ollama_client`, not on `commitments`.

    `propose_from_turn` imports the client inside the function, so the name is
    resolved from its home module at call time — patching an attribute onto
    `commitments` creates something nothing reads, and the test then makes a
    real request to a real Ollama and takes twenty seconds to not prove
    anything."""
    from carrot import ollama_client

    class Client:
        def is_available(self):
            return True

        def structured_chat(self, *args, **kwargs):
            return payload

    return patch.object(ollama_client, "OllamaClient", Client)


class TestProposing:
    def test_a_commitment_becomes_a_proposal_not_a_goal(self, isolated_db):
        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-03-12"}')
        with _fake_model(payload):
            made = commitments.propose_from_turn(
                "I need to finish the thesis by March 12th 2027",
                conversation_id="c1", message_id="7")
        assert made["status"] == goals.STATUS_PROPOSED
        assert made["title"] == "Finish the thesis"
        assert made["deadline"] == "2027-03-12"

    def test_it_keeps_the_sentence_it_came_from(self, isolated_db):
        """A goal you cannot trace back to something you said is one you have
        to take Carrot's word for, and proposing rather than asserting only
        means something if you can check."""
        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-03-12"}')
        with _fake_model(payload):
            made = commitments.propose_from_turn(
                "I need to finish the thesis by March 12th 2027",
                conversation_id="c1", message_id="7")
        assert "thesis" in made["source_text"]
        assert made["conversation_id"] == "c1" and made["message_id"] == "7"

    def test_a_proposal_is_not_yet_a_goal_anybody_is_tracking(self, isolated_db):
        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-03-12"}')
        with _fake_model(payload):
            commitments.propose_from_turn("I will finish the thesis by 2027-03-12")
        assert goals.by_status(goals.STATUS_ACCEPTED) == []


class TestDeciding:
    def make(self, isolated_db):
        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-03-12"}')
        with _fake_model(payload):
            return commitments.propose_from_turn(
                "I need to finish the thesis by March 12th 2027", conversation_id="c1")

    def test_ticking_it_keeps_it(self, isolated_db):
        made = self.make(isolated_db)
        decided = goals.decide(made["id"], accepted=True)
        assert decided["status"] == goals.STATUS_ACCEPTED
        assert decided["decided_at"]

    def test_ticking_it_also_remembers_it(self, isolated_db):
        """A goal you agreed to is a fact about you, and belongs where the
        rest of them are — that is what makes it answerable in Cursor three
        months later rather than only in this tab."""
        from carrot import memory as memory_mod

        made = self.make(isolated_db)
        goals.decide(made["id"], accepted=True)
        found = [m for m in memory_mod.list_memories() if "thesis" in m["content"].lower()]
        assert found, "accepting a goal stored no memory"
        assert "2027-03-12" in found[0]["content"]

    def test_dismissing_it_stores_no_goal(self, isolated_db):
        made = self.make(isolated_db)
        goals.decide(made["id"], accepted=False)
        assert goals.by_status(goals.STATUS_ACCEPTED) == []

    def test_dismissing_it_stops_the_subject_being_raised_again(self, isolated_db):
        """A proposal declined and then made again next week is worse than
        never having proposed at all."""
        made = self.make(isolated_db)
        goals.decide(made["id"], accepted=False)
        assert "thesis" in goals.declined_subjects()

        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-04-01"}')
        with _fake_model(payload):
            again = commitments.propose_from_turn(
                "I will finish the thesis by April instead")
        assert again is None

    def test_a_decision_cannot_be_made_twice(self, isolated_db):
        made = self.make(isolated_db)
        assert goals.decide(made["id"], accepted=True) is not None
        assert goals.decide(made["id"], accepted=False) is None

    def test_deciding_something_that_is_not_a_proposal_is_refused(self, isolated_db):
        typed = goals.create_goal("Typed by hand")
        assert goals.decide(typed["id"], accepted=True) is None


class TestDoingNothingIsAlsoAnAnswer:
    def test_an_undecided_proposal_survives_a_reload(self, isolated_db):
        """A chip that evaporates on refresh is a question the user never got
        to answer, which is the same as not having asked."""
        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-03-12"}')
        with _fake_model(payload):
            commitments.propose_from_turn(
                "I will finish the thesis by 2027-03-12", conversation_id="c1")
        assert len(goals.proposals_for("c1")) == 1

    def test_a_decided_one_does_not_come_back(self, isolated_db):
        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-03-12"}')
        with _fake_model(payload):
            made = commitments.propose_from_turn(
                "I will finish the thesis by 2027-03-12", conversation_id="c1")
        goals.decide(made["id"], accepted=True)
        assert goals.proposals_for("c1") == []

    def test_proposals_belong_to_their_conversation(self, isolated_db):
        payload = ('{"is_commitment": true, "title": "Finish the thesis",'
                   ' "subject": "thesis", "deadline": "2027-03-12"}')
        with _fake_model(payload):
            commitments.propose_from_turn(
                "I will finish the thesis by 2027-03-12", conversation_id="c1")
        assert goals.proposals_for("c2") == []


class TestOldGoalsAreNotDisturbed:
    def test_a_goal_typed_by_hand_reads_as_accepted(self, isolated_db):
        """Typing it *was* the acceptance. `accepted` is the truth about those
        rows, not a default picked to be safe."""
        made = goals.create_goal("Typed by hand")
        assert goals.get_goal(made["id"])["status"] == goals.STATUS_ACCEPTED

    def test_the_old_listing_still_works(self, isolated_db):
        goals.create_goal("Typed by hand")
        assert [g["title"] for g in goals.list_goals()] == ["Typed by hand"]


class TestTheChatWiring:
    def read(self, *parts):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "carrot" / "web"
                ).joinpath(*parts).read_text(encoding="utf-8")

    def test_the_turn_proposes(self):
        from pathlib import Path
        app_src = (Path(__file__).resolve().parents[1] / "carrot" / "app.py"
                   ).read_text(encoding="utf-8")
        assert "commitments_mod.propose_from_turn" in app_src

    def test_it_can_be_switched_off(self):
        """It is the one feature here that speaks without being spoken to, and
        a thing that appears uninvited has to be a thing you can stop."""
        from carrot import config
        assert config.DEFAULTS["goal_chips_enabled"] is True
        from pathlib import Path
        app_src = (Path(__file__).resolve().parents[1] / "carrot" / "app.py"
                   ).read_text(encoding="utf-8")
        assert 'settings.get("goal_chips_enabled"' in app_src

    def test_the_chip_is_drawn(self):
        js = self.read("js", "features.js")
        assert "function goalChip" in js and "mountGoalChips" in js

    def test_the_chip_is_a_checkbox_and_a_dismiss(self):
        js = self.read("js", "features.js")
        chip = js[js.index("function goalChip"):]
        assert "type = 'checkbox'" in chip
        assert "goal-chip-dismiss" in chip

    def test_reopening_a_conversation_asks_again(self):
        js = self.read("js", "app.js")
        assert js.count("mountGoalChips") >= 2

    def test_a_date_is_shown_as_a_date(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "features.js").read_text(encoding="utf-8")
        assert "function formatGoalDate" in js
