import datetime as dt
from zoneinfo import ZoneInfo

import mddb
import pytest

from mdcal.ics import import_ics
from mdcal.ics import normalise_until
from mdcal.window import events_in_window

UTC = dt.timezone.utc

VTZ = """\
BEGIN:VTIMEZONE
TZID:Europe/London
BEGIN:DAYLIGHT
TZOFFSETFROM:+0000
TZOFFSETTO:+0100
TZNAME:BST
DTSTART:19700329T010000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0100
TZOFFSETTO:+0000
TZNAME:GMT
DTSTART:19701025T020000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE
"""


def _utc(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc)


def _seed(tmp_path, vevents):
    ics = (
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        + VTZ
        + vevents
        + "END:VCALENDAR\n"
    )
    path = tmp_path / "c.ics"
    path.write_text(ics)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(path), "test")
    return mddb.MDDB(deck)


def _vevent(uid, **lines):
    body = [f"UID:{uid}", "DTSTAMP:20260101T120000Z", "SEQUENCE:0", "TRANSP:OPAQUE"]
    body += [f"{k.replace('_', '-')}:{v}" for k, v in lines.items()]
    body.append("SUMMARY:E")
    return "BEGIN:VEVENT\n" + "\n".join(body) + "\nEND:VEVENT\n"


def test_concrete_half_open_overlap(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "p@x",
            DTSTART="20240115T100000Z",
            DTEND="20240115T110000Z",
            STATUS="CONFIRMED",
        ),
    )
    assert len(events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1))) == 1
    assert events_in_window(db, _utc(2024, 1, 15, 11), _utc(2024, 1, 15, 12)) == []
    assert events_in_window(db, _utc(2024, 1, 15, 8), _utc(2024, 1, 15, 10)) == []


def test_recurring_count(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "s1@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T103000Z",
            RRULE="FREQ=WEEKLY;COUNT=6;BYDAY=MO",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1))
    assert len(occ) == 5
    assert all(o.recurring and o.card.yaml["uid"] == "s1@x" for o in occ)


def test_recurring_overlap_starts_before_window(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "long@x",
            DTSTART="20240101T090000Z",
            DTEND="20240101T110000Z",
            RRULE="FREQ=WEEKLY;COUNT=1",
            STATUS="CONFIRMED",
        ),
    )
    assert len(events_in_window(db, _utc(2024, 1, 1, 10), _utc(2024, 1, 1, 12))) == 1
    assert events_in_window(db, _utc(2024, 1, 1, 11), _utc(2024, 1, 1, 13)) == []


def test_exdate_subtracted(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "ex@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T103000Z",
            RRULE="FREQ=WEEKLY;COUNT=4;BYDAY=MO",
            EXDATE="20240108T100000Z",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1))
    assert len(occ) == 3
    assert _utc(2024, 1, 8, 10) not in {o.start for o in occ}


def test_exception_moves_and_suppresses(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "m@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T103000Z",
            RRULE="FREQ=WEEKLY;COUNT=4;BYDAY=MO",
            STATUS="CONFIRMED",
        )
        + _vevent(
            "m@x",
            RECURRENCE_ID="20240108T100000Z",
            DTSTART="20240108T140000Z",
            DTEND="20240108T143000Z",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1))
    starts = {o.start for o in occ}
    assert _utc(2024, 1, 8, 10) not in starts
    assert _utc(2024, 1, 8, 14) in starts
    assert len(occ) == 4
    moved = [o for o in occ if o.start == _utc(2024, 1, 8, 14)]
    assert len(moved) == 1 and moved[0].recurring is False


def test_infinite_old_rooted_master(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "inf@x",
            DTSTART="20200106T100000Z",
            DTEND="20200106T103000Z",
            RRULE="FREQ=WEEKLY;BYDAY=MO",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2024, 1, 1), _utc(2024, 1, 8))
    assert len(occ) == 1 and occ[0].recurring
    assert occ[0].start == _utc(2024, 1, 1, 10)


def test_exception_moved_outside_window(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "mo@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T103000Z",
            RRULE="FREQ=WEEKLY;COUNT=4;BYDAY=MO",
            STATUS="CONFIRMED",
        )
        + _vevent(
            "mo@x",
            RECURRENCE_ID="20240108T100000Z",
            DTSTART="20240301T100000Z",
            DTEND="20240301T103000Z",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1))
    starts = {o.start for o in occ}
    assert _utc(2024, 1, 8, 10) not in starts
    assert all(o.start.month == 1 for o in occ)
    assert len(occ) == 3


def test_cancelled_master_renders_nothing(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "c@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T103000Z",
            RRULE="FREQ=WEEKLY;COUNT=4",
            STATUS="CANCELLED",
        ),
    )
    assert events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1)) == []


def test_cancelled_singleton_not_emitted(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "cs@x",
            DTSTART="20240110T100000Z",
            DTEND="20240110T110000Z",
            STATUS="CANCELLED",
        ),
    )
    assert events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1)) == []


