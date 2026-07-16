import datetime as dt
import re
import subprocess

import icalendar
import mddb
import pytest
import yaml

from mdcal.ics import (
    RenderedCard,
    _ident,
    import_ics,
    main,
    render_text,
    vevent_to_card,
)


def _count(deck, **field):
    conn = mddb.MDDB(deck).conn
    if not field:
        return conn.execute("SELECT count(*) FROM entries").fetchone()[0]
    ((key, value),) = field.items()
    return conn.execute(
        "SELECT count(*) FROM entry_fields WHERE key=? AND value_str=?", (key, value)
    ).fetchone()[0]


def _vevents(ics_text):
    return list(icalendar.Calendar.from_ical(ics_text).walk("VEVENT"))


def _one(vevent_body):
    cal = (
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\n"
        f"BEGIN:VEVENT\n{vevent_body}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    return _vevents(cal)[0]


def test_sample_fixture_parses(ics_sample):
    assert len(_vevents(ics_sample)) == 4


def test_rendered_card_shape():
    card = RenderedCard(
        title="x", summary="x · y", body="", tags=[], yaml={}, relpath="2024/x-abc.md"
    )
    assert card.relpath.endswith(".md")


def test_plain(ics_sample):
    card = vevent_to_card(_vevents(ics_sample)[0], "test")
    assert card.title == "Plain meeting"
    assert card.summary == "Plain meeting · 2024-01-15 10:00–11:00 Europe/London"
    assert card.yaml["all_day"] is False
    assert card.yaml["tzid"] == "Europe/London"
    assert isinstance(card.yaml["dtstart_epoch"], int)
    assert card.yaml["location"] == "Room 1"
    assert "rrule" not in card.yaml and "recurrence_id" not in card.yaml
    assert re.fullmatch(r"2024-01-15-plain-meeting-[0-9a-f]{12}\.md", card.relpath)
    assert "```ics" in card.body and "END:VEVENT" in card.body


def test_master(ics_sample):
    card = vevent_to_card(_vevents(ics_sample)[1], "test")
    assert card.yaml["rrule"] == "FREQ=WEEKLY;COUNT=6;BYDAY=MO"
    assert isinstance(card.yaml["exdate"], list) and len(card.yaml["exdate"]) == 1
    assert card.summary.endswith("· recurring")
    assert "recurrence_id" not in card.yaml


def test_exception(ics_sample):
    card = vevent_to_card(_vevents(ics_sample)[2], "test")
    assert card.title == "Weekly standup (moved)"
    assert isinstance(card.yaml["recurrence_id"], dt.datetime)
    assert isinstance(card.yaml["recurrence_id_epoch"], int)
    assert card.summary.endswith("· override")
    assert "Pushed an hour later." in card.body


def test_allday_exclusive_dtend(ics_sample):
    card = vevent_to_card(_vevents(ics_sample)[3], "test")
    assert card.yaml["all_day"] is True
    assert type(card.yaml["dtstart"]) is dt.date
    assert "tzid" not in card.yaml
    assert card.summary == "Conference trip · 2024-02-01–2024-02-02 (all-day)"


def test_untitled():
    body = "UID:u1@x\nDTSTAMP:20260101T120000Z\nDTSTART:20240115T100000Z\nDTEND:20240115T110000Z"
    card = vevent_to_card(_one(body), "test")
    assert card.title == "(untitled)"
    assert card.relpath.startswith("2024-01-15-untitled-")


def test_missing_dtend_timed():
    body = "UID:u2@x\nDTSTAMP:20260101T120000Z\nDTSTART:20240115T100000Z"
    card = vevent_to_card(_one(body), "test")
    assert card.yaml["dtend"] == card.yaml["dtstart"]


def test_missing_dtend_allday():
    body = "UID:u3@x\nDTSTAMP:20260101T120000Z\nDTSTART;VALUE=DATE:20240201"
    card = vevent_to_card(_one(body), "test")
    assert card.yaml["dtend"] == dt.date(2024, 2, 2)


def test_cancelled_prefix():
    body = (
        "UID:u4@x\nDTSTAMP:20260101T120000Z\nDTSTART:20240115T100000Z\n"
        "DTEND:20240115T110000Z\nSTATUS:CANCELLED\nSUMMARY:Scrapped"
    )
    card = vevent_to_card(_one(body), "test")
    assert card.summary.startswith("[cancelled] Scrapped · ")


def test_floating_datetime_raises():
    body = (
        "UID:u5@x\nDTSTAMP:20260101T120000Z\nDTSTART:20240115T100000\nSUMMARY:Floating"
    )
    with pytest.raises(ValueError, match="floating"):
        vevent_to_card(_one(body), "test")


def test_master_and_exception_distinct_relpath(ics_sample):
    master = vevent_to_card(_vevents(ics_sample)[1], "test")
    exception = vevent_to_card(_vevents(ics_sample)[2], "test")
    assert master.yaml["uid"] == exception.yaml["uid"]
    assert master.relpath != exception.relpath


def test_ident_roundtrip_str_stable():
    moment = dt.datetime(2024, 1, 15, 13, 0, tzinfo=dt.timezone.utc)
    reloaded = yaml.safe_load(yaml.safe_dump({"r": moment}))["r"]
    assert _ident(moment) == _ident(reloaded)
    assert _ident(None) == ""


def test_render_text_frontmatter_then_body(ics_sample):
    text = render_text(vevent_to_card(_vevents(ics_sample)[0], "test"))
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    keys = list(yaml.safe_load(front))
    assert keys[:4] == ["id", "title", "summary", "source"]
    parsed = yaml.safe_load(front)
    assert parsed["title"] == "Plain meeting"
    assert parsed["uid"] == "plain-1@example.com"
    assert "```ics" in text


def test_main_dry_run_writes_nothing(ics_sample, tmp_path, capsys):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = tmp_path / "deck"
    main(["--source", "research", "--ics", str(ics), "--deck", str(deck), "--dry-run"])
    out = capsys.readouterr().out
    for title in ["Plain meeting", "Weekly standup", "Conference trip"]:
        assert title in out
    assert not deck.exists()


def test_main_dry_run_uid_filters(ics_sample, tmp_path, capsys):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    main(
        [
            "--source",
            "research",
            "--ics",
            str(ics),
            "--uid",
            "plain-1@example.com",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert "Plain meeting" in out and "Conference trip" not in out


def test_main_requires_deck_without_dry_run(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    with pytest.raises(SystemExit):
        main(["--source", "research", "--ics", str(ics)])


def test_import_creates(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    counts = import_ics(deck, str(ics), "research")
    assert counts == {"created": 4, "updated": 0, "skipped": 0, "pruned": 0}
    assert _count(deck) == 4
    assert _count(deck, rrule="FREQ=WEEKLY;COUNT=6;BYDAY=MO") == 1
    assert _count(deck, recurrence_id="2024-01-15 13:00:00+00:00") == 1


def test_import_idempotent_no_new_commit(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    head_before = mddb.MDDB(deck).head()
    counts = import_ics(deck, str(ics), "research")
    assert counts == {"created": 0, "updated": 0, "skipped": 4, "pruned": 0}
    assert _count(deck) == 4
    assert mddb.MDDB(deck).head() == head_before


def test_import_updates_changed_without_duplicating(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    ics.write_text(ics_sample.replace("Plain meeting", "Plain meeting EDITED"))
    counts = import_ics(deck, str(ics), "research")
    assert counts["updated"] == 1 and counts["created"] == 0
    assert _count(deck) == 4
    title = (
        mddb.MDDB(deck)
        .conn.execute(
            "SELECT e.title FROM entries e JOIN entry_fields f ON f.entry_rowid=e.rowid "
            "WHERE f.key='uid' AND f.value_str='plain-1@example.com'"
        )
        .fetchone()[0]
    )
    assert title == "Plain meeting EDITED"


def test_import_preserves_local_key_and_still_skips(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    db = mddb.MDDB(deck)
    cid = db.conn.execute(
        "SELECT e.id FROM entries e JOIN entry_fields f ON f.entry_rowid=e.rowid "
        "WHERE f.key='uid' AND f.value_str='plain-1@example.com'"
    ).fetchone()[0]
    with db.editor(rationale="add a local key") as e:
        card = e.read(cid)
        card.yaml["notes"] = "hello"
        e.update(card, summary=card.summary)
    head = mddb.MDDB(deck).head()
    counts = import_ics(deck, str(ics), "research")
    assert counts["updated"] == 0 and counts["skipped"] == 4
    assert mddb.MDDB(deck).read(cid).yaml["notes"] == "hello"
    assert mddb.MDDB(deck).head() == head


def test_local_retag_survives_clean_reimport_as_skip(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    db = mddb.MDDB(deck)
    cid = db.conn.execute(
        "SELECT e.id FROM entries e JOIN entry_fields f ON f.entry_rowid=e.rowid "
        "WHERE f.key='uid' AND f.value_str='plain-1@example.com'"
    ).fetchone()[0]
    with db.editor(rationale="hide from the grid") as e:
        card = e.read(cid)
        e.update(card, summary=card.summary, tags=["mdcal/hidden", "area/research"])
    head = mddb.MDDB(deck).head()
    counts = import_ics(deck, str(ics), "research")
    assert counts["updated"] == 0 and counts["skipped"] == 4
    assert mddb.MDDB(deck).head() == head


def test_local_retag_survives_upstream_update(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    db = mddb.MDDB(deck)
    cid = db.conn.execute(
        "SELECT e.id FROM entries e JOIN entry_fields f ON f.entry_rowid=e.rowid "
        "WHERE f.key='uid' AND f.value_str='plain-1@example.com'"
    ).fetchone()[0]
    with db.editor(rationale="hide from the grid") as e:
        card = e.read(cid)
        e.update(card, summary=card.summary, tags=["mdcal/hidden"])
    ics.write_text(ics_sample.replace("Plain meeting", "Plain meeting EDITED"))
    counts = import_ics(deck, str(ics), "research")
    assert counts["updated"] == 1
    updated = mddb.MDDB(deck).read(cid)
    assert updated.title == "Plain meeting EDITED"
    assert updated.yaml["tags"] == ["mdcal/hidden"]


def test_seed_tags_created_only_and_deduped(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    counts = import_ics(deck, str(ics), "research", tags=["area/research"])
    assert counts["created"] == 4
    db = mddb.MDDB(deck)
    tagged = db.conn.execute(
        "SELECT COUNT(*) FROM entry_fields WHERE key='tags' "
        "AND value_str='area/research'"
    ).fetchone()[0]
    assert tagged == 4
    cid = db.conn.execute(
        "SELECT e.id FROM entries e JOIN entry_fields f ON f.entry_rowid=e.rowid "
        "WHERE f.key='uid' AND f.value_str='plain-1@example.com'"
    ).fetchone()[0]
    with db.editor(rationale="reclassify") as e:
        card = e.read(cid)
        e.update(card, summary=card.summary, tags=["area/work"])
    ics.write_text(ics_sample.replace("Plain meeting", "Plain meeting EDITED"))
    import_ics(deck, str(ics), "research", tags=["area/research"])
    assert mddb.MDDB(deck).read(cid).yaml["tags"] == ["area/work"]


def test_seed_tags_union_is_duplicate_free(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(
        ics_sample.replace(
            "UID:plain-1@example.com\n",
            "UID:plain-1@example.com\nCATEGORIES:area/research,area/research\n",
        )
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research", tags=["area/research", "area/research"])
    tags = (
        mddb.MDDB(deck)
        .conn.execute(
            "SELECT f.value_str FROM entry_fields f "
            "JOIN entry_fields u ON u.entry_rowid=f.entry_rowid "
            "WHERE f.key='tags' AND u.key='uid' "
            "AND u.value_str='plain-1@example.com'"
        )
        .fetchall()
    )
    assert tags == [("area/research",)]


def test_import_drops_stale_field(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    assert _count(deck, location="Room 1") == 1
    ics.write_text(ics_sample.replace("LOCATION:Room 1\n", ""))
    import_ics(deck, str(ics), "research")
    assert _count(deck, location="Room 1") == 0


def test_fence_excludes_dtstamp(ics_sample):
    card = vevent_to_card(_vevents(ics_sample)[0], "test")
    assert "DTSTAMP" not in card.body
    assert "UID:plain-1@example.com" in card.body


def test_reimport_with_churned_dtstamp_is_noop(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    head = mddb.MDDB(deck).head()
    ics.write_text(
        ics_sample.replace("DTSTAMP:20260101T120000Z", "DTSTAMP:20260703T090909Z")
    )
    counts = import_ics(deck, str(ics), "research")
    assert counts == {"created": 0, "updated": 0, "skipped": 4, "pruned": 0}
    assert mddb.MDDB(deck).head() == head


def test_import_is_one_commit_pinned_to_read_base(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    head_after_first = mddb.MDDB(deck).head()
    plain = ics_sample[
        ics_sample.index("BEGIN:VEVENT") : ics_sample.index("END:VEVENT")
        + len("END:VEVENT\n")
    ]
    churned = ics_sample.replace(plain, "").replace(
        "SUMMARY:Conference trip", "SUMMARY:Conference trip renamed"
    )
    ics.write_text(churned)
    counts = import_ics(deck, str(ics), "research", prune=True)
    assert counts["updated"] == 1
    assert counts["pruned"] == 1
    log = subprocess.run(
        ["git", "-C", deck, "rev-list", "--count", f"{head_after_first}..HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.stdout.strip() == "1"


def test_prune_removes_absent(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    plain = ics_sample[
        ics_sample.index("BEGIN:VEVENT") : ics_sample.index("END:VEVENT")
        + len("END:VEVENT\n")
    ]
    ics.write_text(ics_sample.replace(plain, ""))
    counts = import_ics(deck, str(ics), "research", prune=True)
    assert counts["pruned"] == 1
    assert _count(deck) == 3
    assert _count(deck, uid="plain-1@example.com") == 0


def test_prune_spares_local_and_other_sources(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    import_ics(deck, str(ics), "other")
    db = mddb.MDDB(deck)
    with db.editor(rationale="seed a local event") as e:
        e.create(
            title="Local",
            summary="local event",
            yaml={
                "source": "local",
                "uid": "l@x",
                "all_day": False,
                "event_status": "CONFIRMED",
            },
            relpath="2024-01-01-local-abc.md",
        )
    plain = ics_sample[
        ics_sample.index("BEGIN:VEVENT") : ics_sample.index("END:VEVENT")
        + len("END:VEVENT\n")
    ]
    ics.write_text(ics_sample.replace(plain, ""))
    counts = import_ics(deck, str(ics), "research", prune=True)
    assert counts["pruned"] == 1
    assert _count(deck, source="research") == 3
    assert _count(deck, source="other") == 4
    assert _count(deck, source="local") == 1


def test_prune_noop_commits_nothing(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    head = mddb.MDDB(deck).head()
    counts = import_ics(deck, str(ics), "research", prune=True)
    assert counts == {"created": 0, "updated": 0, "skipped": 4, "pruned": 0}
    assert mddb.MDDB(deck).head() == head


def test_prune_rejects_partial_imports(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    with pytest.raises(ValueError, match="prune"):
        import_ics(deck, str(ics), "research", uid="plain-1@example.com", prune=True)
    with pytest.raises(ValueError, match="prune"):
        import_ics(deck, str(ics), "research", limit=1, prune=True)


def _dispatch(deck, uid, dispatched):
    db = mddb.MDDB(deck)
    cid = db.conn.execute(
        "SELECT e.id FROM entries e JOIN entry_fields f ON f.entry_rowid=e.rowid "
        "WHERE f.key='uid' AND f.value_str=?",
        (uid,),
    ).fetchone()[0]
    with db.editor(rationale="mark dispatched") as e:
        card = e.read(cid)
        card.yaml["dispatched"] = dispatched
        e.update(card, summary=card.summary)
    return cid


def test_guard_skips_stale_feed_content(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    ics_with_lm = ics_sample.replace(
        "SUMMARY:Plain meeting", "LAST-MODIFIED:20260101T120000Z\nSUMMARY:Plain meeting"
    )
    ics.write_text(ics_with_lm)
    import_ics(deck, str(ics), "research")
    _dispatch(
        deck,
        "plain-1@example.com",
        dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    head = mddb.MDDB(deck).head()
    ics.write_text(ics_with_lm.replace("Plain meeting", "Plain meeting REVERTED"))
    counts = import_ics(deck, str(ics), "research")
    assert counts["updated"] == 0 and counts["skipped"] == 4
    assert mddb.MDDB(deck).head() == head


def test_guard_inert_once_feed_catches_up(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(
        ics_sample.replace(
            "SUMMARY:Plain meeting",
            "LAST-MODIFIED:20260101T120000Z\nSUMMARY:Plain meeting",
        )
    )
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    cid = _dispatch(
        deck,
        "plain-1@example.com",
        dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    ics.write_text(
        ics_sample.replace(
            "SUMMARY:Plain meeting",
            "LAST-MODIFIED:20260701T120000Z\nSUMMARY:Plain meeting CONVERGED",
        )
    )
    counts = import_ics(deck, str(ics), "research")
    assert counts["updated"] == 1
    card = mddb.MDDB(deck).read(cid)
    assert card.title == "Plain meeting CONVERGED"
    assert card.yaml["dispatched"] == dt.datetime(
        2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc
    )


def test_guarded_card_without_feed_last_modified_raises(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    _dispatch(
        deck,
        "plain-1@example.com",
        dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    ics.write_text(ics_sample.replace("Plain meeting", "Plain meeting CHANGED"))
    with pytest.raises(ValueError, match="LAST-MODIFIED"):
        import_ics(deck, str(ics), "research")


def test_prune_exempts_recent_dispatch(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    _dispatch(deck, "plain-1@example.com", dt.datetime.now(dt.timezone.utc))
    plain = ics_sample[
        ics_sample.index("BEGIN:VEVENT") : ics_sample.index("END:VEVENT")
        + len("END:VEVENT\n")
    ]
    ics.write_text(ics_sample.replace(plain, ""))
    counts = import_ics(deck, str(ics), "research", prune=True)
    assert counts["pruned"] == 0
    assert _count(deck, uid="plain-1@example.com") == 1


def test_prune_collects_after_grace(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    _dispatch(
        deck,
        "plain-1@example.com",
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2),
    )
    plain = ics_sample[
        ics_sample.index("BEGIN:VEVENT") : ics_sample.index("END:VEVENT")
        + len("END:VEVENT\n")
    ]
    ics.write_text(ics_sample.replace(plain, ""))
    counts = import_ics(deck, str(ics), "research", prune=True)
    assert counts["pruned"] == 1
    assert _count(deck, uid="plain-1@example.com") == 0


def test_gcal_not_imported_by_core_modules():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import mdcal.ics, mdcal.window, sys; "
            "assert 'googleapiclient' not in sys.modules",
        ],
        check=True,
        env={"PYTHONPATH": "src"},
    )


def test_guarded_missing_lm_raises_even_when_unchanged(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    deck = str(tmp_path / "deck")
    import_ics(deck, str(ics), "research")
    _dispatch(
        deck,
        "plain-1@example.com",
        dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(ValueError, match="LAST-MODIFIED"):
        import_ics(deck, str(ics), "research")
