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
    default_enabled=False,
)

extensions.register(PACK)
