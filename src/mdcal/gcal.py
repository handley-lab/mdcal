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

import datetime as _dt
import re as _re

import httplib2
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


def _watermark(updated):
    """Parse the API's RFC3339 ``updated`` into the ``dispatched`` guard value.

    Truncated to whole seconds — the feed's ``LAST-MODIFIED`` carries second
    precision and equals ``updated`` exactly at that precision (measured), so
    the importer's ``last_modified < dispatched`` comparison converges the
    moment the feed catches up.
    """
    stamp = _dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
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
