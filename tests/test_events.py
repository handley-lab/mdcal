import datetime as dt
import types

import mddb
import pytest

import mdcal.events
from mdcal.events import (
    _committed,
    _writable,
    create_event,
    delete_event,
    occurrences_json,
    set_hidden,
    undo_event,
    undo_token,
    update_event,
    window_bound,
)
from mdcal.ics import import_ics, vevent_to_card

UTC = dt.timezone.utc


@pytest.fixture
def db(tmp_path, ics_sample):
    ics = tmp_path / "sample.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    return mddb.MDDB(deck)


@pytest.fixture
def deck(db, tmp_path):
    return str(tmp_path / "deck")


@pytest.fixture
def synced():
    """A fake write-through boundary: the synced map plus a recording gapi."""
    stamp = dt.datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    calls = types.SimpleNamespace(pushes=[], patches=[], deletes=[], stamp=stamp)

    def push(calendar_id, rendered):
        calls.pushes.append((calendar_id, rendered))
        return stamp

    def patch(calendar_id, uid, slot, rendered):
        calls.patches.append((calendar_id, uid, slot, rendered))
        return stamp

    def delete(calendar_id, uid):
        calls.deletes.append((calendar_id, uid))
        return stamp

    calls.gapi = types.SimpleNamespace(push=push, patch=patch, delete=delete)
    calls.map = {"research": "cal@group.calendar.google.com"}
    calls.kw = {"synced": calls.map, "gapi": calls.gapi}
    return calls


def _gapi(synced, **overrides):
    kw = {"push": synced.gapi.push, "patch": synced.gapi.patch}
    kw["delete"] = synced.gapi.delete
    kw.update(overrides)
    return types.SimpleNamespace(**kw)


def _screate(deck, form, synced):
    return create_event(deck, form, synced_sources={"research"}, **synced.kw)


def _window(deck_or_db, y1, m1, d1, y2, m2, d2, synced=()):
    db = deck_or_db if isinstance(deck_or_db, mddb.MDDB) else mddb.MDDB(deck_or_db)
    return occurrences_json(
        db,
        dt.datetime(y1, m1, d1, tzinfo=UTC),
        dt.datetime(y2, m2, d2, tzinfo=UTC),
        synced,
    )


def test_module_imports_without_google_modules():
    for value in vars(mdcal.events).values():
        if isinstance(value, types.ModuleType):
            assert not value.__name__.startswith(("google", "googleapiclient"))


def test_window_bound_parses_offset_forms():
    assert window_bound("2024-01-15T10:00:00Z") == dt.datetime(
        2024, 1, 15, 10, tzinfo=dt.timezone.utc
    )
    assert window_bound("2024-01-15T10:00:00+01:00").utcoffset() == dt.timedelta(
        hours=1
    )


def test_window_bound_rejects_naive():
    with pytest.raises(ValueError, match="offset"):
        window_bound("2024-01-15T10:00:00")


def test_window_bound_rejects_garbage():
    with pytest.raises(ValueError):
        window_bound("not-a-date")


def test_timed_occurrence_shape(db):
    occ = _window(db, 2024, 1, 15, 2024, 1, 16)
    (plain,) = [o for o in occ if o["title"] == "Plain meeting"]
    assert plain["title"] == "Plain meeting"
    assert plain["uid"] == "plain-1@example.com"
    assert plain["start"] == "2024-01-15T10:00:00+00:00"
    assert plain["end"] == "2024-01-15T11:00:00+00:00"
    assert plain["all_day"] is False
    assert plain["location"] == "Room 1"
    assert plain["tzid"] == "Europe/London"
    assert plain["status"] == "CONFIRMED"
    assert plain["editable"] is False
    assert plain["id"]


def test_recurring_expansion_and_exdate(db):
    occ = _window(db, 2024, 1, 1, 2024, 3, 1)
    standups = [o for o in occ if o["title"] == "Weekly standup"]
    starts = {o["start"] for o in standups}
    assert all(o["recurring"] for o in standups)
    assert "2024-01-22T13:00:00+00:00" not in starts
    assert "2024-01-15T13:00:00+00:00" not in starts
    assert len(standups) == 4


def test_exception_replaces_master_instance(db):
    occ = _window(db, 2024, 1, 15, 2024, 1, 16)
    (moved,) = [o for o in occ if o["title"] == "Weekly standup (moved)"]
    assert moved["start"] == "2024-01-15T14:00:00+00:00"
    assert moved["recurring"] is False
    assert moved["uid"] == "series-1@example.com"


def test_all_day_dates_not_datetimes(db):
    (trip,) = _window(db, 2024, 2, 1, 2024, 2, 5)
    assert trip["title"] == "Conference trip"
    assert trip["start"] == "2024-02-01"
    assert trip["end"] == "2024-02-03"
    assert trip["all_day"] is True
    assert trip["tzid"] is None


def test_empty_window(db):
    assert _window(db, 2030, 1, 1, 2030, 2, 1) == []


TIMED = {
    "title": "Coffee with Sam",
    "start": "2024-06-03T10:00",
    "end": "2024-06-03T10:30",
    "all_day": False,
    "location": "CMS",
    "description": "Catch up on the grant.",
    "tzid": "Europe/London",
}
EDIT = {**TIMED, "repeat": "none", "time_changed": True}


def test_create_timed_round_trips(deck):
    card_id = create_event(deck, TIMED)
    (occ,) = [o for o in _window(deck, 2024, 6, 3, 2024, 6, 4) if o["editable"]]
    assert occ["id"] == card_id
    assert occ["title"] == "Coffee with Sam"
    assert occ["start"] == "2024-06-03T10:00:00+01:00"
    assert occ["end"] == "2024-06-03T10:30:00+01:00"
    assert occ["location"] == "CMS"
    assert occ["description"] == "Catch up on the grant."
    assert occ["tzid"] == "Europe/London"
    assert occ["uid"].endswith("@lovelace.fritz.box")


def test_create_all_day_inclusive_end(deck):
    create_event(
        deck,
        {"title": "Trip", "start": "2024-06-10", "end": "2024-06-11", "all_day": True},
    )
    (occ,) = [o for o in _window(deck, 2024, 6, 10, 2024, 6, 13) if o["editable"]]
    assert occ["start"] == "2024-06-10"
    assert occ["end"] == "2024-06-12"
    assert occ["all_day"] is True


def test_create_all_day_single_day(deck):
    create_event(
        deck,
        {"title": "Away", "start": "2024-06-20", "end": "2024-06-20", "all_day": True},
    )
    (occ,) = [o for o in _window(deck, 2024, 6, 20, 2024, 6, 21) if o["editable"]]
    assert occ["start"] == "2024-06-20"
    assert occ["end"] == "2024-06-21"


def test_create_weekly_recurring(deck):
    create_event(deck, {**EDIT, "title": "Standup", "repeat": "weekly"})
    occ = [o for o in _window(deck, 2024, 6, 1, 2024, 7, 1) if o["editable"]]
    assert len(occ) == 4
    assert all(o["recurring"] and o["rrule"] == "FREQ=WEEKLY" for o in occ)


def test_create_rejects_bad_forms(deck):
    with pytest.raises(ValueError, match="missing form field"):
        create_event(deck, {"title": "x", "start": "2024-06-03T10:00"})
    with pytest.raises(ValueError, match="repeat"):
        create_event(deck, {**TIMED, "repeat": "fortnightly"})
    with pytest.raises(ValueError, match="wall-clock"):
        create_event(deck, {**TIMED, "start": "2024-06-03T10:00+01:00"})
    with pytest.raises(ValueError, match="end before start"):
        create_event(deck, {**TIMED, "end": "2024-06-03T09:00"})
    with pytest.raises(ValueError, match="end day before start day"):
        create_event(
            deck,
            {"title": "T", "start": "2024-06-10", "end": "2024-06-09", "all_day": True},
        )


def test_update_retitle_moves_relpath_keeps_identity(deck):
    card_id = create_event(deck, TIMED)
    db = mddb.MDDB(deck)
    uid = db.read(card_id).yaml["uid"]
    old_relpath = db.conn.execute(
        "SELECT relpath FROM entries WHERE id=?", (card_id,)
    ).fetchone()[0]

    update_event(deck, card_id, {**EDIT, "title": "Coffee (moved)", "location": ""})
    db = mddb.MDDB(deck)
    card = db.read(card_id)
    assert card.title == "Coffee (moved)"
    assert card.yaml["uid"] == uid
    assert "location" not in card.yaml
    new_relpath = db.conn.execute(
        "SELECT relpath FROM entries WHERE id=?", (card_id,)
    ).fetchone()[0]
    assert new_relpath != old_relpath
    assert new_relpath.endswith(old_relpath[-15:])


def test_update_imported_refused(db):
    deck = str(db.root)
    (card_id,) = [
        r[0]
        for r in db.conn.execute(
            "SELECT e.id FROM entries e JOIN entry_fields u "
            "ON u.entry_rowid=e.rowid AND u.key='uid' "
            "AND u.value_str='plain-1@example.com'"
        )
    ]
    with pytest.raises(PermissionError, match="read-only"):
        update_event(deck, card_id, EDIT)
    with pytest.raises(PermissionError, match="read-only"):
        delete_event(deck, card_id, "event")


