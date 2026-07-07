import datetime as dt

import icalendar
import pytest

from mdcal.imip import (
    Invite,
    build_reply,
    build_reply_email,
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
