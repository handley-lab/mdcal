"""Import iCalendar (`.ics`) calendars into an mddb deck as one card per VEVENT.

The mapping engine (`vevent_to_card`) is a pure function over an `icalendar`
VEVENT component, so it drives both the dry-run renderer and the deck writer.
All calendar semantics live here in the mdcal layer; the substrate is reached
only through the public `mddb.MDDB` API and raw SQL over `db.conn`.
"""

import argparse
import collections
import datetime as _dt
from zoneinfo import ZoneInfo
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import icalendar
import mddb
import yaml
from dateutil.rrule import rrulestr
from slugify import slugify

EVENT_KEYS = (
    "source",
    "uid",
    "recurrence_id",
    "recurrence_id_epoch",
    "dtstart",
    "dtstart_epoch",
    "dtend",
    "dtend_epoch",
    "all_day",
    "tzid",
    "status",
    "transp",
    "sequence",
    "rrule",
    "recurrence_end_epoch",
    "exdate",
    "rdate",
    "location",
    "organizer",
    "attendee_emails",
    "attendees",
    "my_status",
    "conference",
    "conference_url",
    "attachments",
    "created",
    "last_modified",
    "gcal_id",
    "gcal_calendar",
    "gcal_link",
)
"""Flat YAML keys owned by `vevent_to_card` on every event card.

The shared write contract: any writer that re-renders a card (a re-import, a
web edit) strips exactly these keys from the existing YAML before applying a
fresh render, so fields the source dropped don't linger while non-owned local
keys survive.

``tags`` is deliberately NOT here: iCal ``CATEGORIES`` seed a card's tags at
creation, after which tags are deck-owned local classification — no re-render
path may pass ``tags=`` to ``editor.update``, so retags (``area/*``,
``mdcal/hidden``) survive every upstream change.
"""

GRACE = _dt.timedelta(hours=1)
"""Prune exemption window for cards with a ``dispatched`` guard.

A write-through create reaches Google immediately but its feed takes ~2
minutes (measured) to serve it; pruning the card in that window would eat a
user-authored event. One hour is a ~30x margin without pinning a genuinely
upstream-deleted card forever.
"""


