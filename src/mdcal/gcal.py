"""Google Calendar write-through for synced feed sources.

Pushes event cards to the Google Calendar API keyed on the card's own
``uid`` — merge-safe: an existing live master is PATCHED (owned fields with
explicit clearing forms; Google-owned fields preserved server-side), an
absent one is imported with the card's full fence enrichment — and
deletes by iCalUID lookup. Google is primary: callers mutate the deck only
after these functions return, carrying the returned watermark as the card's
``dispatched`` guard so the lagging ICS feed can't revert the write before it
catches up (measured lag ~2 minutes; feed ``LAST-MODIFIED`` equals the API's
``updated`` at second precision).

Credential-agnostic: callers pass a ``google.oauth2.credentials.Credentials``.
This module is the only part of mdcal that imports the Google client
libraries (the ``mdcal[gcal]`` extra); ``mdcal.ics``/``mdcal.window`` stay
free of them.
"""

import argparse
import datetime as _dt
import json as _json
import re as _re
from urllib.parse import urlsplit as _urlsplit
from zoneinfo import ZoneInfo

import httplib2
import icalendar
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

_TIMEOUT = 10
"""Seconds before a Google HTTP call fails rather than hanging its caller.

The push/delete calls run synchronously inside web request handlers; an
unbounded external call would pin a handler thread indefinitely, so a hung
Google becomes a visible failure instead.
"""


def _service(credentials):
    return build(
        "calendar",
        "v3",
        http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=_TIMEOUT)),
    )


def _rfc3339(value):
    """Parse a Google RFC3339 timestamp (``Z`` normalised for Python 3.10)."""
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _watermark(updated):
    """Parse the API's RFC3339 ``updated`` into the ``dispatched`` guard value.

    Truncated to whole seconds — the feed's ``LAST-MODIFIED`` carries second
    precision and equals ``updated`` exactly at that precision (measured), so
    the importer's ``last_modified < dispatched`` comparison converges the
    moment the feed catches up.
    """
    stamp = _rfc3339(updated)
    if stamp.tzinfo is None:
        raise ValueError(f"Google updated timestamp is naive: {updated!r}")
    return stamp.replace(microsecond=0)


def push_event(credentials, calendar_id, card):
    """Upsert a card's event in Google, keyed on the card's ``uid``.

    Merge-safe: an EXISTING live master is PATCHED, never imported —
    ``events.import`` replaces the whole resource (proven live: one import
    wiped the attendees, colour, and reminders Google held but the card did
    not carry). The patch body always sends the mdcal-owned fields with
    explicit clearing forms (``None``/``[]``; omitting a cleared field would
    leave Google's value standing for the feed to re-import) and never the
    Google-owned ones (attendees, conferenceData, reminders, colorId,
    visibility, attachments, extendedProperties, guest flags), which patch
    preserves server-side. When no live master exists (create, or an
    undo-restore after a Google-side delete) there is nothing to preserve:
    ``events.import`` sends the card's full state including its fence
    enrichment (`_event_body`) — live probes show every captured class
    round-trips through import.

    Sends the card's ``sequence`` verbatim — the caller owns the increment
    (Google rejects stale sequences with a 400, so the caller must bump it
    from the existing card before rendering).

    Args:
        credentials: A ``google.oauth2.credentials.Credentials``.
        calendar_id: The target Google calendar id.
        card: Any card-shaped object with ``title``, ``yaml``, ``body``
            (an ``mdcal.ics.RenderedCard`` or an ``mddb.Card``).

    Returns:
        The tz-aware ``dispatched`` watermark (the API's ``updated``,
        second-truncated).

    Raises:
        ValueError: The card's fence carries ``X-GOOGLE-ATTENDEES-OMITTED``
            and no live master exists — a known-incomplete attendee list
            must not seed a new Google event.
    """
    service = _service(credentials)
    items = (
        service.events()
        .list(calendarId=calendar_id, iCalUID=card.yaml["uid"], showDeleted=True)
        .execute()["items"]
    )
    live = [
        item
        for item in items
        if not item.get("recurringEventId") and item["status"] != "cancelled"
    ]
    if live:
        result = (
            service.events()
            .patch(calendarId=calendar_id, eventId=live[0]["id"], body=_patch_body(card))
            .execute()
        )
    else:
        body = _event_body(card)
        kwargs = {}
        if "attachments" in body:
            kwargs["supportsAttachments"] = True
        if "conferenceData" in body:
            kwargs["conferenceDataVersion"] = 1
        result = (
            service.events()
            .import_(calendarId=calendar_id, body=body, **kwargs)
            .execute()
        )
    return _watermark(result["updated"])


