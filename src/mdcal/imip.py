"""iMIP REQUEST/REPLY serialisation — the third mdcal sync pipe.

An event invitation arrives as email carrying a ``text/calendar; method=REQUEST``
attachment (RFC 5546 iMIP). This module owns the two `.ics` transforms mdcal is
responsible for in that pipe:

* :func:`parse_request` — read a REQUEST into an :class:`Invite` for surfacing as
  a GTD clarify item (organiser, when, where);
* :func:`build_reply` / :func:`build_reply_email` — produce the ``METHOD:REPLY``
  the responder mails back to the organiser (``PARTSTAT`` = accepted / declined /
  tentative).

The mail fetch, the GTD-inbox surface, and the actual send are the caller's job
(mdgtd#12); mdcal only serialises. On accept, the caller additionally imports the
event locally via :func:`mdcal.ics.vevent_to_card`.
"""

import argparse
import datetime as _dt
import sys
from dataclasses import dataclass
from email.message import EmailMessage

import icalendar

PARTSTATS = {"accept": "ACCEPTED", "decline": "DECLINED", "tentative": "TENTATIVE"}

_PRODID = "-//handley-lab//mdcal//EN"


def _addr(value):
    """The bare email of an ``ORGANIZER``/``ATTENDEE`` value (drops ``mailto:``)."""
    return str(value).split(":", 1)[-1] if value is not None else None


@dataclass
class Invite:
    """A parsed iMIP REQUEST, ready to surface as a clarify decision.

    Attributes:
        uid: The event UID (identity for the matching REPLY).
        sequence: The REQUEST's SEQUENCE (echoed in the REPLY so the organiser's
            client pairs the response with the right revision).
        organiser: Email of the organiser — the REPLY's recipient.
        summary: The event title.
        dtstart: Event start (date or aware datetime, as icalendar decoded it).
        dtend: Event end, or ``None`` if the REQUEST omitted it.
        location: Free-text location, or ``None``.
        vevent: The raw ``VEVENT`` component, for handing to
            :func:`mdcal.ics.vevent_to_card` on accept.
    """

    uid: str
    sequence: int
    organiser: str
    summary: str
    dtstart: object
    dtend: object
    location: str
    vevent: icalendar.Event


def _request_master(ics_text):
    """The METHOD:REQUEST calendar and its master VEVENT.

    Raises:
        ValueError: The calendar's METHOD is not REQUEST, or it carries no
            VEVENT — either means this is not an invitation (crash-on-drift
            rather than silently replying to the wrong thing).
    """
    cal = icalendar.Calendar.from_ical(ics_text)
    method = str(cal.get("METHOD") or "")
    if method != "REQUEST":
        raise ValueError(f"not an iMIP REQUEST (METHOD={method or 'absent'})")
    events = cal.walk("VEVENT")
    if not events:
        raise ValueError("REQUEST carries no VEVENT")
    return cal, events[0]


def parse_request(ics_text):
    """Parse an iMIP REQUEST `.ics` into an :class:`Invite`.

    Args:
        ics_text: The raw ``text/calendar; method=REQUEST`` body.

    Returns:
        The :class:`Invite` describing the invitation.

    Raises:
        ValueError: The body is not a REQUEST, carries no VEVENT, or the VEVENT
            has no ORGANIZER (there would be nobody to reply to).
    """
    _, vevent = _request_master(ics_text)
    organiser = _addr(vevent.get("ORGANIZER"))
    if organiser is None:
        raise ValueError(f"REQUEST VEVENT uid={vevent.get('UID')} has no ORGANIZER")
    dtend = vevent.get("DTEND")
    location = vevent.get("LOCATION")
    return Invite(
        uid=str(vevent["UID"]),
        sequence=int(vevent.get("SEQUENCE", 0)),
        organiser=organiser,
        summary=str(vevent.get("SUMMARY", "")),
        dtstart=vevent["DTSTART"].dt,
        dtend=dtend.dt if dtend is not None else None,
        location=str(location) if location is not None else None,
        vevent=vevent,
    )


