"""Occurrence serialisation and the write path for calendar events.

Read side: ``events_in_window`` occurrences → grid JSON. Write side: form
fields → ``icalendar.Event`` → `mdcal.ics.vevent_to_card` — the one card
factory shared with the importer, so locally-authored cards are
schema-identical and the resolver's ``dtstart_epoch``/date-typing invariants
hold by construction.

Web CRUD touches ``source: local`` cards and *synced* sources. Deployment
configuration is the caller's: ``synced`` maps each synced source to its
Google calendar id, and ``gapi`` is a namespace of write-through callables —
``push(calendar_id, rendered)``, ``patch(calendar_id, uid, slot, rendered)``,
``delete(calendar_id, uid)`` — each returning the dispatched watermark. This
module never imports Google client libraries and never reads deployment
paths; credentials and the write-through error taxonomy live with the caller.

Synced writes go to Google FIRST (write-through; failure propagates from
``gapi``, nothing local changes) and the local card carries a ``dispatched``
watermark so the lagging feed can't revert the write before it catches up.
Unsynced imported events' CONTENT stays read-only (``PermissionError``): the
importer is idempotent-skip-unchanged, so a web edit would be silently
overwritten on the next re-import. The one exception is non-owned tag
annotations (``mdcal/hidden``, ``area/*``) via `set_hidden` — tags are
deck-owned after creation (mdcal's contract: no re-render path passes
``tags=``), so they survive sync on ANY card. Synced RECURRENCE-ID exception
cards are writable as single occurrences: they share the master's iCalUID,
so their writes route through ``gapi.patch`` (keyed by master + recurrence
id), never the uid-keyed push/delete.
"""

import datetime
import subprocess
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
import mddb
from dateutil.rrule import rrulestr

from .ics import (
    apply_render,
    description_of,
    fenced_vevent,
    instant_epoch,
    normalise_until,
    vevent_to_card,
)
from .window import events_in_window

SOURCE = "local"
RRULES = {
    "none": None,
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
}


def window_bound(value):
    """Parse an ISO-8601 query bound to a tz-aware datetime.

    Args:
        value: ISO-8601 string carrying a UTC offset (``Z`` or ``±HH:MM``).

    Returns:
        A timezone-aware ``datetime.datetime``.

    Raises:
        ValueError: The string is malformed or lacks an offset —
            ``events_in_window`` rejects naive bounds, so the boundary
            refuses them up front.
    """
    bound = datetime.datetime.fromisoformat(value)
    if bound.tzinfo is None:
        raise ValueError(f"window bound must carry a UTC offset: {value}")
    return bound


def occurrence_json(occurrence, synced=()):
    """Serialise one resolver occurrence for the grid.

    ``start``/``end`` are ISO-8601: bare dates for all-day occurrences,
    offset-carrying datetimes otherwise. ``id`` is the mddb card id — the
    handle the write API edits. ``editable`` carries the read-only-imported
    decision (web CRUD only touches ``source: local`` and synced-source
    cards) so the frontend never infers it. ``rrule``, ``description``, and
    the master's ``dtstart``/``dtend`` let the edit modal round-trip the
    event's full state; generated occurrences prefill from their own
    ``start``/``end`` (per-occurrence editing) and a series save re-anchors
    to the master's own date server-side (`_anchor_form`).

    Args:
        occurrence: An ``mdcal.window.Occurrence``.
        synced: Mapping of synced source → Google calendar id (membership
            decides ``editable``).

    Returns:
        A JSON-ready dict with keys ``id``, ``uid``, ``title``, ``start``,
        ``end``, ``dtstart``, ``dtend``, ``all_day``, ``recurring``,
        ``location``, ``tzid``, ``status`` (the card's ``event_status``),
        ``rrule``, ``description``, ``editable``, ``tags`` (``[]`` when
        absent — the client's area toggles read them), ``hidden`` (the
        resolver's computed per-occurrence hide flag — series/point/ray —
        the client's default render skips it and the "show hidden" lens
        reveals it), ``series_member`` (this occurrence belongs to a
        recurring series — generated OR a promoted exception card — so the
        grid offers occurrence/from hide scopes even on an exception, whose
        ``recurring`` is ``False``), and the read-only detail fields the
        modal renders: ``organizer``, ``attendees`` (with
        ``attendees_omitted`` marking a Google-capped list), ``my_status``
        (the user's own PARTSTAT — declined events render struck through),
        card-level ``meeting_links``, ``conference`` entry points with
        ``conference_url`` as the pre-enrichment fallback link,
        ``attachments``, and ``gcal_link``.
    """
    yaml = occurrence.card.yaml
    return {
        "id": yaml["id"],
        "uid": yaml["uid"],
        "title": occurrence.card.title,
        "start": occurrence.start.isoformat(),
        "end": occurrence.end.isoformat(),
        "dtstart": yaml["dtstart"].isoformat(),
        "dtend": yaml["dtend"].isoformat(),
        "all_day": yaml["all_day"],
        "recurring": occurrence.recurring,
        "location": yaml.get("location"),
        "tzid": yaml.get("tzid"),
        "status": yaml["event_status"],
        "rrule": yaml.get("rrule"),
        "description": description_of(occurrence.card.body),
        "editable": _writable(yaml, synced),
        "tags": yaml.get("tags") or [],
        "hidden": occurrence.hidden,
        "series_member": occurrence.recurring or "recurrence_id" in yaml,
        "organizer": yaml.get("organizer"),
        "attendees": yaml.get("attendees") or [],
        "attendees_omitted": bool(yaml.get("attendees_omitted")),
        "my_status": yaml.get("my_status"),
        "conference": yaml.get("conference") or [],
        "conference_url": yaml.get("conference_url"),
        "meeting_links": yaml.get("meeting_links") or [],
        "attachments": yaml.get("attachments") or [],
        "gcal_link": yaml.get("gcal_link"),
    }


def occurrences_json(db, start, end, synced=()):
    """Resolve the window ``[start, end)`` and serialise it for the grid.

    Args:
        db: An open ``mddb.MDDB`` deck handle.
        start: Tz-aware window start (inclusive).
        end: Tz-aware window end (exclusive).
        synced: Mapping of synced source → Google calendar id.

    Returns:
        A list of `occurrence_json` dicts.
    """
    return [occurrence_json(o, synced) for o in events_in_window(db, start, end)]


def _wall_clock(value, tzid):
    """Parse a ``datetime-local`` form value as wall-clock in ``tzid``.

    The client sends its device's IANA zone with every form
    (``Intl.DateTimeFormat().resolvedOptions().timeZone``), so a create typed
    in Toronto lands at Toronto wall time — not a hardcoded home zone.
    ``fold=0``: a wall time in an autumn changeover's ambiguous hour resolves
    to the earlier instant.
    """
    naive = datetime.datetime.fromisoformat(value)
    if naive.tzinfo is not None:
        raise ValueError(f"form datetimes are wall-clock, not offset-carrying: {value}")
    try:
        zone = ZoneInfo(tzid)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown tzid: {tzid}") from error
    return naive.replace(tzinfo=zone)


def form_to_vevent(form, uid):
    """Build an ``icalendar.Event`` from validated form fields.

    Timed events carry the client's device zone as TZID; all-day events are
    DATE-valued with the user's inclusive end day converted to iCalendar's
    exclusive ``DTEND`` (+1 day). ``repeat`` is a fixed preset from
    ``RRULES`` — a free-form RRULE editor is deliberately not offered.

    Args:
        form: Dict with ``title``, ``start``, ``end``, ``all_day``, and
            optional ``location``, ``description``, ``repeat``.
        uid: The event's stable iCalendar UID.

    Returns:
        The ``icalendar.Event``, ready for ``vevent_to_card``.

    Raises:
        ValueError: A missing required field, unknown ``repeat`` preset,
            malformed dates, an offset-carrying timed value, or ``end``
            before ``start`` — all client errors, 400 at the HTTP boundary
            (``KeyError`` is reserved there for "no such card" → 404).
    """
    for key in ("title", "start", "end", "all_day"):
        if key not in form:
            raise ValueError(f"missing form field: {key}")
    repeat = form.get("repeat", "none")
    if repeat not in RRULES:
        raise ValueError(f"unknown repeat preset: {repeat}")
    start, end = _form_times(form)
    vevent = icalendar.Event()
    vevent.add("UID", uid)
    vevent.add("DTSTAMP", datetime.datetime.now(datetime.timezone.utc))
    vevent.add("SUMMARY", form["title"])
    vevent.add("STATUS", "CONFIRMED")
    vevent.add("DTSTART", start)
    vevent.add("DTEND", end)
    if RRULES[repeat]:
        vevent.add("RRULE", icalendar.vRecur.from_ical(RRULES[repeat]))
    if form.get("location"):
        vevent.add("LOCATION", form["location"])
    if form.get("description"):
        vevent.add("DESCRIPTION", form["description"])
    return vevent