def delete_event(credentials, calendar_id, uid):
    """Delete the event with iCalUID ``uid`` from a Google calendar.

    Resolves the Google event id by iCalUID (selecting the master — the item
    without ``recurringEventId``; Google models RECURRENCE-ID overrides as
    sibling items sharing the iCalUID). Already-gone is success (idempotent
    retry, e.g. after a Google-success/local-conflict 409).

    Args:
        credentials: A ``google.oauth2.credentials.Credentials``.
        calendar_id: The Google calendar id.
        uid: The event's iCalUID.

    Returns:
        The tz-aware ``dispatched`` watermark: the deleted item's ``updated``
        (Google serves deleted items as ``status: cancelled`` with a usable
        ``updated``), or local UTC now when the event is already gone — the
        stale feed VEVENT's ``LAST-MODIFIED`` predates the delete by far more
        than any NTP skew, so local time is a sufficient guard there.
    """
    service = _service(credentials)

    def masters():
        items = (
            service.events()
            .list(calendarId=calendar_id, iCalUID=uid, showDeleted=True)
            .execute()["items"]
        )
        return [item for item in items if not item.get("recurringEventId")]

    live = [item for item in masters() if item["status"] != "cancelled"]
    if live:
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=live[0]["id"]
            ).execute()
        except HttpError as error:
            if error.resp.status not in (404, 410):
                raise
    cancelled = [item for item in masters() if item["status"] == "cancelled"]
    if cancelled:
        return _watermark(cancelled[0]["updated"])
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def export_ics(credentials, calendar_id):
    """Fetch an OWNED Google calendar via the API and synthesize its `.ics`.

    The inbound counterpart to `push_event`: for calendars we hold a
    credential to, the OAuth API replaces the secret-iCal-URL feed — no
    per-calendar read token to harvest, store, or rotate. Returns the same
    kind of full-span VCALENDAR the secret feed serves, so the existing
    ``mdcal-import --prune`` consumes it unchanged.

    The identity set is reconstructed to MATCH Google's own ICS export, not
    the raw API item stream (verified against the research feed): the API
    returns each cancelled recurring instance as its own ``status: cancelled``
    child resource, whereas the ICS export folds it into an ``EXDATE`` on the
    master. So cancelled children become master EXDATEs here (not cards),
    confirmed children become ``RECURRENCE-ID`` VEVENTs, and singles/masters
    map straight across. Every other ``cancelled`` item is SKIPPED — Google
    also serves skeletal tombstones (no ``iCalUID``, sometimes no parent
    link) even with ``showDeleted=false``; the feed omits them too, and
    omission from the identity set is exactly a prune — yielding the same ``(uid, recurrence_id)`` set the
    secret feed would, which is what keeps ``--prune`` safe on a source
    switched from URL to API.

    Pages are fetched to exhaustion BEFORE any VCALENDAR is built: a failed
    page raises (crash-on-drift) rather than handing ``--prune`` a partial
    identity set that would mass-delete the unfetched remainder. The
    synthesis is FULL-FIDELITY over the Google Events resource: every field
    is mapped to an ICS property/param or deliberately excluded per
    `COMPLETENESS`, and `_known` crashes on anything outside that table, so
    a Google schema addition fails the sync loudly instead of silently
    dropping data. Raw ICS stays the reference for URL feeds.

    Args:
        credentials: A ``google.oauth2.credentials.Credentials``.
        calendar_id: The Google calendar id to export.

    Returns:
        The synthesized VCALENDAR as a ``str``.
    """
    service = _service(credentials)
    default_tz = service.calendars().get(calendarId=calendar_id).execute()["timeZone"]
    items, page = [], None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                singleEvents=False,
                showDeleted=False,
                maxResults=2500,
                pageToken=page,
            )
            .execute()
        )
        items.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    zone_of = {
        item["id"]: item["start"].get("timeZone", default_tz)
        for item in items
        if "recurrence" in item
    }
    excluded = {}
    for item in items:
        parent = item.get("recurringEventId")
        if parent and item["status"] == "cancelled":
            excluded.setdefault(parent, []).append(item["originalStartTime"])

    cal = icalendar.Calendar()
    cal.add("prodid", "-//mdcal//google-export//EN")
    cal.add("version", "2.0")
    for item in items:
        if item["status"] == "cancelled":
            continue
        parent = item.get("recurringEventId")
        master_zone = zone_of.get(item["id"]) or zone_of.get(parent) or default_tz
        cal.add_component(
            _item_to_vevent(
                item, excluded.get(item["id"], []), default_tz, master_zone, calendar_id
            )
        )
    return cal.to_ical().decode()