def test_update_unknown_id(deck):
    with pytest.raises(KeyError):
        update_event(deck, "no-such-id", EDIT)


def test_delete_event_scope(deck):
    card_id = create_event(deck, TIMED)
    delete_event(deck, card_id, "event")
    assert [o for o in _window(deck, 2024, 6, 3, 2024, 6, 4) if o["editable"]] == []


def test_delete_event_scope_rejects_recurring(deck):
    card_id = create_event(deck, {**TIMED, "repeat": "weekly"})
    with pytest.raises(ValueError, match="recurring master"):
        delete_event(deck, card_id, "event")


def test_delete_unknown_scope(deck):
    card_id = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="unknown scope"):
        delete_event(deck, card_id, "all")


def test_delete_one_scope_rejects_singleton(deck):
    card_id = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="non-recurring"):
        delete_event(deck, card_id, "one", "2024-06-03T10:00:00+01:00")


def test_delete_one_occurrence_exdates_master(deck):
    card_id = create_event(deck, {**EDIT, "title": "Standup", "repeat": "weekly"})
    delete_event(deck, card_id, "one", "2024-06-10T10:00:00+01:00")
    occ = [o for o in _window(deck, 2024, 6, 1, 2024, 7, 1) if o["editable"]]
    assert len(occ) == 3
    assert "2024-06-10T10:00:00+01:00" not in {o["start"] for o in occ}
    card = mddb.MDDB(deck).read(card_id)
    assert len(card.yaml["exdate"]) == 1
    assert "EXDATE" in card.body


def test_delete_one_all_day(deck):
    card_id = create_event(
        deck,
        {
            "title": "Daily",
            "start": "2024-06-10",
            "end": "2024-06-10",
            "all_day": True,
            "repeat": "daily",
        },
    )
    delete_event(deck, card_id, "one", "2024-06-11")
    occ = [o for o in _window(deck, 2024, 6, 10, 2024, 6, 13) if o["editable"]]
    assert {o["start"] for o in occ} == {"2024-06-10", "2024-06-12"}
    assert mddb.MDDB(deck).read(card_id).yaml["exdate"] == [dt.date(2024, 6, 11)]


def test_update_preserves_exdates(deck):
    card_id = create_event(deck, {**EDIT, "title": "Standup", "repeat": "weekly"})
    delete_event(deck, card_id, "one", "2024-06-10T10:00:00+01:00")
    update_event(
        deck, card_id, {**EDIT, "title": "Standup (renamed)", "repeat": "keep"}
    )
    occ = [o for o in _window(deck, 2024, 6, 1, 2024, 7, 1) if o["editable"]]
    assert len(occ) == 3
    assert "2024-06-10T10:00:00+01:00" not in {o["start"] for o in occ}
    assert mddb.MDDB(deck).read(card_id).yaml["exdate"] == [
        dt.datetime(2024, 6, 10, 9, 0, tzinfo=UTC)
    ]


def test_delete_series(deck):
    card_id = create_event(deck, {**EDIT, "title": "Standup", "repeat": "weekly"})
    create_event(deck, TIMED)
    delete_event(deck, card_id, "series")
    occ = [o for o in _window(deck, 2024, 6, 1, 2024, 7, 1) if o["editable"]]
    assert [o["title"] for o in occ] == ["Coffee with Sam"]


def test_committed_retries_conflicts(deck):
    attempts = []

    def flaky(db):
        attempts.append(db)
        if len(attempts) < 3:
            raise mddb.ConflictError("head moved")
        return "done"

    assert _committed(deck, flaky) == "done"
    assert len(attempts) == 3

    def hopeless(db):
        raise mddb.ConflictError("head moved")

    with pytest.raises(mddb.ConflictError):
        _committed(deck, hopeless)


RECUR = {
    "title": "Standup",
    "start": "2024-06-03T09:00",
    "end": "2024-06-03T09:15",
    "all_day": False,
    "tzid": "Europe/London",
    "repeat": "weekly",
}


def _card_of(deck, card_id):
    return mddb.MDDB(deck).read(card_id)


