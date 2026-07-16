"""Card-level meeting_links derivation + the description_of raw-source authority."""

import icalendar
import pytest

from mdcal.ics import description_of, vevent_to_card


def _card(vevent_text):
    cal = (
        "BEGIN:VCALENDAR\nPRODID:-//t//EN\nVERSION:2.0\nBEGIN:VEVENT\n"
        "UID:m@x\nSUMMARY:Meeting\n"
        "DTSTART;TZID=Europe/London:20260901T100000\n"
        "DTEND;TZID=Europe/London:20260901T103000\nSTATUS:CONFIRMED\n"
        f"{vevent_text}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    vevent = list(icalendar.Calendar.from_ical(cal).walk("VEVENT"))[0]
    return vevent_to_card(vevent, "probe")


def _links(vevent_text):
    return _card(vevent_text).yaml.get("meeting_links")


def test_structured_video_entry_labels_by_provider():
    assert _links("X-GOOGLE-CONFERENCE-ENTRY;TYPE=video:https://zoom.us/j/123") == [
        {"url": "https://zoom.us/j/123", "provider": "Zoom"}
    ]


def test_bare_conference_link_google_meet():
    assert _links("X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij") == [
        {"url": "https://meet.google.com/abc-defg-hij", "provider": "Google Meet"}
    ]


def test_unknown_host_structured_entry_kept_as_generic_meeting():
    assert _links(
        "X-GOOGLE-CONFERENCE-ENTRY;TYPE=video:https://jitsi.example/room"
    ) == [{"url": "https://jitsi.example/room", "provider": "Meeting"}]


def test_non_video_conference_entry_is_not_a_meeting_link():
    assert _links("X-GOOGLE-CONFERENCE-ENTRY;TYPE=phone:tel:+441234") is None


def test_description_only_link_is_matched():
    assert _links(
        'DESCRIPTION:Dial in <a href="https://cam-ac-uk.zoom.us/j/847">here</a>'
    ) == [{"url": "https://cam-ac-uk.zoom.us/j/847", "provider": "Zoom"}]


def test_description_non_conferencing_link_ignored():
    assert (
        _links('DESCRIPTION:Notes at <a href="https://www.vle.cam.ac.uk/x">VLE</a>')
        is None
    )


def test_description_teams_and_webex_and_plain_text_url():
    links = _links(
        "DESCRIPTION:Teams https://teams.microsoft.com/l/meetup/a "
        "or webex https://acme.webex.com/meet/x"
    )
    assert {link["provider"] for link in links} == {"Teams", "Webex"}


def test_html_entity_in_url_unescaped():
    (link,) = _links(
        'DESCRIPTION:<a href="https://zoom.us/j/9?pwd=aa&amp;uid=bb">join</a>'
    )
    assert link["url"] == "https://zoom.us/j/9?pwd=aa&uid=bb"


def test_same_link_in_conference_and_description_deduped():
    links = _links(
        "X-GOOGLE-CONFERENCE:https://meet.google.com/abc\n"
        'DESCRIPTION:<a href="https://meet.google.com/abc">Meet</a>'
    )
    assert links == [{"url": "https://meet.google.com/abc", "provider": "Google Meet"}]


def test_multiple_distinct_links_all_kept():
    links = _links(
        "X-GOOGLE-CONFERENCE-ENTRY;TYPE=video:https://meet.google.com/a\n"
        "DESCRIPTION:backup https://zoom.us/j/2"
    )
    assert links == [
        {"url": "https://meet.google.com/a", "provider": "Google Meet"},
        {"url": "https://zoom.us/j/2", "provider": "Zoom"},
    ]


def test_lookalike_host_rejected():
    # description scrape: a lookalike host is not Zoom, so it is ignored entirely
    assert _links("DESCRIPTION:evil https://zoom.us.evil.example/j/1") is None


def test_no_link_leaves_field_absent():
    assert "meeting_links" not in _card("DESCRIPTION:just a note").yaml


def test_trailing_punctuation_trimmed_from_description_only():
    (link,) = _links("DESCRIPTION:join (https://zoom.us/j/5).")
    assert link["url"] == "https://zoom.us/j/5"


def test_structured_url_punctuation_preserved():
    # a structured conference value is authoritative — not prose to clean
    assert _links(
        "X-GOOGLE-CONFERENCE-ENTRY;TYPE=video:https://jitsi.example/room)"
    ) == [{"url": "https://jitsi.example/room)", "provider": "Meeting"}]


def test_meeting_links_card_reparses():
    card = _card("X-GOOGLE-CONFERENCE:https://meet.google.com/abc")
    fence = card.body.split("```ics\n", 1)[1].split("\n```", 1)[0]
    again = vevent_to_card(icalendar.Event.from_ical(fence), "probe")
    assert again.yaml["meeting_links"] == card.yaml["meeting_links"]


def test_description_of_preserves_whitespace_exactly():
    assert description_of("  spaced  \n\n```ics\nUID:x\n```\n") == "  spaced  "
    assert description_of("plain\n\n```ics\nUID:x\n```\n") == "plain"
    assert description_of("```ics\nUID:x\n```\n") == ""
    # a description that itself ends in a blank line keeps it
    assert description_of("a\n\n\n\n```ics\nUID:x\n```\n") == "a\n\n"


def test_description_of_crashes_on_fenceless_body():
    with pytest.raises(ValueError, match="fenced VEVENT"):
        description_of("no fence here")