@dataclass
class RenderedCard:
    """A fully-rendered card ready to create or update in an mddb deck.

    Attributes:
        title: The mddb card title (iCal ``SUMMARY`` or ``"(untitled)"``).
        summary: The mddb disclosure one-liner synthesised for the event.
        body: The card body (``DESCRIPTION`` plus a fenced ``ics`` VEVENT block).
        tags: The card tags (iCal ``CATEGORIES``), or an empty list.
        yaml: The flat layer YAML fields (``uid``, ``dtstart``, ``rrule`` …).
        relpath: The deterministic date-led ``YYYY-MM-DD-<stub>-<hash>.md`` deck path.
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


RECURRENCE_FOREVER_EPOCH = 253402300799
"""The ``recurrence_end_epoch`` sentinel for unbounded rules (9999-12-31Z)."""


def normalise_until(rrule):
    """Coerce a DATE-form ``RRULE`` ``UNTIL`` to end-of-day UTC.

    `dateutil` requires the ``UNTIL`` value be a UTC ``datetime`` when DTSTART is
    tz-aware, but real Google exports sometimes emit a DATE-form ``UNTIL=20200412``
    (observed in 2/442 Research masters). Map a date-form ``UNTIL`` to that day's
    end in UTC; any other form (notably a well-formed ``...Z`` datetime) is left
    untouched and `dateutil` raises naturally if it is malformed. Shared by the
    importer (computing ``recurrence_end_epoch``) and the window resolver
    (expansion), so both read a rule's bound identically.

    Args:
        rrule: The stored RRULE string (no ``RRULE:`` prefix).

    Returns:
        The RRULE string with a date-form ``UNTIL`` normalised to UTC.
    """
    return re.sub(r"UNTIL=([0-9]{8})(?=;|$)", r"UNTIL=\1T235959Z", rrule)


def _recurrence_end_epoch(rrule, dtstart, dtend, all_day, tzid, rdates=()):
    """Epoch of a master's final generated occurrence end (its query bound).

    The resolver prefilters masters to ``recurrence_end_epoch > window_start``,
    so long-dead recurrences are never loaded or expanded. Semantics mirror the
    resolver's expansion exactly: the all-day anchor is UTC midnight and every
    occurrence end is start + (dtend - dtstart). EXDATEs and RECURRENCE-ID
    exception cards never move this bound — an excluded final slot only leaves
    the bound conservatively late, and a moved exception is its own concrete
    card found by its own epochs. Unbounded rules (no COUNT/UNTIL) get
    `RECURRENCE_FOREVER_EPOCH`. A finite rule generating ZERO instances is
    real upstream data (Google serves a GAMBIT master whose UNTIL falls the
    day before its DTSTART); such a master expands to nothing, so its bound
    is its own ``dtstart``. RDATEs extend a bounded rule (a series prolonged
    by explicit dates after its UNTIL), so the bound covers the last RDATE's
    occurrence end too.
    """
    if "COUNT=" not in rrule and "UNTIL=" not in rrule:
        return RECURRENCE_FOREVER_EPOCH
    rdate_bound = max((_epoch(r) for r in rdates), default=None)
    anchor = (
        _dt.datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=_dt.timezone.utc)
        if all_day
        else dtstart.astimezone(ZoneInfo(tzid))
    )
    instances = list(rrulestr(normalise_until(rrule), dtstart=anchor))
    rule_bound = (
        _epoch(instances[-1] + (dtend - dtstart)) if instances else _epoch(dtstart)
    )
    duration = int((dtend - dtstart).total_seconds())
    if rdate_bound is not None:
        return max(rule_bound, rdate_bound + duration)
    return rule_bound


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
    return _date_list(vevent, "EXDATE", uid)


def _rdates(vevent, uid):
    return _date_list(vevent, "RDATE", uid)


def _date_list(vevent, key, uid):
    raw = vevent.get(key)
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
        The `RenderedCard` for the event. The body's fenced VEVENT keeps every
        event-content property but excludes ``DTSTAMP``: Google feeds stamp it
        with the serve time, so keeping it would make every re-fetch of an
        unchanged feed rewrite every card.

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
        "recurrence_end_epoch": _recurrence_end_epoch(
            vevent["RRULE"].to_ical().decode(),
            dtstart,
            dtend,
            all_day,
            tzid,
            _rdates(vevent, uid),
        )
        if vevent.get("RRULE")
        else None,
        "exdate": _exdates(vevent, uid) or None,
        "rdate": _rdates(vevent, uid) or None,
        "location": str(vevent["LOCATION"]) if vevent.get("LOCATION") else None,
        "organizer": _email(vevent["ORGANIZER"]) if vevent.get("ORGANIZER") else None,
        "attendee_emails": _attendees(vevent) or None,
        "attendees": _attendee_details(vevent) or None,
        "my_status": _my_status(vevent),
        "conference": _conference_entries(vevent) or None,
        "conference_url": str(vevent["X-GOOGLE-CONFERENCE"])
        if vevent.get("X-GOOGLE-CONFERENCE")
        else None,
        "attachments": _attachments(vevent) or None,
        "gcal_id": str(vevent["X-GOOGLE-EVENT-ID"])
        if vevent.get("X-GOOGLE-EVENT-ID")
        else None,
        "gcal_calendar": str(vevent["X-GOOGLE-CALENDAR-ID"])
        if vevent.get("X-GOOGLE-CALENDAR-ID")
        else None,
        "gcal_link": str(vevent["X-GOOGLE-HTML-LINK"])
        if vevent.get("X-GOOGLE-HTML-LINK")
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
    ics_block = "\n".join(
        line
        for line in vevent.to_ical().decode().replace("\r\n", "\n").strip().split("\n")
        if not line.startswith("DTSTAMP")
    )
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


def _attendee_props(vevent):
    raw = vevent.get("ATTENDEE")
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


def _attendee_details(vevent):
    """Display-shaped attendee list from ATTENDEE properties and their params.

    ``status`` carries the PARTSTAT token verbatim (``ACCEPTED``,
    ``NEEDS-ACTION``, or whatever a foreign feed wrote) — faithful to the
    source; Google's value vocabulary is enforced only at the API boundary
    where mdcal itself writes.
    """
    out = []
    for prop in _attendee_props(vevent):
        params = prop.params
        entry = {"email": _email(prop)}
        if params.get("CN"):
            entry["name"] = str(params["CN"])
        if params.get("PARTSTAT"):
            entry["status"] = str(params["PARTSTAT"])
        if str(params.get("ROLE", "")) == "OPT-PARTICIPANT":
            entry["optional"] = True
        out.append(entry)
    return out


def _my_status(vevent):
    """The user's own PARTSTAT — the attendee the API export marked as self."""
    for prop in _attendee_props(vevent):
        params = prop.params
        if str(params.get("X-GOOGLE-SELF", "")).upper() == "TRUE":
            return str(params["PARTSTAT"]) if params.get("PARTSTAT") else None
    return None