def _form_times(form):
    """Parse and validate the form's start/end into VEVENT-ready values.

    All-day events are DATE-valued with the user's inclusive end day converted
    to iCalendar's exclusive ``DTEND`` (+1 day); timed events are wall-clock
    in the form's ``tzid``.

    Raises:
        ValueError: Missing ``tzid`` on a timed event, malformed dates, an
            offset-carrying timed value, or ``end`` before ``start``.
    """
    if form["all_day"]:
        start = datetime.date.fromisoformat(form["start"])
        end = datetime.date.fromisoformat(form["end"]) + datetime.timedelta(days=1)
        if end <= start:
            raise ValueError(f"end day before start day: {form['end']}")
    else:
        if "tzid" not in form:
            raise ValueError("missing form field: tzid")
        start = _wall_clock(form["start"], form["tzid"])
        end = _wall_clock(form["end"], form["tzid"])
        if end < start:
            raise ValueError(f"end before start: {form['end']}")
    return start, end


def _set(vevent, prop, value):
    """Replace (or, for a falsy value, remove) a property on a VEVENT."""
    if prop in vevent:
        del vevent[prop]
    if value:
        vevent.add(prop, value)


def update_vevent(vevent, form):
    """Apply the form's owned fields onto a card's existing VEVENT, in place.

    The edit-path counterpart of `form_to_vevent`: instead of rebuilding an
    event from the form's (smaller) vocabulary — which silently drops every
    property the form doesn't carry (ATTENDEE, ORGANIZER, TRANSP, Google
    provenance, non-preset RRULEs…) — the existing VEVENT is modified and
    everything unowned survives by construction.

    The form's recurrence and time fields are *lossy controls*, so they only
    apply on explicit intent:

    - ``repeat``: ``"keep"`` leaves RRULE and EXDATEs untouched (required
      vocabulary for events whose rule the presets can't express, e.g.
      Google's ``FREQ=WEEKLY;BYDAY=MO``); ``"none"`` removes both; a preset
      replaces the RRULE and drops old EXDATEs (exclusions are keyed to the
      old schedule — carrying them could silently suppress the new one).
    - ``time_changed``: only when true are DTSTART/DTEND rebuilt from the
      form; otherwise they stay verbatim — a title edit from a device in
      another timezone must not reserialise the event into that timezone.

    Raises:
        ValueError: Missing required field, ``repeat="keep"`` on a
            non-recurring event, an unknown preset, an ``all_day`` flip
            without ``time_changed``, or malformed times.
    """
    for key in ("title", "all_day", "repeat", "time_changed"):
        if key not in form:
            raise ValueError(f"missing form field: {key}")
    _set(vevent, "SUMMARY", form["title"])
    _set(vevent, "LOCATION", form.get("location") or None)
    _set(vevent, "DESCRIPTION", form.get("description") or None)
    repeat = form["repeat"]
    if repeat == "keep":
        if "RRULE" not in vevent:
            raise ValueError("repeat=keep on a non-recurring event")
    elif repeat in RRULES:
        _set(vevent, "EXDATE", None)
        _set(
            vevent,
            "RRULE",
            icalendar.vRecur.from_ical(RRULES[repeat]) if RRULES[repeat] else None,
        )
    else:
        raise ValueError(f"unknown repeat preset: {repeat}")
    was_all_day = not isinstance(vevent["DTSTART"].dt, datetime.datetime)
    if form["time_changed"]:
        start, end = _form_times(form)
        if "EXDATE" in vevent:
            _reanchor_exdates(vevent, start)
        _set(vevent, "DTSTART", start)
        _set(vevent, "DTEND", end)
    elif bool(form["all_day"]) != was_all_day:
        raise ValueError("all_day changed without time_changed")


def _remap_instant(instant, old_start, new_start, rule):
    """Carry one excluded/annotated occurrence instant across a series retime.

    An occurrence instant is keyed to the schedule that generated it; when the
    series' wall-clock time changes, the instant moves with its occurrence
    (same date, new time, the new series' zone) or it silently stops matching
    anything — the exclusion resurrects, the annotation goes dead. Only an
    instant the OLD rule actually generated has a defined new position:
    anything else (an excluded RDATE, orphaned junk) crashes rather than
    being guessed at.

    Raises:
        ValueError: The instant is not an occurrence the old rule generates.
    """
    local = instant.astimezone(old_start.tzinfo)
    if (
        local.time() != old_start.time()
        or rule.after(local - datetime.timedelta(seconds=1)) != local
    ):
        raise ValueError(
            f"exclusion {instant.isoformat()} is not an occurrence of the "
            f"old schedule: the retimed series cannot carry it — remove it "
            f"or recreate the series"
        )
    return datetime.datetime.combine(
        local.date(), new_start.time(), tzinfo=new_start.tzinfo
    )


def _reanchor_hidden(yaml, old_start, new_start):
    """Re-anchor the hide annotations' occurrence epochs across a retime.

    ``hidden_occurrences`` (epoch points) and ``hidden_from`` (epoch ray
    anchor) identify occurrences by instant exactly as EXDATEs do, and orphan
    the same way when the series' wall-clock time changes — the hidden
    occurrence would silently reappear. Returns the yaml keys to overwrite
    ({} when nothing applies: no retime, non-recurring, or no annotations).
    Runs BEFORE the Google push so a bad annotation aborts the edit with
    nothing written anywhere.

    Raises:
        ValueError: An all-day flip or date-anchor change on a series with
            hide annotations (same undefined-position rule as EXDATEs), or an
            annotation epoch that is not an old generated occurrence
            (`_remap_instant`).
    """
    if new_start == old_start or "rrule" not in yaml:
        return {}
    keys = [k for k in ("hidden_occurrences", "hidden_from") if k in yaml]
    if not keys:
        return {}
    old_timed = isinstance(old_start, datetime.datetime)
    if old_timed != isinstance(new_start, datetime.datetime):
        raise ValueError(
            "all_day change on a series with hide annotations is undefined"
        )
    if not old_timed or new_start.date() != old_start.date():
        raise ValueError(
            "date change on a series with hide annotations is undefined: "
            "unhide the occurrences or recreate the series"
        )
    rule = rrulestr(normalise_until(yaml["rrule"]), dtstart=old_start)

    def remap(epoch):
        instant = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
        return int(_remap_instant(instant, old_start, new_start, rule).timestamp())

    out = {}
    if "hidden_occurrences" in yaml:
        out["hidden_occurrences"] = [remap(e) for e in yaml["hidden_occurrences"]]
    if "hidden_from" in yaml:
        out["hidden_from"] = remap(yaml["hidden_from"])
    return out


def _reanchor_exdates(vevent, new_start):
    """Re-anchor a kept series' EXDATEs onto the retimed schedule, in place.

    Called before DTSTART/DTEND are replaced, so the VEVENT still carries the
    old schedule. Without this, a series retime leaves EXDATE instants
    orphaned at the old wall-clock time and every previously deleted
    occurrence silently resurrects (the 2026-07-08 supervision clobber).

    Raises:
        ValueError: The event or the form's times are all-day (date-valued
            exclusions don't survive a type or date change), the anchor DATE
            moved (exclusions are undefined under a different weekday), or an
            EXDATE is not an old generated occurrence (`_remap_instant`).
    """
    old_start = vevent["DTSTART"].dt
    if not isinstance(old_start, datetime.datetime) or not isinstance(
        new_start, datetime.datetime
    ):
        raise ValueError("all_day change on a series with exclusions is undefined")
    if new_start.date() != old_start.date():
        raise ValueError(
            "date change on a series with exclusions is undefined: "
            "remove the exclusions or recreate the series"
        )
    rule = rrulestr(
        normalise_until(vevent["RRULE"].to_ical().decode()), dtstart=old_start
    )
    raw = vevent.get("EXDATE")
    props = raw if isinstance(raw, list) else [raw]
    remapped = [
        _remap_instant(item.dt, old_start, new_start, rule)
        for prop in props
        for item in prop.dts
    ]
    _set(vevent, "EXDATE", None)
    for instant in remapped:
        vevent.add("EXDATE", instant)


def _committed(deck, mutate):
    """Run ``mutate(db)`` against a fresh deck handle, retrying on conflict.

    Each attempt opens a fresh ``mddb.MDDB`` (self-heals the cache to the new
    HEAD after a concurrent writer wins); the third ``ConflictError``
    propagates for the HTTP boundary to map to 409.
    """
    for attempt in (1, 2, 3):
        db = mddb.MDDB(deck)
        try:
            return mutate(db)
        except mddb.ConflictError:
            if attempt == 3:
                raise


def _writable(yaml, synced):
    """Local cards and synced-source cards — exceptions included.

    Synced RECURRENCE-ID exception cards share the master's iCalUID, so the
    uid-keyed push/delete must never touch them; their write paths go through
    ``gapi.patch`` (keyed by master + the card's own recurrence_id) instead —
    `_update_exception` and the exception branch of `delete_event` enforce
    that routing.
    """
    return yaml["source"] == SOURCE or yaml["source"] in synced


def _require_writable(card, synced):
    if not _writable(card.yaml, synced):
        raise PermissionError(f"read-only event: source={card.yaml['source']}")


