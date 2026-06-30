import icalendar
import pytest

from mdcal.ics import RenderedCard, vevent_to_card


def _vevents(ics_text):
    return list(icalendar.Calendar.from_ical(ics_text).walk("VEVENT"))


def test_sample_fixture_parses(ics_sample):
    assert len(_vevents(ics_sample)) == 4


def test_vevent_to_card_not_yet_implemented(ics_sample):
    plain = _vevents(ics_sample)[0]
    with pytest.raises(NotImplementedError):
        vevent_to_card(plain, "test")


def test_rendered_card_shape():
    card = RenderedCard(
        title="x", summary="x · y", body="", tags=[], yaml={}, relpath="2024/x-abc.md"
    )
    assert card.relpath.endswith(".md")
