import datetime as dt

import icalendar
import pytest

from mdcal.imip import (
    InvalidInvitation,
    Invite,
    build_cancel,
    build_reply,
    build_reply_email,
    build_request,
    build_update,
    invitation_intent,
    invitation_preview,
    parse_request,
)

REQUEST = (
    "BEGIN:VCALENDAR\r\n"
    "PRODID:-//Google Inc//Google Calendar 70.9054//EN\r\n"
    "VERSION:2.0\r\n"
    "METHOD:REQUEST\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:abc123@google.com\r\n"
    "SEQUENCE:2\r\n"
    "DTSTART:20260710T140000Z\r\n"
    "DTEND:20260710T150000Z\r\n"
    "SUMMARY:Project sync\r\n"
    "LOCATION:MR13, Pavilion B\r\n"
    "ORGANIZER;CN=Jane Doe:mailto:jane@example.org\r\n"
    "ATTENDEE;CN=Will Handley;PARTSTAT=NEEDS-ACTION:mailto:wh260@cam.ac.uk\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_parse_request_extracts_fields():
    inv = parse_request(REQUEST)
    assert isinstance(inv, Invite)
    assert inv.uid == "abc123@google.com"
    assert inv.sequence == 2
    assert inv.organiser == "jane@example.org"
    assert inv.summary == "Project sync"
    assert inv.location == "MR13, Pavilion B"
    assert inv.dtstart == dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.timezone.utc)
    assert inv.dtend == dt.datetime(2026, 7, 10, 15, 0, tzinfo=dt.timezone.utc)


def test_parse_request_rejects_non_request():
    published = REQUEST.replace("METHOD:REQUEST", "METHOD:PUBLISH")
    with pytest.raises(ValueError, match="not an iMIP REQUEST"):
        parse_request(published)


def test_parse_request_requires_organiser():
    no_org = "\r\n".join(
        line for line in REQUEST.splitlines() if not line.startswith("ORGANIZER")
    )
    with pytest.raises(ValueError, match="no ORGANIZER"):
        parse_request(no_org)


@pytest.mark.parametrize(
    "response,partstat",
    [("accept", "ACCEPTED"), ("decline", "DECLINED"), ("tentative", "TENTATIVE")],
)
def test_build_reply_roundtrips(response, partstat):
    reply = build_reply(REQUEST, "wh260@cam.ac.uk", response, cn="Will Handley")
    cal = icalendar.Calendar.from_ical(reply)
    assert str(cal["METHOD"]) == "REPLY"
    (event,) = cal.walk("VEVENT")
    # identity echoed so the organiser's client pairs the response
    assert str(event["UID"]) == "abc123@google.com"
    assert int(event["SEQUENCE"]) == 2
    assert str(event["ORGANIZER"]).endswith("jane@example.org")
    # exactly one attendee — the responder — with the chosen status
    att = event["ATTENDEE"]
    assert str(att) == "mailto:wh260@cam.ac.uk"
    assert att.params["PARTSTAT"] == partstat
    assert att.params["CN"] == "Will Handley"
    assert "DTSTAMP" in event


def test_build_reply_rejects_unknown_response():
    with pytest.raises(ValueError, match="response must be one of"):
        build_reply(REQUEST, "wh260@cam.ac.uk", "maybe")


def test_build_reply_email_addresses_organiser():
    msg = build_reply_email(REQUEST, "wh260@cam.ac.uk", "accept", cn="Will Handley")
    assert msg["To"] == "jane@example.org"
    assert msg["From"] == "Will Handley <wh260@cam.ac.uk>"
    assert msg["Subject"] == "Accepted: Project sync"
    cal_part = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    assert cal_part.get_param("method") == "REPLY"
    assert "METHOD:REPLY" in cal_part.get_content()


def _outbound_event(sequence=0):
    event = icalendar.Event()
    event.add("uid", "outbound-1@mdcal")
    event.add("sequence", sequence)
    event.add("dtstamp", dt.datetime(2026, 7, 24, 12, tzinfo=dt.timezone.utc))
    event.add("dtstart", dt.datetime(2026, 7, 24, 20, tzinfo=dt.timezone.utc))
    event.add("dtend", dt.datetime(2026, 7, 24, 21, tzinfo=dt.timezone.utc))
    event.add("summary", "Dinner")
    event.add("location", "Cambridge")
    return event


