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

    starts = dict(
        db.conn.execute(
            "SELECT entry_rowid, value_num FROM entry_fields WHERE key='dtstart_epoch'"
        ).fetchall()
    )
    assert (
        window._masters(
            db,
            starts,
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


def _winter_anchored_oslo(tmp_path):
    ics = tmp_path / "dst.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "UID:dst-1@example.com\n"
        "DTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/Oslo:20231227T080000\n"
        "DTEND;TZID=Europe/Oslo:20231227T090000\n"
        "RRULE:FREQ=WEEKLY;INTERVAL=4;BYDAY=WE;WKST=MO\n"
        "SEQUENCE:0\nSTATUS:CONFIRMED\nSUMMARY:DST series\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "test")
    return deck


def test_recurrence_follows_dst_not_anchor_offset(tmp_path):
    deck = _winter_anchored_oslo(tmp_path)
    (july,) = events_in_window(
        mddb.MDDB(deck),
        dt.datetime(2026, 7, 6, tzinfo=UTC),
        dt.datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert july.start.astimezone(UTC) == dt.datetime(2026, 7, 8, 6, 0, tzinfo=UTC)
    (jan,) = events_in_window(
        mddb.MDDB(deck),
        dt.datetime(2027, 1, 18, tzinfo=UTC),
        dt.datetime(2027, 1, 25, tzinfo=UTC),
    )
    assert jan.start.astimezone(UTC) == dt.datetime(2027, 1, 20, 7, 0, tzinfo=UTC)


def test_exdate_suppresses_across_dst(tmp_path):
    ics = tmp_path / "dst2.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "UID:dst-2@example.com\n"
        "DTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/Oslo:20251203T080000\n"
        "DTEND;TZID=Europe/Oslo:20251203T090000\n"
        "RRULE:FREQ=WEEKLY;INTERVAL=4;BYDAY=WE;WKST=MO\n"
        "EXDATE;TZID=Europe/Oslo:20260708T080000\n"
        "SEQUENCE:0\nSTATUS:CONFIRMED\nSUMMARY:DST exdate\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "test")
    occ = events_in_window(
        mddb.MDDB(deck),
        dt.datetime(2026, 7, 6, tzinfo=UTC),
        dt.datetime(2026, 7, 13, tzinfo=UTC),
    )
    assert occ == []


def _weekly(tmp_path, extra_yaml=""):
    ics = tmp_path / "h.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "UID:hide-1@example.com\n"
        "DTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/London:20260504T130000\n"
        "DTEND;TZID=Europe/London:20260504T133000\n"
        "RRULE:FREQ=WEEKLY;COUNT=6;BYDAY=MO\n"
        "SEQUENCE:0\nSTATUS:CONFIRMED\nSUMMARY:Seminar\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "test")
    if extra_yaml:
        db = mddb.MDDB(deck)
        cid = db.conn.execute("SELECT id FROM entries").fetchone()[0]
        with db.editor(rationale="annotate") as e:
            card = e.read(cid)
            for k, v in extra_yaml.items():
                card.yaml[k] = v
            e.update(card, summary=card.summary)
    return deck


def _month(deck):
    return sorted(
        events_in_window(
            mddb.MDDB(deck),
            dt.datetime(2026, 5, 1, tzinfo=UTC),
            dt.datetime(2026, 6, 20, tzinfo=UTC),
        ),
        key=lambda o: o.start,
    )


def test_hide_occurrences_flags_only_those_instants(tmp_path):
    deck = _weekly(tmp_path)
    occ = _month(deck)
    second = occ[1].start
    db = mddb.MDDB(deck)
    cid = db.conn.execute("SELECT id FROM entries").fetchone()[0]
    with db.editor(rationale="hide one") as e:
        card = e.read(cid)
        card.yaml["hidden_occurrences"] = [int(second.timestamp())]
        e.update(card, summary=card.summary)
    occ = _month(deck)
    assert [o.hidden for o in occ] == [False, True, False, False, False, False]


def test_hidden_from_flags_the_ray(tmp_path):
    deck = _weekly(tmp_path)
    occ = _month(deck)
    cutoff = int(occ[3].start.timestamp())
    db = mddb.MDDB(deck)
    cid = db.conn.execute("SELECT id FROM entries").fetchone()[0]
    with db.editor(rationale="hide from") as e:
        card = e.read(cid)
        card.yaml["hidden_from"] = cutoff
        e.update(card, summary=card.summary)
    occ = _month(deck)
    assert [o.hidden for o in occ] == [False, False, False, True, True, True]


def test_series_tag_hides_all_including_exception_card(tmp_path):
    ics = tmp_path / "s.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        "BEGIN:VEVENT\nUID:hide-2@example.com\nDTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/London:20260504T130000\n"
        "DTEND;TZID=Europe/London:20260504T133000\n"
        "RRULE:FREQ=WEEKLY;COUNT=6;BYDAY=MO\nSEQUENCE:0\nSTATUS:CONFIRMED\n"
        "SUMMARY:Seminar\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:hide-2@example.com\nDTSTAMP:20260101T120000Z\n"
        "RECURRENCE-ID;TZID=Europe/London:20260511T130000\n"
        "DTSTART;TZID=Europe/London:20260511T150000\n"
        "DTEND;TZID=Europe/London:20260511T153000\n"
        "SEQUENCE:1\nSTATUS:CONFIRMED\nSUMMARY:Seminar (moved)\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "test")
    db = mddb.MDDB(deck)
    master = db.conn.execute(
        "SELECT e.id FROM entries e JOIN entry_fields r ON r.entry_rowid=e.rowid "
        "AND r.key='rrule'"
    ).fetchone()[0]
    with db.editor(rationale="hide series") as e:
        card = e.read(master)
        e.update(card, summary=card.summary, tags=["mdcal/hidden"])
    occ = _month(deck)
    assert occ and all(o.hidden for o in occ)
    assert any(o.card.title == "Seminar (moved)" for o in occ)


def test_hide_does_not_leak_across_sources(tmp_path):
    body = (
        "BEGIN:VEVENT\nUID:shared@example.com\nDTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/London:20260504T130000\n"
        "DTEND;TZID=Europe/London:20260504T133000\n"
        "RRULE:FREQ=WEEKLY;COUNT=6;BYDAY=MO\nSEQUENCE:0\nSTATUS:CONFIRMED\n"
        "SUMMARY:Shared\nEND:VEVENT\n"
    )
    cal = f"BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n{body}END:VCALENDAR\n"
    ics = tmp_path / "s.ics"
    ics.write_text(cal)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "alpha")
    import_ics(deck, str(ics), "beta")
    db = mddb.MDDB(deck)
    alpha = db.conn.execute(
        "SELECT e.id FROM entries e JOIN entry_fields s ON s.entry_rowid=e.rowid "
        "AND s.key='source' AND s.value_str='alpha'"
    ).fetchone()[0]
    with db.editor(rationale="hide alpha series") as e:
        card = e.read(alpha)
        e.update(card, summary=card.summary, tags=["mdcal/hidden"])
    occ = _month(deck)
    by_source = {}
    for o in occ:
        by_source.setdefault(o.card.yaml["source"], []).append(o.hidden)
    assert all(by_source["alpha"]) and not any(by_source["beta"])


def test_exception_suppression_does_not_leak_across_sources(tmp_path):
    master = (
        "BEGIN:VEVENT\nUID:sup@example.com\nDTSTAMP:20260101T120000Z\n"
        "DTSTART;TZID=Europe/London:20260504T130000\n"
        "DTEND;TZID=Europe/London:20260504T133000\n"
        "RRULE:FREQ=WEEKLY;COUNT=6;BYDAY=MO\nSEQUENCE:0\nSTATUS:CONFIRMED\n"
        "SUMMARY:Shared\nEND:VEVENT\n"
    )
    exception = (
        "BEGIN:VEVENT\nUID:sup@example.com\nDTSTAMP:20260101T120000Z\n"
        "RECURRENCE-ID;TZID=Europe/London:20260511T130000\n"
        "DTSTART;TZID=Europe/London:20260511T160000\n"
        "DTEND;TZID=Europe/London:20260511T163000\n"
        "SEQUENCE:1\nSTATUS:CONFIRMED\nSUMMARY:Moved\nEND:VEVENT\n"
    )
    deck = str(tmp_path / "deck")
    (tmp_path / "a.ics").write_text(
        f"BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n{master}{exception}END:VCALENDAR\n"
    )
    (tmp_path / "b.ics").write_text(
        f"BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n{master}END:VCALENDAR\n"
    )
    import_ics(deck, str(tmp_path / "a.ics"), "alpha")
    import_ics(deck, str(tmp_path / "b.ics"), "beta")
    occ = events_in_window(
        mddb.MDDB(deck),
        dt.datetime(2026, 5, 11, tzinfo=UTC),
        dt.datetime(2026, 5, 12, tzinfo=UTC),
    )
    # alpha: the 13:00 generated slot is suppressed, only the 16:00 exception shows.
    alpha = sorted(o.start.hour for o in occ if o.card.yaml["source"] == "alpha")
    beta = sorted(o.start.hour for o in occ if o.card.yaml["source"] == "beta")
    assert alpha == [16]
    assert beta == [13]
