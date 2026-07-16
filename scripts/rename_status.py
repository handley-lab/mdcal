"""One-shot converter: rename the mdcal storage field ``status`` -> ``event_status``.

Run once per deck during the deployment maintenance window (services stopped),
then delete alongside the rename release. Not packaged: the library carries no
migration code and no compatibility reads.

For every card whose top-level ``status`` holds an iCalendar value the field is
renamed in place — the old key is DELETED, not shadowed, so the strip
discipline can never resurrect it. GTD ``status`` values (next/waiting/...)
are never mdcal's and are left untouched (and reported). One editor commit per
deck, pinned to the HEAD captured at the read (a concurrent commit raises
ConflictError rather than converting a stale view).

Usage: python rename_status.py [--dry-run] <deck> [<deck> ...]
"""

import argparse

import mddb

MDCAL_STATUS = {"CONFIRMED", "TENTATIVE", "CANCELLED"}


def convert(deck, dry_run):
    db = mddb.MDDB(deck)
    base = db.head()
    rows = db.conn.execute(
        "SELECT e.id, f.value_str FROM entries e "
        "JOIN entry_fields f ON f.entry_rowid = e.rowid AND f.key = 'status'"
    ).fetchall()
    targets = [cid for cid, value in rows if value in MDCAL_STATUS]
    foreign = [(cid, value) for cid, value in rows if value not in MDCAL_STATUS]
    print(f"{deck}: {len(targets)} event cards to rename")
    for cid, value in foreign:
        print(f"  untouched non-mdcal status {value!r}: {cid}")
    if dry_run:
        return
    if targets:
        rationale = f"rename status -> event_status ({len(targets)} cards)"
        with db.editor(rationale=rationale, base=base) as editor:
            for cid in targets:
                card = editor.read(cid)
                card.yaml["event_status"] = card.yaml.pop("status")
                editor.update(card, summary=card.summary)
    leftover = db.conn.execute(
        "SELECT count(*) FROM entry_fields f "
        "WHERE f.key = 'status' AND f.value_str IN ('CONFIRMED', 'TENTATIVE', 'CANCELLED')"
    ).fetchone()[0]
    missing = db.conn.execute(
        "SELECT count(*) FROM entries e "
        "JOIN entry_fields ds ON ds.entry_rowid = e.rowid AND ds.key = 'dtstart_epoch' "
        "JOIN entry_fields de ON de.entry_rowid = e.rowid AND de.key = 'dtend_epoch' "
        "JOIN entry_fields u ON u.entry_rowid = e.rowid AND u.key = 'uid' "
        "JOIN entry_fields s ON s.entry_rowid = e.rowid AND s.key = 'source' "
        "WHERE NOT EXISTS (SELECT 1 FROM entry_fields es "
        "WHERE es.entry_rowid = e.rowid AND es.key = 'event_status')"
    ).fetchone()[0]
    print(
        f"{deck}: converted {len(targets)}; leftover mdcal status = {leftover}; "
        f"event-shaped cards missing event_status = {missing}"
    )
    assert leftover == 0 and missing == 0, "conversion incomplete"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("decks", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for deck in args.decks:
        convert(deck, args.dry_run)


if __name__ == "__main__":
    main()