def _conference_entries(vevent):
    raw = vevent.get("X-GOOGLE-CONFERENCE-ENTRY")
    if raw is None:
        return []
    out = []
    for prop in raw if isinstance(raw, list) else [raw]:
        entry = {"uri": str(prop), "type": str(prop.params["TYPE"])}
        if prop.params.get("LABEL"):
            entry["label"] = str(prop.params["LABEL"])
        out.append(entry)
    return out


def _attachments(vevent):
    raw = vevent.get("ATTACH")
    if raw is None:
        return []
    out = []
    for prop in raw if isinstance(raw, list) else [raw]:
        entry = {"url": str(prop)}
        params = getattr(prop, "params", {})
        if params.get("FILENAME"):
            entry["title"] = str(params["FILENAME"])
        out.append(entry)
    return out


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
    return f"{dtstart.strftime('%Y-%m-%d')}-{stub}-{digest}.md"


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


def _open_or_init(deck):
    return mddb.MDDB(deck) if (Path(deck) / ".git").is_dir() else mddb.MDDB.init(deck)


def _existing_map(db, source):
    rows = db.conn.execute(
        "SELECT e.id, us.value_str, rs.value_str "
        "FROM entries e "
        "JOIN entry_fields ss ON ss.entry_rowid=e.rowid AND ss.key='source' AND ss.value_str=? "
        "JOIN entry_fields us ON us.entry_rowid=e.rowid AND us.key='uid' "
        "LEFT JOIN entry_fields rs ON rs.entry_rowid=e.rowid AND rs.key='recurrence_id'",
        (source,),
    ).fetchall()
    return {(uid, _ident(rid)): cid for cid, uid, rid in rows}


def _guarded(card, existing):
    """True when a dispatched write-through outruns the feed for this card.

    A card carrying a ``dispatched`` watermark (set by a Google write-through)
    must not be updated from feed content older than that write — the feed
    lags the API and would revert it. Once the feed catches up
    (``last_modified >= dispatched``) the guard is inert and the normal
    update converges the card to the feed's canonical serialisation.

    Raises:
        ValueError: The guarded card's feed counterpart lacks
            ``LAST-MODIFIED`` — the watermark premise is broken
            (crash-on-drift, never a silent weaker guard).
    """
    dispatched = existing.yaml.get("dispatched")
    if dispatched is None:
        return False
    if "last_modified" not in card.yaml:
        raise ValueError(
            f"feed VEVENT lacks LAST-MODIFIED for guarded uid={card.yaml['uid']}"
        )
    return card.yaml["last_modified"] < dispatched


def _seeded_tags(card_tags, seed_tags):
    """Ordered duplicate-free union of a new card's CATEGORIES and seed tags."""
    return list(dict.fromkeys([*card_tags, *seed_tags]))


def _unchanged(card, existing):
    existing_importer = {k: v for k, v in existing.yaml.items() if k in EVENT_KEYS}
    return (
        card.title == existing.title
        and card.summary == existing.summary
        and card.body == existing.body
        and card.yaml == existing_importer
    )