def _area_tags(tags, form):
    """Merge the form's chosen ``area`` into a card's tag list.

    Replaces any existing ``area/*`` (an event has one area — its colour and
    the axis it's reviewed under), preserving every other tag (``mdcal/hidden``,
    …). Empty ``area`` clears it; an absent ``area`` key leaves it untouched (a
    non-area edit must not drop the tag). The picker is how the grid sets an
    event's area, since areas are colour you set, not a toggle.
    """
    if "area" not in form:
        return tags
    area = form["area"]
    if area and not area.startswith("area/"):
        raise ValueError(f"area must be an area/* tag or empty: {area!r}")
    kept = [t for t in tags if not t.startswith("area/")]
    return [*kept, area] if area else kept


def create_event(deck, form, synced_sources=frozenset(), synced=(), gapi=None):
    """Create an event card from form fields.

    When the target deck carries a synced source (``synced_sources``, the
    caller's routing for this deck) the event goes to Google FIRST
    (``gapi.push`` with our uid; failure propagates, nothing local) and the
    card gets the synced source + the returned ``dispatched`` guard — true by
    construction: Google accepted it before the deck did. Decks without a
    synced source keep the ``source: local`` path.

    Args:
        deck: Path to the mddb deck.
        form: See `form_to_vevent`.
        synced_sources: The set of synced sources configured for this deck.
        synced: Mapping of synced source → Google calendar id.
        gapi: Write-through namespace (``push``/``patch``/``delete``).

    Returns:
        The new card's mddb id.

    Raises:
        ValueError: The deck carries more than one synced source — create
            routing would be arbitrary; unsupported until a source selector
            exists (edits are unaffected: they route by the card's source).
    """
    if len(synced_sources) > 1:
        raise ValueError(
            f"deck has {len(synced_sources)} synced sources "
            f"({', '.join(sorted(synced_sources))}): create routing is ambiguous"
        )
    source = next(iter(synced_sources), SOURCE)
    uid = f"{uuid.uuid4()}@lovelace.fritz.box"
    vevent = form_to_vevent(form, uid)
    if source != SOURCE:
        vevent.add("SEQUENCE", 1)
        vevent.add("X-GOOGLE-CALENDAR-ID", synced[source])
    rendered = vevent_to_card(vevent, source)
    yaml = dict(rendered.yaml)
    if source != SOURCE:
        yaml["dispatched"] = gapi.push(synced[source], rendered)

    def mutate(db):
        with db.editor(rationale=f"web: create {rendered.title}") as editor:
            card = editor.create(
                title=rendered.title,
                summary=rendered.summary,
                tags=_area_tags(rendered.tags or [], form) or None,
                body=rendered.body,
                relpath=rendered.relpath,
                yaml=yaml,
            )
        return card.id

    return _committed(deck, mutate)


def update_event(deck, card_id, form, scope="series", start="", synced=(), gapi=None):
    """Edit a writable event at one of three scopes (Google's edit dialog).

    Scopes (anything else raises ``ValueError`` → 400):
        ``series``: rewrite the event/whole series (`_update_series`) — the
            only scope for a non-recurring standalone event. For a recurring
            master, the form's DATE is ignored and the master's anchor date
            kept: "all events" changes what every occurrence looks like
            (times are wall-clock re-anchored, exclusions carried by
            `_reanchor_exdates`), it does not move the series to the tapped
            occurrence's date.
        ``one``: change a single occurrence. On a recurring master this
            creates (or updates, if one already exists) an override card at
            the tapped slot (`_update_one`); tapping an existing override
            card edits it in place. Requires ``start``.
        ``from``: split the series at the tapped occurrence — earlier
            occurrences keep the old schedule, this-and-later move to a new
            master carrying the edit (`_update_from`). Requires ``start``.

    An override card (``recurrence_id``) IS one occurrence: only ``one``
    applies; ``series``/``from`` point at the master and raise.

    Args:
        deck: Path to the mddb deck.
        card_id: The card's mddb id.
        form: See `update_vevent` / `form_to_vevent`.
        scope: ``series`` | ``one`` | ``from``.
        start: For ``one``/``from``: the tapped occurrence's start as
            serialised by `occurrence_json`.
        synced: Mapping of synced source → Google calendar id.
        gapi: Write-through namespace (``push``/``patch``/``delete``).

    Returns:
        The edited (or created override / new master) card id.

    Raises:
        ValueError: Unknown scope, a scope/card-shape mismatch, or the
            refusals documented on the scope helpers.
        PermissionError: The card is read-only (unsynced import).
        KeyError: No card with that id.
    """
    if scope not in ("series", "one", "from"):
        raise ValueError(f"unknown scope: {scope}")
    existing = mddb.MDDB(deck).read(card_id)
    _require_writable(existing, synced)
    if "recurrence_id" in existing.yaml:
        if scope != "one":
            raise ValueError(
                "a recurrence exception is one occurrence: edit the master "
                "for series/from"
            )
        return _update_exception(deck, existing, form, synced, gapi)
    if scope == "series":
        return _update_series(deck, existing, form, synced, gapi)
    if "rrule" not in existing.yaml:
        raise ValueError(f"non-recurring event: scope={scope} needs a series")
    if scope == "one":
        return _update_one(deck, existing, form, start, synced, gapi)
    return _update_from(deck, existing, form, start, synced, gapi)


def _anchor_form(existing, form):
    """Pin a recurring master's series edit to its own anchor date.

    The modal prefills the TAPPED occurrence, so a series save would carry
    that occurrence's date and rebase the whole series onto it (and trip the
    exclusion date-change guard). "All events" means every occurrence changes
    shape, not position: keep the master's anchor date, take the form's
    times. All-day masters keep the form as-is (their dates ARE the times).
    """
    if not form.get("time_changed") or form["all_day"] or existing.yaml["all_day"]:
        return form
    anchor = existing.yaml["dtstart"].date()
    span = datetime.date.fromisoformat(form["end"][:10]) - datetime.date.fromisoformat(
        form["start"][:10]
    )
    return {
        **form,
        "start": f"{anchor.isoformat()}T{form['start'][11:]}",
        "end": f"{(anchor + span).isoformat()}T{form['end'][11:]}",
    }


def _update_series(deck, existing, form, synced, gapi):
    """Rewrite the event (or whole series) from the form — scope ``series``.

    The card's fenced VEVENT is modified in place (`update_vevent`) and
    re-rendered through the one card factory — every property the form
    doesn't own (ATTENDEE, ORGANIZER, non-preset RRULEs, EXDATEs under
    ``repeat="keep"``, Google provenance) survives verbatim, and times only
    change on explicit ``time_changed``. A date/title change shifts the
    date-led relpath prefix, so the card is moved to keep the filename
    meaningful — history follows the stable mddb id.

    Synced sources render once, push to Google FIRST (sequence bumped from
    the existing card — Google 400s stale sequences), then commit locally
    with the ``dispatched`` guard. The push happens exactly once, before
    `_committed`: conflict retries are local-only, and a conflict after
    Google success surfaces as 409 while the next feed poll converges the
    deck inbound.
    """
    card_id = existing.yaml["id"]
    source = existing.yaml["source"]
    vevent = fenced_vevent(existing.body)
    if "rrule" in existing.yaml:
        form = _anchor_form(existing, form)
    old_start = vevent["DTSTART"].dt
    update_vevent(vevent, form)
    hidden = _reanchor_hidden(existing.yaml, old_start, vevent["DTSTART"].dt)
    if source != SOURCE:
        _set(vevent, "SEQUENCE", int(existing.yaml.get("sequence", 0)) + 1)
    rendered = vevent_to_card(vevent, source)
    dispatched = gapi.push(synced[source], rendered) if source != SOURCE else None

    def mutate(db):
        base = db.head()
        card = db.read(card_id)
        _require_unmoved(card, existing)
        relpath = db.conn.execute(
            "SELECT relpath FROM entries WHERE id=?", (card_id,)
        ).fetchone()[0]
        apply_render(card, rendered)
        card.yaml.update(hidden)
        if dispatched is not None:
            card.yaml["dispatched"] = dispatched
        tags = _area_tags(card.yaml.get("tags", []), form)
        with db.editor(rationale=f"web: edit {rendered.title}", base=base) as editor:
            editor.update(card, summary=rendered.summary, tags=tags)
            if rendered.relpath != relpath:
                editor.move(card_id, rendered.relpath)
        return card_id

    return _committed(deck, mutate)


def _override_slot(master, start):
    """The tapped occurrence's ORIGINAL slot in the master's own terms.

    A ``date`` for an all-day master; otherwise the tz-aware instant rendered
    in the master's ``tzid`` — the zone imports serialise RECURRENCE-IDs in,
    so grid-created overrides are byte-identical in kind to imported ones.
    """
    if master.yaml["all_day"]:
        return datetime.date.fromisoformat(start)
    slot = datetime.datetime.fromisoformat(start)
    if slot.tzinfo is None:
        raise ValueError(f"occurrence start must carry a UTC offset: {start}")
    return slot.astimezone(ZoneInfo(master.yaml["tzid"]))


