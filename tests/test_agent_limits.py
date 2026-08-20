"""The four numbers that stop a runaway agent, and the fact you can set them.

`A run stops after 40 steps, 900s, 30 navigations, or 10 distinct sites.` was a
sentence on a card. Every one of those had always been read from config —
`Budget.from_config` looked them up by key — but nothing anywhere wrote those
keys, so the only way to disagree with the sentence was to edit the database by
hand. The card stated a policy and offered no way to hold a different one.

The interesting half is not the inputs, it is where the bounds live.

**Clamped on the way out, not on the way in.** These are the numbers that stop a
run: `max_steps` of 0 ends every run before its first action, and `max_domains`
of 0 makes the domain check meaningless. `/api/config/{key}` is a generic setter
that will take any value, so a floor enforced by an `min=` attribute on an input
is a floor enforced by whatever last wrote to the config table. `from_config` is
the one place every caller passes through, so that is where the range is.

**A per-run override is clamped by the same rule.** It arrives in a request
body. Trusting it would be a way to ask for a run with no ceiling at all.
"""
import pytest

from carrot import policy
from carrot.config import set_config


class TestTheBoundsAreRealRatherThanAdvisory:

    def test_every_field_of_the_budget_has_a_spec(self):
        # A field with no entry is one with no floor, no ceiling and no input.
        assert set(policy.BUDGET_LIMITS) == set(policy.Budget().as_dict())

    def test_every_spec_is_usable_by_both_ends(self):
        for field, spec in policy.BUDGET_LIMITS.items():
            assert spec["key"].startswith("agent_"), field
            assert spec["min"] >= 1, field
            assert spec["min"] <= spec["default"] <= spec["max"], field
            assert spec["label"] and spec["unit"] and spec["help"], field

    @pytest.mark.parametrize("field", list(policy.BUDGET_LIMITS))
    def test_a_zero_cannot_disarm_a_limit(self, isolated_db, field):
        spec = policy.BUDGET_LIMITS[field]
        set_config(spec["key"], 0)
        assert getattr(policy.Budget.from_config(), field) == spec["min"]

    @pytest.mark.parametrize("field", list(policy.BUDGET_LIMITS))
    def test_an_enormous_number_is_still_a_budget(self, isolated_db, field):
        spec = policy.BUDGET_LIMITS[field]
        set_config(spec["key"], 10 ** 9)
        assert getattr(policy.Budget.from_config(), field) == spec["max"]

    def test_a_value_that_is_not_a_number_falls_back_to_the_default(self, isolated_db):
        set_config("agent_max_steps", "as many as it takes")
        assert policy.Budget.from_config().max_steps == 40

    def test_a_reasonable_value_is_honoured(self, isolated_db):
        set_config("agent_max_steps", 120)
        set_config("agent_max_seconds", 1800)
        budget = policy.Budget.from_config()
        assert budget.max_steps == 120
        assert budget.max_seconds == 1800

    def test_an_override_from_a_request_is_clamped_too(self, isolated_db):
        budget = policy.Budget.from_config({"max_navigations": 10 ** 6})
        assert budget.max_navigations == policy.BUDGET_LIMITS["max_navigations"]["max"]

    def test_an_override_of_something_that_is_not_a_limit_is_ignored(self, isolated_db):
        budget = policy.Budget.from_config({"max_steps": 60, "cancel_everything": True})
        assert budget.max_steps == 60
        assert not hasattr(budget, "cancel_everything")


class TestThePanelIsToldWhatItMaySet:
    """The inputs are built from this, so the bounds on the box are the bounds
    the kernel enforces rather than a second copy in the browser."""

    def test_the_status_carries_the_limits(self, isolated_db):
        status = policy.status()
        assert set(status["budget_limits"]) == set(status["budget"])
        for field, spec in status["budget_limits"].items():
            assert {"key", "default", "min", "max", "label", "unit", "help"} <= set(spec)

    def test_the_status_reports_what_is_actually_in_force(self, isolated_db):
        set_config("agent_max_domains", 25)
        assert policy.status()["budget"]["max_domains"] == 25


# ===== The panel =====

from pathlib import Path  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"


@pytest.fixture(scope="module")
def agents_js():
    return (WEB / "js" / "agents.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css():
    return (WEB / "css" / "style.css").read_text(encoding="utf-8")


class TestTheCardIsNowFourBoxes:

    def test_it_is_no_longer_only_a_sentence(self, agents_js):
        assert "function renderBudget(" in agents_js
        assert "budget-input" in agents_js
        # The sentence is not simply reworded — it is gone from the path that
        # runs when the server can be asked what may be set.
        body = agents_js[agents_js.index("function renderBudget("):]
        after_fallback = body[body.index("if (!fields.length)"):]
        assert after_fallback.index("return;") < after_fallback.index("budget-grid")

    def test_the_fields_come_from_the_server(self, agents_js):
        # Not a list here. A second copy is a copy that drifts, and the way it
        # drifts is an input that accepts a number the run will ignore.
        body = agents_js[agents_js.index("function renderBudget("):]
        body = body[:body.index("\n/**")]
        assert "policy.budget_limits" in body
        assert "Object.keys(limits)" in body

    def test_the_bounds_on_the_box_are_the_servers(self, agents_js):
        body = agents_js[agents_js.index("function renderBudget("):]
        assert 'min="${Number(spec.min)}" max="${Number(spec.max)}"' in body

    def test_each_field_writes_the_key_the_kernel_reads(self, agents_js):
        save = agents_js[agents_js.index("async function saveBudgetField("):]
        save = save[:save.index("\nasync function ", 1)]
        assert "api(`/api/config/${spec.key}`" in save
        assert "method: 'PUT'" in save

    def test_the_box_is_clamped_before_it_is_written(self, agents_js):
        # The server is what makes the bound real; this stops the box showing
        # 9999 beside a run that will stop at 500 and leaving the reader to
        # work out which of the two is true.
        save = agents_js[agents_js.index("async function saveBudgetField("):]
        save = save[:save.index("\nasync function ", 1)]
        assert "Math.max(spec.min, Math.min(spec.max, value))" in save
        assert "input.value = value;" in save

    def test_an_emptied_box_goes_back_to_the_default(self, agents_js):
        # `Number('')` is 0, which is finite and clamps to the floor — so
        # clearing the field gave the minimum while typing gibberish gave the
        # default, from one gesture meaning one thing.
        save = agents_js[agents_js.index("async function saveBudgetField("):]
        save = save[:save.index("\nasync function ", 1)]
        assert "typed === '' ? spec.default" in save

    def test_an_older_backend_still_gets_a_sentence(self, agents_js):
        # It would not read keys this panel writes, so it is told the numbers
        # rather than given controls that do nothing.
        body = agents_js[agents_js.index("function renderBudget("):]
        assert "if (!fields.length)" in body
        assert "A run stops after" in body

    def test_the_card_has_somewhere_to_draw_them(self, index_html, style_css):
        assert 'id="policy-budget" class="budget-limits"' in index_html
        for selector in (".budget-grid", ".budget-input", ".budget-range"):
            assert selector in style_css, f"{selector} is drawn by nothing"
