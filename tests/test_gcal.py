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


class _FakeExportService:
    """The calendars()+events() surface export_ics touches, with paging."""

    def __init__(self, pages, timezone="Europe/London", fail_on_page=None):
        self.pages = pages
        self.timezone = timezone
        self.fail_on_page = fail_on_page
        self.calls = 0

    def calendars(self):
        return self

    def get(self, calendarId):
        return _Result({"timeZone": self.timezone})

    def events(self):
        return self

    def list(self, calendarId, singleEvents, showDeleted, maxResults, pageToken):
        assert singleEvents is False and showDeleted is False
        index = int(pageToken or 0)
        if self.fail_on_page == index:
            raise OSError("page fetch failed")
        self.calls += 1
        page = {"items": self.pages[index]}
        if index + 1 < len(self.pages):
            page["nextPageToken"] = str(index + 1)
        return _Result(page)


def _gitem(uid, start, end, **extra):
    return {
        "id": extra.pop("gid", uid.split("@")[0]),
        "iCalUID": uid,
        "status": extra.pop("status", "confirmed"),
        "summary": extra.pop("summary", "Event"),
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-02T00:00:00.500Z",
        "start": start,
        "end": end,
        **extra,
    }


def _timed(iso, tz=None):
    point = {"dateTime": iso}
    if tz:
        point["timeZone"] = tz
    return point


def test_export_ics_stitches_pages_and_folds_cancelled(monkeypatch):
    import mdcal.gcal as gcal

    master = _gitem(
        "series@x",
        _timed("2026-01-05T10:00:00Z", "Europe/London"),
        _timed("2026-01-05T11:00:00Z", "Europe/London"),
        gid="m1",
        recurrence=["RRULE:FREQ=WEEKLY;COUNT=6"],
    )
    kept = _gitem(
        "series@x",
        _timed("2026-01-12T14:00:00Z", "UTC"),
        _timed("2026-01-12T15:00:00Z", "UTC"),
        gid="e1",
        recurringEventId="m1",
        originalStartTime=_timed("2026-01-12T10:00:00Z", "UTC"),
    )
    cancelled = _gitem(
        "series@x",
        _timed("2026-01-19T10:00:00Z", "UTC"),
        _timed("2026-01-19T11:00:00Z", "UTC"),
        gid="e2",
        status="cancelled",
        recurringEventId="m1",
        originalStartTime=_timed("2026-01-19T10:00:00Z", "UTC"),
    )
    single = _gitem(
        "single@x",
        {"date": "2026-02-01"},
        {"date": "2026-02-02"},
    )
    fake = _FakeExportService(pages=[[master, kept], [cancelled, single]])
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    out = icalendar.Calendar.from_ical(gcal.export_ics(None, "cal"))
    vevents = list(out.walk("VEVENT"))
    assert fake.calls == 2
    assert len(vevents) == 3
    m = [v for v in vevents if v.get("RRULE")][0]
    ex = m["EXDATE"]
    groups = ex if isinstance(ex, list) else [ex]
    (excluded,) = [d.dt for g in groups for d in g.dts]
    assert excluded.tzname() is not None
    assert str(excluded.tzinfo) == "Europe/London"
    assert excluded.hour == 10
    exc = [v for v in vevents if v.get("RECURRENCE-ID")][0]
    rid = exc["RECURRENCE-ID"].dt
    assert str(rid.tzinfo) == "Europe/London"
    assert rid.hour == 10
    allday = [v for v in vevents if str(v["UID"]) == "single@x"][0]
    assert allday["DTSTART"].dt == dt.date(2026, 2, 1)


def test_export_ics_partial_page_failure_crashes(monkeypatch):
    import mdcal.gcal as gcal

    single = _gitem("a@x", {"date": "2026-02-01"}, {"date": "2026-02-02"})
    fake = _FakeExportService(pages=[[single], [single]], fail_on_page=1)
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    with pytest.raises(OSError, match="page fetch failed"):
        gcal.export_ics(None, "cal")


