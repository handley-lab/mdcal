# mdcal — design

Living design notes for an mddb-backed personal calendar. Decisions are settled unless under
**Open questions**. This records *what* and *why*; it is not an implementation plan.

## Goal and scope

A Google-Calendar replacement that owns its data as [mddb](https://github.com/handley-lab/mddb)
cards, queryable locally by agents, synced across the owner's own devices, with a phone-friendly
visual grid and standards-based interop at the boundary with other people's calendars.

Tight scope: **it does one thing — be a calendar.** It provides the *mechanisms* (event store,
recurrence, ICS import/export, iMIP payloads, free/busy, feed sync). Workflow — email ingestion,
invite triage, scheduling negotiation — is left to agents *composing* mdcal, never built into it.

mdcal is an early module of a larger arc: rebuilding an agentic GTD system up from mddb
primitives. Design choices must not paint that arc into a corner.

## Topology

- **mddb** — the substrate (generic, `handley-lab`, PyPI). Untouched by mdcal; no calendar code
  goes into it.
- **mdcal** — the calendar *library* (generic, `handley-lab`, PyPI). Event model, ICS/iMIP
  serialisation, recurrence expansion, feed sync, free/busy. Depends on `mddb`.
- **alan-work** — a deployment repo (sibling to `alan-home`). Hosts the grid HTTP app and
  systemd units, served on `lovelace` now (movable to a work box later). *Consumes* mdcal.
- **Decks** — the calendar data. Each deck is its **own private GitHub repo**, living in neither
  app repo, cloned to a local path chosen by the *deployment* (the mdcal library takes the deck path
  as an argument and implies no privileged location; Alan's deployment uses `/var/lib/mdcal/<deck>`,
  FHS-correct application state mirroring `/var/lib/alan-loop`). The grid app
  overlays multiple decks and toggles each (work / home / polychord / subscribed) like Google's
  left-hand calendar list. Multiple calendars = separate decks unified in one view, *not* a field
  within one deck.

## Event model

**An event is a card.** Substrate filing vocabulary does real work:

| iCalendar (RFC 5545) | mddb | note |
|---|---|---|
| `SUMMARY` (short title) | `title` | NB: iCal `SUMMARY` ≠ mddb `summary` |
| `DESCRIPTION` | `body` | human notes / original description |
| `CATEGORIES` | `tags` | |
| — | `summary` | mddb's mandatory disclosure one-liner; mdcal must supply a real value |

Everything else is **flat layer YAML** modelled on VEVENT defaults, indexed by mddb's
`entry_fields` and reached via raw SQL: `uid`, `dtstart`, `dtend`/`duration`, `dtstart_epoch`
(UTC, for range queries), `tzid`, `all_day`, `rrule`, `status`, `transp`, `sequence`, `organizer`,
`location`, `source`. Structured iCal data (attendees, organizer) keeps a flat companion field for
search (e.g. `attendee_emails: [...]`) alongside full fidelity; a **content-complete VEVENT
serialisation is preserved** (body fence, via `icalendar.to_ical()` — every *event-content*
property/param kept, normalised, *not* byte-original; the sole exclusion is `DTSTAMP`, which
Google feeds stamp with the serve time — volatile serialisation metadata, see §Subscribed
external feeds) so import bugs are fixable without re-exporting. Byte-original
preservation and full VTIMEZONE/semantic re-export fidelity are deferred (an importer that needs
faithful outbound re-serialisation would split the source `.ics` into original VEVENT byte spans).

**`uid` (iCal) is a layer field, distinct from mddb `id`** (a substrate-owned UUIDv4). Imported and
subscribed events carry an upstream `uid` preserved for sync-by-uid. mddb does **not** enforce `uid`
uniqueness — dedup is mdcal's job (SQL lookup before create/update). The same `uid` in two
calendars is **not** auto-collapsed into one card; cross-calendar coalescing, if wanted, is a
*view* concern. Import/sync identity is `source_calendar + uid + recurrence_id`.

## Recurrence and overlays — one pattern: base + overlay, resolved at read time

A recurring event is **one master card** holding the rule, not N materialised instances. The master
carries `rrule` + `exdate` (a flat date list, top-level/indexed) + an `overrides` block keyed by
`recurrence_id` (each: moved time / new title / `cancelled`).

**Read-time resolution** (per visible window): expand the master's `rrule` → drop `exdate`s → for
each `recurrence_id`, suppress the generated instance (its exception card renders concretely at its own
time). Implemented in `mdcal/window.py` (`events_in_window`). **Conclusion: read-time expansion is the
model; no materialised instance cache.** Performance (measured on the live 442-master Research deck):
the *stateless* primitive runs **~85 ms/month-window** after two targeted fixes — libyaml's
`CSafeLoader` for frontmatter parsing (identical SafeLoader semantics, ~9× faster than pure-Python
PyYAML, which alone cost ~750 ms/window) and Python-side joins replacing two `entry_fields` key-range
SQL joins that SQLite could only nested-loop (~640 ms combined; no `(entry_rowid, key)` index exists).
dateutil rrule expansion itself is ~17 ms for all 442 masters. It reads cards from the
`entries.yaml_text` cache (not `db.read`, whose blob-scan is O(deck) on a flat deck). At ~85 ms no
request-level caching is needed; if it ever is, it lives in the **grid layer**, which owns the
navigation loop — not in this primitive. The grid's two-deck union (research + subscribed, 4,810
cards total) measures ~117 ms/month-window end-to-end over TLS.