def test_synced_create_pushes_first_and_guards(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    card = _card_of(deck, card_id)
    assert card.yaml["source"] == "research"
    assert card.yaml["sequence"] == 1
    assert card.yaml["dispatched"] == synced.stamp
    ((calendar_id, rendered),) = synced.pushes
    assert calendar_id == "cal@group.calendar.google.com"
    assert rendered.yaml["sequence"] == 1
    (occ,) = _window(deck, 2024, 6, 3, 2024, 6, 4, synced=synced.map)
    assert occ["editable"]


def test_synced_create_failure_leaves_nothing(deck, synced):
    def boom(calendar_id, rendered):
        raise RuntimeError("google write failed: 503")

    head = mddb.MDDB(deck).head()
    with pytest.raises(RuntimeError, match="google write failed"):
        create_event(
            deck,
            TIMED,
            synced_sources={"research"},
            synced=synced.map,
            gapi=_gapi(synced, push=boom),
        )
    assert mddb.MDDB(deck).head() == head


def test_synced_edit_bumps_sequence(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    update_event(deck, card_id, {**EDIT, "title": "Coffee moved"}, **synced.kw)
    card = _card_of(deck, card_id)
    assert card.title == "Coffee moved"
    assert card.yaml["sequence"] == 2
    assert card.yaml["dispatched"] == synced.stamp
    assert len(synced.pushes) == 2
    assert synced.pushes[1][1].yaml["sequence"] == 2


def test_synced_edit_failure_changes_nothing(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    head = mddb.MDDB(deck).head()

    def boom(calendar_id, rendered):
        raise RuntimeError("google write failed: timeout")

    with pytest.raises(RuntimeError, match="google write failed"):
        update_event(
            deck,
            card_id,
            {**EDIT, "title": "Never lands"},
            synced=synced.map,
            gapi=_gapi(synced, push=boom),
        )
    assert mddb.MDDB(deck).head() == head
    assert _card_of(deck, card_id).title == "Coffee with Sam"


def test_synced_delete_cancels_instead_of_deleting(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    delete_event(deck, card_id, "event", **synced.kw)
    ((_, uid),) = synced.deletes
    card = _card_of(deck, card_id)
    assert card.yaml["uid"] == uid
    assert card.yaml["event_status"] == "CANCELLED"
    assert card.yaml["dispatched"] == synced.stamp
    assert card.summary.startswith("[cancelled]")
    assert _window(deck, 2024, 6, 3, 2024, 6, 4, synced=synced.map) == []


def test_synced_delete_one_pushes_exdate_upsert(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    delete_event(deck, card_id, "one", start="2024-06-10T09:00:00+01:00", **synced.kw)
    assert synced.deletes == []
    assert len(synced.pushes) == 2
    card = _card_of(deck, card_id)
    assert card.yaml["sequence"] == 2
    assert card.yaml["dispatched"] == synced.stamp
    starts = [
        o["start"] for o in _window(deck, 2024, 6, 3, 2024, 6, 25, synced=synced.map)
    ]
    assert "2024-06-10T09:00:00+01:00" not in starts and len(starts) == 3


def _add_rendered(deck, rendered):
    def add(db):
        with db.editor(rationale="test exception card") as editor:
            return editor.create(
                title=rendered.title,
                summary=rendered.summary,
                body=rendered.body,
                relpath=rendered.relpath,
                yaml=rendered.yaml,
            ).id

    return _committed(deck, add)


def _exception_vevent(uid, text):
    import icalendar

    return list(
        icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\nBEGIN:VEVENT\n"
            f"UID:{uid}\n{text}\nEND:VEVENT\nEND:VCALENDAR\n"
        ).walk("VEVENT")
    )[0]


def test_synced_series_delete_cancels_exception_cards_too(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    uid = _card_of(deck, card_id).yaml["uid"]
    exception = _exception_vevent(
        uid,
        "RECURRENCE-ID;TZID=Europe/London:20240617T090000\n"
        "SUMMARY:Standup (moved)\n"
        "DTSTART;TZID=Europe/London:20240617T110000\n"
        "DTEND;TZID=Europe/London:20240617T111500\n"
        "STATUS:CONFIRMED\nSEQUENCE:1",
    )
    exc_id = _add_rendered(deck, vevent_to_card(exception, "research"))
    delete_event(deck, card_id, "series", **synced.kw)
    assert len(synced.deletes) == 1
    for each in (card_id, exc_id):
        card = _card_of(deck, each)
        assert card.yaml["event_status"] == "CANCELLED"
        assert card.yaml["dispatched"] == synced.stamp
    assert _window(deck, 2024, 6, 3, 2024, 7, 1, synced=synced.map) == []


def test_synced_exception_editable_as_one_occurrence_only(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    uid = _card_of(deck, card_id).yaml["uid"]
    exception = _exception_vevent(
        uid,
        "RECURRENCE-ID;TZID=Europe/London:20240610T090000\n"
        "SUMMARY:Standup (moved)\n"
        "DTSTART;TZID=Europe/London:20240610T140000\n"
        "DTEND;TZID=Europe/London:20240610T141500\n"
        "STATUS:CONFIRMED\nSEQUENCE:1",
    )
    exc_id = _add_rendered(deck, vevent_to_card(exception, "research"))
    with pytest.raises(ValueError, match="one occurrence"):
        update_event(
            deck,
            exc_id,
            {**RECUR, "repeat": "keep", "time_changed": False, "title": "nope"},
            scope="series",
            **synced.kw,
        )
    with pytest.raises(ValueError, match="one occurrence"):
        delete_event(deck, exc_id, "event", **synced.kw)
    exc_occ = [
        o
        for o in _window(deck, 2024, 6, 10, 2024, 6, 11, synced=synced.map)
        if o["id"] == exc_id
    ]
    assert exc_occ and exc_occ[0]["editable"]


def test_create_on_multi_synced_deck_is_ambiguous(deck):
    with pytest.raises(ValueError, match="create routing is ambiguous"):
        create_event(deck, TIMED, synced_sources={"ivf", "travel"})


def test_grid_edit_preserves_local_tags(deck):
    card_id = create_event(deck, TIMED)
    db = mddb.MDDB(deck)
    with db.editor(rationale="classify") as e:
        card = e.read(card_id)
        e.update(card, summary=card.summary, tags=["area/home", "mdcal/hidden"])
    update_event(deck, card_id, {**EDIT, "title": "Renamed"})
    card = _card_of(deck, card_id)
    assert card.title == "Renamed"
    assert card.yaml["tags"] == ["area/home", "mdcal/hidden"]


def test_delete_one_preserves_local_tags(deck):
    card_id = create_event(deck, {**TIMED, "repeat": "weekly"})
    db = mddb.MDDB(deck)
    with db.editor(rationale="classify") as e:
        card = e.read(card_id)
        e.update(card, summary=card.summary, tags=["area/home"])
    (first, *_rest) = sorted(
        _window(deck, 2024, 6, 3, 2024, 6, 24), key=lambda o: o["start"]
    )
    delete_event(deck, card_id, "one", first["start"])
    assert _card_of(deck, card_id).yaml["tags"] == ["area/home"]


def test_synced_cancel_preserves_local_tags(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    db = mddb.MDDB(deck)
    with db.editor(rationale="classify") as e:
        card = e.read(card_id)
        e.update(card, summary=card.summary, tags=["area/research"])
    delete_event(deck, card_id, "event", **synced.kw)
    card = _card_of(deck, card_id)
    assert card.yaml["event_status"] == "CANCELLED"
    assert card.yaml["tags"] == ["area/research"]


def test_set_hidden_round_trip_on_read_only_import(db):
    deck = str(db.root)
    (card_id,) = [
        r[0]
        for r in db.conn.execute(
            "SELECT e.id FROM entries e JOIN entry_fields u "
            "ON u.entry_rowid=e.rowid AND u.key='uid' "
            "AND u.value_str='plain-1@example.com'"
        )
    ]
    assert not _writable(db.read(card_id).yaml, ())
    set_hidden(deck, card_id, True)
    assert mddb.MDDB(deck).read(card_id).yaml["tags"] == ["mdcal/hidden"]
    (occ,) = [o for o in _window(deck, 2024, 1, 15, 2024, 1, 16) if o["id"] == card_id]
    assert occ["tags"] == ["mdcal/hidden"] and not occ["editable"]
    set_hidden(deck, card_id, False)
    assert "tags" not in mddb.MDDB(deck).read(card_id).yaml


def test_synced_edit_conflicts_when_card_races(deck, synced):
    card_id = _screate(deck, TIMED, synced)

    def racing_push(calendar_id, rendered):
        update_event(deck, card_id, {**EDIT, "title": "Racer won"}, **synced.kw)
        return synced.stamp

    with pytest.raises(mddb.ConflictError, match="card changed since read"):
        update_event(
            deck,
            card_id,
            {**EDIT, "title": "Stale loser"},
            synced=synced.map,
            gapi=_gapi(synced, push=racing_push),
        )
    assert _card_of(deck, card_id).title == "Racer won"


def test_synced_delete_conflicts_when_card_races(deck, synced):
    card_id = _screate(deck, TIMED, synced)

    def racing_delete(calendar_id, uid):
        update_event(deck, card_id, {**EDIT, "title": "Racer won"}, **synced.kw)
        return synced.stamp

    with pytest.raises(mddb.ConflictError, match="card changed since read"):
        delete_event(
            deck,
            card_id,
            "event",
            synced=synced.map,
            gapi=_gapi(synced, delete=racing_delete),
        )
    card = _card_of(deck, card_id)
    assert card.title == "Racer won" and card.yaml["event_status"] != "CANCELLED"


def test_synced_edit_tolerates_unrelated_commits(deck, synced):
    card_id = _screate(deck, TIMED, synced)

    def unrelated_commit_push(calendar_id, rendered):
        create_event(
            deck,
            {
                "title": "Unrelated",
                "start": "2024-07-01T09:00",
                "end": "2024-07-01T09:30",
                "all_day": False,
                "tzid": "Europe/London",
            },
        )
        return synced.stamp

    update_event(
        deck,
        card_id,
        {**EDIT, "title": "Coffee moved"},
        synced=synced.map,
        gapi=_gapi(synced, push=unrelated_commit_push),
    )
    assert _card_of(deck, card_id).title == "Coffee moved"


def test_create_uses_client_device_zone(deck):
    card_id = create_event(
        deck,
        {
            "title": "Toronto meeting",
            "start": "2024-06-03T10:00",
            "end": "2024-06-03T10:30",
            "all_day": False,
            "tzid": "America/Toronto",
        },
    )
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["tzid"] == "America/Toronto"
    assert card.yaml["dtstart"].utcoffset() == dt.timedelta(hours=-4)


def test_create_timed_requires_tzid(deck):
    with pytest.raises(ValueError, match="missing form field: tzid"):
        create_event(
            deck,
            {
                "title": "No zone",
                "start": "2024-06-03T10:00",
                "end": "2024-06-03T10:30",
                "all_day": False,
            },
        )


def test_create_rejects_unknown_tzid(deck):
    with pytest.raises(ValueError, match="unknown tzid"):
        create_event(deck, {**TIMED, "tzid": "Mars/Olympus_Mons"})


def _recurring(deck):
    return create_event(deck, {**EDIT, "title": "Seminar", "repeat": "weekly"})


def _hidden_starts(deck):
    return {
        o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1) if o.get("hidden")
    }


def test_hide_one_flags_only_that_occurrence(deck):
    _recurring(deck)
    (target,) = [
        o["start"]
        for o in _window(deck, 2024, 6, 1, 2024, 7, 1)
        if o["start"].startswith("2024-06-17")
    ]
    card_id = _window(deck, 2024, 6, 1, 2024, 7, 1)[0]["id"]
    set_hidden(deck, card_id, True, "one", target)
    assert _hidden_starts(deck) == {target}


def test_hide_from_flags_the_ray(deck):
    _recurring(deck)
    occ = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    card_id = _window(deck, 2024, 6, 1, 2024, 7, 1)[0]["id"]
    set_hidden(deck, card_id, True, "from", occ[2])
    assert _hidden_starts(deck) == set(occ[2:])


def test_reveal_reverses_any_scope(deck):
    _recurring(deck)
    occ = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    card_id = _window(deck, 2024, 6, 1, 2024, 7, 1)[0]["id"]
    set_hidden(deck, card_id, True, "from", occ[1])
    assert _hidden_starts(deck) == set(occ[1:])
    set_hidden(deck, card_id, False, "series", occ[1])
    assert _hidden_starts(deck) == set()


def test_hide_one_on_nonrecurring_rejected(deck):
    card_id = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="non-recurring event"):
        set_hidden(deck, card_id, True, "one", "2024-06-03T10:00:00+01:00")


def test_hide_unknown_scope_rejected(deck):
    card_id = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="unknown scope"):
        set_hidden(deck, card_id, True, "everything")


def test_hide_series_from_exception_hides_whole_series(tmp_path):
    deck = str(tmp_path / "deck")
    ics = tmp_path / "s.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        "BEGIN:VEVENT\nUID:sem@x\nDTSTAMP:20240101T120000Z\n"
        "DTSTART;TZID=Europe/London:20240603T130000\n"
        "DTEND;TZID=Europe/London:20240603T133000\n"
        "RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO\nSEQUENCE:0\nSTATUS:CONFIRMED\n"
        "SUMMARY:Seminar\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:sem@x\nDTSTAMP:20240101T120000Z\n"
        "RECURRENCE-ID;TZID=Europe/London:20240610T130000\n"
        "DTSTART;TZID=Europe/London:20240610T160000\n"
        "DTEND;TZID=Europe/London:20240610T163000\n"
        "SEQUENCE:1\nSTATUS:CONFIRMED\nSUMMARY:Seminar (moved)\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    import_ics(deck, str(ics), "feed")
    exception = (
        mddb.MDDB(deck)
        .conn.execute(
            "SELECT e.id FROM entries e JOIN entry_fields r "
            "ON r.entry_rowid=e.rowid AND r.key='recurrence_id'"
        )
        .fetchone()[0]
    )
    exc_occ = next(
        o for o in _window(deck, 2024, 6, 1, 2024, 7, 1) if o["id"] == exception
    )
    set_hidden(deck, exception, True, "series", exc_occ["start"])
    after = _window(deck, 2024, 6, 1, 2024, 7, 1)
    assert after and all(o["hidden"] for o in after)
    assert any(o["title"] == "Seminar (moved)" for o in after)


def test_reveal_series_tag_clears_master(deck):
    master = create_event(deck, {**EDIT, "title": "Seminar", "repeat": "weekly"})
    first = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))[0]
    set_hidden(deck, master, True, "series", first)
    assert all(o["hidden"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    set_hidden(deck, master, False, "series", first)
    assert not any(o["hidden"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    assert "mdcal/hidden" not in (mddb.MDDB(deck).read(master).yaml.get("tags") or [])


def test_synced_create_sets_gcal_calendar(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    y = mddb.MDDB(deck).read(card_id).yaml
    assert y["source"] == "research"
    assert y["gcal_calendar"] == "cal@group.calendar.google.com"


def test_create_with_area_tags_it(deck):
    card_id = create_event(deck, {**TIMED, "area": "area/caius"})
    assert "area/caius" in (mddb.MDDB(deck).read(card_id).yaml.get("tags") or [])


def test_edit_changes_area_preserving_other_tags(deck):
    card_id = create_event(deck, {**TIMED, "area": "area/work"})
    db = mddb.MDDB(deck)
    with db.editor(rationale="add hidden") as e:
        c = e.read(card_id)
        e.update(
            c, summary=c.summary, tags=[*(c.yaml.get("tags") or []), "mdcal/hidden"]
        )
    update_event(deck, card_id, {**EDIT, "title": "Moved", "area": "area/caius"})
    tags = mddb.MDDB(deck).read(card_id).yaml.get("tags") or []
    assert "area/caius" in tags and "area/work" not in tags and "mdcal/hidden" in tags


def test_edit_without_area_key_keeps_area(deck):
    card_id = create_event(deck, {**TIMED, "area": "area/work"})
    update_event(deck, card_id, {**EDIT, "title": "Renamed"})
    assert "area/work" in (mddb.MDDB(deck).read(card_id).yaml.get("tags") or [])


def test_create_rejects_bad_area(deck):
    with pytest.raises(ValueError, match="area must be"):
        create_event(deck, {**TIMED, "area": "work"})


def _card_with(deck_path, vevent_text, source="local"):
    import icalendar

    vevent = list(
        icalendar.Calendar.from_ical(
            "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
            f"BEGIN:VEVENT\n{vevent_text}\nEND:VEVENT\nEND:VCALENDAR\n"
        ).walk("VEVENT")
    )[0]
    rendered = vevent_to_card(vevent, source)

    def add(db):
        with db.editor(rationale="seed") as editor:
            return editor.create(
                title=rendered.title,
                summary=rendered.summary,
                body=rendered.body,
                relpath=rendered.relpath,
                yaml=rendered.yaml,
            ).id

    return _committed(deck_path, add)


BYDAY = (
    "UID:byday@x\nSUMMARY:Namu & Gabor\n"
    "DTSTART;TZID=Europe/London:20250217T171500\n"
    "DTEND;TZID=Europe/London:20250217T180000\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO\n"
    "EXDATE;TZID=Europe/London:20250303T171500\n"
    "STATUS:CONFIRMED"
)


def test_title_edit_keeps_nonpreset_rrule_and_exdates(deck):
    card_id = _card_with(deck, BYDAY)
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Namu & Gabor (moved room)",
            "repeat": "keep",
            "time_changed": False,
        },
    )
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["rrule"] == "FREQ=WEEKLY;BYDAY=MO"
    assert card.yaml["exdate"] == [dt.datetime(2025, 3, 3, 17, 15, tzinfo=UTC)]
    assert card.title == "Namu & Gabor (moved room)"


def test_title_edit_preserves_unowned_vevent_properties(deck):
    card_id = _card_with(
        deck,
        "UID:rich@x\nSUMMARY:Grant call\n"
        "DTSTART;TZID=Europe/London:20240603T100000\n"
        "DTEND;TZID=Europe/London:20240603T103000\n"
        "ORGANIZER:mailto:pi@cam.ac.uk\n"
        "ATTENDEE:mailto:sam@cam.ac.uk\nATTENDEE:mailto:kim@cam.ac.uk\n"
        "TRANSP:TRANSPARENT\n"
        "X-GOOGLE-CONFERENCE:https://meet.google.com/xyz\n"
        "X-GOOGLE-EVENT-ID:g123\nX-GOOGLE-CALENDAR-ID:cal@group\n"
        "X-GOOGLE-HTML-LINK:https://calendar.google.com/event?eid=xyz\n"
        "STATUS:CONFIRMED",
    )
    update_event(deck, card_id, {**EDIT, "title": "Grant call (renamed)"})
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["organizer"] == "pi@cam.ac.uk"
    assert card.yaml["attendee_emails"] == ["sam@cam.ac.uk", "kim@cam.ac.uk"]
    assert card.yaml["transp"] == "TRANSPARENT"
    assert card.yaml["conference_url"] == "https://meet.google.com/xyz"
    assert card.yaml["gcal_id"] == "g123"
    assert card.yaml["gcal_calendar"] == "cal@group"
    assert card.yaml["gcal_link"] == "https://calendar.google.com/event?eid=xyz"


def test_unchanged_times_survive_foreign_device_tz(deck):
    card_id = create_event(deck, TIMED)
    before = mddb.MDDB(deck).read(card_id)
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Coffee (retitled)",
            "time_changed": False,
            "start": "2024-06-03T05:00",
            "end": "2024-06-03T05:30",
            "tzid": "America/Toronto",
        },
    )
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["dtstart"] == before.yaml["dtstart"]
    assert card.yaml["dtend"] == before.yaml["dtend"]
    assert card.yaml["tzid"] == "Europe/London"


def test_repeat_none_drops_rrule_and_exdates(deck):
    card_id = _card_with(deck, BYDAY)
    update_event(
        deck,
        card_id,
        {**EDIT, "repeat": "none", "time_changed": False, "title": "One-off now"},
    )
    card = mddb.MDDB(deck).read(card_id)
    assert "rrule" not in card.yaml and "exdate" not in card.yaml


def test_repeat_preset_change_drops_old_exdates(deck):
    card_id = _card_with(deck, BYDAY)
    update_event(
        deck,
        card_id,
        {**EDIT, "repeat": "monthly", "time_changed": False, "title": "Monthly now"},
    )
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["rrule"] == "FREQ=MONTHLY"
    assert "exdate" not in card.yaml


def test_repeat_keep_on_nonrecurring_400(deck):
    card_id = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="repeat=keep"):
        update_event(deck, card_id, {**EDIT, "repeat": "keep"})


def test_all_day_flip_requires_time_changed(deck):
    card_id = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="all_day"):
        update_event(deck, card_id, {**EDIT, "all_day": True, "time_changed": False})


def test_update_missing_intent_fields_400(deck):
    card_id = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="time_changed"):
        update_event(deck, card_id, {**TIMED, "repeat": "none"})
    with pytest.raises(ValueError, match="repeat"):
        update_event(deck, card_id, {**TIMED, "time_changed": True})