def test_export_ics_round_trips_through_import(monkeypatch, tmp_path):
    import mdcal.gcal as gcal
    from mdcal.ics import import_ics

    master = _gitem(
        "series@x",
        _timed("2026-01-05T10:00:00Z", "Europe/London"),
        _timed("2026-01-05T11:00:00Z", "Europe/London"),
        gid="m1",
        recurrence=["RRULE:FREQ=WEEKLY;COUNT=6"],
    )
    exception = _gitem(
        "series@x",
        _timed("2026-01-12T14:00:00Z", "UTC"),
        _timed("2026-01-12T15:00:00Z", "UTC"),
        gid="e1",
        recurringEventId="m1",
        originalStartTime=_timed("2026-01-12T10:00:00Z", "UTC"),
    )
    master["htmlLink"] = "https://calendar.google.com/event?eid=abc"
    fake = _FakeExportService(pages=[[master, exception]])
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    ics = tmp_path / "owned.ics"
    ics.write_text(gcal.export_ics(None, "my-cal@group.calendar.google.com"))
    deck = str(tmp_path / "deck")
    counts = import_ics(deck, str(ics), "owned", tags=["area/work"])
    assert counts == {"created": 2, "updated": 0, "skipped": 0, "pruned": 0}
    again = import_ics(deck, str(ics), "owned", tags=["area/work"])
    assert again["skipped"] == 2 and again["created"] == 0

    import mddb

    db = mddb.MDDB(deck)
    cards = {
        db.read(cid).yaml["gcal_id"]: db.read(cid).yaml
        for (cid,) in db.conn.execute("SELECT id FROM entries")
    }
    # both cards carry the origin calendar; each its own Google event id
    assert set(cards) == {"m1", "e1"}
    assert all(
        y["gcal_calendar"] == "my-cal@group.calendar.google.com" for y in cards.values()
    )
    assert cards["m1"]["gcal_link"] == "https://calendar.google.com/event?eid=abc"


def test_export_ics_recurrence_exdate_rdate_lines(monkeypatch, tmp_path):
    import mdcal.gcal as gcal

    master = _gitem(
        "series@x",
        _timed("2026-01-05T10:00:00Z", "Europe/London"),
        _timed("2026-01-05T11:00:00Z", "Europe/London"),
        gid="m1",
        recurrence=[
            "RRULE:FREQ=WEEKLY;COUNT=6",
            "EXDATE;TZID=Europe/London:20260119T100000",
            "RDATE;VALUE=DATE:20260301",
        ],
    )
    fake = _FakeExportService(pages=[[master]])
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    (vevent,) = icalendar.Calendar.from_ical(gcal.export_ics(None, "cal")).walk(
        "VEVENT"
    )
    ex = vevent["EXDATE"]
    groups = ex if isinstance(ex, list) else [ex]
    (excluded,) = [d.dt for g in groups for d in g.dts]
    assert str(excluded.tzinfo) == "Europe/London" and excluded.hour == 10
    rd = vevent["RDATE"]
    groups = rd if isinstance(rd, list) else [rd]
    (added,) = [d.dt for g in groups for d in g.dts]
    assert added == dt.date(2026, 3, 1)


def test_export_ics_unknown_recurrence_line_crashes(monkeypatch):
    import mdcal.gcal as gcal

    master = _gitem(
        "series@x",
        _timed("2026-01-05T10:00:00Z", "Europe/London"),
        _timed("2026-01-05T11:00:00Z", "Europe/London"),
        gid="m1",
        recurrence=["EXRULE:FREQ=WEEKLY"],
    )
    fake = _FakeExportService(pages=[[master]])
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    with pytest.raises(ValueError, match="unsupported recurrence line"):
        gcal.export_ics(None, "cal")


def test_export_ics_skips_skeletal_cancelled_tombstones(monkeypatch):
    import mdcal.gcal as gcal

    live = _gitem("a@x", {"date": "2026-02-01"}, {"date": "2026-02-02"})
    tombstone = {"id": "gone123", "status": "cancelled"}
    fake = _FakeExportService(pages=[[live, tombstone]])
    monkeypatch.setattr(gcal, "_service", lambda credentials: fake)
    vevents = list(
        icalendar.Calendar.from_ical(gcal.export_ics(None, "cal")).walk("VEVENT")
    )
    assert [str(v["UID"]) for v in vevents] == ["a@x"]
