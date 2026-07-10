"""Full-fidelity Google capture: field map, value maps, canary, flatten."""

import json

import icalendar
import pytest

import mdcal.gcal as gcal
from mdcal.ics import vevent_to_card


class _Result:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Service:
    def __init__(self, items):
        self.items = items

    def calendars(self):
        class _Calendars:
            def get(self, calendarId):
                return _Result({"timeZone": "Europe/London"})

        return _Calendars()

    def events(self):
        service = self

        class _Events:
            def list(self, **kwargs):
                return _Result({"items": service.items})

        return _Events()


FULL = {
    "kind": "calendar#event",
    "etag": '"etag"',
    "id": "g1",
    "iCalUID": "full@x",
    "status": "confirmed",
    "summary": "Board meeting",
    "description": "Agenda attached.",
    "location": "Boardroom",
    "created": "2026-01-01T00:00:00Z",
    "updated": "2026-01-02T00:00:00Z",
    "htmlLink": "https://calendar.google.com/event?eid=abc",
    "sequence": 2,
    "start": {"dateTime": "2026-09-01T10:00:00Z", "timeZone": "Europe/London"},
    "end": {"dateTime": "2026-09-01T11:00:00Z", "timeZone": "Europe/London"},
    "transparency": "transparent",
    "visibility": "private",
    "colorId": "5",
    "organizer": {"email": "will@x", "displayName": "Will", "self": False},
    "creator": {"email": "assistant@x"},
    "attendees": [
        {
            "email": "will@x",
            "displayName": "Handley, Will",
            "responseStatus": "accepted",
            "self": True,
            "organizer": True,
        },
        {
            "email": "alice@x",
            "displayName": 'Smith, "Alice"',
            "responseStatus": "needsAction",
            "optional": True,
            "additionalGuests": 2,
            "comment": "may be late",
        },
        {"email": "room@x", "resource": True, "responseStatus": "declined"},
    ],
    "hangoutLink": "https://meet.google.com/abc",
    "conferenceData": {
        "conferenceId": "abc-defg-hij",
        "notes": "dial-in below",
        "conferenceSolution": {"name": "Google Meet", "key": {"type": "hangoutsMeet"}},
        "entryPoints": [
            {"entryPointType": "video", "uri": "https://meet.google.com/abc"},
            {
                "entryPointType": "phone",
                "uri": "tel:+44-20-1234",
                "label": "+44 20 1234",
                "pin": "123456",
                "regionCode": "GB",
            },
        ],
    },
    "attachments": [
        {
            "fileUrl": "https://drive.google.com/a",
            "title": "Agenda.pdf",
            "mimeType": "application/pdf",
            "fileId": "fidA",
            "iconLink": "https://icons/x",
        },
        {"fileUrl": "https://drive.google.com/b", "fileId": "fidB"},
    ],
    "reminders": {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 30}],
    },
    "guestsCanModify": True,
    "guestsCanSeeOtherGuests": False,
    "extendedProperties": {"private": {"k": "a,b;c:d"}},
    "source": {"url": "https://example.org/ticket", "title": "Ticket"},
}


def _export(item, monkeypatch):
    service = _Service([item])
    monkeypatch.setattr(gcal, "_service", lambda credentials: service)
    (vevent,) = icalendar.Calendar.from_ical(gcal.export_ics(None, "cal@x")).walk(
        "VEVENT"
    )
    return vevent


def _params(prop):
    return {str(k): str(v) for k, v in prop.params.items()}