def _head(deck):
    return mddb.MDDB(deck).head()


def test_undo_reverts_local_create(deck):
    card_id = create_event(deck, TIMED)
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"])
    with pytest.raises(KeyError):
        mddb.MDDB(deck).read(card_id)


def test_undo_reverts_local_edit(deck):
    card_id = create_event(deck, TIMED)
    update_event(deck, card_id, {**EDIT, "title": "Oops"})
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"])
    assert mddb.MDDB(deck).read(card_id).title == "Coffee with Sam"


def test_undo_unavailable_after_new_commit(deck):
    card_id = create_event(deck, TIMED)
    token = undo_token(deck)
    update_event(deck, card_id, {**EDIT, "title": "Newer"})
    with pytest.raises(mddb.ConflictError, match="changed since"):
        undo_event(token["deck"], token["commit"])


def test_undo_refuses_non_web_commit(deck):
    create_event(deck, TIMED)

    def poke(db):
        with db.editor(rationale="import research 2024") as editor:
            card = editor.read(
                db.conn.execute("SELECT id FROM entries LIMIT 1").fetchone()[0]
            )
            card.yaml["poked"] = True
            editor.update(card, summary=card.summary)

    _committed(deck, poke)
    with pytest.raises(mddb.ConflictError, match="not a web change"):
        undo_event(deck, _head(deck))


