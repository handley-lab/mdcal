"""Merge-safe push: patch-when-exists with clearing forms, import-when-absent
with fence enrichment, and the attendeesOmitted refusal."""

import icalendar
import pytest

import mdcal.gcal as gcal
from mdcal.ics import vevent_to_card


def _card(vevent_text, sequence=1):
    cal = (
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        f"BEGIN:VEVENT\n{vevent_text}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    vevent = list(icalendar.Calendar.from_ical(cal).walk("VEVENT"))[0]
    vevent.add("SEQUENCE", sequence)
    return vevent_to_card(vevent, "research")


BARE = (
    "UID:t@x\nSUMMARY:Coffee\n"
    "DTSTART;TZID=Europe/London:20260706T100000\n"
    "DTEND;TZID=Europe/London:20260706T103000\nSTATUS:CONFIRMED"
)

ENRICHED = (
    "UID:rich@x\nSUMMARY:Board\n"
    "DTSTART;TZID=Europe/London:20260706T100000\n"
    "DTEND;TZID=Europe/London:20260706T103000\nSTATUS:CONFIRMED\n"
    'ATTENDEE;CN="Smith, Alice";PARTSTAT=NEEDS-ACTION;ROLE=OPT-PARTICIPANT;'
    "X-NUM-GUESTS=2:mailto:alice@x\n"
    "ATTENDEE;CUTYPE=RESOURCE;PARTSTAT=ACCEPTED:mailto:room@x\n"
    "X-GOOGLE-CONFERENCE-ENTRY;TYPE=video:https://meet.google.com/abc\n"
    "X-GOOGLE-CONFERENCE-ENTRY;TYPE=phone;LABEL=+44 20 1234;PIN=123456:tel:+44201234\n"
    "X-GOOGLE-CONFERENCE-ID:abc-defg-hij\n"
    "X-GOOGLE-CONFERENCE-SOLUTION;X-GOOGLE-KEY-TYPE=hangoutsMeet:Google Meet\n"
    "ATTACH;FMTTYPE=application/pdf;FILENAME=Agenda.pdf;"
    "X-GOOGLE-FILE-ID=fidA:https://drive.google.com/a\n"
    "X-GOOGLE-REMINDER:popup,30\n"
    "X-GOOGLE-COLOR-ID:5\nCLASS:PRIVATE\nTRANSP:TRANSPARENT\n"
    'X-GOOGLE-GUESTS-CAN:{"guestsCanModify":true}\n'
    'X-GOOGLE-EXTENDED-PROPS:{"private":{"k":"v"}}'
)


class _Result:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _PushService:
    def __init__(self, listed):
        self.listed = listed
        self.patches = []
        self.imports = []

    def events(self):
        return self

    def list(self, calendarId, iCalUID, showDeleted):
        return _Result({"items": self.listed})

    def patch(self, calendarId, eventId, body):
        self.patches.append((eventId, body))
        return _Result({"updated": "2026-07-10T12:00:00Z"})

    def import_(self, calendarId, body, **kwargs):
        self.imports.append((body, kwargs))
        return _Result({"updated": "2026-07-10T12:00:00Z"})


def test_push_patches_existing_with_clearing_forms(monkeypatch):
    service = _PushService(listed=[{"id": "g1", "status": "confirmed"}])
    monkeypatch.setattr(gcal, "_service", lambda credentials: service)
    gcal.push_event(None, "cal", _card(BARE))
    assert service.imports == []
    ((event_id, body),) = service.patches
    assert event_id == "g1"
    assert body["location"] is None
    assert body["description"] is None
    assert body["recurrence"] == []
    assert body["status"] == "confirmed"
    assert body["sequence"] == 1
    for google_owned in (
        "attendees",
        "conferenceData",
        "reminders",
        "colorId",
        "visibility",
        "attachments",
        "extendedProperties",
        "transparency",
    ):
        assert google_owned not in body


def test_push_patch_skips_cancelled_and_instance_siblings(monkeypatch):
    service = _PushService(
        listed=[
            {"id": "inst", "status": "confirmed", "recurringEventId": "g1"},
            {"id": "g1", "status": "cancelled"},
        ]
    )
    monkeypatch.setattr(gcal, "_service", lambda credentials: service)
    gcal.push_event(None, "cal", _card(BARE))
    assert service.patches == []
    assert len(service.imports) == 1


def test_push_imports_absent_with_enrichment(monkeypatch):
    service = _PushService(listed=[])
    monkeypatch.setattr(gcal, "_service", lambda credentials: service)
    gcal.push_event(None, "cal", _card(ENRICHED))
    assert service.patches == []
    ((body, kwargs),) = service.imports
    assert kwargs == {"supportsAttachments": True, "conferenceDataVersion": 1}
    assert body["iCalUID"] == "rich@x"
    assert body["status"] == "confirmed"
    assert "location" not in body
    alice, room = body["attendees"]
    assert alice == {
        "email": "alice@x",
        "displayName": "Smith, Alice",
        "responseStatus": "needsAction",
        "optional": True,
        "additionalGuests": 2,
    }
    assert room == {"email": "room@x", "resource": True, "responseStatus": "accepted"}
    conference = body["conferenceData"]
    assert conference["conferenceId"] == "abc-defg-hij"
    assert conference["conferenceSolution"] == {
        "name": "Google Meet",
        "key": {"type": "hangoutsMeet"},
    }
    video, phone = conference["entryPoints"]
    assert video == {"entryPointType": "video", "uri": "https://meet.google.com/abc"}
    assert phone["pin"] == "123456"
    assert body["attachments"] == [
        {
            "fileUrl": "https://drive.google.com/a",
            "mimeType": "application/pdf",
            "title": "Agenda.pdf",
            "fileId": "fidA",
        }
    ]
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 30}],
    }
    assert body["colorId"] == "5"
    assert body["visibility"] == "private"
    assert body["transparency"] == "transparent"
    assert body["guestsCanModify"] is True
    assert body["extendedProperties"] == {"private": {"k": "v"}}


