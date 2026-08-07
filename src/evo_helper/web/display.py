"""Display choices for the console that are not domain rules."""

from __future__ import annotations

#: Ship types shown as their own column in the intel list.
#:
#: The list is a scanning surface, so it carries only the few types the user
#: sorts targets by. Every other type stays in the detail dialog — a column per
#: recorded ship type made the table wider than any laptop screen.
LIST_SHIP_COLUMNS: tuple[str, ...] = (
    "深空吞噬者",
    "噬能截击者",
    "钛能守卫者",
    "收割者",
)