def test_undo_synced_edit_pushes_prior_state(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    update_event(deck, card_id, {**EDIT, "title": "Synced edit"}, **synced.kw)
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    card = _card_of(deck, card_id)
    assert card.title == "Coffee with Sam"
    assert card.yaml["sequence"] == 3
    assert card.yaml["dispatched"] == synced.stamp
    assert len(synced.pushes) == 3
    assert synced.pushes[-1][1].yaml["sequence"] == 3


def test_undo_synced_create_deletes_from_google(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    assert len(synced.deletes) == 1
    with pytest.raises(KeyError):
        _card_of(deck, card_id)


def test_undo_synced_delete_restores_confirmed(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    delete_event(deck, card_id, "event", **synced.kw)
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    card = _card_of(deck, card_id)
    assert card.yaml["event_status"] == "CONFIRMED"
    assert card.yaml["dispatched"] == synced.stamp
    assert synced.pushes[-1][1].yaml["event_status"] == "CONFIRMED"


def test_undo_synced_override_create_resets_instance(deck, synced):
    description = "  <b>series description</b>  "
    card_id = _screate(deck, {**RECUR, "description": description}, synced)
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Standup",
            "repeat": "keep",
            "start": "2024-06-10T13:00",
            "end": "2024-06-10T13:15",
        },
        scope="one",
        start="2024-06-10T09:00:00+01:00",
        **synced.kw,
    )
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    assert len(synced.patches) == 2
    reset = synced.patches[-1][3]
    assert str(reset.yaml["dtstart"]).startswith("2024-06-10 09:00")
    assert reset.yaml["event_status"] == "CONFIRMED"
    assert mdcal.events.description_of(reset.body) == description
    starts = [
        o["start"] for o in _window(deck, 2024, 6, 10, 2024, 6, 11, synced=synced.map)
    ]
    assert starts == ["2024-06-10T09:00:00+01:00"]


def test_undo_synced_split_restores_series_and_deletes_new(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    new_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Standup",
            "repeat": "keep",
            "start": "2024-06-17T13:00",
            "end": "2024-06-17T13:15",
        },
        scope="from",
        start="2024-06-17T09:00:00+01:00",
        **synced.kw,
    )
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    assert len(synced.deletes) == 1
    old = _card_of(deck, card_id)
    assert "UNTIL" not in old.yaml["rrule"]
    with pytest.raises(KeyError):
        _card_of(deck, new_id)
    starts = sorted(
        o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1, synced=synced.map)
    )
    assert starts == [
        "2024-06-03T09:00:00+01:00",
        "2024-06-10T09:00:00+01:00",
        "2024-06-17T09:00:00+01:00",
        "2024-06-24T09:00:00+01:00",
    ]


def test_undo_synced_failure_commits_nothing(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    update_event(deck, card_id, {**EDIT, "title": "Synced edit"}, **synced.kw)
    token = undo_token(deck)

    def boom(calendar_id, rendered):
        raise RuntimeError("google down")

    with pytest.raises(RuntimeError, match="google down"):
        undo_event(
            token["deck"],
            token["commit"],
            synced=synced.map,
            gapi=_gapi(synced, push=boom),
        )
    assert mddb.MDDB(deck).head() == token["commit"]
    assert _card_of(deck, card_id).title == "Synced edit"


DST_SPAN = (
    "UID:dst@x\nSUMMARY:Supervision\n"
    "DTSTART;TZID=Europe/London:20250217T171500\n"
    "DTEND;TZID=Europe/London:20250217T180000\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO\n"
    "EXDATE;TZID=Europe/London:20250303T171500\n"
    "EXDATE;TZID=Europe/London:20250602T171500\n"
    "STATUS:CONFIRMED"
)

RETIME = {
    **EDIT,
    "title": "Supervision",
    "repeat": "keep",
    "start": "2025-02-17T10:30",
    "end": "2025-02-17T11:00",
}


def test_retime_reanchors_exdates_across_dst(deck):
    card_id = _card_with(deck, DST_SPAN)
    update_event(deck, card_id, RETIME)
    card = mddb.MDDB(deck).read(card_id)
    assert str(card.yaml["dtstart"]) == "2025-02-17 10:30:00+00:00"
    exdates = {str(e) for e in card.yaml["exdate"]}
    assert exdates == {
        "2025-03-03 10:30:00+00:00",
        "2025-06-02 10:30:00+01:00",
    }
    march = {o["start"] for o in _window(deck, 2025, 3, 1, 2025, 3, 31)}
    assert "2025-03-03T10:30:00+00:00" not in march
    assert "2025-03-10T10:30:00+00:00" in march
    june = {o["start"] for o in _window(deck, 2025, 6, 1, 2025, 6, 30)}
    assert "2025-06-02T10:30:00+01:00" not in june
    assert "2025-06-09T10:30:00+01:00" in june


def test_retime_refuses_non_generated_exclusion(deck):
    rdate_excluded = DST_SPAN.replace(
        "EXDATE;TZID=Europe/London:20250602T171500",
        "EXDATE;TZID=Europe/London:20250602T160000",
    )
    card_id = _card_with(deck, rdate_excluded)
    with pytest.raises(ValueError, match="not an occurrence of the old schedule"):
        update_event(deck, card_id, RETIME)
    assert str(mddb.MDDB(deck).read(card_id).yaml["dtstart"]).startswith(
        "2025-02-17 17:15"
    )


def test_series_edit_pins_the_anchor_date(deck):
    card_id = _card_with(deck, DST_SPAN)
    update_event(
        deck,
        card_id,
        {**RETIME, "start": "2025-03-10T10:30", "end": "2025-03-10T11:00"},
    )
    card = mddb.MDDB(deck).read(card_id)
    assert str(card.yaml["dtstart"]) == "2025-02-17 10:30:00+00:00"
    assert len(card.yaml["exdate"]) == 2


def test_retime_refuses_allday_flip_with_exclusions(deck):
    card_id = _card_with(deck, DST_SPAN)
    with pytest.raises(ValueError, match="all_day change on a series with exclusions"):
        update_event(
            deck,
            card_id,
            {
                **RETIME,
                "all_day": True,
                "start": "2025-02-17",
                "end": "2025-02-17",
            },
        )


def test_retime_reanchors_hidden_annotations(deck):
    card_id = _recurring(deck)
    set_hidden(deck, card_id, True, "one", "2024-06-17T10:00:00+01:00")
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar",
            "repeat": "keep",
            "start": "2024-06-03T14:00",
            "end": "2024-06-03T14:30",
        },
    )
    hidden = _hidden_starts(deck)
    assert hidden == {"2024-06-17T14:00:00+01:00"}
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["hidden_occurrences"] == [
        int(
            dt.datetime(
                2024, 6, 17, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))
            ).timestamp()
        )
    ]


def test_series_edit_from_occurrence_prefill_keeps_hidden(deck):
    card_id = _recurring(deck)
    set_hidden(deck, card_id, True, "one", "2024-06-17T10:00:00+01:00")
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar",
            "repeat": "keep",
            "start": "2024-06-24T14:00",
            "end": "2024-06-24T14:30",
        },
    )
    card = mddb.MDDB(deck).read(card_id)
    assert str(card.yaml["dtstart"]).startswith("2024-06-03 14:00")
    assert _hidden_starts(deck) == {"2024-06-17T14:00:00+01:00"}


def test_retime_refuses_allday_flip_with_hidden_annotations(deck):
    card_id = _recurring(deck)
    set_hidden(deck, card_id, True, "from", "2024-06-17T10:00:00+01:00")
    with pytest.raises(ValueError, match="all_day change on a series with hide"):
        update_event(
            deck,
            card_id,
            {
                **EDIT,
                "title": "Seminar",
                "repeat": "keep",
                "all_day": True,
                "start": "2024-06-03",
                "end": "2024-06-03",
            },
        )


def test_allday_series_date_change_with_hidden_refuses(deck):
    card_id = create_event(
        deck,
        {
            "title": "Away day",
            "start": "2024-06-10",
            "end": "2024-06-10",
            "all_day": True,
            "repeat": "weekly",
        },
    )
    set_hidden(deck, card_id, True, "one", "2024-06-17")
    with pytest.raises(ValueError, match="date change on a series with hide"):
        update_event(
            deck,
            card_id,
            {
                "title": "Away day",
                "start": "2024-06-11",
                "end": "2024-06-11",
                "all_day": True,
                "repeat": "keep",
                "time_changed": True,
            },
        )
    assert mddb.MDDB(deck).read(card_id).yaml["hidden_occurrences"] == [
        int(dt.datetime(2024, 6, 17, tzinfo=UTC).timestamp())
    ]


def test_allday_to_timed_flip_with_hidden_refuses(deck):
    card_id = create_event(
        deck,
        {
            "title": "Away day",
            "start": "2024-06-10",
            "end": "2024-06-10",
            "all_day": True,
            "repeat": "weekly",
        },
    )
    set_hidden(deck, card_id, True, "from", "2024-06-17")
    with pytest.raises(ValueError, match="all_day change on a series with hide"):
        update_event(
            deck,
            card_id,
            {
                **EDIT,
                "title": "Away day",
                "repeat": "keep",
                "start": "2024-06-10T09:00",
                "end": "2024-06-10T10:00",
            },
        )


def test_edit_one_creates_override_and_master_untouched(deck):
    card_id = _recurring(deck)
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="one",
        start="2024-06-17T10:00:00+01:00",
    )
    master = mddb.MDDB(deck).read(card_id)
    assert str(master.yaml["dtstart"]).startswith("2024-06-03 10:00")
    assert "exdate" not in master.yaml
    starts = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    assert "2024-06-17T14:00:00+01:00" in starts
    assert "2024-06-17T10:00:00+01:00" not in starts
    assert "2024-06-10T10:00:00+01:00" in starts
    (override,) = [
        o
        for o in _window(deck, 2024, 6, 1, 2024, 7, 1)
        if o["start"].startswith("2024-06-17")
    ]
    assert override["series_member"] and not override["recurring"]
    assert override["editable"]