def test_build_request_is_one_stably_ordered_multi_attendee_message():
    msg, payload = build_request(
        _outbound_event(),
        "wh260@cam.ac.uk",
        ["second@example.org", "first@example.org"],
    )
    assert msg["From"] == "wh260@cam.ac.uk"
    assert msg["To"] == "second@example.org, first@example.org"
    assert msg["Subject"] == "Invitation: Dinner"
    calendar = icalendar.Calendar.from_ical(payload)
    assert str(calendar["METHOD"]) == "REQUEST"
    (event,) = calendar.walk("VEVENT")
    assert str(event["UID"]) == "outbound-1@mdcal"
    assert int(event["SEQUENCE"]) == 0
    assert [str(value) for value in event["ATTENDEE"]] == [
        "mailto:second@example.org",
        "mailto:first@example.org",
    ]
    assert all(
        value.params["PARTSTAT"] == "NEEDS-ACTION" for value in event["ATTENDEE"]
    )
    part = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
    assert part.get_param("method") == "REQUEST"
    assert icalendar.Calendar.from_ical(part.get_content()).to_ical() == payload


def test_build_update_is_retry_stable_without_mutating_caller_sequence():
    source = _outbound_event(sequence=3)
    msg, payload = build_update(source, "wh260@cam.ac.uk", ["person@example.org"])
    _, retried = build_update(source, "wh260@cam.ac.uk", ["person@example.org"])
    assert msg["Subject"] == "Updated: Dinner"
    (event,) = icalendar.Calendar.from_ical(payload).walk("VEVENT")
    assert int(event["SEQUENCE"]) == 4
    assert int(source["SEQUENCE"]) == 3
    assert retried == payload
    assert invitation_intent(retried) == invitation_intent(payload)


def test_build_request_preserves_timezone_and_recurrence():
    event = _outbound_event()
    event["DTSTART"] = icalendar.vDatetime(
        dt.datetime(2026, 7, 24, 20, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    )
    event["DTSTART"].params["TZID"] = "Europe/London"
    event.add("rrule", {"freq": "weekly", "count": 3})
    _, payload = build_request(event, "wh260@cam.ac.uk", ["person@example.org"])
    calendar = icalendar.Calendar.from_ical(payload)
    (outbound,) = calendar.walk("VEVENT")
    assert outbound["DTSTART"].params["TZID"] == "Europe/London"
    assert outbound["RRULE"]["FREQ"] == ["WEEKLY"]
    assert outbound["RRULE"]["COUNT"] == [3]
    (timezone,) = calendar.walk("VTIMEZONE")
    assert str(timezone["TZID"]) == "Europe/London"


def test_build_cancel_is_retry_stable_without_mutating_caller():
    source = _outbound_event(sequence=4)
    msg, payload = build_cancel(source, "wh260@cam.ac.uk", ["person@example.org"])
    _, retried = build_cancel(source, "wh260@cam.ac.uk", ["person@example.org"])
    assert msg["Subject"] == "Cancelled: Dinner"
    calendar = icalendar.Calendar.from_ical(payload)
    assert str(calendar["METHOD"]) == "CANCEL"
    (event,) = calendar.walk("VEVENT")
    assert str(event["UID"]) == "outbound-1@mdcal"
    assert int(event["SEQUENCE"]) == 5
    assert str(event["STATUS"]) == "CANCELLED"
    assert int(source["SEQUENCE"]) == 4
    assert source.get("STATUS") is None
    assert retried == payload
    assert invitation_intent(retried) == invitation_intent(payload)


def test_build_reply_preserves_recurring_instance_identity():
    instance = REQUEST.replace(
        "DTSTART:20260710T140000Z\r\n",
        "DTSTART:20260710T140000Z\r\nRECURRENCE-ID:20260710T140000Z\r\n",
    )
    reply = build_reply(
        instance,
        "wh260@cam.ac.uk",
        "accept",
        dtstamp=dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc),
    )
    (event,) = icalendar.Calendar.from_ical(reply).walk("VEVENT")
    assert event["RECURRENCE-ID"].dt == dt.datetime(
        2026, 7, 10, 14, tzinfo=dt.timezone.utc
    )