def test_cancelled_exception_suppresses_but_hidden(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "ce@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T103000Z",
            RRULE="FREQ=WEEKLY;COUNT=2;BYDAY=MO",
            STATUS="CONFIRMED",
        )
        + _vevent(
            "ce@x",
            RECURRENCE_ID="20240108T100000Z",
            DTSTART="20240108T100000Z",
            DTEND="20240108T103000Z",
            STATUS="CANCELLED",
        ),
    )
    occ = events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1))
    assert len(occ) == 1
    assert occ[0].start == _utc(2024, 1, 1, 10)


def test_all_day_recurring(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "ad@x",
            **{"DTSTART;VALUE=DATE": "20240101", "DTEND;VALUE=DATE": "20240102"},
            RRULE="FREQ=DAILY;COUNT=3",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2024, 1, 1), _utc(2024, 2, 1))
    assert len(occ) == 3
    assert all(type(o.start) is dt.date for o in occ)
    assert {o.start for o in occ} == {
        dt.date(2024, 1, 1),
        dt.date(2024, 1, 2),
        dt.date(2024, 1, 3),
    }


def test_timezone_by_instant_not_wallclock(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "tz@x",
            **{
                "DTSTART;TZID=Europe/London": "20240701T010000",
                "DTEND;TZID=Europe/London": "20240701T013000",
            },
            RRULE="FREQ=WEEKLY;COUNT=1",
            STATUS="CONFIRMED",
        ),
    )
    assert len(events_in_window(db, _utc(2024, 7, 1, 0), _utc(2024, 7, 1, 1))) == 1
    assert events_in_window(db, _utc(2024, 7, 1, 0, 30), _utc(2024, 7, 1, 2)) == []


def test_empty_window(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "e@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T110000Z",
            STATUS="CONFIRMED",
        ),
    )
    assert events_in_window(db, _utc(2025, 1, 1), _utc(2025, 2, 1)) == []


def testnormalise_until():
    assert normalise_until("FREQ=WEEKLY;UNTIL=20200412;BYDAY=MO") == (
        "FREQ=WEEKLY;UNTIL=20200412T235959Z;BYDAY=MO"
    )
    assert (
        normalise_until("FREQ=WEEKLY;UNTIL=20200412")
        == "FREQ=WEEKLY;UNTIL=20200412T235959Z"
    )
    unchanged = "FREQ=WEEKLY;UNTIL=20201022T225959Z;BYDAY=FR"
    assert normalise_until(unchanged) == unchanged


def test_date_form_until_tz_aware_master(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "du@x",
            **{
                "DTSTART;TZID=Europe/London": "20220818T163000",
                "DTEND;TZID=Europe/London": "20220818T170000",
            },
            RRULE="FREQ=WEEKLY;UNTIL=20220907;BYDAY=TH;WKST=MO",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2022, 8, 1), _utc(2022, 10, 1))
    assert len(occ) == 3 and all(o.recurring for o in occ)


def test_date_form_until_all_day_master(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "dad@x",
            **{"DTSTART;VALUE=DATE": "20200406", "DTEND;VALUE=DATE": "20200407"},
            RRULE="FREQ=WEEKLY;UNTIL=20200412;BYDAY=MO",
            STATUS="CONFIRMED",
        ),
    )
    occ = events_in_window(db, _utc(2020, 4, 1), _utc(2020, 5, 1))
    assert len(occ) == 1 and occ[0].start == dt.date(2020, 4, 6)


def test_naive_window_raises(tmp_path):
    db = _seed(
        tmp_path,
        _vevent(
            "n@x",
            DTSTART="20240101T100000Z",
            DTEND="20240101T110000Z",
            STATUS="CONFIRMED",
        ),
    )
    with pytest.raises(ValueError, match="naive"):
        events_in_window(db, dt.datetime(2024, 1, 1), dt.datetime(2024, 2, 1))