def test_edit_one_conflicts_when_override_lands_between_check_and_commit(
    deck, monkeypatch
):
    card_id = _recurring(deck)
    one = {
        **EDIT,
        "title": "Seminar",
        "repeat": "keep",
        "start": "2024-06-17T14:00",
        "end": "2024-06-17T14:30",
    }
    real_override_id = mdcal.events._override_id
    calls = {"n": 0}

    def racing_override_id(db, source, uid, slot):
        calls["n"] += 1
        found = real_override_id(db, source, uid, slot)
        if calls["n"] == 2 and found is None:
            monkeypatch.setattr(mdcal.events, "_override_id", real_override_id)
            update_event(
                deck,
                card_id,
                {**one, "title": "Colloquium"},
                scope="one",
                start="2024-06-17T10:00:00+01:00",
            )
            monkeypatch.setattr(mdcal.events, "_override_id", racing_override_id)
            return None
        return found

    monkeypatch.setattr(mdcal.events, "_override_id", racing_override_id)
    with pytest.raises(mddb.ConflictError):
        update_event(deck, card_id, one, scope="one", start="2024-06-17T10:00:00+01:00")
    overrides = [
        o
        for o in _window(deck, 2024, 6, 1, 2024, 7, 1)
        if o["start"].startswith("2024-06-17") and not o["recurring"]
    ]
    assert len(overrides) == 1


def test_edit_one_twice_updates_the_override_not_a_duplicate(deck):
    card_id = _recurring(deck)
    one = {
        **EDIT,
        "title": "Seminar",
        "repeat": "keep",
        "start": "2024-06-17T14:00",
        "end": "2024-06-17T14:30",
    }
    update_event(deck, card_id, one, scope="one", start="2024-06-17T10:00:00+01:00")
    update_event(
        deck,
        card_id,
        {**one, "start": "2024-06-17T15:00", "end": "2024-06-17T15:30"},
        scope="one",
        start="2024-06-17T10:00:00+01:00",
    )
    seventeenth = [
        o
        for o in _window(deck, 2024, 6, 1, 2024, 7, 1)
        if o["start"].startswith("2024-06-17")
    ]
    assert len(seventeenth) == 1
    assert seventeenth[0]["start"] == "2024-06-17T15:00:00+01:00"


def test_delete_one_with_override_removes_it_and_excludes_slot(deck):
    card_id = _recurring(deck)
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="one",
        start="2024-06-17T10:00:00+01:00",
    )
    (override,) = [
        o
        for o in _window(deck, 2024, 6, 1, 2024, 7, 1)
        if o["start"].startswith("2024-06-17")
    ]
    delete_event(deck, override["id"], "one")
    starts = [o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1)]
    assert not any(s.startswith("2024-06-17") for s in starts)
    master = mddb.MDDB(deck).read(card_id)
    assert len(master.yaml["exdate"]) == 1


def test_edit_from_splits_the_series(deck):
    card_id = _recurring(deck)
    new_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar (moved)",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="from",
        start="2024-06-17T10:00:00+01:00",
    )
    assert new_id != card_id
    junes = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    assert "2024-06-03T10:00:00+01:00" in junes
    assert "2024-06-10T10:00:00+01:00" in junes
    assert "2024-06-17T14:00:00+01:00" in junes
    assert "2024-06-24T14:00:00+01:00" in junes
    assert not any(s == "2024-06-17T10:00:00+01:00" for s in junes)
    old = mddb.MDDB(deck).read(card_id)
    new = mddb.MDDB(deck).read(new_id)
    assert "UNTIL" in old.yaml["rrule"]
    assert old.yaml["uid"] != new.yaml["uid"]
    assert new.yaml["rrule"].startswith("FREQ=WEEKLY")


def test_edit_from_refuses_rdate_and_hidden_from(deck):
    card_id = _recurring(deck)
    set_hidden(deck, card_id, True, "from", "2024-06-24T10:00:00+01:00")
    with pytest.raises(ValueError, match="hide-from ray"):
        update_event(
            deck,
            card_id,
            {**EDIT, "title": "Seminar", "repeat": "keep"},
            scope="from",
            start="2024-06-17T10:00:00+01:00",
        )


def test_edit_from_partitions_exclusions_and_hidden_points(deck):
    card_id = _recurring(deck)
    delete_event(deck, card_id, "one", "2024-06-10T10:00:00+01:00")
    set_hidden(deck, card_id, True, "one", "2024-06-24T10:00:00+01:00")
    new_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="from",
        start="2024-06-17T10:00:00+01:00",
    )
    old = mddb.MDDB(deck).read(card_id)
    new = mddb.MDDB(deck).read(new_id)
    assert len(old.yaml["exdate"]) == 1
    assert "exdate" not in new.yaml
    assert "hidden_occurrences" not in old.yaml
    assert new.yaml["hidden_occurrences"] == [
        int(
            dt.datetime(
                2024, 6, 24, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))
            ).timestamp()
        )
    ]
    hidden = _hidden_starts(deck)
    assert hidden == {"2024-06-24T14:00:00+01:00"}


def test_synced_edit_one_patches_instance(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Standup",
            "repeat": "keep",
            "start": "2024-06-10T13:00",
            "end": "2024-06-10T13:15",
        },
        scope="one",
        start="2024-06-10T09:00:00+01:00",
        **synced.kw,
    )
    assert len(synced.patches) == 1
    cal, uid, slot, rendered = synced.patches[0]
    assert slot == dt.datetime(
        2024, 6, 10, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=1))
    )
    (override,) = [
        o
        for o in _window(deck, 2024, 6, 10, 2024, 6, 11, synced=synced.map)
        if not o["recurring"] and o["series_member"]
    ]
    card = _card_of(deck, override["id"])
    assert card.yaml["dispatched"] == synced.stamp


def test_synced_edit_from_pushes_new_master_first(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Standup",
            "repeat": "keep",
            "start": "2024-06-17T13:00",
            "end": "2024-06-17T13:15",
        },
        scope="from",
        start="2024-06-17T09:00:00+01:00",
        **synced.kw,
    )
    assert len(synced.pushes) == 3
    created, new_master, old_master = synced.pushes
    assert "UNTIL" not in new_master[1].yaml["rrule"]
    assert "UNTIL" in old_master[1].yaml["rrule"]
    assert new_master[1].yaml["uid"] != old_master[1].yaml["uid"]


def test_edit_from_count_rule_keeps_remaining_occurrences_after_retime(deck):
    card_id = _card_with(
        deck,
        "UID:count@x\nSUMMARY:Course\n"
        "DTSTART;TZID=Europe/London:20240603T100000\n"
        "DTEND;TZID=Europe/London:20240603T103000\n"
        "RRULE:FREQ=WEEKLY;COUNT=4\n"
        "STATUS:CONFIRMED",
    )
    new_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Course",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="from",
        start="2024-06-17T10:00:00+01:00",
    )
    starts = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    assert starts == [
        "2024-06-03T10:00:00+01:00",
        "2024-06-10T10:00:00+01:00",
        "2024-06-17T14:00:00+01:00",
        "2024-06-24T14:00:00+01:00",
    ]
    new = mddb.MDDB(deck).read(new_id)
    assert "COUNT" not in new.yaml["rrule"] and "UNTIL" in new.yaml["rrule"]


def test_edit_one_refuses_non_generated_slot(deck):
    card_id = _recurring(deck)
    with pytest.raises(ValueError, match="not a generated occurrence"):
        update_event(
            deck,
            card_id,
            {
                **EDIT,
                "title": "Seminar",
                "repeat": "keep",
                "start": "2024-06-18T14:00",
                "end": "2024-06-18T14:30",
            },
            scope="one",
            start="2024-06-18T10:00:00+01:00",
        )
    assert all(o["recurring"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))


def test_edit_from_keep_refuses_date_change_on_anchored_rule(deck):
    card_id = _card_with(deck, BYDAY)
    with pytest.raises(ValueError, match="date change with repeat=keep"):
        update_event(
            deck,
            card_id,
            {
                **EDIT,
                "title": "Namu & Gabor",
                "repeat": "keep",
                "start": "2025-03-11T17:15",
                "end": "2025-03-11T18:00",
            },
            scope="from",
            start="2025-03-10T17:15:00+00:00",
        )
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["rrule"] == "FREQ=WEEKLY;BYDAY=MO"


def test_edit_from_date_move_refuses_with_post_split_exclusion(deck):
    card_id = _recurring(deck)
    delete_event(deck, card_id, "one", "2024-06-24T10:00:00+01:00")
    with pytest.raises(ValueError, match="date-moving split"):
        update_event(
            deck,
            card_id,
            {
                **EDIT,
                "title": "Seminar",
                "repeat": "weekly",
                "start": "2024-06-18T14:00",
                "end": "2024-06-18T14:30",
            },
            scope="from",
            start="2024-06-17T10:00:00+01:00",
        )
    card = mddb.MDDB(deck).read(card_id)
    assert "UNTIL" not in card.yaml["rrule"]


def test_edit_from_date_move_refuses_with_post_split_hidden(deck):
    card_id = _recurring(deck)
    set_hidden(deck, card_id, True, "one", "2024-06-24T10:00:00+01:00")
    with pytest.raises(ValueError, match="date-moving split"):
        update_event(
            deck,
            card_id,
            {
                **EDIT,
                "title": "Seminar",
                "repeat": "weekly",
                "start": "2024-06-18T14:00",
                "end": "2024-06-18T14:30",
            },
            scope="from",
            start="2024-06-17T10:00:00+01:00",
        )
    assert _hidden_starts(deck) == {"2024-06-24T10:00:00+01:00"}


