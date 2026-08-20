"""The Your-voice skill is a template, not a packed instruction.

It has to stay a file the user pastes their own prose into. If the template
ever ships with sample essays, those essays become the voice — which is how
a 'sound like me' skill silently becomes 'sound like whoever wrote the
readme'. The paste fence is the load-bearing bit.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "carrot" / "skill_templates" / "your-voice" / "SKILL.md"
GPT = ROOT / "carrot" / "skill_templates" / "your-voice" / "chatgpt-custom-instructions.txt"


def test_the_template_is_empty_of_someone_elses_prose():
    text = SKILL.read_text(encoding="utf-8")
    assert "<<<PASTE YOUR WRITING BELOW THIS LINE>>>" in text
    assert "Additionally" in text
    # The word appears as a ban, not as a model of how to start a paragraph.
    assert not any(
        line.startswith("Additionally") for line in text.splitlines()
    )


def test_it_refuses_to_invent_a_voice():
    text = SKILL.read_text(encoding="utf-8")
    assert "say so and stop" in text.lower()


def test_chatgpt_free_instructions_fit_the_character_cap():
    """Free custom instructions are 1,500 characters. One extra sentence
    here is one they cannot paste."""
    text = GPT.read_text(encoding="utf-8").strip()
    assert len(text) <= 1500
    assert "samples.txt" in text
    assert "If no file is attached, say so and stop" in text