def _master_card(
    rrule,
    dtstart="DTSTART;TZID=Europe/London:20200106T090000",
    dtend="DTEND;TZID=Europe/London:20200106T100000",
):
    import icalendar

    from mdcal.ics import vevent_to_card

    cal = (
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\nBEGIN:VEVENT\n"
        f"UID:bound@x\nSUMMARY:Bound test\n{dtstart}\n{dtend}\n"
        f"RRULE:{rrule}\nSTATUS:CONFIRMED\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    vevent = list(icalendar.Calendar.from_ical(cal).walk("VEVENT"))[0]
    return vevent_to_card(vevent, "research")


def test_recurrence_end_epoch_count():
    card = _master_card("FREQ=WEEKLY;COUNT=3")
    last_end = dt.datetime(2020, 1, 20, 10, 0, tzinfo=ZoneInfo("Europe/London"))
    assert card.yaml["recurrence_end_epoch"] == int(last_end.timestamp())


def test_recurrence_end_epoch_date_form_until():
    card = _master_card("FREQ=WEEKLY;UNTIL=20200412")
    ends = card.yaml["recurrence_end_epoch"]
    last_start = dt.datetime(2020, 4, 6, 9, 0, tzinfo=ZoneInfo("Europe/London"))
    assert ends == int((last_start + dt.timedelta(hours=1)).timestamp())


def test_recurrence_end_epoch_unbounded_sentinel():
    from mdcal.ics import RECURRENCE_FOREVER_EPOCH

    card = _master_card("FREQ=WEEKLY")
    assert card.yaml["recurrence_end_epoch"] == RECURRENCE_FOREVER_EPOCH


def test_recurrence_end_epoch_all_day():
    card = _master_card(
        "FREQ=DAILY;COUNT=2",
        dtstart="DTSTART;VALUE=DATE:20200106",
        dtend="DTEND;VALUE=DATE:20200107",
    )
    last_end = dt.datetime(2020, 1, 8, tzinfo=dt.timezone.utc)
    assert card.yaml["recurrence_end_epoch"] == int(last_end.timestamp())


def test_dead_master_not_loaded(tmp_path):
    db = _seed(
        tmp_path,
        "BEGIN:VEVENT\nUID:dead@x\nSUMMARY:Dead weekly\n"
        "DTSTART;TZID=Europe/London:20200106T090000\n"
        "DTEND;TZID=Europe/London:20200106T100000\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\nSTATUS:CONFIRMED\nEND:VEVENT\n",
    )
    from mdcal import window

    assert (
        window._masters(
            db,
            int(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp()),
            int(dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc).timestamp()),
        )
        == []
    )
    assert (
        events_in_window(
            db,
            dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        == []
    )
    live = events_in_window(
        db,
        dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2020, 2, 1, tzinfo=dt.timezone.utc),
    )
    assert len(live) == 3


def test_unbounded_master_without_field_still_loads(tmp_path):
    db = _seed(
        tmp_path,
        "BEGIN:VEVENT\nUID:legacy@x\nSUMMARY:Legacy weekly\n"
        "DTSTART;TZID=Europe/London:20200106T090000\n"
        "DTEND;TZID=Europe/London:20200106T100000\n"
        "RRULE:FREQ=WEEKLY;COUNT=400\nSTATUS:CONFIRMED\nEND:VEVENT\n",
    )
    cid = db.conn.execute("SELECT id FROM entries").fetchone()[0]
    with db.editor(rationale="strip bound (pre-field card)") as e:
        card = e.read(cid)
        del card.yaml["recurrence_end_epoch"]
        e.update(card, summary=card.summary)
    db = mddb.MDDB(str(tmp_path / "deck"))
    occ = events_in_window(
        db,
        dt.datetime(2020, 3, 2, tzinfo=dt.timezone.utc),
        dt.datetime(2020, 3, 9, tzinfo=dt.timezone.utc),
    )
    assert len(occ) == 1


def test_recurrence_end_epoch_zero_instance_rule():
    card = _master_card(
        "FREQ=WEEKLY;UNTIL=20200105T235959Z;BYDAY=TH",
        dtstart="DTSTART;TZID=Europe/London:20200106T090000",
        dtend="DTEND;TZID=Europe/London:20200106T100000",
    )
    assert card.yaml["recurrence_end_epoch"] == card.yaml["dtstart_epoch"]


def test_rdate_extends_series_beyond_until(tmp_path):
    ics = tmp_path / "r.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "UID:rd-1@example.com\n"
        "DTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/London:20260105T130000\n"
        "DTEND;TZID=Europe/London:20260105T133000\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20260119T235959Z;BYDAY=MO\n"
        "RDATE;TZID=Europe/London:20260210T130000,20260224T130000\n"
        "SEQUENCE:0\nSTATUS:CONFIRMED\nSUMMARY:Extended series\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "test")
    db = mddb.MDDB(deck)
    bound = db.conn.execute(
        "SELECT value_num FROM entry_fields WHERE key='recurrence_end_epoch'"
    ).fetchone()[0]
    assert bound >= dt.datetime(2026, 2, 24, 13, 30, tzinfo=UTC).timestamp()
    occ = events_in_window(
        db,
        dt.datetime(2026, 2, 1, tzinfo=UTC),
        dt.datetime(2026, 3, 1, tzinfo=UTC),
    )
    starts = sorted(o.start.isoformat() for o in occ)
    assert starts == ["2026-02-10T13:00:00+00:00", "2026-02-24T13:00:00+00:00"]


def test_rdate_duplicate_of_generated_instance_not_doubled(tmp_path):
    ics = tmp_path / "r.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "UID:rd-2@example.com\n"
        "DTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/London:20260105T130000\n"
        "DTEND;TZID=Europe/London:20260105T133000\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20260119T235959Z;BYDAY=MO\n"
        "RDATE;TZID=Europe/London:20260112T130000\n"
        "SEQUENCE:0\nSTATUS:CONFIRMED\nSUMMARY:Overlapping rdate\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "test")
    occ = events_in_window(
        mddb.MDDB(deck),
        dt.datetime(2026, 1, 12, tzinfo=UTC),
        dt.datetime(2026, 1, 13, tzinfo=UTC),
    )
    assert len(occ) == 1