def test_push_import_reminders_none_state(monkeypatch):
    service = _PushService(listed=[])
    monkeypatch.setattr(gcal, "_service", lambda credentials: service)
    gcal.push_event(None, "cal", _card(BARE + "\nX-GOOGLE-REMINDERS:NONE"))
    ((body, kwargs),) = service.imports
    assert body["reminders"] == {"useDefault": False, "overrides": []}
    assert kwargs == {}


def test_push_import_refuses_omitted_attendee_list(monkeypatch):
    service = _PushService(listed=[])
    monkeypatch.setattr(gcal, "_service", lambda credentials: service)
    card = _card(BARE + "\nATTENDEE:mailto:a@x\nX-GOOGLE-ATTENDEES-OMITTED:TRUE")
    with pytest.raises(ValueError, match="attendeesOmitted"):
        gcal.push_event(None, "cal", card)
    assert service.imports == []


def test_bare_card_import_body_has_no_enrichment_keys():
    body = gcal._event_body(_card(BARE))
    assert set(body) == {"iCalUID", "sequence", "summary", "status", "start", "end"}


class _InstanceRecorder:
    def __init__(self):
        self.body = None

    def events(self):
        return self

    def list(self, calendarId, iCalUID):
        return _Result({"items": [{"id": "m1", "status": "confirmed"}]})

    def instances(self, calendarId, eventId, originalStart, showDeleted):
        return _Result({"items": [{"id": "i1"}]})

    def patch(self, calendarId, eventId, body):
        self.body = body
        return _Result({"updated": "2026-07-10T12:00:00Z"})


def test_patch_instance_sends_clearing_forms(monkeypatch):
    import datetime as dt

    service = _InstanceRecorder()
    monkeypatch.setattr(gcal, "_service", lambda credentials: service)
    slot = dt.datetime(2026, 7, 6, 10, 0, tzinfo=dt.timezone.utc)
    gcal.patch_instance(None, "cal", "t@x", slot, _card(BARE))
    assert service.body["location"] is None
    assert service.body["description"] is None
    assert service.body["status"] == "confirmed"
    assert "sequence" not in service.body
    assert "recurrence" not in service.body