def _override_id(db, source, uid, slot):
    """The existing override card for ``(source, uid, slot)``, or ``None``."""
    row = db.conn.execute(
        "SELECT e.id FROM entries e "
        "JOIN entry_fields a ON a.entry_rowid=e.rowid "
        "AND a.key='recurrence_id_epoch' AND a.value_num=? "
        "JOIN entry_fields u ON u.entry_rowid=e.rowid "
        "AND u.key='uid' AND u.value_str=? "
        "JOIN entry_fields s ON s.entry_rowid=e.rowid "
        "AND s.key='source' AND s.value_str=?",
        (instant_epoch(slot), uid, source),
    ).fetchone()
    return row[0] if row else None


def _update_one(deck, master, form, start, synced, gapi):
    """Change one occurrence of a recurring master — scope ``one``.

    Creates an override card: the form's fields as a single concrete event
    (``repeat`` forced to ``none`` — an override is never itself recurring)
    carrying the master's ``uid``, ``RECURRENCE-ID`` = the tapped slot, and
    the master's fence enrichment (`_carry_enrichment` — the occurrence keeps
    its participants and meeting links; on a local deck no feed would ever
    restore them).
    The resolver's suppression map hides the master's generated slot on the
    override's existence alone, so the master is untouched — one create, one
    commit, ordinary undo. If an override already exists at that slot the
    edit applies to it instead (never two cards at one slot).

    Synced sources patch the Google instance FIRST (``gapi.patch`` — Google
    materialises the exception server-side and its ICS export renders the
    same RECURRENCE-ID VEVENT back); the local card carries the
    ``dispatched`` watermark.
    """
    if not start:
        raise ValueError("scope=one needs the occurrence start")
    source = master.yaml["source"]
    uid = master.yaml["uid"]
    slot = _override_slot(master, start)
    _require_generated(master, slot, start)
    existing_id = _override_id(mddb.MDDB(deck), source, uid, slot)
    if existing_id is not None:
        return _update_exception(
            deck, mddb.MDDB(deck).read(existing_id), form, synced, gapi
        )
    vevent = form_to_vevent({**form, "repeat": "none"}, uid)
    _carry_enrichment(fenced_vevent(master.body), vevent)
    vevent.add("RECURRENCE-ID", slot)
    if source != SOURCE:
        vevent.add("X-GOOGLE-CALENDAR-ID", synced[source])
    rendered = vevent_to_card(vevent, source)
    yaml = dict(rendered.yaml)
    if source != SOURCE:
        yaml["dispatched"] = gapi.patch(synced[source], uid, slot, rendered)
    tags = _area_tags(master.yaml.get("tags", []), form)

    def mutate(db):
        base = db.head()
        fresh = db.read(master.yaml["id"])
        _require_unmoved(fresh, master)
        if _override_id(db, source, uid, slot) is not None:
            raise mddb.ConflictError(f"override appeared concurrently at {start}")
        with db.editor(
            rationale=f"web: edit one occurrence of {rendered.title}", base=base
        ) as e:
            card = e.create(
                title=rendered.title,
                summary=rendered.summary,
                tags=tags or None,
                body=rendered.body,
                relpath=rendered.relpath,
                yaml=yaml,
            )
        return card.id

    return _committed(deck, mutate)


def _require_generated(master, slot, start):
    """Refuse a slot the master's rule does not generate.

    A stale tap (the series changed between open and save) would otherwise
    create an override whose slot suppresses nothing — an orphan extra event.
    The synced path gets this for free (Google 404s a missing instance); the
    local path must check. RDATE extras are occurrences too (the resolver
    suppresses them the same way). Mirrors ``_expand_master``'s anchoring:
    all-day rules generate at UTC midnight.
    """
    if instant_epoch(slot) in {instant_epoch(r) for r in master.yaml.get("rdate", [])}:
        return
    vevent = fenced_vevent(master.body)
    old_start = vevent["DTSTART"].dt
    if isinstance(old_start, datetime.datetime):
        candidate = slot.astimezone(old_start.tzinfo)
    else:
        old_start = datetime.datetime(
            old_start.year, old_start.month, old_start.day, tzinfo=datetime.timezone.utc
        )
        candidate = datetime.datetime(
            slot.year, slot.month, slot.day, tzinfo=datetime.timezone.utc
        )
    rule = rrulestr(
        normalise_until(vevent["RRULE"].to_ical().decode()), dtstart=old_start
    )
    if rule.after(candidate - datetime.timedelta(seconds=1)) != candidate:
        raise ValueError(f"{start} is not a generated occurrence of the series")


def _update_exception(deck, existing, form, synced, gapi):
    """Edit an override card in place — the only scope it has.

    The card's own fenced VEVENT (which carries its RECURRENCE-ID and
    provenance) goes through `update_vevent` with ``repeat`` forced to
    ``none``: an override is a single concrete event whatever the modal's
    repeat control claims. Synced overrides patch their Google instance
    (keyed by the master uid + the card's recurrence_id) — the write path
    that makes imported Google-side moved instances editable at last.
    """
    card_id = existing.yaml["id"]
    source = existing.yaml["source"]
    vevent = fenced_vevent(existing.body)
    update_vevent(vevent, {**form, "repeat": "none"})
    rendered = vevent_to_card(vevent, source)
    dispatched = (
        gapi.patch(
            synced[source],
            existing.yaml["uid"],
            existing.yaml["recurrence_id"],
            rendered,
        )
        if source != SOURCE
        else None
    )

    def mutate(db):
        base = db.head()
        card = db.read(card_id)
        _require_unmoved(card, existing)
        relpath = db.conn.execute(
            "SELECT relpath FROM entries WHERE id=?", (card_id,)
        ).fetchone()[0]
        apply_render(card, rendered)
        if dispatched is not None:
            card.yaml["dispatched"] = dispatched
        tags = _area_tags(card.yaml.get("tags", []), form)
        with db.editor(rationale=f"web: edit {rendered.title}", base=base) as editor:
            editor.update(card, summary=rendered.summary, tags=tags)
            if rendered.relpath != relpath:
                editor.move(card_id, rendered.relpath)
        return card_id

    return _committed(deck, mutate)


def _split_rule(vevent, slot_epoch):
    """Truncate the master's RRULE before the split, in place.

    Returns ``(rule_text, old_final)``: the ORIGINAL rule's text and, for a
    bounded rule (COUNT or UNTIL), its exact last generated occurrence —
    counted with dateutil, not guessed (``None`` when unbounded). The caller
    rebuilds the carried rule for the new master from these: a bound
    expressed at the OLD wall-clock time would silently drop the final
    occurrences once the future half is retimed later in the day.
    """
    old_start = vevent["DTSTART"].dt
    rule_text = vevent["RRULE"].to_ical().decode()
    recur = vevent["RRULE"]
    old_final = None
    if "COUNT" in recur or "UNTIL" in recur:
        old_final = list(rrulestr(normalise_until(rule_text), dtstart=old_start))[-1]
    recur.pop("COUNT", None)
    recur["UNTIL"] = [
        datetime.datetime.fromtimestamp(slot_epoch - 1, datetime.timezone.utc)
    ]
    _set(vevent, "RRULE", None)
    vevent.add("RRULE", recur)
    return rule_text, old_final


def _carried_rule(rule_text, old_final, old_start, new_start, old_rule):
    """The new master's RRULE after a split under ``repeat="keep"``.

    The original rule minus its bound; a bounded rule's end re-anchors with
    the schedule (`_remap_instant` of the old final occurrence) so the
    remaining occurrences all survive the retime.
    """
    recur = icalendar.vRecur.from_ical(rule_text)
    recur.pop("COUNT", None)
    recur.pop("UNTIL", None)
    if old_final is not None:
        new_final = _remap_instant(old_final, old_start, new_start, old_rule)
        recur["UNTIL"] = [new_final.astimezone(datetime.timezone.utc)]
    return recur


_FORM_OWNED = {
    "UID",
    "DTSTAMP",
    "SUMMARY",
    "STATUS",
    "SEQUENCE",
    "DTSTART",
    "DTEND",
    "RRULE",
    "EXDATE",
    "RDATE",
    "LOCATION",
    "DESCRIPTION",
}
"""Properties the split's new-master VEVENT is built from (form + rule code)."""

_PROVENANCE = {
    "RECURRENCE-ID",
    "X-GOOGLE-EVENT-ID",
    "X-GOOGLE-HTML-LINK",
    "X-GOOGLE-CALENDAR-ID",
    "ORGANIZER",
    "X-GOOGLE-CREATOR",
    "X-GOOGLE-SOURCE",
    "CREATED",
    "LAST-MODIFIED",
}
"""Identity and provenance of the OLD event — a split's new master earns its
own (fresh uid; Google assigns its ids, organizer, and timestamps)."""


