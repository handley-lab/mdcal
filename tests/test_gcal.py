import datetime as dt

import icalendar
import pytest

from mdcal.gcal import _event_body, _recurrence_lines, _watermark
from mdcal.ics import vevent_to_card


def _card(vevent_text, sequence=1):
    cal = (
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        f"BEGIN:VEVENT\n{vevent_text}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    vevent = list(icalendar.Calendar.from_ical(cal).walk("VEVENT"))[0]
    vevent.add("SEQUENCE", sequence)
    return vevent_to_card(vevent, "research")


def test_watermark_truncates_to_seconds():
    assert _watermark("2026-07-04T00:39:19.378Z") == dt.datetime(
        2026, 7, 4, 0, 39, 19, tzinfo=dt.timezone.utc
    )


def test_event_body_timed():
    card = _card(
        "UID:t@x\nSUMMARY:Coffee\nLOCATION:CMS\nDESCRIPTION:Grant chat.\n"
        "DTSTART;TZID=Europe/London:20260706T100000\n"
        "DTEND;TZID=Europe/London:20260706T103000\nSTATUS:CONFIRMED"
    )
    body = _event_body(card)
    assert body["iCalUID"] == "t@x"
    assert body["summary"] == "Coffee"
    assert body["sequence"] == 1
    assert body["start"] == {
        "dateTime": "2026-07-06T10:00:00+01:00",
        "timeZone": "Europe/London",
    }
    assert body["end"]["dateTime"] == "2026-07-06T10:30:00+01:00"
    assert body["location"] == "CMS"
    assert body["description"] == "Grant chat."
    assert "recurrence" not in body


def test_event_body_all_day():
    card = _card(
        "UID:a@x\nSUMMARY:Trip\nDTSTART;VALUE=DATE:20260720\n"
        "DTEND;VALUE=DATE:20260722\nSTATUS:CONFIRMED"
    )
    body = _event_body(card)
    assert body["start"] == {"date": "2026-07-20"}
    assert body["end"] == {"date": "2026-07-22"}


def test_event_body_recurrence_verbatim_from_fence():
    card = _card(
        "UID:r@x\nSUMMARY:Standup\n"
        "DTSTART;TZID=Europe/London:20260706T090000\n"
        "DTEND;TZID=Europe/London:20260706T091500\n"
        "RRULE:FREQ=WEEKLY;COUNT=6\nEXDATE:20260713T080000Z\nSTATUS:CONFIRMED"
    )
    body = _event_body(card)
    assert "RRULE:FREQ=WEEKLY;COUNT=6" in body["recurrence"]
    assert any(line.startswith("EXDATE") for line in body["recurrence"])


def test_recurrence_lines_unfold():
    body = "```ics\nRRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNT\n IL=20261225T000000Z\nUID:x\n```\n"
    assert _recurrence_lines(body) == [
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20261225T000000Z"
    ]


class _FakeService:
    """The minimal events() surface delete_event touches."""

    def __init__(self, items_pages, deleted_status=None):
        self.items_pages = list(items_pages)
        self.deleted = []
        self.deleted_status = deleted_status

    def events(self):
        return self

    def list(self, calendarId, iCalUID, showDeleted):
        page = self.items_pages.pop(0)
        return _Result({"items": page})

    def delete(self, calendarId, eventId):
        self.deleted.append(eventId)
        return _Result(None)


class _Result:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def test_delete_event_returns_cancelled_updated(monkeypatch):
    import mdcal.gcal as gcal

    fake = _FakeService(
        items_pages=[
            [{"id": "g1", "status": "confirmed"}],
            [
                {
                    "id": "g1",
                    "status": "cancelled",
                    "updated": "2026-07-04T10:00:00.500Z",
                }
            ],
        ]
    )
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    stamp = gcal.delete_event(None, "cal", "u@x")
    assert fake.deleted == ["g1"]
    assert stamp == dt.datetime(2026, 7, 4, 10, 0, 0, tzinfo=dt.timezone.utc)


def test_delete_event_already_gone_uses_local_now(monkeypatch):
    import mdcal.gcal as gcal

    fake = _FakeService(items_pages=[[], []])
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    stamp = gcal.delete_event(None, "cal", "gone@x")
    after = dt.datetime.now(dt.timezone.utc)
    assert fake.deleted == []
    assert before <= stamp <= after


def test_delete_event_skips_override_instances(monkeypatch):
    import mdcal.gcal as gcal

    fake = _FakeService(
        items_pages=[
            [
                {"id": "inst", "status": "confirmed", "recurringEventId": "g1"},
                {"id": "g1", "status": "confirmed"},
            ],
            [
                {
                    "id": "inst",
                    "status": "cancelled",
                    "recurringEventId": "g1",
                    "updated": "2026-07-04T09:00:00Z",
                },
                {"id": "g1", "status": "cancelled", "updated": "2026-07-04T11:00:00Z"},
            ],
        ]
    )
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    stamp = gcal.delete_event(None, "cal", "r@x")
    assert fake.deleted == ["g1"]
    assert stamp == dt.datetime(2026, 7, 4, 11, 0, tzinfo=dt.timezone.utc)


def test_event_body_requires_sequence():
    card = _card(
        "UID:t@x\nSUMMARY:NoSeq\nDTSTART:20260706T100000Z\n"
        "DTEND:20260706T103000Z\nSTATUS:CONFIRMED"
    )
    del card.yaml["sequence"]
    with pytest.raises(KeyError):
        _event_body(card)


def test_watermark_rejects_naive():
    with pytest.raises(ValueError, match="naive"):
        _watermark("2026-07-04T00:39:19")