**Promotion.** An occurrence that accrues real content (notes, a write-up, its own attendees) is
**promoted to its own card** keyed `uid + recurrence_id`, so it is independently FTS-findable and
linkable. A promoted card is just a `recurrence_id` override that lives in its own card: the
resolver suppresses the master's generated instance for that slot and uses the card. Trivial
overrides (moved / cancelled) stay inline on the master; substantive ones graduate. One resolver,
two storage locations. **At import** (§Import) the choice is settled the simple way: *every* upstream
`RECURRENCE-ID` component becomes its own card (a Google export's overrides are content-bearing by
construction), so the importer never writes an inline `overrides` block — that block remains for
locally-authored trivial overrides later.

The grid query is therefore **SQL for candidates (non-recurring events overlapping the window +
all cards with an `rrule`) then Python expand/resolve** — not pure SQL. Nested `overrides`/overlay
data is not itself SQL/FTS-queryable (mddb indexes only top-level scalars and lists); flat fields
stay queryable, override *details* are seen only after resolution. Promotion is the escape hatch
when an occurrence must be findable.

## Subscribed external feeds (e.g. GAMBIT) — read-only subset now, base + overlay later

A feed is a `.ics` URL polled periodically into a deck. **The implemented subset (feed-sync
slice) is read-only ingest**: the fetcher is `curl` + the `mdcal-import` CLI on a systemd timer
(composition, not a Python fetcher module — the Python lives in `import_ics` itself), writing
ordinary flat importer cards keyed `source + uid + recurrence_id`, with **prune-on-absent**
(`mdcal-import --prune`): a card of that source absent from the fetched feed's identity set is
deleted, git history serving as the tombstone. Prune is only sound against a feed serving the
calendar's **full historical span** — verified per feed before it ships (both Google URL
flavours checked live: spans back to the oldest events, counts matching the deck). The layer
invariant makes prune lossless: web/local edits are forbidden on feed-sourced cards
(`source != local` is read-only), so pruned cards carry nothing local. `--prune` refuses
`--uid`/`--limit` — a partial import's identity set would mass-delete the unselected remainder.

