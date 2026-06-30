import datetime as dt
import re

import icalendar
import pytest
import yaml

from mdcal.ics import RenderedCard, _ident, main, render_text, vevent_to_card


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
    assert re.fullmatch(r"2024/plain-meeting-[0-9a-f]{12}\.md", card.relpath)
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
    assert card.relpath.startswith("2024/untitled-")


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


def test_main_write_path_not_yet_implemented(ics_sample, tmp_path):
    ics = tmp_path / "research.ics"
    ics.write_text(ics_sample)
    with pytest.raises(NotImplementedError):
        main(["--source", "research", "--ics", str(ics), "--deck", str(tmp_path / "d")])