COMPLETENESS = {
    "event": (
        frozenset({
            "id", "iCalUID", "status", "summary", "description", "location",
            "start", "end", "recurrence", "recurringEventId",
            "originalStartTime", "transparency", "visibility", "sequence",
            "attendees", "attendeesOmitted", "organizer", "creator",
            "created", "updated", "htmlLink", "colorId", "hangoutLink",
            "conferenceData", "reminders", "attachments",
            "extendedProperties", "eventType", "guestsCanModify",
            "guestsCanInviteOthers", "guestsCanSeeOtherGuests",
            "anyoneCanAddSelf", "source", "workingLocationProperties",
            "outOfOfficeProperties", "focusTimeProperties",
            "birthdayProperties",
        }),
        frozenset({
            "kind", "etag",
            "endTimeUnspecified",
            "privateCopy", "locked",
            "gadget",
        }),
    ),
    "time": (frozenset({"date", "dateTime", "timeZone"}), frozenset()),
    "person": (
        frozenset({"email"}),
        frozenset({"displayName", "self", "id"}),
    ),
    "attendee": (
        frozenset({
            "email", "displayName", "responseStatus", "optional", "resource",
            "additionalGuests", "self", "comment",
        }),
        frozenset({"organizer", "id"}),
    ),
    "conferenceData": (
        frozenset({"entryPoints", "conferenceSolution", "conferenceId", "notes"}),
        frozenset({"createRequest", "signature", "parameters"}),
    ),
    "conference entry point": (
        frozenset({
            "entryPointType", "uri", "label", "pin", "accessCode",
            "meetingCode", "passcode", "password", "regionCode",
        }),
        frozenset(),
    ),
    "conference solution": (
        frozenset({"name", "key"}),
        frozenset({"iconUri"}),
    ),
    "conference solution key": (frozenset({"type"}), frozenset()),
    "attachment": (
        frozenset({"fileUrl", "title", "mimeType", "fileId"}),
        frozenset({"iconLink"}),
    ),
    "reminders": (frozenset({"useDefault", "overrides"}), frozenset()),
    "reminder override": (frozenset({"method", "minutes"}), frozenset()),
    "source": (frozenset({"url", "title"}), frozenset()),
}
"""The Google Events resource, exhaustively: every field mapped or excluded.

The audit artifact for "are we capturing everything Google stores". Each
entry is ``(mapped, excluded)``; `_known` crashes on any field outside the
union, so a Google schema addition turns the hourly sync red instead of
silently dropping data — the fix is a one-line entry here plus its mapping.

Exclusion rationales:
    event.kind/etag: transport artifacts, not event content.
    event.endTimeUnspecified: derivable from start/end.
    event.privateCopy/locked: server-side ACL state, not event content.
    event.gadget: deprecated by Google.
    person.displayName/self/id: organizer/creator identity is the email;
        the rest is directory display and ACL plumbing.
    attendee.organizer: derivable from the top-level ORGANIZER property.
    attendee.id: profile-directory plumbing.
    conferenceData.createRequest/signature/parameters: conference-request
        lifecycle plumbing, not the conference itself — a live probe showed
        the minimal copy body (entryPoints + conferenceId + solution
        name+key) round-trips through ``events.import`` without them.
    conference solution.iconUri: service branding; ``name`` is the display
        content and ``key`` is REQUIRED for the copy round-trip (the same
        probe: a name-only solution makes Google drop the conference).
    attachment.iconLink: display plumbing derivable from the file.

Structures carried VERBATIM as compact JSON need no nested entry — the
whole bag is preserved by construction: extendedProperties and the
eventType property bags (workingLocation/outOfOffice/focusTime/birthday).

reminders.useDefault=true is represented by ABSENCE (calendar-level
default, not event content); useDefault=false renders each override, or
``X-GOOGLE-REMINDERS:NONE`` when there are none — reminders explicitly off
is event content and must stay distinguishable from the default.

attendeesOmitted renders as ``X-GOOGLE-ATTENDEES-OMITTED:TRUE``: the
captured attendee list is INCOMPLETE (Google caps served guest lists) —
consumers must never treat it as the full invite list.

visibility=default emits no CLASS (RFC 5545 has no DEFAULT token) and is
excluded as calendar-default; CLASS is capture/display-only for web edits.
"""

