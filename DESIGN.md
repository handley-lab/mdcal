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
serialisation is preserved** (body fence, via `icalendar.to_ical()` — every property/param kept,
but normalised, *not* byte-original) so import bugs are fixable without re-exporting. Byte-original
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
the *stateless* primitive runs **~0.8–1.4 s/window** — dominated by parsing the ~442 rrules; it reads
cards from the `entries.yaml_text` cache (not `db.read`, whose blob-scan is O(deck) on a flat deck). A
bare rrule-expansion micro-benchmark is ~tens of ms, but parse+load is the real cost. Request-level
caching (parsed masters keyed by deck HEAD) and an indexed `until_epoch` prefilter to skip expired
masters live in the **grid layer**, which owns the navigation loop — not in this primitive.

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

## Subscribed external feeds (e.g. GAMBIT) — same base + overlay pattern

A feed is a read-only `.ics` URL the owner does not control. A small mdcal **fetcher** periodically
pulls it and writes/updates cards tagged `source=<feed>`, keyed by `uid`. Each card splits into:

- a **base** section — the fetcher overwrites this verbatim each pull (upstream facts);
- an **adjustments** overlay — the fetcher *never* touches it (local edits).

Render = base with adjustments applied. Local hide = `adjustments: {hidden: true}`; local rename =
`adjustments: {title: ...}`. Kept events track upstream changes (the fetcher refreshes base); local
overlay always survives. This is OpenAI's recommended fix for "the fetcher is just another writer
that clobbers local edits" — and the **same** base+overlay shape as recurrence overrides.

**Tombstones, not deletes.** An event removed upstream, or a single occurrence the owner drops, is
marked (`adjustments: {hidden}` / a tombstone) so the next pull does not resurrect it. SQL queries
filter tombstones. Honour `sequence`: never apply an older upstream component over a newer one.
`STATUS:CANCELLED` stays as a (hidden) card, not a delete — for faithful history and interop.

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
2. **Subscribed feed = one-way ICS pull** (web → deck, read-only upstream + local overlay). Not
   git, not the merge driver — pure Python in the fetcher.
3. **Invites to/from others = iMIP email** (an invite is an `.ics`-bearing email; RSVP returns by
   email). The interop boundary; unrelated to (1) and (2). No retained Google API credential.

## Interop and the outbox

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