def _carry_enrichment(old_vevent, new_vevent):
    """Copy every non-owned, non-provenance property across a split.

    A subtract-rule, not a list: the new future master keeps everything the
    old master carried (attendees, conference entry points, attachments,
    reminders, colour, visibility, guest flags, extended properties, event
    type…) except the form-owned properties it was just built from and the
    old event's identity — so the future half of "this and following" never
    sheds participants or meeting links, and the copy set grows with the
    capture map instead of silently narrowing.
    """
    for name in old_vevent.keys():
        if name in _FORM_OWNED or name in _PROVENANCE:
            continue
        value = old_vevent[name]
        for item in value if isinstance(value, list) else [value]:
            new_vevent.add(name, item, encode=0)


def _update_from(deck, master, form, start, synced, gapi):
    """Split the series at the tapped occurrence — scope ``from``.

    The old master's RRULE gains ``UNTIL = slot − 1s`` (its own times, tags
    and pre-split exclusions untouched); a NEW master with a fresh uid starts
    at the form's values and carries the edit forward — ``repeat="keep"``
    carries the original rule (COUNT converted to its exact UNTIL), a preset
    replaces it, ``none`` ends the recurrence at this one event. The new
    master also carries the old fence's enrichment (`_carry_enrichment`) —
    participants, meeting links, attachments — minus the old identity.
    Post-split EXDATEs move to the new master re-anchored (`_remap_instant`);
    post-split ``hidden_occurrences`` likewise. Both cards land in ONE editor
    commit — atomic locally, one undo token.

    Refusals (ValueError → 400, nothing written anywhere): masters with
    ``rdate`` (partitioning extra occurrences across a split is genuinely
    ambiguous), a ``hidden_from`` ray (it spans the split — unhide first),
    and a slot that isn't a generated occurrence.

    Synced ordering is load-bearing: the NEW future master is inserted into
    Google FIRST, then the truncated old master is pushed — a failure between
    the writes leaves duplicates, never lost future occurrences; on old-push
    failure the new Google master is best-effort deleted before the 502.
    """
    if not start:
        raise ValueError("scope=from needs the occurrence start")
    if master.yaml["all_day"]:
        raise ValueError(
            "this-and-following on an all-day series is not supported: "
            "edit the whole series or single occurrences"
        )
    if "rdate" in master.yaml:
        raise ValueError(
            "this-and-following on a series with RDATEs is not supported: "
            "the extra occurrences cannot be split unambiguously"
        )
    if "hidden_from" in master.yaml:
        raise ValueError(
            "this-and-following on a series with a hide-from ray is not "
            "supported: unhide the series first"
        )
    card_id = master.yaml["id"]
    source = master.yaml["source"]
    is_synced = source != SOURCE
    slot = _override_slot(master, start)
    slot_epoch = instant_epoch(slot)
    old_vevent = fenced_vevent(master.body)
    old_start = old_vevent["DTSTART"].dt
    old_rule = rrulestr(
        normalise_until(old_vevent["RRULE"].to_ical().decode()), dtstart=old_start
    )
    if isinstance(slot, datetime.datetime) and old_rule.after(
        slot - datetime.timedelta(seconds=1)
    ) != slot.astimezone(old_start.tzinfo):
        raise ValueError(f"{start} is not a generated occurrence of the series")
    rule_text, old_final = _split_rule(old_vevent, slot_epoch)

    new_uid = f"{uuid.uuid4()}@lovelace.fritz.box"
    new_vevent = form_to_vevent({**form, "repeat": "none"}, new_uid)
    repeat = form["repeat"]
    new_start = new_vevent["DTSTART"].dt
    if repeat == "keep":
        if new_start.date() != slot.date():
            raise ValueError(
                "date change with repeat=keep is undefined for "
                "this-and-following (the carried rule is anchored to the old "
                "weekday pattern): choose a repeat preset or keep the date"
            )
        new_vevent.add(
            "RRULE",
            _carried_rule(rule_text, old_final, old_start, new_start, old_rule),
        )
    elif repeat in RRULES:
        if RRULES[repeat]:
            new_vevent.add("RRULE", icalendar.vRecur.from_ical(RRULES[repeat]))
    else:
        raise ValueError(f"unknown repeat preset: {repeat}")
    _carry_enrichment(old_vevent, new_vevent)

    kept_hidden, moved_hidden = [], []
    for epoch in master.yaml.get("hidden_occurrences", []):
        (kept_hidden if epoch < slot_epoch else moved_hidden).append(epoch)
    raw = old_vevent.get("EXDATE")
    kept_ex, moved_raw = [], []
    if raw is not None:
        props = raw if isinstance(raw, list) else [raw]
        for item in (i for p in props for i in p.dts):
            (kept_ex if instant_epoch(item.dt) < slot_epoch else moved_raw).append(
                item.dt
            )
    if (moved_raw or moved_hidden) and new_start.date() != slot.date():
        raise ValueError(
            "date-moving split with post-split exclusions or hidden "
            "occurrences is undefined (they are keyed to the old dates): "
            "remove them or keep the date"
        )
    if raw is not None:
        moved_ex = [
            _remap_instant(instant, old_start, new_start, old_rule)
            for instant in moved_raw
        ]
        _set(old_vevent, "EXDATE", None)
        for instant in kept_ex:
            old_vevent.add("EXDATE", instant)
        for instant in moved_ex:
            new_vevent.add("EXDATE", instant)
    new_hidden = [
        int(
            _remap_instant(
                datetime.datetime.fromtimestamp(e, datetime.timezone.utc),
                old_start,
                new_start,
                old_rule,
            ).timestamp()
        )
        for e in moved_hidden
    ]

    if is_synced:
        _set(old_vevent, "SEQUENCE", int(master.yaml.get("sequence", 0)) + 1)
        new_vevent.add("SEQUENCE", 1)
        new_vevent.add("X-GOOGLE-CALENDAR-ID", synced[source])
    old_rendered = vevent_to_card(old_vevent, source)
    new_rendered = vevent_to_card(new_vevent, source)
    old_dispatched = new_dispatched = None
    if is_synced:
        new_dispatched = gapi.push(synced[source], new_rendered)
        try:
            old_dispatched = gapi.push(synced[source], old_rendered)
        except Exception:
            gapi.delete(synced[source], new_uid)
            raise
    tags = _area_tags(master.yaml.get("tags", []), form)

    def mutate(db):
        base = db.head()
        card = db.read(card_id)
        _require_unmoved(card, master)
        apply_render(card, old_rendered)
        if kept_hidden or moved_hidden:
            if kept_hidden:
                card.yaml["hidden_occurrences"] = kept_hidden
            else:
                card.yaml.pop("hidden_occurrences", None)
        if old_dispatched is not None:
            card.yaml["dispatched"] = old_dispatched
        new_yaml = dict(new_rendered.yaml)
        if new_hidden:
            new_yaml["hidden_occurrences"] = new_hidden
        if new_dispatched is not None:
            new_yaml["dispatched"] = new_dispatched
        rationale = f"web: edit this and following {new_rendered.title}"
        with db.editor(rationale=rationale, base=base) as editor:
            editor.update(card, summary=old_rendered.summary)
            new_card = editor.create(
                title=new_rendered.title,
                summary=new_rendered.summary,
                tags=tags or None,
                body=new_rendered.body,
                relpath=new_rendered.relpath,
                yaml=new_yaml,
            )
        return new_card.id

    return _committed(deck, mutate)


def _require_unmoved(card, snapshot):
    """Card-scoped guard for the pre-read → Google-push → local-commit span.

    The render (and any Google push) was built from ``snapshot``; if the card
    changed underneath — another writer, or the feed poll updating this very
    card — committing the stale render would silently overwrite that write.
    A deck-HEAD base can't express this: it would also 409 on unrelated
    commits (the hourly poll routinely lands other cards mid-request), so
    the guard compares this card only. `_committed` retries re-read fresh;
    a genuine same-card change persists and surfaces as 409, while Google
    already holds the push — self-healing inbound via the next poll.
    """
    if (card.yaml, card.body) != (snapshot.yaml, snapshot.body):
        raise mddb.ConflictError(f"card changed since read: {card.yaml['id']}")