def test_undo_synced_edit_restores_summary_and_tags(deck, synced):
    card_id = create_event(
        deck, {**TIMED, "area": "area/work"}, synced_sources={"research"}, **synced.kw
    )
    before = _card_of(deck, card_id)
    update_event(
        deck,
        card_id,
        {**EDIT, "title": "Synced edit", "area": "area/caius"},
        **synced.kw,
    )
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    card = _card_of(deck, card_id)
    assert card.yaml["summary"] == before.yaml["summary"]
    assert card.yaml["tags"] == ["area/work"]


def test_undo_refuses_orphaned_google_provenance(deck, synced):
    card_id = _screate(deck, TIMED, synced)
    update_event(deck, card_id, {**EDIT, "title": "Synced edit"}, **synced.kw)
    token = undo_token(deck)
    with pytest.raises(mddb.ConflictError, match="Google provenance"):
        undo_event(token["deck"], token["commit"])
    assert _card_of(deck, card_id).title == "Synced edit"


def test_undo_synced_exception_edit_restores_prior_state(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    one = {
        **EDIT,
        "title": "Standup",
        "repeat": "keep",
        "start": "2024-06-10T13:00",
        "end": "2024-06-10T13:15",
    }
    override_id = update_event(
        deck, card_id, one, scope="one", start="2024-06-10T09:00:00+01:00", **synced.kw
    )
    update_event(
        deck,
        override_id,
        {
            **one,
            "title": "Standup (room B)",
            "start": "2024-06-10T15:00",
            "end": "2024-06-10T15:15",
        },
        scope="one",
        **synced.kw,
    )
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    card = _card_of(deck, override_id)
    assert card.title == "Standup"
    assert str(card.yaml["dtstart"]).startswith("2024-06-10 13:00")
    assert "sequence" not in card.yaml
    assert card.yaml["dispatched"] == synced.stamp
    assert len(synced.patches) == 3


def test_delete_from_truncates_series(deck):
    card_id = _recurring(deck)
    delete_event(deck, card_id, "from", "2024-06-17T10:00:00+01:00")
    starts = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    assert starts == ["2024-06-03T10:00:00+01:00", "2024-06-10T10:00:00+01:00"]
    assert "UNTIL" in mddb.MDDB(deck).read(card_id).yaml["rrule"]


def test_delete_from_count_rule_converts_exactly(deck):
    card_id = _card_with(
        deck,
        "UID:count@x\nSUMMARY:Course\n"
        "DTSTART;TZID=Europe/London:20240603T100000\n"
        "DTEND;TZID=Europe/London:20240603T103000\n"
        "RRULE:FREQ=WEEKLY;COUNT=4\n"
        "STATUS:CONFIRMED",
    )
    delete_event(deck, card_id, "from", "2024-06-17T10:00:00+01:00")
    starts = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    assert starts == ["2024-06-03T10:00:00+01:00", "2024-06-10T10:00:00+01:00"]
    rule = mddb.MDDB(deck).read(card_id).yaml["rrule"]
    assert "COUNT" not in rule
    assert "UNTIL" in rule


def test_delete_from_partitions_exclusions_and_hidden_points(deck):
    card_id = _recurring(deck)
    delete_event(deck, card_id, "one", "2024-06-10T10:00:00+01:00")
    delete_event(deck, card_id, "one", "2024-06-24T10:00:00+01:00")
    set_hidden(deck, card_id, True, "one", "2024-06-03T10:00:00+01:00")
    delete_event(deck, card_id, "from", "2024-06-17T10:00:00+01:00")
    card = mddb.MDDB(deck).read(card_id)
    assert card.yaml["exdate"] == [dt.datetime(2024, 6, 10, 9, 0, tzinfo=UTC)]
    assert card.yaml["hidden_occurrences"] == [
        int(dt.datetime(2024, 6, 3, 9, 0, tzinfo=UTC).timestamp())
    ]
    assert _hidden_starts(deck) == {"2024-06-03T10:00:00+01:00"}
    assert [o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1)] == [
        "2024-06-03T10:00:00+01:00"
    ]


def test_delete_from_keeps_pre_boundary_hidden_ray(deck):
    card_id = _recurring(deck)
    set_hidden(deck, card_id, True, "from", "2024-06-10T10:00:00+01:00")
    delete_event(deck, card_id, "from", "2024-06-17T10:00:00+01:00")
    assert _hidden_starts(deck) == {"2024-06-10T10:00:00+01:00"}
    assert "hidden_from" in mddb.MDDB(deck).read(card_id).yaml


def test_delete_from_drops_post_boundary_hidden_ray(deck):
    card_id = _recurring(deck)
    set_hidden(deck, card_id, True, "from", "2024-06-17T10:00:00+01:00")
    delete_event(deck, card_id, "from", "2024-06-17T10:00:00+01:00")
    assert _hidden_starts(deck) == set()
    assert "hidden_from" not in mddb.MDDB(deck).read(card_id).yaml


def test_delete_from_removes_post_boundary_override(deck):
    card_id = _recurring(deck)
    override_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar (moved)",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="one",
        start="2024-06-17T10:00:00+01:00",
    )
    delete_event(deck, card_id, "from", "2024-06-10T10:00:00+01:00")
    assert [o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1)] == [
        "2024-06-03T10:00:00+01:00"
    ]
    with pytest.raises(KeyError):
        mddb.MDDB(deck).read(override_id)


def test_delete_from_refusals(deck):
    card_id = _recurring(deck)
    with pytest.raises(ValueError, match="occurrence start"):
        delete_event(deck, card_id, "from")
    with pytest.raises(ValueError, match="not a generated occurrence"):
        delete_event(deck, card_id, "from", "2024-06-18T10:00:00+01:00")
    with pytest.raises(ValueError, match="first occurrence"):
        delete_event(deck, card_id, "from", "2024-06-03T10:00:00+01:00")
    allday = create_event(
        deck,
        {
            "title": "Daily",
            "start": "2024-06-10",
            "end": "2024-06-10",
            "all_day": True,
            "repeat": "daily",
        },
    )
    with pytest.raises(ValueError, match="all-day"):
        delete_event(deck, allday, "from", "2024-06-12")
    single = create_event(deck, TIMED)
    with pytest.raises(ValueError, match="non-recurring"):
        delete_event(deck, single, "from", "2024-06-03T10:00:00+01:00")
    assert "UNTIL" not in mddb.MDDB(deck).read(card_id).yaml["rrule"]


def test_delete_from_refuses_rdate(deck):
    card_id = _card_with(
        deck,
        "UID:rd@x\nSUMMARY:Extras\n"
        "DTSTART;TZID=Europe/London:20240603T100000\n"
        "DTEND;TZID=Europe/London:20240603T103000\n"
        "RRULE:FREQ=WEEKLY\n"
        "RDATE;TZID=Europe/London:20240605T100000\n"
        "STATUS:CONFIRMED",
    )
    with pytest.raises(ValueError, match="RDATE"):
        delete_event(deck, card_id, "from", "2024-06-17T10:00:00+01:00")


def test_synced_delete_from_cancels_instances_before_truncating(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    override_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Standup (moved)",
            "repeat": "keep",
            "start": "2024-06-17T13:00",
            "end": "2024-06-17T13:15",
        },
        scope="one",
        start="2024-06-17T09:00:00+01:00",
        **synced.kw,
    )
    before_seq = int(_card_of(deck, card_id).yaml.get("sequence", 0))
    order = []

    def patch(cal, uid, slot, rendered):
        order.append(("patch", rendered))
        return synced.stamp

    def push(cal, rendered):
        order.append(("push", rendered))
        return synced.stamp

    delete_event(
        deck,
        card_id,
        "from",
        "2024-06-10T09:00:00+01:00",
        synced=synced.map,
        gapi=_gapi(synced, push=push, patch=patch),
    )
    assert [kind for kind, _ in order] == ["patch", "push"]
    assert order[0][1].yaml["event_status"] == "CANCELLED"
    assert "UNTIL" in order[1][1].yaml["rrule"]
    assert order[1][1].yaml["sequence"] == before_seq + 1
    master = _card_of(deck, card_id)
    assert master.yaml["sequence"] == before_seq + 1
    assert master.yaml["dispatched"] == synced.stamp
    over = _card_of(deck, override_id)
    assert over.yaml["event_status"] == "CANCELLED"
    assert over.yaml["dispatched"] == synced.stamp
    assert [
        o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1, synced=synced.map)
    ] == ["2024-06-03T09:00:00+01:00"]


def test_delete_from_conflicts_on_concurrent_override_creation(deck, monkeypatch):
    card_id = _recurring(deck)
    real = mdcal.events._committed
    state = {"armed": True}

    def racing(deck_path, mutate):
        if state["armed"]:
            state["armed"] = False
            update_event(
                deck,
                card_id,
                {
                    **EDIT,
                    "title": "Seminar (moved)",
                    "repeat": "keep",
                    "start": "2024-06-17T14:00",
                    "end": "2024-06-17T14:30",
                },
                scope="one",
                start="2024-06-17T10:00:00+01:00",
            )
        return real(deck_path, mutate)

    monkeypatch.setattr(mdcal.events, "_committed", racing)
    with pytest.raises(mddb.ConflictError, match="overrides changed"):
        delete_event(deck, card_id, "from", "2024-06-10T10:00:00+01:00")
    assert "UNTIL" not in mddb.MDDB(deck).read(card_id).yaml["rrule"]


