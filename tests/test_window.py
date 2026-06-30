import datetime as dt

import mddb
import pytest

from mdcal.ics import import_ics
from mdcal.window import _normalise_until, events_in_window

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


def test_normalise_until():
    assert _normalise_until("FREQ=WEEKLY;UNTIL=20200412;BYDAY=MO") == (
        "FREQ=WEEKLY;UNTIL=20200412T235959Z;BYDAY=MO"
    )
    assert (
        _normalise_until("FREQ=WEEKLY;UNTIL=20200412")
        == "FREQ=WEEKLY;UNTIL=20200412T235959Z"
    )
    unchanged = "FREQ=WEEKLY;UNTIL=20201022T225959Z;BYDAY=FR"
    assert _normalise_until(unchanged) == unchanged


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