def delete_event(deck, card_id, scope, start="", synced=(), gapi=None):
    """Delete a writable event, an occurrence of it, or its whole series.

    Scopes (anything else raises ``ValueError`` → 400):
        ``event``: delete a non-recurring standalone card. Rejected for a
            recurring master — that requires ``series`` or ``one``, so a
            master can't be deleted out from under its exception cards.
        ``one``: hide one generated occurrence of a recurring master by
            appending its start to the master's ``EXDATE`` (the VEVENT is
            reparsed from the card's fenced ics block, so the flat yaml and
            the fence move together). Rejected for a non-recurring card —
            the resolver applies ``exdate`` only during master expansion, so
            the write would commit yet change nothing.
        ``from``: delete the tapped occurrence and everything after it
            (`_delete_from`) — Google's "this and following". Requires a
            recurring master and ``start``.
        ``series``: delete the master and every same-source card sharing
            its uid (exception cards included).

    Local sources delete cards outright. Synced sources go to Google FIRST
    (delete by iCalUID, or an EXDATE upsert for ``one``); locally, deleted
    cards become ``STATUS:CANCELLED`` re-renders carrying the ``dispatched``
    guard instead of being removed — the resolver hides CANCELLED so the
    grid updates instantly, and prune collects the cards once the feed drops
    the uid. Series deletes cancel every same-source card in one editor
    block so exception cards can't linger visibly until the feed catches up.

    Args:
        deck: Path to the mddb deck.
        card_id: The card's mddb id.
        scope: One of ``event``, ``one``, ``from``, ``series``.
        start: For ``one``/``from``: the occurrence start as serialised by
            `occurrence_json` (bare date for all-day, offset ISO otherwise).
        synced: Mapping of synced source → Google calendar id.
        gapi: Write-through namespace (``push``/``patch``/``delete``).

    Raises:
        ValueError: Unknown scope, ``event`` on a recurring master, a
            malformed/naive ``start``, or the `_delete_from` refusals.
        PermissionError: The card is read-only (unsynced import).
        KeyError: No card with that id.
    """
    if scope not in ("event", "one", "from", "series"):
        raise ValueError(f"unknown scope: {scope}")
    existing = mddb.MDDB(deck).read(card_id)
    _require_writable(existing, synced)
    source = existing.yaml["source"]
    is_synced = source != SOURCE
    if "recurrence_id" in existing.yaml:
        if scope != "one":
            raise ValueError(
                "a recurrence exception is one occurrence: delete with "
                "scope=one, or delete from/the series on the master"
            )
        return _delete_exception(deck, existing, synced, gapi)
    if scope == "event" and "rrule" in existing.yaml:
        raise ValueError("recurring master: use scope=series or scope=one")
    if scope in ("one", "from") and "rrule" not in existing.yaml:
        raise ValueError("non-recurring event: use scope=event")
    if scope == "from":
        return _delete_from(deck, existing, start, synced, gapi)

    if scope == "one":
        vevent = fenced_vevent(existing.body)
        vevent.add("EXDATE", _occurrence_start(existing, start))
        if is_synced:
            if "SEQUENCE" in vevent:
                del vevent["SEQUENCE"]
            vevent.add("SEQUENCE", int(existing.yaml.get("sequence", 0)) + 1)
        rendered = vevent_to_card(vevent, source)
        dispatched = gapi.push(synced[source], rendered) if is_synced else None

        def mutate(db):
            base = db.head()
            card = db.read(card_id)
            _require_unmoved(card, existing)
            apply_render(card, rendered)
            if dispatched is not None:
                card.yaml["dispatched"] = dispatched
            rationale = f"web: delete one occurrence of {rendered.title}"
            with db.editor(rationale=rationale, base=base) as editor:
                editor.update(card, summary=rendered.summary)

        return _committed(deck, mutate)

    dispatched = (
        gapi.delete(synced[source], existing.yaml["uid"]) if is_synced else None
    )

    def mutate(db):
        base = db.head()
        card = db.read(card_id)
        _require_unmoved(card, existing)
        if scope == "event":
            ids = [card_id]
        else:
            ids = [
                each
                for (each,) in db.conn.execute(
                    "SELECT e.id FROM entries e "
                    "JOIN entry_fields u ON u.entry_rowid=e.rowid "
                    "AND u.key='uid' AND u.value_str=? "
                    "JOIN entry_fields s ON s.entry_rowid=e.rowid "
                    "AND s.key='source' AND s.value_str=?",
                    (card.yaml["uid"], source),
                ).fetchall()
            ]
        rationale = (
            f"web: delete {card.title}"
            if scope == "event"
            else f"web: delete series {card.title}"
        )
        with db.editor(rationale=rationale, base=base) as editor:
            for each in ids:
                if is_synced:
                    victim = editor.read(each)
                    rendered = _cancelled_render(victim, source)
                    apply_render(victim, rendered)
                    victim.yaml["dispatched"] = dispatched
                    editor.update(victim, summary=rendered.summary)
                else:
                    editor.delete(each)

    return _committed(deck, mutate)


def _delete_from(deck, master, start, synced, gapi):
    """Delete the tapped occurrence and everything after it — scope ``from``.

    The master's RRULE gains ``UNTIL = slot − 1s`` (COUNT converts to its
    exact UNTIL first, via `_split_rule`; no new master — nothing survives
    the cut). Annotations partition by the boundary: EXDATEs and
    ``hidden_occurrences`` before the slot survive (they annotate surviving
    occurrences), at/after it drop; a ``hidden_from`` ray keeps its anchor
    when it starts BEFORE the boundary — a "stopped attending from here"
    must keep hiding the surviving tail — and drops only when the anchor
    itself is cut off. Post-boundary override cards die with their
    occurrences: deleted locally, and for synced sources each is explicitly
    instance-cancelled (``gapi.patch``) — never trusting Google to drop
    modified instances on master truncation; a surviving confirmed instance
    would re-import and resurrect the deleted occurrence.

    Synced ordering: instance-cancels FIRST, then the truncated master push —
    a mid-failure leaves a still-whole series with some occurrences
    cancelled (self-consistent), never a truncated master with orphaned
    confirmed instances. One editor commit → one undo token. The doomed
    overrides are snapshot-guarded like the master (`_require_unmoved`, plus
    a set re-query so an override created mid-request can't survive the
    cut): a concurrent writer surfaces as 409, never a silent lost update.

    Raises:
        ValueError: Missing ``start``, an all-day or RDATE master, a
            non-generated slot, or the first occurrence (that is
            scope=series).
    """
    if not start:
        raise ValueError("scope=from needs the occurrence start")
    if master.yaml["all_day"]:
        raise ValueError(
            "delete this-and-following on an all-day series is not "
            "supported: delete the series or single occurrences"
        )
    if "rdate" in master.yaml:
        raise ValueError(
            "delete this-and-following on a series with RDATEs is not "
            "supported: the extra occurrences cannot be split unambiguously"
        )
    card_id = master.yaml["id"]
    source = master.yaml["source"]
    is_synced = source != SOURCE
    slot = _override_slot(master, start)
    _require_generated(master, slot, start)
    slot_epoch = instant_epoch(slot)
    if slot_epoch <= instant_epoch(master.yaml["dtstart"]):
        raise ValueError("deleting from the first occurrence: use scope=series")

    vevent = fenced_vevent(master.body)
    _split_rule(vevent, slot_epoch)
    raw = vevent.get("EXDATE")
    if raw is not None:
        props = raw if isinstance(raw, list) else [raw]
        kept = [
            item.dt
            for prop in props
            for item in prop.dts
            if instant_epoch(item.dt) < slot_epoch
        ]
        _set(vevent, "EXDATE", None)
        for instant in kept:
            vevent.add("EXDATE", instant)
    if is_synced:
        _set(vevent, "SEQUENCE", int(master.yaml.get("sequence", 0)) + 1)
    rendered = vevent_to_card(vevent, source)

    def post_boundary(db):
        return {
            row_id
            for (row_id, epoch) in db.conn.execute(
                "SELECT e.id, a.value_num FROM entries e "
                "JOIN entry_fields a ON a.entry_rowid=e.rowid "
                "AND a.key='recurrence_id_epoch' "
                "JOIN entry_fields u ON u.entry_rowid=e.rowid "
                "AND u.key='uid' AND u.value_str=? "
                "JOIN entry_fields s ON s.entry_rowid=e.rowid "
                "AND s.key='source' AND s.value_str=?",
                (master.yaml["uid"], source),
            ).fetchall()
            if epoch >= slot_epoch
        }

    db = mddb.MDDB(deck)
    doomed = {each: db.read(each) for each in post_boundary(db)}
    cancelled = {}
    dispatched = None
    if is_synced:
        for override_id, victim in doomed.items():
            render = _cancelled_render(victim, source)
            stamp = gapi.patch(
                synced[source],
                victim.yaml["uid"],
                victim.yaml["recurrence_id"],
                render,
            )
            cancelled[override_id] = (render, stamp)
        dispatched = gapi.push(synced[source], rendered)

    def mutate(db):
        base = db.head()
        card = db.read(card_id)
        _require_unmoved(card, master)
        if post_boundary(db) != set(doomed):
            raise mddb.ConflictError("post-boundary overrides changed since read")
        apply_render(card, rendered)
        kept_hidden = [
            e for e in card.yaml.get("hidden_occurrences", []) if e < slot_epoch
        ]
        if kept_hidden:
            card.yaml["hidden_occurrences"] = kept_hidden
        else:
            card.yaml.pop("hidden_occurrences", None)
        if card.yaml.get("hidden_from", slot_epoch) >= slot_epoch:
            card.yaml.pop("hidden_from", None)
        if dispatched is not None:
            card.yaml["dispatched"] = dispatched
        rationale = f"web: delete this and following {card.title}"
        with db.editor(rationale=rationale, base=base) as editor:
            editor.update(card, summary=rendered.summary)
            for override_id, snapshot in doomed.items():
                victim = editor.read(override_id)
                _require_unmoved(victim, snapshot)
                if is_synced:
                    render, stamp = cancelled[override_id]
                    apply_render(victim, render)
                    victim.yaml["dispatched"] = stamp
                    editor.update(victim, summary=render.summary)
                else:
                    editor.delete(override_id)

    return _committed(deck, mutate)