def test_full_fixture_maps_every_field(monkeypatch):
    vevent = _export(FULL, monkeypatch)

    attendees = vevent["ATTENDEE"]
    by_email = {str(a).lower().removeprefix("mailto:"): a for a in attendees}
    assert set(by_email) == {"will@x", "alice@x", "room@x"}
    me = _params(by_email["will@x"])
    assert me["CN"] == "Handley, Will"
    assert me["PARTSTAT"] == "ACCEPTED"
    assert me["X-GOOGLE-SELF"] == "TRUE"
    alice = _params(by_email["alice@x"])
    assert alice["PARTSTAT"] == "NEEDS-ACTION"
    assert alice["ROLE"] == "OPT-PARTICIPANT"
    assert alice["X-NUM-GUESTS"] == "2"
    assert alice["X-GOOGLE-COMMENT"] == "may be late"
    room = _params(by_email["room@x"])
    assert room["CUTYPE"] == "RESOURCE"
    assert room["PARTSTAT"] == "DECLINED"

    entries = vevent["X-GOOGLE-CONFERENCE-ENTRY"]
    entries = entries if isinstance(entries, list) else [entries]
    phone = [e for e in entries if _params(e)["TYPE"] == "phone"][0]
    assert _params(phone)["PIN"] == "123456"
    assert _params(phone)["REGION-CODE"] == "GB"
    assert str(vevent["X-GOOGLE-CONFERENCE-ID"]) == "abc-defg-hij"
    assert str(vevent["X-GOOGLE-CONFERENCE-SOLUTION"]) == "Google Meet"
    assert (
        _params(vevent["X-GOOGLE-CONFERENCE-SOLUTION"])["X-GOOGLE-KEY-TYPE"]
        == "hangoutsMeet"
    )
    assert str(vevent["X-GOOGLE-CONFERENCE-NOTES"]) == "dial-in below"
    assert str(vevent["X-GOOGLE-CONFERENCE"]) == "https://meet.google.com/abc"

    attaches = vevent["ATTACH"]
    attaches = attaches if isinstance(attaches, list) else [attaches]
    by_url = {str(a): _params(a) for a in attaches}
    assert by_url["https://drive.google.com/a"]["X-GOOGLE-FILE-ID"] == "fidA"
    assert by_url["https://drive.google.com/a"]["FILENAME"] == "Agenda.pdf"
    assert by_url["https://drive.google.com/b"]["X-GOOGLE-FILE-ID"] == "fidB"

    assert str(vevent["X-GOOGLE-REMINDER"]) == "popup,30"
    assert str(vevent["CLASS"]) == "PRIVATE"
    assert str(vevent["TRANSP"]) == "TRANSPARENT"
    assert str(vevent["X-GOOGLE-COLOR-ID"]) == "5"
    assert str(vevent["X-GOOGLE-CREATOR"]) == "assistant@x"
    assert json.loads(str(vevent["X-GOOGLE-GUESTS-CAN"])) == {
        "guestsCanModify": True,
        "guestsCanSeeOtherGuests": False,
    }
    assert json.loads(str(vevent["X-GOOGLE-EXTENDED-PROPS"])) == {
        "private": {"k": "a,b;c:d"}
    }
    assert str(vevent["X-GOOGLE-SOURCE"]) == "https://example.org/ticket"
    assert _params(vevent["X-GOOGLE-SOURCE"])["TITLE"] == "Ticket"


def test_full_fixture_flattens_for_display(monkeypatch):
    vevent = _export(FULL, monkeypatch)
    card = vevent_to_card(vevent, "probe")
    assert card.yaml["my_status"] == "ACCEPTED"
    assert card.yaml["attendee_emails"] == ["will@x", "alice@x", "room@x"]
    by_email = {a["email"]: a for a in card.yaml["attendees"]}
    assert by_email["alice@x"]["name"] == 'Smith, "Alice"'
    assert by_email["alice@x"]["status"] == "NEEDS-ACTION"
    assert by_email["alice@x"]["optional"] is True
    assert by_email["room@x"]["status"] == "DECLINED"
    types = {c["type"]: c for c in card.yaml["conference"]}
    assert types["video"]["uri"] == "https://meet.google.com/abc"
    assert types["phone"]["label"] == "+44 20 1234"
    assert card.yaml["conference_url"] == "https://meet.google.com/abc"
    assert card.yaml["attachments"] == [
        {"url": "https://drive.google.com/a", "title": "Agenda.pdf"},
        {"url": "https://drive.google.com/b"},
    ]


def test_full_fixture_fence_reparses_identically(monkeypatch):
    vevent = _export(FULL, monkeypatch)
    card = vevent_to_card(vevent, "probe")
    fence = card.body.split("```ics\n", 1)[1].split("\n```", 1)[0]
    reparsed = icalendar.Event.from_ical(fence)
    again = vevent_to_card(reparsed, "probe")
    assert again.yaml == card.yaml
    assert again.body == card.body


def test_reminders_three_states(monkeypatch):
    on = _export(
        {
            **FULL,
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "email", "minutes": 10}],
            },
        },
        monkeypatch,
    )
    assert str(on["X-GOOGLE-REMINDER"]) == "email,10"
    off = _export(
        {**FULL, "reminders": {"useDefault": False, "overrides": []}}, monkeypatch
    )
    assert str(off["X-GOOGLE-REMINDERS"]) == "NONE"
    assert off.get("X-GOOGLE-REMINDER") is None
    default = _export({**FULL, "reminders": {"useDefault": True}}, monkeypatch)
    assert default.get("X-GOOGLE-REMINDERS") is None
    assert default.get("X-GOOGLE-REMINDER") is None


