"""One-shot: carry snapshot-only area tags onto the live mdcal cards.

The stale pre-cutover snapshot in the GTD personal deck carries ``area/*``
tags on ~700 event cards whose live mdcal counterparts lost them (live cards
were re-created at the calendar migration; tags seed only at creation). Tags
are deck-owned classification — deleting the snapshot without carrying these
would silently discard the only copy of that classification.

For every snapshot card whose tag set exceeds its live counterpart's, the
missing tags are added to the live card (existing live tags untouched — live
wins on everything else). Keyed by ``source + uid + recurrence_id``. One
editor commit on the live deck, pinned to the HEAD captured at the read.
Run BEFORE delete_stale_snapshot.py; that script then asserts no snapshot
card still carries a tag its live counterpart lacks.

Usage: python transfer_snapshot_tags.py [--dry-run] <gtd-personal-deck> <live-mdcal-personal-deck>
"""

import argparse

import mddb

MDCAL_STATUS = {"CONFIRMED", "TENTATIVE", "CANCELLED"}

IDENTITY_SQL = (
    "SELECT e.id, e.relpath, u.value_str, s.value_str, "
    "COALESCE(r.value_str, ''), st.value_str "
    "FROM entries e "
    "JOIN entry_fields u ON u.entry_rowid = e.rowid AND u.key = 'uid' "
    "JOIN entry_fields s ON s.entry_rowid = e.rowid AND s.key = 'source' "
    "JOIN entry_fields ds ON ds.entry_rowid = e.rowid AND ds.key = 'dtstart_epoch' "
    "JOIN entry_fields de ON de.entry_rowid = e.rowid AND de.key = 'dtend_epoch' "
    "LEFT JOIN entry_fields r ON r.entry_rowid = e.rowid AND r.key = 'recurrence_id' "
    "JOIN entry_fields st ON st.entry_rowid = e.rowid AND st.key = {status_key!r}"
)


def _identities(db, status_key, snapshot_shape=False):
    """Identity map; ``snapshot_shape`` adds the deletion script's conjuncts."""
    rows = db.conn.execute(IDENTITY_SQL.format(status_key=status_key)).fetchall()
    return {
        (source, uid, rid): cid
        for cid, relpath, uid, source, rid, status in rows
        if not snapshot_shape or ("/" not in relpath and status in MDCAL_STATUS)
    }


def _tags(db):
    out = {}
    for cid, value in db.conn.execute(
        "SELECT e.id, f.value_str FROM entries e "
        "JOIN entry_fields f ON f.entry_rowid = e.rowid AND f.key = 'tags'"
    ):
        out.setdefault(cid, set()).add(value)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("gtd_deck")
    parser.add_argument("live_deck")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    gtd = mddb.MDDB(args.gtd_deck)
    live = mddb.MDDB(args.live_deck)
    base = live.head()
    stale_ids = _identities(gtd, "status", snapshot_shape=True)
    live_ids = _identities(live, "event_status") or _identities(live, "status")
    stale_tags = _tags(gtd)
    live_tags = _tags(live)

    transfers = []
    for identity, cid in stale_ids.items():
        live_cid = live_ids.get(identity)
        if live_cid is None:
            continue
        extra = stale_tags.get(cid, set()) - live_tags.get(live_cid, set())
        if extra:
            transfers.append((live_cid, sorted(extra)))

    print(f"{len(transfers)} live cards gain tags from the snapshot")
    counts = {}
    for _, extra in transfers:
        for tag in extra:
            counts[tag] = counts.get(tag, 0) + 1
    for tag, count in sorted(counts.items()):
        print(f"  {tag}: {count}")
    if args.dry_run or not transfers:
        return
    with live.editor(
        rationale=f"carry snapshot area tags onto {len(transfers)} live cards",
        base=base,
    ) as editor:
        for live_cid, extra in transfers:
            card = editor.read(live_cid)
            editor.update(
                card,
                summary=card.summary,
                tags=[*card.yaml.get("tags", []), *extra],
            )
    remaining = sum(
        1
        for identity, cid in stale_ids.items()
        if live_ids.get(identity) is not None
        and stale_tags.get(cid, set()) - _tags(live).get(live_ids[identity], set())
    )
    print(f"transferred; snapshot cards still exceeding live tags: {remaining}")
    assert remaining == 0


if __name__ == "__main__":
    main()