def build_reply(ics_text, attendee, response, cn=None):
    """Serialise the ``METHOD:REPLY`` for a responder's decision on a REQUEST.

    The reply echoes the REQUEST's UID, SEQUENCE, ORGANIZER, DTSTART and SUMMARY,
    and carries exactly one ATTENDEE — the responder — with the chosen PARTSTAT,
    plus a fresh DTSTAMP (RFC 5546 §3.2.3).

    Args:
        ics_text: The originating REQUEST body.
        attendee: The responder's email (the reply's From/ATTENDEE).
        response: One of ``accept`` / ``decline`` / ``tentative``.
        cn: Optional display name for the responder's ATTENDEE line.

    Returns:
        The REPLY `.ics` text.

    Raises:
        ValueError: ``response`` is not a known key, or the source is not a
            valid REQUEST (via :func:`_request_master`).
    """
    if response not in PARTSTATS:
        raise ValueError(
            f"response must be one of {sorted(PARTSTATS)}, got {response!r}"
        )
    _, req = _request_master(ics_text)

    reply = icalendar.Calendar()
    reply.add("prodid", _PRODID)
    reply.add("version", "2.0")
    reply.add("method", "REPLY")

    event = icalendar.Event()
    event.add("uid", req["UID"])
    event.add("sequence", int(req.get("SEQUENCE", 0)))
    event.add("dtstamp", _dt.datetime.now(_dt.timezone.utc))
    event.add("dtstart", req["DTSTART"].dt)
    if "SUMMARY" in req:
        event.add("summary", req["SUMMARY"])
    event["ORGANIZER"] = req["ORGANIZER"]

    who = icalendar.vCalAddress(f"mailto:{attendee}")
    who.params["PARTSTAT"] = PARTSTATS[response]
    if cn:
        who.params["CN"] = cn
    event.add("attendee", who, encode=False)

    reply.add_component(event)
    return reply.to_ical().decode()


def build_reply_email(ics_text, attendee, response, cn=None):
    """Build the iMIP REPLY *email* the responder sends to the organiser.

    Wraps :func:`build_reply` as a ``text/calendar; method=REPLY`` message
    addressed to the REQUEST's organiser. The caller submits it (e.g. via
    ``msmtp``); mdcal does not send.

    Args:
        ics_text: The originating REQUEST body.
        attendee: The responder's email (``From``).
        response: One of ``accept`` / ``decline`` / ``tentative``.
        cn: Optional display name for the responder.

    Returns:
        An :class:`email.message.EmailMessage` ready to submit.
    """
    invite = parse_request(ics_text)
    reply_ics = build_reply(ics_text, attendee, response, cn=cn)

    verb = {"accept": "Accepted", "decline": "Declined", "tentative": "Tentative"}[
        response
    ]
    msg = EmailMessage()
    msg["From"] = f"{cn} <{attendee}>" if cn else attendee
    msg["To"] = invite.organiser
    msg["Subject"] = f"{verb}: {invite.summary}"
    msg.set_content(f"{verb} the invitation “{invite.summary}”.")
    msg.add_alternative(
        reply_ics, subtype="calendar", params={"method": "REPLY", "charset": "UTF-8"}
    )
    return msg


def main(argv=None):
    """Run the ``mdcal-reply`` console script.

    Reads a REQUEST `.ics` (``--ics`` or stdin) and either prints the invitation
    summary (``--show``) or emits the REPLY — the raw `.ics` (default) or the full
    email (``--email``) — to stdout for the caller to send.

    Args:
        argv: Argument list for testing; defaults to ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(prog="mdcal-reply")
    parser.add_argument("--ics", help="REQUEST .ics path (default: stdin)")
    parser.add_argument(
        "--show", action="store_true", help="print the invite summary, no reply"
    )
    parser.add_argument("--response", choices=sorted(PARTSTATS))
    parser.add_argument(
        "--attendee", help="responder email (the reply's From/ATTENDEE)"
    )
    parser.add_argument("--cn", help="responder display name")
    parser.add_argument(
        "--email",
        action="store_true",
        help="emit the full REPLY email, not just the .ics",
    )
    args = parser.parse_args(argv)

    ics_text = sys.stdin.read() if args.ics is None else open(args.ics).read()

    if args.show:
        inv = parse_request(ics_text)
        where = f" @ {inv.location}" if inv.location else ""
        print(f"{inv.summary}{where}\n  when: {inv.dtstart}\n  from: {inv.organiser}")
        return
    if not args.response or not args.attendee:
        parser.error("--response and --attendee are required unless --show")
    if args.email:
        print(build_reply_email(ics_text, args.attendee, args.response, cn=args.cn))
    else:
        print(build_reply(ics_text, args.attendee, args.response, cn=args.cn))


if __name__ == "__main__":
    main()
