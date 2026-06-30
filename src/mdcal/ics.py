"""Import iCalendar (`.ics`) calendars into an mddb deck as one card per VEVENT.

The mapping engine (`vevent_to_card`) is a pure function over an `icalendar`
VEVENT component, so it drives both the dry-run renderer and the deck writer.
All calendar semantics live here in the mdcal layer; the substrate is reached
only through the public `mddb.MDDB` API and raw SQL over `db.conn`.
"""

import argparse
import datetime as _dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import icalendar
import yaml
from slugify import slugify


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


def _ident(value):
    """Return the canonical identity string for a recurrence id (`""` if absent).

    The one serialiser shared by the relpath hash, the dedup-map key, and the
    re-import lookup, so all three agree. Uses ``str(value)`` to match how mddb
    indexes the ``recurrence_id`` YAML object into ``entry_fields.value_str``.

    Args:
        value: The ``recurrence_id`` ``date``/``datetime`` object, or ``None``.

    Returns:
        ``str(value)`` for a present recurrence id, else ``""``.
    """
    return "" if value is None else str(value)


def _email(addr):
    return re.sub(r"^mailto:", "", str(addr), flags=re.IGNORECASE)


def _epoch(value):
    if isinstance(value, _dt.datetime):
        return int(value.timestamp())
    midnight = _dt.datetime(value.year, value.month, value.day, tzinfo=_dt.timezone.utc)
    return int(midnight.timestamp())


def _check_aware(value, uid):
    if isinstance(value, _dt.datetime) and value.tzinfo is None:
        raise ValueError(
            f"floating (naive) datetime unsupported: uid={uid} value={value!r}"
        )


def _point(prop, uid):
    value = prop.dt
    _check_aware(value, uid)
    tzid = prop.params.get("TZID") if isinstance(value, _dt.datetime) else None
    if tzid is None and isinstance(value, _dt.datetime):
        tzid = str(value.tzinfo)
    return (
        value,
        _epoch(value),
        isinstance(value, _dt.date) and not isinstance(value, _dt.datetime),
        tzid,
    )


def _exdates(vevent, uid):
    raw = vevent.get("EXDATE")
    if raw is None:
        return []
    groups = raw if isinstance(raw, list) else [raw]
    out = []
    for group in groups:
        for item in group.dts:
            _check_aware(item.dt, uid)
            out.append(item.dt)
    return sorted(out)


def _when_clause(dtstart, dtend, all_day, tzid):
    if all_day:
        last = dtend - _dt.timedelta(days=1)
        if last <= dtstart:
            return f"{dtstart:%Y-%m-%d} (all-day)"
        return f"{dtstart:%Y-%m-%d}–{last:%Y-%m-%d} (all-day)"
    return f"{dtstart:%Y-%m-%d %H:%M}–{dtend:%H:%M} {tzid}"