def import_ics(deck, ics_path, source, uid=None, limit=None, prune=False, tags=()):
    """Idempotently import an `.ics` calendar into the mddb deck at `deck`.

    Opens (or initialises) the deck, renders one card per VEVENT, and within a
    per-year `db.editor` block creates new cards, updates changed ones, and skips
    unchanged ones — keyed on ``source + uid + recurrence_id``. A year with no
    creates/updates opens no editor block, so a clean re-import produces no commit.

    Tags are deck-owned after creation: a new card's tags are seeded once
    (feed ``CATEGORIES`` first, then ``tags`` values not already present) and
    updates never touch them, so local classification (``area/*``,
    ``mdcal/hidden``) survives every upstream change.

    Args:
        deck: Path to the deck (created via `mddb.MDDB.init` if absent).
        ics_path: Path to the source `.ics` file.
        source: The calendar/source label written to each card's ``source`` field.
        uid: Optional single iCal UID to restrict the import to.
        limit: Optional cap on the number of VEVENTs imported.
        prune: Delete existing cards of this ``source`` absent from the file's
            identity set — for polling a feed that serves the calendar's full
            span, where an absent event means an upstream deletion. Cards of
            other sources (``local``, …) are never touched; git history is the
            tombstone. Nothing to prune opens no editor block.
        tags: Seed tags applied to CREATED cards only.

    Returns:
        A ``{"created": int, "updated": int, "skipped": int, "pruned": int}``
        count summary.

    Raises:
        ValueError: ``prune`` combined with ``uid`` or ``limit`` — a partial
            import's identity set is incomplete by construction, so pruning
            against it would delete every unselected card of the source.
    """
    if prune and (uid is not None or limit is not None):
        raise ValueError("prune requires a full feed; cannot combine with uid/limit")
    db = _open_or_init(deck)
    existing = _existing_map(db, source)
    by_year = collections.defaultdict(list)
    for vevent in _vevents(ics_path, uid, limit):
        card = vevent_to_card(vevent, source)
        by_year[card.yaml["dtstart"].year].append(card)

    counts = {"created": 0, "updated": 0, "skipped": 0, "pruned": 0}
    for year in sorted(by_year):
        actions = []
        year_skipped = 0
        for card in by_year[year]:
            cid = existing.get(
                (card.yaml["uid"], _ident(card.yaml.get("recurrence_id")))
            )
            if cid is None:
                actions.append((card, None))
                continue
            existing_card = db.read(cid)
            if _guarded(card, existing_card) or _unchanged(card, existing_card):
                year_skipped += 1
            else:
                actions.append((card, cid))
        counts["skipped"] += year_skipped
        if not actions:
            continue
        year_created = year_updated = 0
        with db.editor(rationale=f"import {source} {year}") as editor:
            for card, cid in actions:
                if cid is None:
                    seeded = _seeded_tags(card.tags, tags)
                    editor.create(
                        title=card.title,
                        summary=card.summary,
                        tags=seeded or None,
                        body=card.body,
                        relpath=card.relpath,
                        yaml=card.yaml,
                    )
                    year_created += 1
                else:
                    existing_card = editor.read(cid)
                    kept = {
                        k: v
                        for k, v in existing_card.yaml.items()
                        if k not in EVENT_KEYS
                    }
                    kept["title"] = card.title
                    kept.update(card.yaml)
                    existing_card.yaml = kept
                    existing_card.body = card.body
                    editor.update(existing_card, summary=card.summary)
                    year_updated += 1
        counts["created"] += year_created
        counts["updated"] += year_updated
        print(f"  {year}: +{year_created} ~{year_updated} ={year_skipped}")
    if prune:
        feed_idents = {
            (card.yaml["uid"], _ident(card.yaml.get("recurrence_id")))
            for cards in by_year.values()
            for card in cards
        }
        now = _dt.datetime.now(_dt.timezone.utc)
        stale = [
            cid
            for ident, cid in existing.items()
            if ident not in feed_idents
            and not (
                (dispatched := db.read(cid).yaml.get("dispatched")) is not None
                and now - dispatched < GRACE
            )
        ]
        if stale:
            with db.editor(rationale=f"prune {source}: {len(stale)} removed") as editor:
                for cid in stale:
                    editor.delete(cid)
        counts["pruned"] = len(stale)
    return counts


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
    parser.add_argument("--prune", action="store_true")
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="seed tag applied to created cards only (repeatable)",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        for vevent in _vevents(args.ics, args.uid, args.limit):
            card = vevent_to_card(vevent, args.source)
            card.tags = _seeded_tags(card.tags, args.tag)
            print(f"# ===== {card.relpath} =====")
            print(render_text(card))
        return
    if not args.deck:
        parser.error("--deck is required unless --dry-run")
    counts = import_ics(
        args.deck, args.ics, args.source, args.uid, args.limit, args.prune, args.tag
    )
    print(
        f"created={counts['created']} updated={counts['updated']} "
        f"skipped={counts['skipped']} pruned={counts['pruned']}"
    )


if __name__ == "__main__":
    main()