_STATUS = {"confirmed": "CONFIRMED", "tentative": "TENTATIVE", "cancelled": "CANCELLED"}
_PARTSTAT = {
    "accepted": "ACCEPTED",
    "declined": "DECLINED",
    "tentative": "TENTATIVE",
    "needsAction": "NEEDS-ACTION",
}
_CLASS = {"public": "PUBLIC", "private": "PRIVATE", "confidential": "CONFIDENTIAL"}
_TRANSP = {"opaque": "OPAQUE", "transparent": "TRANSPARENT"}
_BAGS = {
    "workingLocationProperties": "X-GOOGLE-WORKING-LOCATION-PROPS",
    "outOfOfficeProperties": "X-GOOGLE-OUT-OF-OFFICE-PROPS",
    "focusTimeProperties": "X-GOOGLE-FOCUS-TIME-PROPS",
    "birthdayProperties": "X-GOOGLE-BIRTHDAY-PROPS",
}
_GUEST_FLAGS = (
    "guestsCanModify",
    "guestsCanInviteOthers",
    "guestsCanSeeOtherGuests",
    "anyoneCanAddSelf",
)
_GOOGLE_STATUS = {ical: google for google, ical in _STATUS.items()}
_GOOGLE_PARTSTAT = {ical: google for google, ical in _PARTSTAT.items()}
_GOOGLE_CLASS = {ical: google for google, ical in _CLASS.items()}
_GOOGLE_TRANSP = {ical: google for google, ical in _TRANSP.items()}


def _known(obj, context):
    """Crash on any Google resource field outside the completeness table."""
    mapped, excluded = COMPLETENESS[context]
    unknown = set(obj) - mapped - excluded
    if unknown:
        raise ValueError(
            f"unmapped Google {context} field(s) {sorted(unknown)}: "
            "map or exclude them in mdcal.gcal.COMPLETENESS"
        )


def _enum(value, mapping, context):
    """Map an enumerated Google value, crashing on an unknown one."""
    try:
        return mapping[value]
    except KeyError:
        raise ValueError(f"unknown Google {context}: {value!r}") from None


def _compact(value):
    return _json.dumps(value, separators=(",", ":"), sort_keys=True)


def _gtime(gtime, tz):
    """A Google start/end/originalStartTime → (value, is_date).

    All-day is a bare ``date``; a timed point is converted to ``tz`` as a
    zone-aware ``datetime``, so icalendar serialises it ``;TZID=<tz>:<local>``.
    ``RECURRENCE-ID``/``EXDATE`` pass the MASTER's zone (Google's ICS export
    renders an exception's id in the series' display zone, not the exception's
    own — they can differ, and mddb's identity is the serialised string), while
    ``DTSTART``/``DTEND`` pass the point's own zone or the calendar default.
    """
    _known(gtime, "time")
    if "date" in gtime:
        return _dt.date.fromisoformat(gtime["date"]), True
    return _rfc3339(gtime["dateTime"]).astimezone(ZoneInfo(tz)), False


