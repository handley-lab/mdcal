"""Google Calendar write-through for synced feed sources.

Pushes event cards to the Google Calendar API keyed on the card's own
``uid`` (``events.import`` upserts on iCalUID, so create and edit unify) and
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
    """
    result = (
        _service(credentials)
        .events()
        .import_(calendarId=calendar_id, body=_event_body(card))
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
    map straight across — yielding the same ``(uid, recurrence_id)`` set the
    secret feed would, which is what keeps ``--prune`` safe on a source
    switched from URL to API.

    Pages are fetched to exhaustion BEFORE any VCALENDAR is built: a failed
    page raises (crash-on-drift) rather than handing ``--prune`` a partial
    identity set that would mass-delete the unfetched remainder. This is a
    VEVENT subset synthesized from the Google event resource (uid, times,
    recurrence, status, sequence, organizer/attendees, timestamps) — not raw
    ICS byte/param fidelity; raw ICS stays the reference for URL feeds.

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
        parent = item.get("recurringEventId")
        if parent and item["status"] == "cancelled":
            continue
        master_zone = zone_of.get(item["id"]) or zone_of.get(parent) or default_tz
        cal.add_component(
            _item_to_vevent(item, excluded.get(item["id"], []), default_tz, master_zone)
        )
    return cal.to_ical().decode()


def _gtime(gtime, tz):
    """A Google start/end/originalStartTime → (value, is_date).

    All-day is a bare ``date``; a timed point is converted to ``tz`` as a
    zone-aware ``datetime``, so icalendar serialises it ``;TZID=<tz>:<local>``.
    ``RECURRENCE-ID``/``EXDATE`` pass the MASTER's zone (Google's ICS export
    renders an exception's id in the series' display zone, not the exception's
    own — they can differ, and mddb's identity is the serialised string), while
    ``DTSTART``/``DTEND`` pass the point's own zone or the calendar default.
    """
    if "date" in gtime:
        return _dt.date.fromisoformat(gtime["date"]), True
    return _rfc3339(gtime["dateTime"]).astimezone(ZoneInfo(tz)), False


def _item_to_vevent(item, excluded, default_tz, master_zone):
    vevent = icalendar.Event()
    vevent.add("UID", item["iCalUID"])
    vevent.add("DTSTAMP", _dt.datetime.now(_dt.timezone.utc))
    vevent.add("SUMMARY", item.get("summary", ""))
    vevent.add("STATUS", item["status"].upper())
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
        vevent.add("TRANSP", item["transparency"].upper())
    if item.get("location"):
        vevent.add("LOCATION", item["location"])
    if item.get("description"):
        vevent.add("DESCRIPTION", item["description"])
    if item.get("organizer", {}).get("email"):
        vevent.add("ORGANIZER", f"mailto:{item['organizer']['email']}")
    for attendee in item.get("attendees", []):
        if attendee.get("email"):
            vevent.add("ATTENDEE", f"mailto:{attendee['email']}")
    vevent.add("CREATED", _rfc3339(item["created"]))
    vevent.add("LAST-MODIFIED", _rfc3339(item["updated"]))
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


def _event_body(card):
    """Map a card to a Google API event resource.

    Scalars come from the flat yaml; ``recurrence`` lines come verbatim from
    the card's fenced VEVENT (unfolded) — re-serialising EXDATEs from the
    flat yaml would re-normalise their form, and the fence is always current
    (both the importer and web writes regenerate it).
    """
    yaml = card.yaml
    body = {
        "iCalUID": yaml["uid"],
        "summary": card.title,
        "sequence": yaml["sequence"],
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
    if yaml.get("location"):
        body["location"] = yaml["location"]
    description = card.body.split("```ics", 1)[0].strip()
    if description:
        body["description"] = description
    recurrence = _recurrence_lines(card.body)
    if recurrence:
        body["recurrence"] = recurrence
    return body


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
