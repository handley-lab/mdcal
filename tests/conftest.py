import os
import subprocess

import mddb
import pytest


@pytest.fixture(autouse=True)
def _xdg_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(autouse=True, scope="session")
def _git_identity():
    if not (_git_config("user.email") and _git_config("user.name")):
        os.environ["GIT_AUTHOR_NAME"] = "mdcal test"
        os.environ["GIT_AUTHOR_EMAIL"] = "test@mdcal"
        os.environ["GIT_COMMITTER_NAME"] = "mdcal test"
        os.environ["GIT_COMMITTER_EMAIL"] = "test@mdcal"


def _git_config(key):
    r = subprocess.run(
        ["git", "config", "--global", "--get", key], capture_output=True, text=True
    )
    return r.stdout.strip()


@pytest.fixture
def db(tmp_path):
    return mddb.MDDB.init(tmp_path)


ICS_SAMPLE = """\
BEGIN:VCALENDAR
PRODID:-//mdcal test//EN
VERSION:2.0
X-WR-CALNAME:Test
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
BEGIN:VEVENT
UID:plain-1@example.com
DTSTAMP:20260101T120000Z
DTSTART;TZID=Europe/London:20240115T100000
DTEND;TZID=Europe/London:20240115T110000
SEQUENCE:0
STATUS:CONFIRMED
TRANSP:OPAQUE
SUMMARY:Plain meeting
LOCATION:Room 1
END:VEVENT
BEGIN:VEVENT
UID:series-1@example.com
DTSTAMP:20260101T120000Z
DTSTART;TZID=Europe/London:20240108T130000
DTEND;TZID=Europe/London:20240108T133000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6
EXDATE;TZID=Europe/London:20240122T130000
SEQUENCE:0
STATUS:CONFIRMED
TRANSP:OPAQUE
SUMMARY:Weekly standup
END:VEVENT
BEGIN:VEVENT
UID:series-1@example.com
DTSTAMP:20260101T120000Z
RECURRENCE-ID;TZID=Europe/London:20240115T130000
DTSTART;TZID=Europe/London:20240115T140000
DTEND;TZID=Europe/London:20240115T150000
SEQUENCE:1
STATUS:CONFIRMED
TRANSP:OPAQUE
SUMMARY:Weekly standup (moved)
DESCRIPTION:Pushed an hour later.
END:VEVENT
BEGIN:VEVENT
UID:allday-1@example.com
DTSTAMP:20260101T120000Z
DTSTART;VALUE=DATE:20240201
DTEND;VALUE=DATE:20240203
SEQUENCE:0
STATUS:CONFIRMED
TRANSP:TRANSPARENT
SUMMARY:Conference trip
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def ics_sample():
    return ICS_SAMPLE