def _item_to_vevent(item, excluded, default_tz, master_zone, calendar_id):
    _known(item, "event")
    vevent = icalendar.Event()
    vevent.add("UID", item["iCalUID"])
    vevent.add("DTSTAMP", _dt.datetime.now(_dt.timezone.utc))
    vevent.add("SUMMARY", item.get("summary", ""))
    vevent.add("STATUS", _enum(item["status"], _STATUS, "event status"))
    vevent.add("SEQUENCE", item.get("sequence", 0))

    start, all_day = _gtime(item["start"], item["start"].get("timeZone", default_tz))
    end, _ = _gtime(item["end"], item["end"].get("timeZone", default_tz))
    vevent.add("DTSTART", start)
    vevent.add("DTEND", end)

    for line in item.get("recurrence", []):
        _add_recurrence_line(vevent, line)
    for original in excluded:
        point, _ = _gtime(original, master_zone)
        vevent.add("EXDATE", point)
    if item.get("recurringEventId"):
        rid, _ = _gtime(item["originalStartTime"], master_zone)
        vevent.add("RECURRENCE-ID", rid)

    if item.get("transparency"):
        vevent.add("TRANSP", _enum(item["transparency"], _TRANSP, "transparency"))
    if item.get("visibility", "default") != "default":
        vevent.add("CLASS", _enum(item["visibility"], _CLASS, "visibility"))
    if item.get("location"):
        vevent.add("LOCATION", item["location"])
    if item.get("description"):
        vevent.add("DESCRIPTION", item["description"])

    conference = item.get("hangoutLink") or next(
        (
            entry["uri"]
            for entry in item.get("conferenceData", {}).get("entryPoints", [])
            if entry.get("entryPointType") == "video"
        ),
        None,
    )
    if conference:
        vevent.add("X-GOOGLE-CONFERENCE", conference)
    data = item.get("conferenceData")
    if data:
        _known(data, "conferenceData")
        for entry in data.get("entryPoints", []):
            _known(entry, "conference entry point")
            params = {"TYPE": entry["entryPointType"]}
            for key, param in (
                ("label", "LABEL"),
                ("pin", "PIN"),
                ("accessCode", "ACCESS-CODE"),
                ("meetingCode", "MEETING-CODE"),
                ("passcode", "PASSCODE"),
                ("password", "PASSWORD"),
                ("regionCode", "REGION-CODE"),
            ):
                if entry.get(key):
                    params[param] = entry[key]
            vevent.add("X-GOOGLE-CONFERENCE-ENTRY", entry["uri"], parameters=params)
        if data.get("conferenceId"):
            vevent.add("X-GOOGLE-CONFERENCE-ID", data["conferenceId"])
        if data.get("notes"):
            vevent.add("X-GOOGLE-CONFERENCE-NOTES", data["notes"])
        if data.get("conferenceSolution"):
            solution = data["conferenceSolution"]
            _known(solution, "conference solution")
            params = {}
            if solution.get("key"):
                _known(solution["key"], "conference solution key")
                params["X-GOOGLE-KEY-TYPE"] = solution["key"]["type"]
            vevent.add(
                "X-GOOGLE-CONFERENCE-SOLUTION", solution["name"], parameters=params
            )

    organizer = item.get("organizer", {})
    if organizer:
        _known(organizer, "person")
    if organizer.get("email"):
        vevent.add("ORGANIZER", f"mailto:{organizer['email']}")
    creator = item.get("creator", {})
    if creator:
        _known(creator, "person")
    if creator.get("email") and creator.get("email") != organizer.get("email"):
        vevent.add("X-GOOGLE-CREATOR", creator["email"])

    for attendee in item.get("attendees", []):
        _known(attendee, "attendee")
        address = icalendar.vCalAddress(f"mailto:{attendee['email']}")
        if attendee.get("displayName"):
            address.params["CN"] = attendee["displayName"]
        if "responseStatus" in attendee:
            address.params["PARTSTAT"] = _enum(
                attendee["responseStatus"], _PARTSTAT, "attendee responseStatus"
            )
        if attendee.get("optional"):
            address.params["ROLE"] = "OPT-PARTICIPANT"
        if attendee.get("resource"):
            address.params["CUTYPE"] = "RESOURCE"
        if attendee.get("additionalGuests"):
            address.params["X-NUM-GUESTS"] = str(attendee["additionalGuests"])
        if attendee.get("self"):
            address.params["X-GOOGLE-SELF"] = "TRUE"
        if attendee.get("comment"):
            address.params["X-GOOGLE-COMMENT"] = attendee["comment"]
        vevent.add("ATTENDEE", address, encode=0)
    if item.get("attendeesOmitted"):
        vevent.add("X-GOOGLE-ATTENDEES-OMITTED", "TRUE")

    for attachment in item.get("attachments", []):
        _known(attachment, "attachment")
        params = {}
        if attachment.get("mimeType"):
            params["FMTTYPE"] = attachment["mimeType"]
        if attachment.get("title"):
            params["FILENAME"] = attachment["title"]
        if attachment.get("fileId"):
            params["X-GOOGLE-FILE-ID"] = attachment["fileId"]
        vevent.add("ATTACH", attachment["fileUrl"], parameters=params)

    reminders = item.get("reminders")
    if reminders:
        _known(reminders, "reminders")
        if not reminders.get("useDefault", False):
            overrides = reminders.get("overrides", [])
            for override in overrides:
                _known(override, "reminder override")
                vevent.add(
                    "X-GOOGLE-REMINDER", f"{override['method']},{override['minutes']}"
                )
            if not overrides:
                vevent.add("X-GOOGLE-REMINDERS", "NONE")

    if item.get("colorId"):
        vevent.add("X-GOOGLE-COLOR-ID", item["colorId"])
    flags = {key: item[key] for key in _GUEST_FLAGS if key in item}
    if flags:
        vevent.add("X-GOOGLE-GUESTS-CAN", _compact(flags))
    if item.get("extendedProperties"):
        vevent.add("X-GOOGLE-EXTENDED-PROPS", _compact(item["extendedProperties"]))
    if item.get("eventType", "default") != "default":
        vevent.add("X-GOOGLE-EVENT-TYPE", item["eventType"])
    for key, prop in _BAGS.items():
        if key in item:
            vevent.add(prop, _compact(item[key]))
    source = item.get("source")
    if source:
        _known(source, "source")
        params = {"TITLE": source["title"]} if source.get("title") else {}
        vevent.add("X-GOOGLE-SOURCE", source["url"], parameters=params)

    vevent.add("CREATED", _rfc3339(item["created"]))
    vevent.add("LAST-MODIFIED", _rfc3339(item["updated"]))
    # Provenance back to the Google source: the event id, the calendar it came
    # from (which `source: local` alone no longer records), and a click-back
    # link. Carried as X- props so `vevent_to_card` can flatten them onto the
    # card without inventing a private card format.
    vevent.add("X-GOOGLE-EVENT-ID", item["id"])
    vevent.add("X-GOOGLE-CALENDAR-ID", calendar_id)
    if item.get("htmlLink"):
        vevent.add("X-GOOGLE-HTML-LINK", item["htmlLink"])
    return vevent