def test_attendees_omitted_flag(monkeypatch):
    vevent = _export({**FULL, "attendeesOmitted": True}, monkeypatch)
    assert str(vevent["X-GOOGLE-ATTENDEES-OMITTED"]) == "TRUE"
    card = vevent_to_card(vevent, "probe")
    assert card.yaml["attendees_omitted"] is True
    plain = vevent_to_card(_export(FULL, monkeypatch), "probe")
    assert "attendees_omitted" not in plain.yaml


def test_visibility_default_emits_no_class(monkeypatch):
    vevent = _export({**FULL, "visibility": "default"}, monkeypatch)
    assert vevent.get("CLASS") is None


def test_event_type_and_bag(monkeypatch):
    vevent = _export(
        {
            **FULL,
            "eventType": "workingLocation",
            "workingLocationProperties": {"type": "homeOffice", "homeOffice": {}},
        },
        monkeypatch,
    )
    assert str(vevent["X-GOOGLE-EVENT-TYPE"]) == "workingLocation"
    assert json.loads(str(vevent["X-GOOGLE-WORKING-LOCATION-PROPS"])) == {
        "homeOffice": {},
        "type": "homeOffice",
    }


def test_canary_unknown_event_field(monkeypatch):
    with pytest.raises(ValueError, match="unmapped Google event field.*futureThing"):
        _export({**FULL, "futureThing": 1}, monkeypatch)


def test_canary_unknown_nested_fields(monkeypatch):
    with pytest.raises(ValueError, match="unmapped Google attendee"):
        _export({**FULL, "attendees": [{"email": "a@x", "vote": "yes"}]}, monkeypatch)
    with pytest.raises(ValueError, match="unmapped Google conference entry point"):
        _export(
            {
                **FULL,
                "conferenceData": {
                    "entryPoints": [
                        {"entryPointType": "video", "uri": "u", "hologram": True}
                    ]
                },
            },
            monkeypatch,
        )
    with pytest.raises(ValueError, match="unmapped Google attachment"):
        _export(
            {**FULL, "attachments": [{"fileUrl": "u", "sizeBytes": 3}]}, monkeypatch
        )
    with pytest.raises(ValueError, match="unmapped Google reminders"):
        _export({**FULL, "reminders": {"useDefault": False, "snooze": 1}}, monkeypatch)
    with pytest.raises(ValueError, match="unmapped Google source"):
        _export({**FULL, "source": {"url": "u", "favicon": "f"}}, monkeypatch)
    with pytest.raises(ValueError, match="unmapped Google person"):
        _export({**FULL, "organizer": {"email": "o@x", "pronouns": "xe"}}, monkeypatch)
    with pytest.raises(ValueError, match="unmapped Google time"):
        _export(
            {**FULL, "start": {"dateTime": "2026-09-01T10:00:00Z", "lunar": True}},
            monkeypatch,
        )
    with pytest.raises(ValueError, match="unmapped Google conferenceData"):
        _export({**FULL, "conferenceData": {"holograms": True}}, monkeypatch)
    with pytest.raises(ValueError, match="unmapped Google conference solution"):
        _export(
            {
                **FULL,
                "conferenceData": {
                    "conferenceSolution": {"name": "Meet", "tier": "pro"}
                },
            },
            monkeypatch,
        )
    with pytest.raises(ValueError, match="unmapped Google reminder override"):
        _export(
            {
                **FULL,
                "reminders": {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": 5, "tone": "chime"}],
                },
            },
            monkeypatch,
        )


def test_unknown_enum_values_crash(monkeypatch):
    with pytest.raises(ValueError, match="unknown Google attendee responseStatus"):
        _export(
            {**FULL, "attendees": [{"email": "a@x", "responseStatus": "maybe"}]},
            monkeypatch,
        )
    with pytest.raises(ValueError, match="unknown Google visibility"):
        _export({**FULL, "visibility": "secret"}, monkeypatch)
    with pytest.raises(ValueError, match="unknown Google event status"):
        _export({**FULL, "status": "pending"}, monkeypatch)
    with pytest.raises(ValueError, match="unknown Google transparency"):
        _export({**FULL, "transparency": "smoked"}, monkeypatch)