def test_delete_from_conflicts_on_concurrent_override_edit(deck, monkeypatch):
    card_id = _recurring(deck)
    one = {
        **EDIT,
        "title": "Seminar (moved)",
        "repeat": "keep",
        "start": "2024-06-17T14:00",
        "end": "2024-06-17T14:30",
    }
    override_id = update_event(
        deck, card_id, one, scope="one", start="2024-06-17T10:00:00+01:00"
    )
    real = mdcal.events._committed
    state = {"armed": True}

    def racing(deck_path, mutate):
        if state["armed"]:
            state["armed"] = False
            update_event(
                deck, override_id, {**one, "title": "Seminar (room B)"}, scope="one"
            )
        return real(deck_path, mutate)

    monkeypatch.setattr(mdcal.events, "_committed", racing)
    with pytest.raises(mddb.ConflictError, match="changed since read"):
        delete_event(deck, card_id, "from", "2024-06-10T10:00:00+01:00")
    assert "UNTIL" not in mddb.MDDB(deck).read(card_id).yaml["rrule"]
    assert mddb.MDDB(deck).read(override_id).title == "Seminar (room B)"


def test_undo_delete_from_restores_series_and_override(deck):
    card_id = _recurring(deck)
    override_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Seminar (moved)",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="one",
        start="2024-06-17T10:00:00+01:00",
    )
    delete_event(deck, card_id, "from", "2024-06-10T10:00:00+01:00")
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"])
    assert "UNTIL" not in mddb.MDDB(deck).read(card_id).yaml["rrule"]
    assert mddb.MDDB(deck).read(override_id).title == "Seminar (moved)"
    starts = sorted(o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1))
    assert len(starts) == 4
    assert "2024-06-17T14:00:00+01:00" in starts


def test_undo_synced_delete_from_restores_master_and_instance(deck, synced):
    card_id = _screate(deck, RECUR, synced)
    override_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Standup (moved)",
            "repeat": "keep",
            "start": "2024-06-17T13:00",
            "end": "2024-06-17T13:15",
        },
        scope="one",
        start="2024-06-17T09:00:00+01:00",
        **synced.kw,
    )
    delete_event(deck, card_id, "from", "2024-06-10T09:00:00+01:00", **synced.kw)
    token = undo_token(deck)
    undo_event(token["deck"], token["commit"], **synced.kw)
    master = _card_of(deck, card_id)
    assert "UNTIL" not in master.yaml["rrule"]
    assert master.yaml["dispatched"] == synced.stamp
    over = _card_of(deck, override_id)
    assert over.yaml["event_status"] == "CONFIRMED"
    assert synced.patches[-1][3].yaml["event_status"] == "CONFIRMED"
    starts = sorted(
        o["start"] for o in _window(deck, 2024, 6, 1, 2024, 7, 1, synced=synced.map)
    )
    assert len(starts) == 4
    assert "2024-06-17T13:00:00+01:00" in starts


ENRICHED_MASTER = (
    "UID:rich@x\nSUMMARY:Board\n"
    "DTSTART;TZID=Europe/London:20240603T100000\n"
    "DTEND;TZID=Europe/London:20240603T103000\n"
    "RRULE:FREQ=WEEKLY\nSTATUS:CONFIRMED\nSEQUENCE:0\n"
    "ORGANIZER:mailto:boss@x\n"
    'ATTENDEE;CN="Smith, Alice";PARTSTAT=ACCEPTED:mailto:alice@x\n'
    "ATTENDEE;PARTSTAT=DECLINED;X-GOOGLE-SELF=TRUE:mailto:will@x\n"
    "X-GOOGLE-CONFERENCE-ENTRY;TYPE=video:https://meet.google.com/abc\n"
    "X-GOOGLE-CONFERENCE:https://meet.google.com/abc\n"
    "ATTACH;FILENAME=Agenda.pdf:https://drive.google.com/a\n"
    'X-GOOGLE-GUESTS-CAN:{"guestsCanModify":true}\n'
    "X-GOOGLE-COLOR-ID:5\n"
    "X-GOOGLE-EVENT-ID:g1\n"
    "X-GOOGLE-CALENDAR-ID:cal@x\n"
    "X-GOOGLE-HTML-LINK:https://calendar.google.com/event?eid=abc"
)


def test_occurrence_json_exposes_details(deck):
    _card_with(deck, ENRICHED_MASTER)
    (occ,) = [o for o in _window(deck, 2024, 6, 3, 2024, 6, 4) if o["title"] == "Board"]
    assert occ["organizer"] == "boss@x"
    assert {a["email"] for a in occ["attendees"]} == {"alice@x", "will@x"}
    alice = [a for a in occ["attendees"] if a["email"] == "alice@x"][0]
    assert alice["name"] == "Smith, Alice"
    assert alice["status"] == "ACCEPTED"
    assert occ["my_status"] == "DECLINED"
    assert occ["conference"] == [
        {"uri": "https://meet.google.com/abc", "type": "video"}
    ]
    assert occ["conference_url"] == "https://meet.google.com/abc"
    assert occ["meeting_links"] == [
        {"url": "https://meet.google.com/abc", "provider": "Google Meet"}
    ]
    assert occ["attachments"] == [
        {"url": "https://drive.google.com/a", "title": "Agenda.pdf"}
    ]
    assert occ["gcal_link"] == "https://calendar.google.com/event?eid=abc"
    assert occ["attendees_omitted"] is False


def test_occurrence_json_preserves_exact_description(deck):
    event = (
        "UID:description@x\nSUMMARY:Description\n"
        "DTSTART;TZID=Europe/London:20240603T100000\n"
        "DTEND;TZID=Europe/London:20240603T103000\n"
        'STATUS:CONFIRMED\nDESCRIPTION:  <a href="https://zoom.us/j/1">Join</a>  '
    )
    _card_with(deck, event)
    (occ,) = [
        o for o in _window(deck, 2024, 6, 3, 2024, 6, 4) if o["title"] == "Description"
    ]
    assert occ["description"] == '  <a href="https://zoom.us/j/1">Join</a>  '
    assert occ["meeting_links"] == [{"url": "https://zoom.us/j/1", "provider": "Zoom"}]


def test_update_from_carries_enrichment(deck):
    card_id = _card_with(deck, ENRICHED_MASTER)
    new_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Board (moved)",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="from",
        start="2024-06-17T10:00:00+01:00",
    )
    new = mddb.MDDB(deck).read(new_id)
    for kept in (
        "ATTENDEE;CN=",
        "X-GOOGLE-SELF=TRUE",
        "X-GOOGLE-CONFERENCE-ENTRY",
        "X-GOOGLE-CONFERENCE:",
        "ATTACH;FILENAME=Agenda.pdf",
        "X-GOOGLE-GUESTS-CAN",
        "X-GOOGLE-COLOR-ID:5",
    ):
        assert kept in new.body, kept
    for shed in ("X-GOOGLE-EVENT-ID", "X-GOOGLE-HTML-LINK", "ORGANIZER"):
        assert shed not in new.body, shed
    assert {a["email"] for a in new.yaml["attendees"]} == {"alice@x", "will@x"}
    assert new.yaml["my_status"] == "DECLINED"
    old = mddb.MDDB(deck).read(card_id)
    assert "ATTENDEE;CN=" in old.body
    assert "X-GOOGLE-EVENT-ID:g1" in old.body


def test_series_edit_preserves_enrichment(deck):
    card_id = _card_with(deck, ENRICHED_MASTER)
    update_event(deck, card_id, {**EDIT, "title": "Board (renamed)", "repeat": "keep"})
    card = mddb.MDDB(deck).read(card_id)
    assert "ATTENDEE;CN=" in card.body
    assert card.yaml["my_status"] == "DECLINED"
    assert {a["email"] for a in card.yaml["attendees"]} == {"alice@x", "will@x"}


def test_edit_one_carries_enrichment_to_override(deck):
    card_id = _card_with(deck, ENRICHED_MASTER)
    override_id = update_event(
        deck,
        card_id,
        {
            **EDIT,
            "title": "Board (moved)",
            "repeat": "keep",
            "start": "2024-06-17T14:00",
            "end": "2024-06-17T14:30",
        },
        scope="one",
        start="2024-06-17T10:00:00+01:00",
    )
    override = mddb.MDDB(deck).read(override_id)
    for kept in (
        "ATTENDEE;CN=",
        "X-GOOGLE-CONFERENCE:",
        "ATTACH;FILENAME=Agenda.pdf",
        "X-GOOGLE-GUESTS-CAN",
    ):
        assert kept in override.body, kept
    for shed in ("X-GOOGLE-EVENT-ID", "X-GOOGLE-HTML-LINK", "ORGANIZER"):
        assert shed not in override.body, shed
    assert {a["email"] for a in override.yaml["attendees"]} == {"alice@x", "will@x"}
    assert override.yaml["my_status"] == "DECLINED"
    assert "rrule" not in override.yaml
    (occ,) = [
        o
        for o in _window(deck, 2024, 6, 17, 2024, 6, 18)
        if o["title"] == "Board (moved)"
    ]
    assert occ["my_status"] == "DECLINED"