def _add_recurrence_line(vevent, line):
    """Add one API ``recurrence[]`` line (``RRULE``/``EXDATE``/``RDATE``).

    ``EXDATE``/``RDATE`` values are date/date-time lists whose ``TZID``/
    ``VALUE=DATE`` params carry identity — they must not go through the
    ``RRULE`` parser. An unrecognised property crashes rather than
    misparsing.
    """
    name, value = line.split(":", 1)
    key, _, param_text = name.partition(";")
    if key == "RRULE":
        vevent.add(key, icalendar.prop.vRecur.from_ical(value))
        return
    if key not in ("EXDATE", "RDATE"):
        raise ValueError(f"unsupported recurrence line: {line!r}")
    params = dict(p.split("=", 1) for p in param_text.split(";") if p)
    tz = params.get("TZID")
    points = [
        icalendar.prop.vDDDTypes.from_ical(v, timezone=tz) for v in value.split(",")
    ]
    vevent.add(key, points)


def _fields_body(card):
    """The card's owned Google fields, patch-shaped: explicit clearing forms.

    ``location``/``description`` are ALWAYS present, ``None`` when the card
    lacks them — ``events.patch`` merges, so omission would leave Google's
    old value standing and the lagging feed would re-import it onto the very
    card that just cleared it (live-probed: ``None`` clears).
    """
    yaml = card.yaml
    body = {
        "summary": card.title,
        "status": _enum(yaml["status"], _GOOGLE_STATUS, "event status"),
    }
    if yaml["all_day"]:
        body["start"] = {"date": yaml["dtstart"].isoformat()}
        body["end"] = {"date": yaml["dtend"].isoformat()}
    else:
        body["start"] = {
            "dateTime": yaml["dtstart"].isoformat(),
            "timeZone": yaml["tzid"],
        }
        body["end"] = {"dateTime": yaml["dtend"].isoformat(), "timeZone": yaml["tzid"]}
    body["location"] = yaml.get("location") or None
    description = card.body.split("```ics", 1)[0].strip()
    body["description"] = description or None
    return body


def _patch_body(card):
    """The merge-safe patch body: owned fields only, clearing forms included.

    ``recurrence`` is always present — ``[]`` clears a rule the card no
    longer has (repeat → none; live-probed). Google-owned fields are never
    sent; patch preserves them server-side.
    """
    return {
        **_fields_body(card),
        "sequence": card.yaml["sequence"],
        "recurrence": _recurrence_lines(card.body),
    }


def _event_body(card):
    """Map a card to a full Google event resource for ``events.import``.

    Import replaces, so this is the create-shaped body: owned scalars from
    the flat yaml (``None`` clearing forms stripped — omission and clearing
    coincide under replace semantics), ``recurrence`` lines verbatim from
    the card's fenced VEVENT (unfolded; re-serialising EXDATEs from the flat
    yaml would re-normalise their form), and the fence's full enrichment
    reverse-mapped to API fields (`_enrichment_body`).
    """
    yaml = card.yaml
    body = {
        "iCalUID": yaml["uid"],
        "sequence": yaml["sequence"],
        **{k: v for k, v in _fields_body(card).items() if v is not None},
        **_enrichment_body(_fence_event(card.body)),
    }
    recurrence = _recurrence_lines(card.body)
    if recurrence:
        body["recurrence"] = recurrence
    return body


def _fence_event(body):
    fence = _re.search(r"```ics\n(.*?)\n```", body, _re.DOTALL).group(1)
    return icalendar.Event.from_ical(fence)


def _fence_props(vevent, name):
    raw = vevent.get(name)
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


_ENTRY_PARAMS = (
    ("LABEL", "label"),
    ("PIN", "pin"),
    ("ACCESS-CODE", "accessCode"),
    ("MEETING-CODE", "meetingCode"),
    ("PASSCODE", "passcode"),
    ("PASSWORD", "password"),
    ("REGION-CODE", "regionCode"),
)


def _conference_solution_for(uri):
    """The conferenceSolution a bare join URI implies.

    Google drops entryPoints without a conferenceSolution (live-probed), so
    a link captured without one — Google's own feed exports and
    pre-enrichment cards — earns the solution its URI states: a meet URI IS
    Google Meet; anything else uses Google's generic ``addOn`` type, which
    round-trips verbatim (probed with a Zoom URI).
    """
    host = _urlsplit(uri).netloc
    if host == "meet.google.com":
        return {"name": "Google Meet", "key": {"type": "hangoutsMeet"}}
    return {"name": host or uri, "key": {"type": "addOn"}}