def _delete_exception(deck, existing, synced, gapi):
    """Delete the occurrence an override card carries.

    Synced: the Google instance is patched to ``cancelled`` (Google's ICS
    export folds a cancelled instance into a master EXDATE, so the feed
    converges); locally the override becomes a CANCELLED render with the
    ``dispatched`` guard — the resolver's suppression still hides the
    master's slot, ``_concrete`` hides the cancelled card, and prune collects
    it once the feed catches up. The master is untouched (Google owns it).

    Local: the override is deleted outright AND the master gains an EXDATE
    at the override's slot in the same commit — without it the master would
    regenerate the slot the override was suppressing.
    """
    card_id = existing.yaml["id"]
    source = existing.yaml["source"]
    slot = existing.yaml["recurrence_id"]
    if source != SOURCE:
        cancelled = _cancelled_render(existing, source)
        dispatched = gapi.patch(synced[source], existing.yaml["uid"], slot, cancelled)

        def mutate(db):
            base = db.head()
            card = db.read(card_id)
            _require_unmoved(card, existing)
            apply_render(card, cancelled)
            card.yaml["dispatched"] = dispatched
            rationale = f"web: delete one occurrence of {card.title}"
            with db.editor(rationale=rationale, base=base) as editor:
                editor.update(card, summary=cancelled.summary)

        return _committed(deck, mutate)

    def mutate(db):
        base = db.head()
        card = db.read(card_id)
        _require_unmoved(card, existing)
        master_id = _override_master_id(db, existing)
        master = db.read(master_id)
        vevent = fenced_vevent(master.body)
        vevent.add("EXDATE", slot)
        rendered = vevent_to_card(vevent, source)
        apply_render(master, rendered)
        rationale = f"web: delete one occurrence of {card.title}"
        with db.editor(rationale=rationale, base=base) as editor:
            editor.delete(card_id)
            editor.update(master, summary=rendered.summary)

    return _committed(deck, mutate)


def _override_master_id(db, override):
    """The recurring master's card id for an override's ``(source, uid)``."""
    row = db.conn.execute(
        "SELECT e.id FROM entries e "
        "JOIN entry_fields r ON r.entry_rowid=e.rowid AND r.key='rrule' "
        "JOIN entry_fields u ON u.entry_rowid=e.rowid "
        "AND u.key='uid' AND u.value_str=? "
        "JOIN entry_fields s ON s.entry_rowid=e.rowid "
        "AND s.key='source' AND s.value_str=?",
        (override.yaml["uid"], override.yaml["source"]),
    ).fetchone()
    if row is None:
        raise KeyError(f"no recurring master for {override.yaml['uid']}")
    return row[0]


def undo_token(deck):
    """The just-committed mutation's undo handle: this deck at its new HEAD.

    Read after `_committed` returns; `undo_event` re-validates everything at
    undo time, so a racing commit merely makes the token unusable (409),
    never wrong.
    """
    return {"deck": deck, "commit": mddb.MDDB(deck).head()}