def vevent_to_card(vevent, source):
    """Render one iCalendar VEVENT into a `RenderedCard`.

    Args:
        vevent: An ``icalendar`` VEVENT component.
        source: The calendar/source label (e.g. ``"research"``), the first
            component of the import identity ``source + uid + recurrence_id``.

    Returns:
        The `RenderedCard` for the event.

    Raises:
        ValueError: A floating (naive) datetime is encountered (crash-on-drift).
    """
    uid = str(vevent["UID"])
    title = str(vevent["SUMMARY"]) if vevent.get("SUMMARY") else "(untitled)"

    dtstart, dtstart_epoch, all_day, tzid = _point(vevent["DTSTART"], uid)
    if vevent.get("DTEND"):
        dtend, dtend_epoch, _, _ = _point(vevent["DTEND"], uid)
    elif all_day:
        dtend = dtstart + _dt.timedelta(days=1)
        dtend_epoch = _epoch(dtend)
    else:
        dtend, dtend_epoch = dtstart, dtstart_epoch

    recurrence_id = None
    if vevent.get("RECURRENCE-ID"):
        recurrence_id = vevent["RECURRENCE-ID"].dt
        _check_aware(recurrence_id, uid)

    status = str(vevent["STATUS"]) if vevent.get("STATUS") else "CONFIRMED"
    yaml = {
        "source": source,
        "uid": uid,
        "recurrence_id": recurrence_id,
        "recurrence_id_epoch": _epoch(recurrence_id)
        if recurrence_id is not None
        else None,
        "dtstart": dtstart,
        "dtstart_epoch": dtstart_epoch,
        "dtend": dtend,
        "dtend_epoch": dtend_epoch,
        "all_day": all_day,
        "tzid": tzid,
        "status": status,
        "transp": str(vevent["TRANSP"]) if vevent.get("TRANSP") else None,
        "sequence": int(vevent["SEQUENCE"])
        if vevent.get("SEQUENCE") is not None
        else None,
        "rrule": vevent["RRULE"].to_ical().decode() if vevent.get("RRULE") else None,
        "exdate": _exdates(vevent, uid) or None,
        "location": str(vevent["LOCATION"]) if vevent.get("LOCATION") else None,
        "organizer": _email(vevent["ORGANIZER"]) if vevent.get("ORGANIZER") else None,
        "attendee_emails": _attendees(vevent) or None,
        "conference_url": str(vevent["X-GOOGLE-CONFERENCE"])
        if vevent.get("X-GOOGLE-CONFERENCE")
        else None,
        "created": vevent["CREATED"].dt if vevent.get("CREATED") else None,
        "last_modified": vevent["LAST-MODIFIED"].dt
        if vevent.get("LAST-MODIFIED")
        else None,
    }
    yaml = {k: v for k, v in yaml.items() if v is not None}

    prefix = "[cancelled] " if status == "CANCELLED" else ""
    flag = (
        " · recurring"
        if "rrule" in yaml
        else " · override"
        if recurrence_id is not None
        else ""
    )
    summary = f"{prefix}{title} · {_when_clause(dtstart, dtend, all_day, tzid)}{flag}"

    tags = _categories(vevent)
    description = str(vevent["DESCRIPTION"]) if vevent.get("DESCRIPTION") else ""
    ics_block = vevent.to_ical().decode().strip()
    body = (f"{description}\n\n" if description else "") + f"```ics\n{ics_block}\n```\n"

    return RenderedCard(
        title=title,
        summary=summary,
        body=body,
        tags=tags,
        yaml=yaml,
        relpath=_relpath(source, uid, recurrence_id, title, dtstart),
    )


def _attendees(vevent):
    raw = vevent.get("ATTENDEE")
    if raw is None:
        return []
    return [_email(a) for a in (raw if isinstance(raw, list) else [raw])]


def _categories(vevent):
    raw = vevent.get("CATEGORIES")
    if raw is None:
        return []
    groups = raw if isinstance(raw, list) else [raw]
    return [str(c) for group in groups for c in group.cats]


def _relpath(source, uid, recurrence_id, title, dtstart):
    ident = f"{source}\x00{uid}\x00{_ident(recurrence_id)}"
    digest = hashlib.sha1(ident.encode()).hexdigest()[:12]
    stub = slugify(title)[:40] or "untitled"
    return f"{dtstart.year}/{stub}-{digest}.md"


def _vevents(ics_path, uid, limit):
    cal = icalendar.Calendar.from_ical(Path(ics_path).read_text())
    events = list(cal.walk("VEVENT"))
    if uid:
        events = [v for v in events if str(v["UID"]) == uid]
    if limit is not None:
        events = events[:limit]
    return events


def render_text(card):
    """Render a `RenderedCard` as the `.md` text mddb would write to disk.

    The on-disk frontmatter order mirrors mddb's canonical ordering (``id``,
    ``title``, ``summary``, ``tags``, then the layer YAML), so a dry-run preview
    shows the exact card shape. The ``id`` is a placeholder — mddb assigns the
    real UUID at write time.

    Args:
        card: The `RenderedCard` to render.

    Returns:
        The full `.md` text (frontmatter + body), starting with ``---``.
    """
    front = {"id": "<assigned-at-write>", "title": card.title, "summary": card.summary}
    if card.tags:
        front["tags"] = card.tags
    front.update(card.yaml)
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
    return f"---\n{dumped}---\n{card.body}"


def main(argv=None):
    """Run the `mdcal-import` console script.

    Parses the source `.ics`, then renders cards to stdout (``--dry-run``) or
    imports them into the deck at ``--deck`` idempotently.

    Args:
        argv: Argument list for testing; defaults to ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(prog="mdcal-import")
    parser.add_argument("--source", required=True)
    parser.add_argument("--ics", required=True)
    parser.add_argument("--deck")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--uid")
    args = parser.parse_args(argv)

    events = _vevents(args.ics, args.uid, args.limit)
    if args.dry_run:
        for vevent in events:
            card = vevent_to_card(vevent, args.source)
            print(f"# ===== {card.relpath} =====")
            print(render_text(card))
        return
    raise NotImplementedError("deck writer lands in Phase 3")


if __name__ == "__main__":
    main()