def _enrichment_body(vevent):
    """Fence enrichment properties → Google API fields, the capture inverse.

    Runs only where there is no server state to preserve (import/create):
    live probes show attendees round-trip without invitations (import has no
    ``sendUpdates``), attachments/reminders/colour/visibility/guest flags/
    extended properties survive verbatim, and a conference copies from
    entryPoints + conferenceId + solution name+key.

    Raises:
        ValueError: The fence carries ``X-GOOGLE-ATTENDEES-OMITTED`` — the
            attendee list is known-incomplete (Google capped it) and must
            not seed a new event that pretends it is the full invite list.
    """
    if vevent.get("X-GOOGLE-ATTENDEES-OMITTED"):
        raise ValueError(
            "attendee list is incomplete (attendeesOmitted): refusing to "
            "seed a new Google event from it"
        )
    body = {}
    attendees = []
    for prop in _fence_props(vevent, "ATTENDEE"):
        params = prop.params
        attendee = {"email": _re.sub(r"^mailto:", "", str(prop), flags=_re.I)}
        if params.get("CN"):
            attendee["displayName"] = str(params["CN"])
        if params.get("PARTSTAT"):
            attendee["responseStatus"] = _enum(
                str(params["PARTSTAT"]), _GOOGLE_PARTSTAT, "PARTSTAT"
            )
        if str(params.get("ROLE", "")) == "OPT-PARTICIPANT":
            attendee["optional"] = True
        if str(params.get("CUTYPE", "")) == "RESOURCE":
            attendee["resource"] = True
        if params.get("X-NUM-GUESTS"):
            attendee["additionalGuests"] = int(str(params["X-NUM-GUESTS"]))
        if params.get("X-GOOGLE-COMMENT"):
            attendee["comment"] = str(params["X-GOOGLE-COMMENT"])
        attendees.append(attendee)
    if attendees:
        body["attendees"] = attendees

    entries = _fence_props(vevent, "X-GOOGLE-CONFERENCE-ENTRY")
    if entries:
        points = []
        for prop in entries:
            point = {"entryPointType": str(prop.params["TYPE"]), "uri": str(prop)}
            for param, key in _ENTRY_PARAMS:
                if prop.params.get(param):
                    point[key] = str(prop.params[param])
            points.append(point)
        conference = {"entryPoints": points}
        if vevent.get("X-GOOGLE-CONFERENCE-ID"):
            conference["conferenceId"] = str(vevent["X-GOOGLE-CONFERENCE-ID"])
        if vevent.get("X-GOOGLE-CONFERENCE-NOTES"):
            conference["notes"] = str(vevent["X-GOOGLE-CONFERENCE-NOTES"])
        solutions = _fence_props(vevent, "X-GOOGLE-CONFERENCE-SOLUTION")
        if solutions:
            solution = {"name": str(solutions[0])}
            if solutions[0].params.get("X-GOOGLE-KEY-TYPE"):
                solution["key"] = {"type": str(solutions[0].params["X-GOOGLE-KEY-TYPE"])}
        else:
            video = next(
                (p["uri"] for p in points if p["entryPointType"] == "video"),
                points[0]["uri"],
            )
            solution = _conference_solution_for(video)
        conference["conferenceSolution"] = solution
        body["conferenceData"] = conference
    elif vevent.get("X-GOOGLE-CONFERENCE"):
        uri = str(vevent["X-GOOGLE-CONFERENCE"])
        body["conferenceData"] = {
            "entryPoints": [{"entryPointType": "video", "uri": uri}],
            "conferenceSolution": _conference_solution_for(uri),
        }

    attachments = []
    for prop in _fence_props(vevent, "ATTACH"):
        attachment = {"fileUrl": str(prop)}
        if prop.params.get("FMTTYPE"):
            attachment["mimeType"] = str(prop.params["FMTTYPE"])
        if prop.params.get("FILENAME"):
            attachment["title"] = str(prop.params["FILENAME"])
        if prop.params.get("X-GOOGLE-FILE-ID"):
            attachment["fileId"] = str(prop.params["X-GOOGLE-FILE-ID"])
        attachments.append(attachment)
    if attachments:
        body["attachments"] = attachments

    overrides = [
        {"method": method, "minutes": int(minutes)}
        for prop in _fence_props(vevent, "X-GOOGLE-REMINDER")
        for method, minutes in [str(prop).split(",", 1)]
    ]
    if overrides:
        body["reminders"] = {"useDefault": False, "overrides": overrides}
    elif vevent.get("X-GOOGLE-REMINDERS"):
        body["reminders"] = {"useDefault": False, "overrides": []}

    if vevent.get("TRANSP"):
        body["transparency"] = _enum(
            str(vevent["TRANSP"]), _GOOGLE_TRANSP, "TRANSP"
        )
    if vevent.get("CLASS"):
        body["visibility"] = _enum(str(vevent["CLASS"]), _GOOGLE_CLASS, "CLASS")
    if vevent.get("X-GOOGLE-COLOR-ID"):
        body["colorId"] = str(vevent["X-GOOGLE-COLOR-ID"])
    if vevent.get("X-GOOGLE-GUESTS-CAN"):
        body.update(_json.loads(str(vevent["X-GOOGLE-GUESTS-CAN"])))
    if vevent.get("X-GOOGLE-EXTENDED-PROPS"):
        body["extendedProperties"] = _json.loads(str(vevent["X-GOOGLE-EXTENDED-PROPS"]))
    if vevent.get("X-GOOGLE-EVENT-TYPE"):
        body["eventType"] = str(vevent["X-GOOGLE-EVENT-TYPE"])
    for key, prop in _BAGS.items():
        if vevent.get(prop):
            body[key] = _json.loads(str(vevent[prop]))
    sources = _fence_props(vevent, "X-GOOGLE-SOURCE")
    if sources:
        source = {"url": str(sources[0])}
        if sources[0].params.get("TITLE"):
            source["title"] = str(sources[0].params["TITLE"])
        body["source"] = source
    return body


