"""What the Code tab says before you have asked it anything.

It said "Tell me what to build, fix or explain" and named the folder. That is
a greeting, and this is the screen in this tab that gets looked at most —
every other coding tool spends it answering the questions you actually have
when you sit down: where am I, what is still running from last time, and what
does this thing already know how to do.

The one that earns its place is "still running". A dev server the agent
started outlives the turn, the conversation and the page reload, and until
this existed a user who had forgotten one had no way to find out except that
the next start failed on a port already in use.
"""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"
FEATURES = (WEB / "js" / "features.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


class TestTheStartupPanel:
    def test_it_asks_the_backend_what_is_still_running(self):
        assert "/api/coder/servers" in FEATURES

    def test_it_lists_what_the_agent_has_been_taught(self):
        """A skill nobody remembers exists is a skill nobody invokes, and this
        is the moment the user is deciding what to ask for."""
        assert "/api/skills" in FEATURES
        assert "hello-skills" in FEATURES

    @pytest.mark.parametrize("cls", [
        ".hello-where", ".hello-root", ".hello-branch", ".hello-title",
        ".hello-server", ".hello-skill", ".agent-skill-chip",
    ])
    def test_every_class_it_builds_is_styled(self, cls):
        assert cls in CSS, f"{cls} is built by features.js and never styled"

    def test_a_long_path_truncates_at_the_front(self):
        """The end of a path is the part that identifies it. Ellipsing there
        leaves every project reading as C:\\Users\\adarz\\Downloads\\…"""
        block = CSS[CSS.index(".hello-root {"):]
        assert "direction: rtl" in block[:block.index("}")]

    def test_it_does_not_redraw_over_a_conversation(self):
        """Coder state arrives asynchronously and more than once. Rendering
        the startup panel on each arrival would delete the task in progress."""
        assert "querySelector('#agent-log .agent-hello')" in FEATURES


class TestArmingASkill:
    def test_the_code_tab_can_invoke_one_at_all(self):
        """Chat has had this since skills existed. The Code tab had no way to
        use one, so a skill about how this project wants tests written could
        only be applied by going to the chat tab to ask about code."""
        assert re.search(r"skill: sentSkill \? sentSkill\.slug : null", FEATURES)

    def test_arming_it_does_not_start_the_task(self):
        """A chip that fires work off the moment it is clicked is a chip
        people stop touching — and the skill is how, not what."""
        body = FEATURES[FEATURES.index("function useSkillInAgent"):]
        body = body[:body.index("\nfunction clearAgentSkill")]
        assert "sendAgentTask" not in body

    def test_it_is_armed_for_one_task_only(self):
        """Left on, it would shape every later message in the panel with
        nothing on screen still saying so by the time it mattered."""
        send = FEATURES[FEATURES.index("async function sendAgentTask"):]
        assert "clearAgentSkill()" in send[:send.index("const response = await fetch")]

    def test_the_armed_skill_is_visible_in_the_composer(self):
        """A skill armed and not visible is the same bug as a model set and
        not visible, which this panel has already had once."""
        assert "agent-skill-chip" in FEATURES
        assert "agent-compose-row" in FEATURES[FEATURES.index("function renderAgentSkillChip"):]
