"""Semester Planner, as a pack you turn on rather than a tab everyone gets.

The planner is good and it is narrow. It takes a photo of a class schedule, a
campus, and a few sentences about when you eat and sleep, and gives back a week
you can actually live in. That is worth having — and it is worth having *if you
are a student in a semester*, which most people installing a local assistant
are not, and none of them are during the summer.

It was in the sidebar regardless, between Research and Goals, permanently. A
feature that cannot apply to you and cannot be removed is worse than the same
feature behind a switch: it makes the nav longer for everyone in order to serve
some of them, and the ones it does serve did not need it to be always-on.

So it is a pack. The code does not move — `carrot/planner.py` and
`carrot/planner_api.py` stay exactly where they are, and the endpoints stay
mounted, because a disabled pack should hide a feature rather than break the
routes behind it. What the pack controls is whether the tab is offered.
"""

from __future__ import annotations

from ... import extensions

PACK = extensions.Pack(
    pack_id="planner",
    name="Semester Planner",
    description=(
        "Turn a class schedule into a week you can live in: reads a timetable "
        "photo, asks what it does not know about your campus and your habits, "
        "and places meals, gym and study around your fixed commitments — with "
        "walking time between buildings accounted for."
    ),
    version="1.0",
    # No tools and no skills. This pack exists to gate a surface, which is a
    # thing packs could not do before it; see `Pack.tabs`.
    tabs=["planner"],
    tutorial=[
        {"title": "Open the Planner tab",
         "body": "Switching this pack on adds Planner to the sidebar, under "
                 "Planning. Nothing else about Carrot changes."},
        {"title": "Give it your timetable",
         "body": "A photo or screenshot of your class schedule is enough — it "
                 "reads the day codes registrars actually use, including R for "
                 "Thursday and U for Sunday. You can also type the classes in."},
        {"title": "Answer what it asks",
         "body": "It will ask where you live, whether you have a meal plan, when "
                 "you actually sleep, and which campus you are on. These are the "
                 "things a timetable does not say and a usable week depends on — "
                 "a plan that puts lunch eleven minutes after a class across "
                 "campus is not a plan."},
        {"title": "Add what has to fit around it",
         "body": "Meals, the gym, work shifts, study blocks. Say how long each "
                 "needs and roughly when; the scheduler places your fixed "
                 "commitments first and fits these into what is genuinely left, "
                 "with walking time between buildings accounted for."},
        {"title": "Check the week, then adjust",
         "body": "Everything it placed can be moved, and it will tell you what a "
                 "change costs — moving the gym into a gap too small to shower in "
                 "is the sort of thing it will say out loud rather than silently "
                 "accept."},
    ],
    default_enabled=False,
)

extensions.register(PACK)
