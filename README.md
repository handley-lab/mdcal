# mdcal

Correct, minimal documentation is best. Omission is preferable to an
unsupported or obsolete claim. Incorrect documentation is worst.

An mddb-backed personal calendar — your events as [mddb](https://github.com/handley-lab/mddb) cards in a git-backed deck, queryable locally and syncable across your own devices, with ICS and iMIP invite interoperability at the boundary with others' calendars.

Part of a broader de-Googling of an agentic system: you own your calendar data; agents read it directly rather than fighting a remote API. The calendar does one thing — be a calendar; workflow (invite triage, scheduling) is left to agents composing the substrate.

## Python API

Calendar operations compose directly from Python values:

```python
from pathlib import Path

from mdcal.gcal import export_ics
from mdcal.ics import import_ics
from mdcal.imip import build_reply_email

ics = export_ics(credentials, calendar_id)
ics_path = Path("calendar.ics")
ics_path.write_text(ics)
counts = import_ics(deck, ics_path, source, prune=True, tags=("area/work",))
reply = build_reply_email(request_ics, attendee, "accept")
```

Fetching, scheduling, and sending belong to their deployment boundaries; mdcal
does not install console commands for semantic calendar operations.