Google's ICS endpoints send **no ETag / no Last-Modified** (`cache-control: no-store`), so
conditional GET is impossible — and feeds are never even byte-stable: measured across paired
fetches, Google reorders VEVENTs per response and stamps **every event's `DTSTAMP` with the
serve time** (all other properties value-stable). So the fetcher keeps no state and always
imports; idempotency is the comparator. To keep serve-time DTSTAMPs from defeating
`_unchanged`, the feed-sync slice changes `vevent_to_card` to exclude the `DTSTAMP` line from
the fenced VEVENT — with these semantics it is serialisation metadata, not event content (the
flat yaml's `created`/`last_modified` come from the stable CREATED/LAST-MODIFIED). Hourly
polling ships only after a captured re-fetch pair imports as create-then-no-op. Feed URLs come in two
flavours: *public* (`…/ical/<id>/public/basic.ics`) and per-user *secret*
(`…/ical/<id>/private-<token>/basic.ics`) for private calendars — the secret token is a
committed credential of the private deployment repo.

**Deferred to the overlay slice** (needed once feed events take local annotations): the
base+overlay split — a **base** section the fetcher overwrites each pull, an **adjustments**
overlay it never touches (local hide = `adjustments: {hidden: true}`, local rename, …), render =
base with adjustments applied, and marked tombstones instead of deletes so a local hide survives
the next pull. Honour `sequence` (never apply an older upstream component over a newer one) when
overlays land. `STATUS:CANCELLED` already stays as a card (the resolver hides it), not a delete.

## Sync — three distinct pipes (do not conflate)

1. **Own calendar across own devices = git.** Each deck a private GitHub repo; devices clone;
   `push` + `pull` (**merge, never rebase**); the `mddb-card` merge driver reconciles. Two-way,
   Google-free. Provisioning is mandatory: `mddb._merge.install_global()` once per machine/account
   (the driver command never clones) + committed `.gitattributes`; alan-work calls
   `mddb._merge.require_installed(deck)` at its write boundary to defeat the silent-fallback
   footgun. The only true conflict is the same event's same scalar edited concurrently on two
   offline devices → frontmatter conflict → invalid YAML → surfaced in the grid for resolution via
   `conflict_rationales()`. The conflict scanner must work at git/filesystem level, because invalid
   YAML makes `db.read`/rebuild raise *before* the grid can query.
2. **Feed = ICS pull inbound; for *synced* sources, Google API write-through outbound.** Inbound
   stays `curl` + the `mdcal-import` CLI on a timer. A source marked *synced* (a `calendar_id`
   column in the deployment's feeds config) is additionally **writable through the Google
   Calendar API, synchronously in-request**: `events.import` carrying the card's own iCalUID
   (measured: import upserts — same Google id on re-import, content updated; stale `SEQUENCE`
   rejected 400, so writers increment from the existing card), deletes via
   `list(iCalUID, showDeleted=True)` → delete (deleted items are served `status: cancelled`
   with a usable `updated` — the delete watermark). Google is primary: API failure → HTTP 502
   and **no local mutation**; API success → local mutation carrying a non-owned
   `dispatched: <API updated, tz-aware, truncated to seconds>` guard — measured: the feed's
   `LAST-MODIFIED` equals the API's `updated` exactly at second precision, and the feed
   reflects API writes in **~2 minutes** each way (grace window: 1 h). The importer protects
   guarded cards from the lagging feed (skip updates while `last_modified < dispatched`;
   prune-exempt within the grace window) until the feed catches up and converges the card —
   then the guard is inert. Every
   crash window between Google and the deck self-heals via the poll, which is why **no outbox
   is used on this pipe** (see §Interop). EXDATE fidelity through `import`: measured faithful
   for both timed Z-form-vs-TZID and all-day date-form exclusions.
3. **Invites to/from others = iMIP email** (an invite is an `.ics`-bearing email; RSVP returns by
   email). The interop boundary; unrelated to (1) and (2).

## Interop and the outbox

**Scope: the outbox is for iMIP email (irreversible sends) and, later, multi-clone dispatch —
NOT for Google API write-back.** Adversarial design review of the write-back slice concluded:
feed lag attacks the *importer* whichever way dispatch works, so the `dispatched` guard is
required regardless — and with Google primary and its API ops idempotent (import-upsert,
absolute patch, 404-tolerant delete), synchronous write-through has no durable divergence
windows, while an outbox adds them (permanently-pending cards pinning prune exemptions, retry
reordering resurrecting old edits, source labels that lie until dispatch).

Sending an invite is an **irreversible external effect**; a merge/replay must not re-fire it. The
mddb merge-driver PR defers a generic outbox — mdcal **owns the boundary in the layer**, does not
wait for the substrate (both reviewers converged on this independently):

- Outbox state is ordinary mddb cards (`kind=mdcal_outbox`, `state=pending|sent`, the exact MIME/ICS
  payload in the body, a stable `effect_id`).
- A **single designated dispatcher node** (`lovelace`) sends and flips `pending → sent`. Other
  clones may *create* pending cards but must not dispatch — so push/pull-merge can't double-send.
- Exactly-once over SMTP is not guaranteed; the outbox makes replays *visible and controllable*.
  Re-editing an event creates a *new* outbox card (new `effect_id`, incremented `sequence`); sent
  cards are never re-sent.

## Import / migration

Source = the Google Calendar **`.ics` export** (verified faithful: carries `UID`, `ORGANIZER`,
`STATUS`, `SEQUENCE`, `TRANSP`, `RRULE`, `RECURRENCE-ID`, `EXDATE` — all of which the Google API and
its MCP drop; the API also returns expanded instances, not masters). The `.ics` *is* RFC 5545, so it
maps ~1:1 onto VEVENT-shaped cards with least translation. Import parses with `icalendar` (an mdcal
dep, never mddb); the importer only re-serialises the `RRULE` string, while **read-time recurrence
expansion** (`mdcal/window.py`, `events_in_window`) uses `python-dateutil` to expand masters over a
view window. Each upstream `RECURRENCE-ID` component
is written as **its own card** keyed `uid+recurrence_id` (a Google export's overrides are
content-bearing by construction); masters keep `rrule+exdate`; read-time resolution suppresses a
master-generated instance when an exception card with that `uid+recurrence_id` exists. Import is
**idempotent** on `source_calendar + uid + recurrence_id`. Archival calendars (no events after 2024)
import once and sit read-only.

A faithful export was taken 2026-06-30: **24 calendars, 12,507 events** (1,096 RRULE masters, 2,383
RECURRENCE-ID exceptions, 2,685 EXDATEs, 12,630 UIDs; some calendars span 12–16 TZIDs). Held at
`~/gcal-export-2026-06-30.ical.zip`.

## Deck partition (from the real export)

- **work** — Research, Work, Caius, Cambridge University, Talks, Tea/Lunch&Coffee · *archival:*
  \*REACH, Coffee-time miniseminars, Cambridge Machines, DSTL, Project dates
- **home** — Home (primary), Home-anniversaries (1950-rooted; name-collides with primary, watch the
  slug), Exercise, Travel, IVF, Birthdays · *archival:* Proms ×3, Barcelona 2025, Tesco
- **subscribed** (read-only upstream + overlay) — GAMBIT, bins/holidays feeds
- **polychord** — PolyChord Ltd, PolyChord, the Work calendar (organizer `…@polychord.co.uk`)
  — *see Open questions.*

## Substrate boundary — what must NOT enter mddb

No calendar fields on substrate tables/APIs; no calendar query helpers (`events_between`, …); no
calendar semantics in the `mddb-card` merge driver (it stays per-field three-way; it must not learn
that `exdate` is a set, attendees merge by email, `sequence` is monotonic, or `CANCELLED` wins —
that logic is mdcal's); no `uid` uniqueness enforcement; no outbox in the substrate yet. All
calendar concepts are layer YAML + raw SQL over `entry_fields`.

## Open questions

1. **PolyChord deck** — its own deck (mirroring an eventual alan-polychord), or folded into work?
   A distinct CTO/PolyChord-Ltd domain (~2.1k events, distinct organizer identity), so not
   obviously "work."
2. **Free/busy publishing** — where generated, static `.ifb` vs `.ics`, which decks included,
   private-event redaction, access model (secret URL vs authenticated), regeneration cadence.
3. **Inbound iTIP primitives** — even though agents drive workflow, mdcal needs deterministic
   apply-an-iTIP-message-to-cards primitives (`REQUEST`/`CANCEL`/`REPLY`, match by
   `uid`+`organizer`+`recurrence_id`, honour `sequence`) so agents don't each invent calendar
   mutation semantics.
4. **Cross-deck moves** — no cross-deck atomicity in mddb; "move home→work" is delete+create across
   two repos. Define the recoverable two-commit sequence.
5. **Grid app write concurrency** — the app must thread mddb's `base` token (read → edit) so two
   tabs / an agent + the browser don't clobber. Deployment health checks for dirty decks and
   driver provisioning.
6. **Privacy** — calendar data is more sensitive than household telemetry: HTTP app auth, agent
   access scope across decks, private-repo exposure, free/busy leakage.
