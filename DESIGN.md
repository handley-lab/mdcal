# mdcal design

Correct, minimal documentation is best. Omission is preferable to an
unsupported or obsolete claim. Incorrect documentation is worst.

MDCal is a Python calendar layer over ordinary mddb cards. It owns event
vocabulary, recurrence, ICS/iMIP serialization, Google translation, free/busy,
and window projection. Deployments own decks, polling, credentials, web
applications, approval, and sending.

## Event cards

An event is one card. `title`, `summary`, `body`, and `tags` retain their mddb
meanings. Calendar fields are flat YAML indexed through mddb. The exact set
owned by a calendar re-render is `mdcal.ics.EVENT_KEYS`.

`uid` is iCalendar identity and is distinct from the card's mddb `id`. Import
identity includes source, UID, and recurrence ID. `status` stores VEVENT
`STATUS`. The substrate's `kind` says which vocabulary owns a card, so `status`
means VEVENT `STATUS` on `kind: event` and a GTD state on `kind: task`, and
each layer reads only its own cards.

Tags become deck-owned after card creation. Imported categories and feed tags
may seed them, but a later source re-render does not replace local
classification. `apply_render` strips only MDCal-owned keys and preserves
foreign frontmatter.

## Recurrence

A recurring series is one master card with RRULE/RDATE/EXDATE data. Window
queries expand it at read time. A source exception is its own card with a
recurrence ID; the resolver suppresses the generated occurrence and uses the
exception. There is no materialized occurrence cache.

All-day and timed values retain their distinct semantics. Timed values must be
timezone-aware. Epoch fields are query projections, not a substitute for the
original calendar value.

## External boundaries

ICS import maps VEVENTs through the same pure renderer used for previews and
writes. `prune=True` deletes only cards in the named source absent from the new
complete input. A recent `dispatched` guard prevents a lagging feed from pruning
a write-through event before the source reflects it.

Google export preserves every mapped field described by `gcal.COMPLETENESS` and
fails on unknown provider fields. Existing events are patched; importing a
replacement representation would erase information Google owns. An
`attendeesOmitted` response cannot seed a new event because its guest list is
known incomplete.

iMIP functions construct and inspect calendar payloads but never fetch mail or
send a reply. Invitation decisions and mail proposals belong to their deployment
capability boundaries.

## Composition

MDCal functions accept mddb decks, cards, ordinary values, and provider clients.
They do not own a deck registry, scheduler, command-line interface, or transport
daemon. A deployment composes:

```python
from mdcal.gcal import export_ics
from mdcal.ics import import_ics
from mdcal.imip import build_reply_email
from mdcal.window import events_in_window
```

Unexpected recurrence, source, or provider state fails visibly. Do not add
compatibility readers, guessed defaults, or a parallel event schema to hide
drift. The code and tests are authoritative for exact field and mutation
semantics.