def test_build_reply_includes_timezone_for_recurring_instance():
    instance = REQUEST.replace(
        "DTSTART:20260710T140000Z\r\n",
        "DTSTART;TZID=Europe/London:20260710T140000\r\n"
        "RECURRENCE-ID;TZID=Europe/London:20260710T140000\r\n",
    )
    reply = build_reply(
        instance,
        "wh260@cam.ac.uk",
        "accept",
        dtstamp=dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc),
    )
    calendar = icalendar.Calendar.from_ical(reply)
    (event,) = calendar.walk("VEVENT")
    assert event["DTSTART"].params["TZID"] == "Europe/London"
    assert event["RECURRENCE-ID"].params["TZID"] == "Europe/London"
    (timezone,) = calendar.walk("VTIMEZONE")
    assert str(timezone["TZID"]) == "Europe/London"


def test_invitation_intent_binds_uid_sequence_method_and_exact_payload():
    _, payload = build_request(
        _outbound_event(sequence=3), "wh260@cam.ac.uk", ["person@example.org"]
    )
    intent = invitation_intent(payload)
    assert intent.uid == "outbound-1@mdcal"
    assert intent.sequence == 3
    assert intent.method == "REQUEST"
    assert intent.key == f"outbound-1@mdcal:3:REQUEST:{intent.digest}"
    _, changed = build_request(
        _outbound_event(sequence=3), "wh260@cam.ac.uk", ["other@example.org"]
    )
    assert invitation_intent(changed).key != intent.key


def test_invitation_preview_uses_shared_event_vocabulary():
    preview = invitation_preview(
        REQUEST.replace(
            "LOCATION:MR13, Pavilion B\r\n",
            "LOCATION:MR13, Pavilion B\r\nDESCRIPTION:Bring notes\r\n",
        ).encode()
    )
    assert preview == {
        "operation": "REQUEST",
        "event": {
            "uid": "abc123@google.com",
            "title": "Project sync",
            "start": "2026-07-10T14:00:00+00:00",
            "end": "2026-07-10T15:00:00+00:00",
            "dtstart": "2026-07-10T14:00:00+00:00",
            "dtend": "2026-07-10T15:00:00+00:00",
            "all_day": False,
            "location": "MR13, Pavilion B",
            "tzid": "UTC",
            "status": "CONFIRMED",
            "rrule": None,
            "description": "Bring notes",
            "organizer": "jane@example.org",
            "attendees": [
                {
                    "email": "wh260@cam.ac.uk",
                    "name": "Will Handley",
                    "status": "NEEDS-ACTION",
                }
            ],
            "attendees_omitted": False,
            "my_status": None,
            "conference": [],
            "conference_url": None,
            "meeting_links": [],
            "attachments": [],
            "gcal_link": None,
        },
    }


@pytest.mark.parametrize("method", ["REQUEST", "REPLY", "CANCEL"])
def test_invitation_preview_accepts_each_invitation_method(method):
    preview = invitation_preview(REQUEST.replace("METHOD:REQUEST", f"METHOD:{method}"))
    assert preview["operation"] == method


@pytest.mark.parametrize(
    "payload,match",
    [
        (b"\xff", "invalid iMIP payload"),
        (b"not a calendar", "invalid iMIP payload"),
        (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", "METHOD"),
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:PUBLISH\r\nEND:VCALENDAR\r\n",
            "METHOD",
        ),
        (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n",
            "exactly one VEVENT",
        ),
        (
            REQUEST.replace(
                "END:VEVENT\r\nEND:VCALENDAR",
                "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:second\r\n"
                "DTSTART:20260710T160000Z\r\nEND:VEVENT\r\nEND:VCALENDAR",
            ).encode(),
            "exactly one VEVENT",
        ),
        (
            REQUEST.replace(
                "DTSTART:20260710T140000Z", "DTSTART:20260710T140000"
            ).encode(),
            "invalid iMIP payload",
        ),
        (
            REQUEST.replace("UID:abc123@google.com\r\n", "").encode(),
            "invalid iMIP payload",
        ),
        (
            REQUEST.replace("DTSTART:20260710T140000Z\r\n", "").encode(),
            "invalid iMIP payload",
        ),
    ],
)
def test_invitation_preview_rejects_unrenderable_payloads(payload, match):
    with pytest.raises(InvalidInvitation, match=match):
        invitation_preview(payload)


@pytest.mark.parametrize(
    "attendees,match",
    [
        ([], "at least one attendee"),
        (["not-an-address"], "invalid attendee"),
        (["A@example.org", "a@example.org"], "duplicate attendee"),
    ],
)
def test_build_request_rejects_invalid_recipient_sets(attendees, match):
    with pytest.raises(ValueError, match=match):
        build_request(_outbound_event(), "wh260@cam.ac.uk", attendees)
