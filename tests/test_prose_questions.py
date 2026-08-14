"""A turn that ended on "Key Decisions Needed:" and then stopped.

Plan mode asks the model to put clarifying questions in a machine-readable
block, and says plainly that prose questions produce no buttons, are ignored,
and will leave it guessing. A local model wrote the prose and skipped the
block — so it was ignored, exactly as promised, and the panel reported "Done —
nothing changed on disk" underneath a model that was waiting for an answer.

A detector rather than a repair, deliberately. The obvious alternative is a
second call asking the model to reformat, but the models that miss this format
are the small local ones, and asking the same model to get the same format
right on a second attempt fails in the same place it just failed. This costs
nothing, cannot make a turn worse, and restores the part that was actually
missing: knowing it is waiting.
"""
import pytest

from carrot import coder


REAL_CASE = """This workspace holds a magnetic field simulation in
magnetic_field_sim/. Here is what I would change.

Key Decisions Needed:
1. Should the simulation run in the browser or as a desktop app?
2. Do you want the field lines animated, or static?
"""


class TestItNoticesTheQuestions:
    def test_the_case_this_came_from(self):
        found = coder.prose_questions(REAL_CASE)
        assert len(found) == 2
        assert found[0].startswith("Should the simulation run")

    @pytest.mark.parametrize("shape", [
        "Questions:\n- Which port should it use?",
        "Questions:\n* Which port should it use?",
        "Decisions needed:\n1. Which port should it use?",
        "**Clarifications required:**\n1) Which port should it use?",
    ])
    def test_the_shapes_a_model_actually_writes(self, shape):
        assert coder.prose_questions(shape) == ["Which port should it use?"]

    def test_it_keeps_at_most_a_formful(self):
        text = "Questions:\n" + "\n".join(
            f"{n}. Is this question number {n} worth asking?" for n in range(10))
        assert len(coder.prose_questions(text)) <= coder.MAX_QUESTIONS


class TestItStaysQuietOtherwise:
    def test_a_proper_block_is_not_second_guessed(self):
        """The form is already there; a card saying it asked in prose next to
        real buttons would be a bug reporting itself."""
        text = ('Plan.\n\n```carrot-questions\n'
                '[{"question": "How should it look?", "options": ["a", "b"]}]\n```')
        assert coder.prose_questions(text) == []

    def test_its_own_rhetorical_questions_are_not_for_the_user(self):
        """"What could go wrong?" as a heading over the answer is the common
        one, and it is the model asking itself."""
        assert coder.prose_questions(
            "I will add retries. What could go wrong? A retry storm.") == []

    def test_one_bare_question_is_not_enough(self):
        """A single question inside prose is usually the model narrating.
        Asking the user to answer it every time would train them to ignore
        the card, which is how this fails a second time."""
        assert coder.prose_questions("I changed the parser. Does that look right?") == []

    def test_but_one_under_a_heading_is(self):
        assert coder.prose_questions("Questions:\n- Which port should it use?")

    def test_an_answer_with_no_questions_in_it(self):
        assert coder.prose_questions(
            "I read main.py and simulation.py. Nothing needs deciding; I would "
            "add the retry loop to client.py and leave the rest alone.") == []

    def test_empty_and_nonsense_do_not_raise(self):
        assert coder.prose_questions("") == []
        assert coder.prose_questions(None) == []


class TestItReachesTheUser:
    def test_the_stream_says_so(self):
        source = (__import__("pathlib").Path(
            __import__("carrot.app", fromlist=["app"]).__file__)).read_text(encoding="utf-8")
        assert "questions_in_prose" in source
        # Only when there is no real form — otherwise it would fire alongside
        # the buttons it is meant to substitute for.
        assert "if not (asked and questions):" in source

    def test_the_panel_draws_it_and_stops_calling_the_turn_done(self):
        from pathlib import Path

        features = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
                    / "features.js").read_text(encoding="utf-8")
        css = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "css"
               / "style.css").read_text(encoding="utf-8")
        assert "payload.questions_in_prose" in features
        assert "'Waiting on you'" in features
        for cls in (".prose-questions", ".pq-head", ".pq-list", ".agent-done.waiting"):
            assert cls in css, cls
