"""Import iCalendar (`.ics`) calendars into an mddb deck as one card per VEVENT.

The mapping engine (`vevent_to_card`) is a pure function over an `icalendar`
VEVENT component, so it drives both the dry-run renderer and the deck writer.
All calendar semantics live here in the mdcal layer; the substrate is reached
only through the public `mddb.MDDB` API and raw SQL over `db.conn`.
"""

import argparse
from dataclasses import dataclass


@dataclass
class RenderedCard:
    """A fully-rendered card ready to create or update in an mddb deck.

    Attributes:
        title: The mddb card title (iCal ``SUMMARY`` or ``"(untitled)"``).
        summary: The mddb disclosure one-liner synthesised for the event.
        body: The card body (``DESCRIPTION`` plus a fenced ``ics`` VEVENT block).
        tags: The card tags (iCal ``CATEGORIES``), or an empty list.
        yaml: The flat layer YAML fields (``uid``, ``dtstart``, ``rrule`` …).
        relpath: The deterministic ``<year>/<stub>-<hash>.md`` deck path.
    """

    title: str
    summary: str
    body: str
    tags: list
    yaml: dict
    relpath: str


def vevent_to_card(vevent, source):
    """Render one iCalendar VEVENT into a `RenderedCard`.

    Args:
        vevent: An ``icalendar`` VEVENT component.
        source: The calendar/source label (e.g. ``"research"``), the first
            component of the import identity ``source + uid + recurrence_id``.

    Returns:
        The `RenderedCard` for the event.

    Raises:
        NotImplementedError: Until the Phase 1 mapping engine lands.
    """
    raise NotImplementedError


def main():
    """Run the `mdcal-import` console script.

    Parses the source `.ics`, then either renders cards to stdout (``--dry-run``)
    or imports them into the deck at ``--deck`` idempotently.
    """
    parser = argparse.ArgumentParser(prog="mdcal-import")
    parser.add_argument("--source", required=True)
    parser.add_argument("--ics", required=True)
    parser.add_argument("--deck")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--uid")
    parser.parse_args()
    raise NotImplementedError


if __name__ == "__main__":
    main()