def undo_event(deck, commit, synced=(), gapi=None):
    """Revert the deck's HEAD commit iff it is the given web mutation.

    Guarded two ways, each refusing with 409 rather than guessing: the deck's
    HEAD must still BE the token's commit (a poll or another edit landing
    after it makes undo unavailable — reverting a non-HEAD commit in a live
    deck is a merge problem, not an undo), and the commit's rationale must
    carry a ``web: ``/``repair: `` prefix (never revert an import).

    A commit touching only local cards is a plain ``git revert``. A commit
    touching synced cards is inverted by write-through (`_undo_synced`):
    Google first — restore each prior card state (``gapi.push`` /
    ``gapi.patch``), delete what the commit created — then ONE editor
    commit writing the inverse local states. A raw revert would silently
    diverge from Google until the feed converged it back, undoing the undo;
    and the inverse local state isn't the old blob anyway (it carries a new
    sequence and dispatched watermark).

    Args:
        deck: Path to the mddb deck.
        commit: The undo token's commit (the deck HEAD to revert).
        synced: Mapping of synced source → Google calendar id.
        gapi: Write-through namespace (``push``/``patch``/``delete``).

    Raises:
        mddb.ConflictError: Undo unavailable, with the reason.
    """
    if mddb.MDDB(deck).head() != commit:
        raise mddb.ConflictError("undo unavailable: the calendar changed since")
    show = subprocess.run(
        [
            "git",
            "-C",
            deck,
            "show",
            "--name-only",
            "--no-renames",
            "--format=%s",
            commit,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if not show[0].startswith(("web: ", "repair: ")):
        raise mddb.ConflictError(f"undo unavailable: not a web change: {show[0]!r}")
    cards = _commit_cards(deck, commit, [line for line in show[1:] if line])
    states = [
        state
        for before, after, _, _ in cards.values()
        for state in (before, after)
        if state is not None
    ]
    if not any(state.yaml["source"] in synced for state in states):
        if any(
            state.yaml["source"] != SOURCE
            and ("dispatched" in state.yaml or "gcal_calendar" in state.yaml)
            for state in states
        ):
            raise mddb.ConflictError(
                "undo unavailable: the card carries Google provenance but its "
                "source is not configured as synced — a local revert would "
                "silently diverge from Google"
            )
        subprocess.run(
            ["git", "-C", deck, "revert", "--no-edit", commit],
            check=True,
            capture_output=True,
        )
        return
    _undo_synced(deck, commit, cards, show[0], synced, gapi)


def _commit_cards(deck, commit, paths):
    """The commit's touched cards as ``{id: (before, after, before_relpath, after_relpath)}``.

    Grouped by card id, not path, so an ``editor.move`` (which shows as a
    delete at the old path plus an add at the new) collapses back into one
    card's before/after pair.
    """
    cards = {}
    for path in paths:
        for rev, slot in ((f"{commit}~1", 0), (commit, 1)):
            blob = subprocess.run(
                ["git", "-C", deck, "show", f"{rev}:{path}"],
                capture_output=True,
                text=True,
            )
            if blob.returncode != 0:
                continue
            card = mddb.Card.from_text(blob.stdout)
            entry = cards.setdefault(card.yaml["id"], [None, None, None, None])
            entry[slot] = card
            entry[slot + 2] = path
    return {cid: tuple(entry) for cid, entry in cards.items()}


def _undo_synced(deck, commit, cards, subject, synced, gapi):
    """Inverse write-through for a commit touching synced cards.

    Google order is load-bearing: prior states are RESTORED first (masters
    before exceptions — an exception's instance needs its live master), then
    commit-created cards are removed — the same never-remove-before-the-
    replacement-exists principle as the series split, so a failure part-way
    leaves duplicates or stale content, never lost events. Every inverse is
    idempotent (upsert by iCalUID / instance patch), so a retried undo
    converges.
    """
    restores = sorted(
        (entry for entry in cards.values() if entry[0] is not None),
        key=lambda entry: "recurrence_id" in entry[0].yaml,
    )
    removals = [entry for entry in cards.values() if entry[0] is None]
    dispatched = {}
    rendered = {}
    for before, after, _, _ in restores:
        source = before.yaml["source"]
        if source not in synced:
            continue
        if "recurrence_id" in before.yaml:
            dispatched[before.yaml["id"]] = gapi.patch(
                synced[source],
                before.yaml["uid"],
                before.yaml["recurrence_id"],
                before,
            )
            rendered[before.yaml["id"]] = vevent_to_card(
                fenced_vevent(before.body), source
            )
        else:
            vevent = fenced_vevent(before.body)
            current = int((after or before).yaml.get("sequence", 0))
            _set(vevent, "SEQUENCE", current + 1)
            restored = vevent_to_card(vevent, source)
            dispatched[before.yaml["id"]] = gapi.push(synced[source], restored)
            rendered[before.yaml["id"]] = restored
    for _, after, _, _ in removals:
        source = after.yaml["source"]
        if source not in synced:
            continue
        if "recurrence_id" in after.yaml:
            _reset_instance(deck, after, synced, gapi)
        else:
            gapi.delete(synced[source], after.yaml["uid"])

    def mutate(db):
        if db.head() != commit:
            raise mddb.ConflictError("undo unavailable: the calendar changed since")
        rationale = f"web: undo {subject.split(': ', 1)[1]}"
        with db.editor(rationale=rationale, base=commit) as editor:
            for before, after, before_path, after_path in cards.values():
                if before is None:
                    editor.delete(after.yaml["id"])
                    continue
                card_id = before.yaml["id"]
                if after is None:
                    editor.create(
                        title=before.title,
                        summary=before.summary,
                        tags=before.yaml.get("tags"),
                        body=before.body,
                        relpath=before_path,
                        yaml=dict(before.yaml),
                    )
                    continue
                card = db.read(card_id)
                card.yaml = dict(before.yaml)
                card.body = before.body
                render = rendered.get(card_id)
                if render is not None:
                    if "sequence" in render.yaml:
                        card.yaml["sequence"] = render.yaml["sequence"]
                    card.body = render.body
                if card_id in dispatched:
                    card.yaml["dispatched"] = dispatched[card_id]
                editor.update(card, summary=before.summary)
                if before_path != after_path:
                    editor.move(card_id, before_path)

    _committed(deck, mutate)


def _reset_instance(deck, override, synced, gapi):
    """Undo a created override: patch its Google instance back to the series default.

    The 'no override' state for an instance is the master's own values at
    the original slot — rebuilt from the live master card and patched over
    the instance, which is how Google models resetting an occurrence.
    """
    db = mddb.MDDB(deck)
    master = db.read(_override_master_id(db, override))
    slot = override.yaml["recurrence_id"]
    duration = master.yaml["dtend"] - master.yaml["dtstart"]
    vevent = icalendar.Event()
    vevent.add("UID", master.yaml["uid"])
    vevent.add("SUMMARY", master.title)
    vevent.add("STATUS", "CONFIRMED")
    vevent.add("DTSTART", slot)
    vevent.add("DTEND", slot + duration)
    if master.yaml.get("location"):
        vevent.add("LOCATION", master.yaml["location"])
    description = description_of(master.body)
    if description:
        vevent.add("DESCRIPTION", description)
    vevent.add("RECURRENCE-ID", slot)
    source = override.yaml["source"]
    gapi.patch(
        synced[source],
        master.yaml["uid"],
        slot,
        vevent_to_card(vevent, source),
    )


def set_hidden(deck, card_id, hidden, scope="series", start=""):
    """Toggle a rendering hide annotation on an event, at one of three scopes.

    All three are non-owned annotations (the tag-annotation exception): they
    work on feed-sourced content-read-only cards — hiding a CASGR seminar is
    the feature's whole point — and mutate no EVENT_KEYS field, so no Google
    push, sequence bump, or render. The resolver (`mdcal.window`) reads them.

    Scopes (mirroring delete's granularities; anything else → ``ValueError``):
        ``series``: toggle the ``mdcal/hidden`` tag on the recurring MASTER
            (resolved from ``card_id`` when it is an exception), or on
            ``card_id`` itself for a single event — hides the whole series.
            The catch-all.
        ``one``: add/remove the occurrence's instant in the recurring MASTER's
            ``hidden_occurrences`` epoch list — hides just this one. Rejected
            for a non-recurring event (nothing to key an instant against).
        ``from``: set/clear the MASTER's ``hidden_from`` epoch — hides this
            occurrence and every later one (a ray). For a feed series you
            stopped attending, where a split is impossible.

    ``one``/``from`` annotate the master even when ``card_id`` is an exception
    card (the resolver applies a master's policy to its exceptions), so they
    resolve the rrule-bearing card of the same ``uid`` + ``source``.

    Args:
        deck: Path to the mddb deck.
        card_id: The tapped occurrence's card id.
        hidden: Desired state; idempotent in both directions.
        scope: ``series`` | ``one`` | ``from``.
        start: For ``one``/``from``: the occurrence start as `occurrence_json`
            serialised it (bare date for all-day, offset ISO otherwise).

    Returns:
        The card id.

    Raises:
        ValueError: Unknown scope, or ``one``/``from`` on a non-recurring event.
        KeyError: No card with that id.
    """
    if scope not in ("series", "one", "from"):
        raise ValueError(f"unknown scope: {scope}")

    def mutate(db):
        base = db.head()
        card = db.read(card_id)
        if not hidden:
            return _reveal(db, card, start, base)
        if scope == "series":
            target = _master_card_or_none(db, card) or card
            tags = [*(t for t in target.yaml.get("tags", []) if t != "mdcal/hidden")]
            tags.append("mdcal/hidden")
            with db.editor(rationale=f"web: hide {target.title}", base=base) as editor:
                editor.update(target, summary=target.summary, tags=tags)
            return card_id
        master = _master_card(db, card)
        epoch = _occurrence_epoch(start, master.yaml["all_day"])
        if scope == "one":
            points = [
                e for e in master.yaml.get("hidden_occurrences", []) if e != epoch
            ]
            master.yaml["hidden_occurrences"] = sorted([*points, epoch])
            verb = "hide one of"
        else:
            master.yaml["hidden_from"] = epoch
            verb = "hide from within"
        with db.editor(rationale=f"web: {verb} {master.title}", base=base) as editor:
            editor.update(master, summary=master.summary)
        return card_id

    return _committed(deck, mutate)


def _reveal(db, card, start, base):
    """Un-hide the tapped occurrence, reversing whichever scope hid it.

    A single inverse for all three hide scopes, so the grid never needs the
    server to report *why* an occurrence was hidden: drop this card's own
    ``mdcal/hidden`` tag (single event / series tag), and — if recurring —
    drop this instant from the master's ``hidden_occurrences`` and clear a
    ``hidden_from`` ray that covers it (revealing the tail from here on). An
    already-visible occurrence changes nothing and opens no editor.
    """
    new_tags = [t for t in card.yaml.get("tags", []) if t != "mdcal/hidden"]
    tag_changed = new_tags != (card.yaml.get("tags") or [])

    master = card if "rrule" in card.yaml else _master_card_or_none(db, card)
    master_changed = False
    master_tags = None
    if master is not None and master is not card:
        master_tags = [t for t in master.yaml.get("tags", []) if t != "mdcal/hidden"]
        if master_tags != (master.yaml.get("tags") or []):
            master_changed = True
    if master is not None:
        epoch = _occurrence_epoch(start, master.yaml["all_day"])
        points = [e for e in master.yaml.get("hidden_occurrences", []) if e != epoch]
        if points != master.yaml.get("hidden_occurrences", []):
            if points:
                master.yaml["hidden_occurrences"] = points
            else:
                master.yaml.pop("hidden_occurrences", None)
            master_changed = True
        ray = master.yaml.get("hidden_from")
        if ray is not None and epoch >= ray:
            master.yaml.pop("hidden_from")
            master_changed = True

    if not (tag_changed or master_changed):
        return card.id
    with db.editor(rationale=f"web: reveal {card.title}", base=base) as editor:
        if master is card:
            editor.update(card, summary=card.summary, tags=new_tags)
        else:
            if tag_changed:
                editor.update(card, summary=card.summary, tags=new_tags)
            if master_changed:
                editor.update(master, summary=master.summary, tags=master_tags)
    return card.id


def _master_card_or_none(db, card):
    row = db.conn.execute(
        "SELECT e.id FROM entries e "
        "JOIN entry_fields u ON u.entry_rowid=e.rowid AND u.key='uid' AND u.value_str=? "
        "JOIN entry_fields s ON s.entry_rowid=e.rowid AND s.key='source' AND s.value_str=? "
        "JOIN entry_fields r ON r.entry_rowid=e.rowid AND r.key='rrule'",
        (card.yaml["uid"], card.yaml["source"]),
    ).fetchone()
    return db.read(row[0]) if row else None


def _master_card(db, card):
    """The recurring master for ``card``.

    Itself if it has an rrule, else the rrule-bearing card sharing its ``uid``
    + ``source``.

    Raises:
        ValueError: The event is not recurring (no master to annotate).
    """
    if "rrule" in card.yaml:
        return card
    master = _master_card_or_none(db, card)
    if master is None:
        raise ValueError("non-recurring event: only scope=series applies")
    return master


def _occurrence_epoch(start, all_day):
    """The occurrence start's UTC-second epoch.

    Parses `occurrence_json`'s serialised start (bare date for all-day,
    offset ISO otherwise) and epochs it via the shared `instant_epoch`
    convention (all-day → UTC-midnight).
    """
    if all_day:
        return instant_epoch(datetime.date.fromisoformat(start))
    instant = datetime.datetime.fromisoformat(start)
    if instant.tzinfo is None:
        raise ValueError(f"occurrence start must carry a UTC offset: {start}")
    return instant_epoch(instant)


def _cancelled_render(card, source):
    """Re-render a card's fenced VEVENT with ``STATUS:CANCELLED``.

    Going through the card factory keeps the flat yaml, the ``[cancelled]``
    summary prefix, and the fence coherent — the one-factory discipline.
    """
    vevent = fenced_vevent(card.body)
    if "STATUS" in vevent:
        del vevent["STATUS"]
    vevent.add("STATUS", "CANCELLED")
    return vevent_to_card(vevent, source)


def _occurrence_start(card, value):
    """Parse a ``scope=one`` occurrence start to the master's own type.

    All-day masters exclude by date; timed masters by instant.
    """
    if card.yaml["all_day"]:
        return datetime.date.fromisoformat(value)
    occurrence = datetime.datetime.fromisoformat(value)
    if occurrence.tzinfo is None:
        raise ValueError(f"occurrence start must carry a UTC offset: {value}")
    return _utc_instant(occurrence)


def _utc_instant(value):
    """Normalise a timed exclusion to UTC before it becomes an ``EXDATE``.

    UTC serialises in Z-form; a fixed-offset tz would emit a ``TZID`` param
    that does not reparse. Bare dates (all-day exclusions) pass through.
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive datetime not supported: {value!r}")
        return value.astimezone(datetime.timezone.utc)
    return value