def patch_instance(credentials, calendar_id, uid, original_start, card):
    """Patch one occurrence of a recurring Google event — its exception instance.

    Google models a modified occurrence as a sibling event carrying
    ``recurringEventId`` (the master) and ``originalStartTime`` (the slot it
    replaces); patching that instance creates or updates the exception
    server-side, and Google's ICS export renders it as a ``RECURRENCE-ID``
    VEVENT the importer already understands. The master is resolved by
    iCalUID (as `delete_event` does); the instance by the API's
    ``originalStart`` filter. ``showDeleted`` is on, so patching a cancelled
    instance back to ``confirmed`` revives it (the undo path).

    The patch body is `_fields_body`: the owned instance fields with explicit
    clearing forms (``location: None`` / ``description: None`` when the card
    lacks them — omission would leave the instance's old value standing for
    the feed to revert) plus ``status``; Google owns the instance's sequence
    on patch, so none is sent, and no recurrence (instances have none).

    Args:
        credentials: A ``google.oauth2.credentials.Credentials``.
        calendar_id: The Google calendar id.
        uid: The master's iCalUID.
        original_start: The ORIGINAL occurrence start (tz-aware ``datetime``,
            or ``date`` for an all-day series) identifying the slot.
        card: The rendered override (``mdcal.ics.RenderedCard`` or
            ``mddb.Card``) carrying the occurrence's new state.

    Returns:
        The tz-aware ``dispatched`` watermark (the patched instance's
        ``updated``, second-truncated).

    Raises:
        ValueError: No live master with that iCalUID, or the series has no
            instance at ``original_start``.
    """
    service = _service(credentials)
    items = (
        service.events().list(calendarId=calendar_id, iCalUID=uid).execute()["items"]
    )
    masters = [
        item
        for item in items
        if not item.get("recurringEventId") and item["status"] != "cancelled"
    ]
    if not masters:
        raise ValueError(f"no live Google master for iCalUID {uid}")
    instances = (
        service.events()
        .instances(
            calendarId=calendar_id,
            eventId=masters[0]["id"],
            originalStart=original_start.isoformat(),
            showDeleted=True,
        )
        .execute()["items"]
    )
    if not instances:
        raise ValueError(f"no instance of {uid} at {original_start.isoformat()}")
    result = (
        service.events()
        .patch(calendarId=calendar_id, eventId=instances[0]["id"], body=_fields_body(card))
        .execute()
    )
    return _watermark(result["updated"])


def _recurrence_lines(body):
    fence = _re.search(r"```ics\n(.*?)\n```", body, _re.DOTALL).group(1)
    unfolded = fence.replace("\n ", "")
    return [
        line
        for line in unfolded.split("\n")
        if line.startswith(("RRULE", "EXDATE", "RDATE"))
    ]


def main(argv=None):
    """Run the ``mdcal-pull`` console script: API-export a calendar as ICS.

    Prints the synthesized VCALENDAR to stdout, ready for
    ``mdcal-import --ics`` — the owned-calendar replacement for curling a
    secret feed URL.

    Args:
        argv: Argument list for testing; defaults to ``sys.argv[1:]``.
    """
    from google.oauth2.credentials import Credentials

    parser = argparse.ArgumentParser(prog="mdcal-pull")
    parser.add_argument("--calendar-id", required=True)
    parser.add_argument("--credentials", default="/etc/mdcal/google.json")
    args = parser.parse_args(argv)
    with open(args.credentials) as f:
        credentials = Credentials(None, **_json.load(f))
    print(export_ics(credentials, args.calendar_id))


if __name__ == "__main__":
    main()
